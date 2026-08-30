#!/usr/bin/env python3
"""
BUMEN — Reviewer Kanban
TUGAS / SELESAI kanban for internal reviewers. One comment per reviewer,
1:1 active card, typeui.sh sleek style.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import sqlite3, json, secrets, os, re, hashlib, time
from http import cookies
import threading

# Bright Data scraper module
try:
    from brightdata_scraper import (
        init_bd_tables, run_full_scrape, get_analytics,
        search_commenters, get_comments_by_user, get_comments_by_post,
        discover_posts, scrape_comments, ingest_posts, ingest_comments
    )
    BD_AVAILABLE = True
except Exception as e:
    print(f"[DEBUG] brightdata_scraper not available: {e}", flush=True)
    BD_AVAILABLE = False

ROOT = Path(__file__).resolve().parent
DB   = Path(os.environ.get("DB_PATH", str(ROOT / "bumen.db")))
PORT = int(os.environ.get("PORT", "8765"))
SESSIONS = {}
DATA_JSON = ROOT / "data.json"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        handle TEXT UNIQUE NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TEXT,
        last_ip TEXT
    );
    CREATE TABLE IF NOT EXISTS posts(
        id INTEGER PRIMARY KEY,
        source_url TEXT,
        title TEXT,
        thumbnail_url TEXT,
        description TEXT DEFAULT '',
        post_date TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS comments(
        id INTEGER PRIMARY KEY,
        post_id INTEGER NOT NULL REFERENCES posts(id),
        body TEXT NOT NULL,
        status TEXT DEFAULT 'available',
        assigned_user_id INTEGER REFERENCES users(id),
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(post_id, body)
    );
    CREATE TABLE IF NOT EXISTS assignments(
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        comment_id INTEGER NOT NULL REFERENCES comments(id),
        state TEXT DEFAULT 'claimed',
        claimed_at TEXT DEFAULT CURRENT_TIMESTAMP,
        copied_at TEXT,
        verified_at TEXT,
        verify_method TEXT DEFAULT 'manual',
        verify_match_score REAL,
        UNIQUE(user_id, comment_id),
        UNIQUE(comment_id)
    );
    CREATE TABLE IF NOT EXISTS reports(
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        comment_id INTEGER NOT NULL REFERENCES comments(id),
        attempt_count INTEGER DEFAULT 1,
        note TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        resolved INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS login_events(
        id INTEGER PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        handle TEXT NOT NULL,
        ip TEXT,
        user_agent TEXT,
        at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS assignment_events(
        id INTEGER PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        assignment_id INTEGER REFERENCES assignments(id),
        event TEXT NOT NULL,
        detail TEXT DEFAULT '',
        at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS live_comments(
        id INTEGER PRIMARY KEY,
        post_id INTEGER NOT NULL REFERENCES posts(id),
        username TEXT NOT NULL DEFAULT 'anonymous',
        body TEXT NOT NULL,
        scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(post_id, body)
    );
    CREATE TABLE IF NOT EXISTS admin_users(
        id INTEGER PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        pw_hash TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_login_at TEXT
    );
    CREATE TABLE IF NOT EXISTS scrape_jobs(
        id INTEGER PRIMARY KEY,
        post_id INTEGER NOT NULL REFERENCES posts(id),
        status TEXT DEFAULT 'queued',
        started_at TEXT,
        finished_at TEXT,
        comments_scraped INTEGER DEFAULT 0,
        error TEXT,
        triggered_by INTEGER REFERENCES admin_users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
    CREATE INDEX IF NOT EXISTS idx_comments_status ON comments(status);
    CREATE INDEX IF NOT EXISTS idx_assign_user ON assignments(user_id);
    CREATE INDEX IF NOT EXISTS idx_assign_state ON assignments(state);
    CREATE INDEX IF NOT EXISTS idx_login_user ON login_events(user_id);
    CREATE INDEX IF NOT EXISTS idx_event_user ON assignment_events(user_id);
    CREATE INDEX IF NOT EXISTS idx_reports_resolved ON reports(resolved);
    """)
    c.commit()

    # Migrate: add columns that may not exist
    for sql in [
        "ALTER TABLE users ADD COLUMN last_seen_at TEXT",
        "ALTER TABLE users ADD COLUMN last_ip TEXT",
        "ALTER TABLE posts ADD COLUMN post_date TEXT",
        "ALTER TABLE assignments ADD COLUMN verify_method TEXT DEFAULT 'manual'",
        "ALTER TABLE assignments ADD COLUMN verify_match_score REAL",
    ]:
        try: c.execute(sql); c.commit()
        except sqlite3.OperationalError: pass

    # Ensure unique index for live_comments upsert (post_id, body)
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_live_comments_post_body ON live_comments(post_id, body)")
        c.commit()
    except sqlite3.OperationalError as e:
        print(f"[DEBUG] Could not create unique index on live_comments: {e}", flush=True)

    c.close()

    # Load posts + comments from data.json — always upsert (INSERT OR IGNORE)
    if DATA_JSON.exists():
        c = db()
        n_posts = c.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        print(f"[DEBUG] data.json exists, posts in DB: {n_posts}", flush=True)
        # Always upsert posts + comments so new posts in data.json get added on deploy
        c.execute("DELETE FROM comments WHERE status='available'")
        for p in json.loads(DATA_JSON.read_text()):
            src_url = normalize_ig_url(p.get("source_url") or p.get("url") or "")
            thumb_url = p.get("thumbnail_url") or p.get("thumb") or ""
            c.execute(
                "INSERT OR IGNORE INTO posts(id, source_url, title, thumbnail_url, description) VALUES(?,?,?,?,?)",
                (p["id"], src_url, p.get("title", ""), thumb_url, p.get("description", "")),
            )
            c.execute(
                "UPDATE posts SET title = ?, source_url = ?, thumbnail_url = CASE WHEN thumbnail_url = '' OR thumbnail_url IS NULL THEN ? ELSE thumbnail_url END WHERE id = ?",
                (p.get("title", ""), src_url, thumb_url, p["id"]),
            )
            for cmt in p.get("comments", []):
                c.execute(
                    "INSERT OR IGNORE INTO comments(post_id, body, status) VALUES(?,?,'available')",
                    (p["id"], cmt),
                )
        c.commit(); c.close()

    # Auto-seed live_comments from data.json so verify_done has a corpus to match against
    # even before the IG scraper has scraped the post. Marked with username='__bumen_seed__'
    # so admin can distinguish from real scraped comments.
    # RESEED on every startup: delete all __bumen_seed__ rows and re-insert fresh.
    # This handles Railway volume with old rows, duplicates, missing unique index, etc.
    if DATA_JSON.exists():
        c = db()
        n_comments = c.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        if n_comments > 0:
            print("[DEBUG] Reseeding live_comments from data.json...", flush=True)
            # Remove old seed rows
            c.execute("DELETE FROM live_comments WHERE username='__bumen_seed__'")
            # Insert fresh
            seeded = 0
            for p in json.loads(DATA_JSON.read_text()):
                for cmt in p.get("comments", []):
                    c.execute(
                        "INSERT OR IGNORE INTO live_comments(post_id, username, body, scraped_at) VALUES(?, '__bumen_seed__', ?, CURRENT_TIMESTAMP)",
                        (p["id"], cmt),
                    )
                    seeded += 1
            c.commit()
            total_seeded = c.execute("SELECT COUNT(*) FROM live_comments WHERE username='__bumen_seed__'").fetchone()[0]
            print(f"[DEBUG] Reseeded {seeded} live_comments (total seeded={total_seeded})", flush=True)
        c.close()
    else:
        print(f"[DEBUG] data.json NOT found at {DATA_JSON}", flush=True)

    # Seed default admin if not exists
    seed_default_admin()

def normalize_ig_url(url):
    """Normalize any Instagram post/reel URL to canonical https://www.instagram.com/p/<shortcode>/ format."""
    if not url:
        return ""
    import re
    m = re.search(r'instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)', str(url))
    if m:
        return f"https://www.instagram.com/p/{m.group(1)}/"
    return str(url)

def get_thumb_path(post_id):
    """Return local disk path for post thumbnail cache."""
    data_dir = Path(os.environ.get("THUMBS_DIR", "/data/thumbs"))
    local_dir = ROOT / "thumbs"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / f"{post_id}.jpg"
    except Exception:
        local_dir.mkdir(parents=True, exist_ok=True)
        return local_dir / f"{post_id}.jpg"

def save_thumbnail_cache(post_id, url):
    """Download image data from url and save to local disk cache."""
    if not url or not url.startswith("http"):
        return False
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
            "Referer": "https://www.instagram.com/",
        })
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                data = resp.read()
                if len(data) > 500:
                    p = get_thumb_path(post_id)
                    p.write_bytes(data)
                    return True
    except Exception as e:
        print(f"[DEBUG] save_thumbnail_cache failed for post {post_id}: {e}", flush=True)
    return False

# Refresh Instagram thumbnails using instaloader
def refresh_instagram_thumbnails():
    """Fetch fresh thumbnail URLs from Instagram for all posts and update database & disk cache."""
    try:
        import instaloader
        L = instaloader.Instaloader()
        
        # Map of post_id to shortcode (extracted from source_url)
        c = db()
        rows = c.execute("SELECT id, source_url FROM posts").fetchall()
        c.close()
        
        for post_id, source_url in rows:
            try:
                if not source_url:
                    continue
                import re
                match = re.search(r'instagram\.com/(?:p|reel)/([A-Za-z0-9_-]+)', source_url)
                if not match:
                    continue
                shortcode = match.group(1)
                
                post = instaloader.Post.from_shortcode(L.context, shortcode)
                fresh_url = post.url
                
                c = db()
                c.execute("UPDATE posts SET thumbnail_url = ? WHERE id = ?", (fresh_url, post_id))
                c.commit()
                c.close()

                save_thumbnail_cache(post_id, fresh_url)
                print(f"✅ Refreshed thumbnail for post {post_id} ({shortcode})")
            except Exception as e:
                print(f"⚠️ Failed to refresh thumbnail for post {post_id}: {e}")
                continue
    except ImportError:
        print("⚠️ instaloader not available, skipping thumbnail refresh")
    except Exception as e:
        print(f"⚠️ Thumbnail refresh error: {e}")

def hash_pw(s): return hashlib.sha256(s.encode()).hexdigest()

ADMIN_PW_DEFAULT = "@poji#1"
def seed_default_admin():
    c = db()
    n = c.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]
    if n == 0:
        c.execute("INSERT INTO admin_users(username, pw_hash) VALUES(?,?)",
                  ("admin", hash_pw(ADMIN_PW_DEFAULT)))
        c.commit()
    c.close()

# Negative-tone blacklist — reject comments containing any of these
NEG_TONE_BLACKLIST = [
    "pencitraan", "janji doang", "gk kayak jaman dulu", "gak kayak jaman dulu",
    "jangan sampe", "capek bgt", "buset", "gak ngaruh", "ga ngaruh",
    "bohong", "palsu", "nyungsep", "gagal lagi", "gimana sih",
    "sok sibuk", "sok pintar", "sok asik", "gak ada hasil",
]
def is_clean_comment(body):
    low = (body or "").lower()
    return not any(p in low for p in NEG_TONE_BLACKLIST)

# Admin auth helper
ADMIN_SESSIONS = {}
def admin_login(username, password):
    c = db()
    row = c.execute("SELECT * FROM admin_users WHERE username = ? AND pw_hash = ?",
                    (username, hash_pw(password))).fetchone()
    c.close()
    if not row: return None
    sid = secrets.token_urlsafe(24)
    ADMIN_SESSIONS[sid] = dict(row)
    c = db()
    c.execute("UPDATE admin_users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
    c.commit(); c.close()
    return sid
def current_admin(handler):
    for part in handler.headers.get("Cookie", "").split(";"):
        if "bumen_admin_sid=" in part:
            return ADMIN_SESSIONS.get(part.split("bumen_admin_sid=")[1].strip())
    return None

# Audit log helpers
def log_login(user_id, handle, ip, ua):
    try:
        c = db()
        c.execute("INSERT INTO login_events(user_id, handle, ip, user_agent) VALUES(?,?,?,?)",
                  (user_id, handle, ip, ua))
        c.execute("UPDATE users SET last_seen_at = CURRENT_TIMESTAMP, last_ip = ? WHERE id = ?",
                  (ip, user_id))
        c.commit(); c.close()
    except Exception: pass

def log_event(user_id, assignment_id, event, detail=""):
    try:
        c = db()
        c.execute("INSERT INTO assignment_events(user_id, assignment_id, event, detail) VALUES(?,?,?,?)",
                  (user_id, assignment_id, event, detail))
        c.commit(); c.close()
    except Exception: pass

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def current_user(handler):
    ck = handler.headers.get("Cookie", "")
    sid = None
    for part in ck.split(";"):
        if "bumen_sid=" in part:
            sid = part.split("bumen_sid=")[1].strip(); break
    if not sid or sid not in SESSIONS: return None
    return SESSIONS[sid]

def login(handle, ip="", ua=""):
    handle = handle.strip().lstrip("@")
    if not handle: raise ValueError("Handle kosong")
    c = db()
    row = c.execute("SELECT * FROM users WHERE handle = ?", (handle,)).fetchone()
    if not row:
        c.execute("INSERT INTO users(handle, last_ip, last_seen_at) VALUES(?,?,CURRENT_TIMESTAMP)", (handle, ip))
        c.commit()
        row = c.execute("SELECT * FROM users WHERE handle = ?", (handle,)).fetchone()
    user_id = row["id"]
    c.execute("UPDATE users SET last_seen_at = CURRENT_TIMESTAMP, last_ip = ? WHERE id = ?", (ip, user_id))
    c.commit(); c.close()
    log_login(user_id, handle, ip, ua)
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = dict(row)
    return sid

# ---------------------------------------------------------------------------
# Assignment logic — one comment per reviewer, comment locked once claimed
# ---------------------------------------------------------------------------
def claim_next_comment(user_id):
    """Pick the next queued comment for THIS user and promote it to active.
    If user has no queue, seed one with 14 fresh comments first.
    Returns comment dict or None if global pool exhausted."""
    c = db()
    # 1) Promote user's first queued (claimed) task → active
    row = c.execute("""
        SELECT cm.id, cm.body, cm.post_id, p.title, p.thumbnail_url, p.source_url, a.id AS aid
        FROM assignments a
        JOIN comments cm ON cm.id = a.comment_id
        JOIN posts p ON p.id = cm.post_id
        WHERE a.user_id = ? AND a.state = 'queued'
        ORDER BY a.id ASC LIMIT 1
    """, (user_id,)).fetchone()
    if not row:
        # Seed this user's queue with 14 fresh comments
        seeded = _seed_user_queue(user_id, count=14, c=c)
        if not seeded:
            c.close(); return None
        # Recurse once to pick the seeded head
        c.close()
        return claim_next_comment(user_id)
    # Promote to active (claimed state)
    c.execute("UPDATE assignments SET state='claimed' WHERE id=?", (row["aid"],))
    c.commit(); c.close()
    log_event(user_id, row["aid"], "promote_to_active")
    return {
        "assignment_id": row["aid"],
        "comment_id": row["id"],
        "body": row["body"],
        "post_id": row["post_id"],
        "post_title": row["title"],
        "thumbnail_url": row["thumbnail_url"],
        "source_url": normalize_ig_url(row["source_url"]),
        "state": "claimed",
    }


def _seed_user_queue(user_id, count, c):
    """Reserve N fresh comments into this user's queue (state='queued').
    Filter out negative-tone comments using blacklist."""
    picked = []
    attempts = 0
    while len(picked) < count and attempts < count * 6:
        attempts += 1
        row = c.execute("""
            SELECT cm.id, cm.body FROM comments cm
            WHERE cm.status = 'available'
              AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.comment_id = cm.id)
            ORDER BY RANDOM() LIMIT 1
        """).fetchone()
        if not row: break
        # Blacklist filter — never assign negative-tone comments
        if not is_clean_comment(row["body"]): continue
        try:
            c.execute("""
                INSERT INTO assignments(user_id, comment_id, state) VALUES(?,?,'queued')
            """, (user_id, row["id"]))
            c.execute("UPDATE comments SET status='queued', assigned_user_id=? WHERE id=?",
                      (user_id, row["id"]))
            picked.append(row["id"])
        except sqlite3.IntegrityError:
            continue
    c.commit()
    return picked

def copy_comment(user_id, assignment_id):
    c = db()
    row = c.execute("""
        SELECT a.id, cm.body, cm.post_id, p.source_url, p.title
        FROM assignments a JOIN comments cm ON cm.id = a.comment_id
        JOIN posts p ON p.id = cm.post_id
        WHERE a.id = ? AND a.user_id = ? AND a.state IN ('claimed','copied','reported')
    """, (assignment_id, user_id)).fetchone()
    if not row:
        c.close(); raise ValueError("Assignment not found")
    c.execute("UPDATE assignments SET state='copied', copied_at=CURRENT_TIMESTAMP WHERE id=?",
              (assignment_id,))
    c.commit(); c.close()
    return {"body": row["body"], "source_url": normalize_ig_url(row["source_url"]), "post_title": row["title"]}

def report_unconfirmed(user_id, assignment_id, note):
    c = db()
    row = c.execute("""
        SELECT a.id, a.comment_id FROM assignments a
        WHERE a.id = ? AND a.user_id = ? AND a.state IN ('claimed','copied','reported')
    """, (assignment_id, user_id)).fetchone()
    if not row:
        c.close(); raise ValueError("Assignment not found")
    existing = c.execute("""
        SELECT id, attempt_count FROM reports
        WHERE user_id = ? AND comment_id = ? AND resolved = 0
        ORDER BY id DESC LIMIT 1
    """, (user_id, row["comment_id"])).fetchone()
    if existing:
        c.execute("UPDATE reports SET attempt_count = attempt_count + 1, note = ?, created_at = CURRENT_TIMESTAMP WHERE id = ?",
                  (note, existing["id"]))
    else:
        c.execute("INSERT INTO reports(user_id, comment_id, note) VALUES(?,?,?)",
                  (user_id, row["comment_id"], note))
    c.execute("UPDATE assignments SET state='reported' WHERE id=?", (assignment_id,))
    c.commit(); c.close()

def _normalize_for_match(s):
    """Normalize text for fuzzy match: lowercase, collapse whitespace, strip emojis+punctuation variants."""
    import re as _re
    if not s: return ""
    t = s.lower()
    # Unify ellipsis and dashes
    t = t.replace("…", "...").replace("—", "-").replace("–", "-")
    # Strip zero-width and BOM
    t = t.replace("\u200b", "").replace("\ufeff", "")
    # Remove all emoji and non-ASCII punctuation (keep word chars + spaces + basic punct)
    t = _re.sub(r"[^\w\s\.\,\!\?\']", " ", t, flags=_re.UNICODE)
    # Collapse whitespace
    t = _re.sub(r"\s+", " ", t).strip()
    return t

def _bigram_set(s):
    """Return set of character bigrams for Jaccard similarity."""
    s = s.replace(" ", "")  # bigrams ignore spaces
    if len(s) < 2: return {s} if s else set()
    return {s[i:i+2] for i in range(len(s)-1)}

def _jaccard(a, b):
    """Jaccard similarity over bigrams."""
    A, B = _bigram_set(a), _bigram_set(b)
    if not A or not B: return 0.0
    return len(A & B) / len(A | B)

def verify_done(user_id, assignment_id, claimed_seen):
    """Mark verified done. Cross-checks live_comments for this post with fuzzy match.

    Match rules (in priority order):
      1. Exact body match (whitespace/case insensitive) → 1.0
      2. Normalized match (strip emoji/punct variants) → 0.95
      3. Bigram-Jaccard >= 0.80 → acceptable (0.80..0.94)
      4. Anything else → reject with helpful message
    """
    c = db()
    row = c.execute("""
        SELECT a.id, a.comment_id, cm.body, cm.post_id
        FROM assignments a JOIN comments cm ON cm.id = a.comment_id
        WHERE a.id = ? AND a.user_id = ? AND a.state IN ('copied','reported')
    """, (assignment_id, user_id)).fetchone()
    if not row:
        c.close(); raise ValueError("Selesaikan copy dulu")

    target_body = row["body"]
    target_norm = _normalize_for_match(target_body)

    # Pull all live_comments for this post, score against the target body
    candidates = c.execute("""
        SELECT id, body FROM live_comments WHERE post_id = ?
    """, (row["post_id"],)).fetchall()

    best = None  # (score, match_id, method)
    target_bigrams = _bigram_set(target_norm)
    for cand in candidates:
        cand_body = cand["body"]
        # Tier 1: exact (case+whitespace insensitive)
        if cand_body.lower().strip() == target_body.lower().strip():
            best = (1.0, cand["id"], "exact")
            break
        cand_norm = _normalize_for_match(cand_body)
        # Tier 2: normalized equal
        if cand_norm == target_norm:
            score = 0.95
            if not best or score > best[0]:
                best = (score, cand["id"], "normalized")
            continue
        # Tier 3: bigram Jaccard
        j = _jaccard(target_norm, cand_norm)
        if j >= 0.80 and (not best or j > best[0]):
            best = (j, cand["id"], "fuzzy")

    if not best:
        # Log rejection to admin dashboard
        c.execute("""
            INSERT INTO reports(user_id, comment_id, note)
            VALUES(?, ?, ?)
        """, (user_id, row["comment_id"], "Auto-reject: comment not found live on IG post"))
        c.commit(); c.close()
        log_event(user_id, assignment_id, "verify_rejected", "not live")
        raise ValueError(
            "Komentar belum terdeteksi live di IG. "
            "Pastikan sudah benar-benar terkirim di kolom komentar post yang ditugaskan, "
            "tunggu 30 detik, lalu coba konfirmasi lagi. "
            "Kalau masih gagal, gunakan tombol Lapor Admin."
        )

    match_score, match_id, method = best
    c.execute("""
        UPDATE assignments SET state='done', verified_at=CURRENT_TIMESTAMP,
            verify_method=?, verify_match_score=?
        WHERE id=?
    """, (f"ig_{method}", match_score, assignment_id))
    c.execute("UPDATE comments SET status='done' WHERE id=?", (row["comment_id"],))
    c.execute("UPDATE reports SET resolved=1 WHERE comment_id=? AND resolved=0", (row["comment_id"],))
    c.commit(); c.close()
    log_event(user_id, assignment_id, "verify_done", f"match_id={match_id} method={method} score={match_score:.2f}")
    return {"verified": True, "method": f"ig_{method}", "match_score": round(match_score, 2)}

# ---------------------------------------------------------------------------
# HTML — typeui.sh sleek kanban (TUGAS / SELESAI)
# ---------------------------------------------------------------------------
ADMIN_HTML = r'''<!doctype html>
<html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BUMEN Intelligence</title>
<style>
*,*::before,*::after{box-sizing:border-box}
:root{--bg:#f5f6fa;--card:#fff;--ink:#0f172a;--muted:#64748b;--line:#e6e8ee;--accent:#3b5bdb;--accent-2:#1f3a8a;--good:#16a34a;--warn:#f59e0b;--bad:#dc2626;--r:14px}
html,body{margin:0;padding:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Rounded","Inter",system-ui,sans-serif}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
.auth-pg{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}
.auth-box{background:#fff;border-radius:24px;padding:48px 40px;max-width:420px;width:100%;box-shadow:0 18px 40px rgba(15,23,42,.1),0 6px 12px rgba(15,23,42,.06);text-align:center}
.auth-box img{width:80px;height:80px;margin:0 auto 16px;display:block}
.auth-box h1{margin:0 0 4px;font-size:24px;font-weight:800;letter-spacing:-.02em}
.auth-box p.sub{margin:0 0 24px;color:var(--muted);font-size:13px}
.auth-box input{width:100%;padding:13px 16px;border:1px solid var(--line);border-radius:12px;background:#f9fafc;font-size:15px;outline:none;transition:.15s;margin-top:6px}
.auth-box input:focus{border-color:var(--accent);background:#fff;box-shadow:0 0 0 4px rgba(59,91,219,.12)}
.auth-box .btn{margin-top:16px;width:100%;background:var(--ink);color:#fff;padding:13px;border-radius:12px;font-weight:600;font-size:15px}
.auth-box .err{color:var(--bad);font-size:13px;margin-top:10px;min-height:16px}

.shell{max-width:1180px;margin:0 auto;padding:24px}
.topbar{display:flex;align-items:center;justify-content:space-between;padding:8px 4px 20px}
.topbar-brand{display:flex;align-items:center;gap:12px;font-weight:800;font-size:18px;letter-spacing:-.02em}
.topbar-brand img{width:38px;height:38px}
.topbar-actions{display:flex;align-items:center;gap:10px}
.topbar-actions button{padding:8px 14px;border-radius:10px;font-weight:600;font-size:13px;background:#fff;border:1px solid var(--line)}
.topbar-actions button:hover{background:#f3f4f6}

.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:20px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:18px;box-shadow:0 1px 2px rgba(15,23,42,.04)}
.kpi .lbl{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.kpi .val{font-size:28px;font-weight:800;letter-spacing:-.02em;margin-top:6px;color:var(--ink)}
.kpi .sub{font-size:12px;color:var(--muted);margin-top:4px}
.kpi.warn .val{color:var(--warn)}
.kpi.bad .val{color:var(--bad)}
.kpi.good .val{color:var(--good)}

.section{background:var(--card);border:1px solid var(--line);border-radius:var(--r);margin-bottom:16px;box-shadow:0 1px 2px rgba(15,23,42,.04)}
.section-head{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between}
.section-head h2{margin:0;font-size:14px;font-weight:700;letter-spacing:-.02em;text-transform:uppercase;color:var(--ink)}
.section-head .count{font-size:11px;font-weight:700;background:#eef0f4;color:var(--muted);padding:3px 9px;border-radius:99px}

table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#f8f9fb;text-align:left;padding:10px 14px;font-weight:700;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;border-bottom:1px solid var(--line)}
td{padding:12px 14px;border-bottom:1px solid #f1f3f7;vertical-align:top}
tr:last-child td{border-bottom:0}
tr:hover td{background:#fafbfd}

.badge{display:inline-block;font-size:10px;font-weight:700;padding:3px 8px;border-radius:99px;text-transform:uppercase;letter-spacing:.04em}
.badge.good{background:#dcfce7;color:#15803d}
.badge.warn{background:#fef3c7;color:#a16207}
.badge.bad{background:#fee2e2;color:#b91c1c}
.badge.muted{background:#f1f3f7;color:var(--muted)}

.report-row{background:#fff7ed;border-left:3px solid var(--warn);padding:12px 14px;margin:6px 14px;border-radius:8px}
.report-row .meta{font-size:11px;color:var(--muted);margin-top:4px}
.report-row .body{font-size:13px;margin-top:6px}

.evt{font-size:12px;padding:8px 14px;border-bottom:1px solid #f1f3f7;display:flex;gap:10px;align-items:center}
.evt .ts{color:var(--muted);font-size:11px;min-width:130px;font-family:monospace}
.evt .tag{font-size:10px;font-weight:700;padding:2px 6px;border-radius:4px;text-transform:uppercase;background:#eef0f4;color:var(--muted)}
.evt .tag.d{background:#dcfce7;color:#15803d}
.evt .tag.r{background:#fee2e2;color:#b91c1c}
.evt .tag.p{background:#eef2ff;color:var(--accent)}

.empty{padding:32px 18px;text-align:center;color:var(--muted);font-size:13px}
.progress{height:6px;background:#eef0f4;border-radius:99px;overflow:hidden;width:80px}
.progress .fill{height:100%;background:var(--accent);transition:.3s}
</style></head><body>

<div id="admin-auth" class="auth-pg">
  <div class="auth-box">
    <img src="/bumen-logo.png" alt="BUMEN" onerror="this.style.display='none'">
    <h1>BUMEN Intelligence</h1>
    <p class="sub">Admin dashboard · Social media intelligence platform</p>
    <input id="adm-user" placeholder="Username" autocomplete="off">
    <input id="adm-pw" type="password" placeholder="Password" autocomplete="off">
    <button class="btn" onclick="doAdminLogin()">LOGIN</button>
    <div class="err" id="adm-err"></div>
  </div>
</div>

<div id="admin-app" style="display:none">
  <div class="shell">
    <div class="topbar">
      <div class="topbar-brand"><img src="/bumen-logo.png" alt="">BUMEN · Admin</div>
      <div class="topbar-actions"><b id="adm-handle"></b><button onclick="doAdminLogout()">LOGOUT</button></div>
    </div>

    <div class="kpis" id="kpis"></div>

    <div class="section">
      <div class="section-head"><h2>📌 Posts</h2><span class="count" id="cnt-posts">0</span></div>
      <div style="overflow-x:auto"><table id="tbl-posts"><thead><tr><th>#</th><th>Title</th><th>Comments</th><th>Assigned</th><th>Done</th><th>Progress</th></tr></thead><tbody></tbody></table></div>
    </div>

    <div class="section">
      <div class="section-head"><h2>👥 Reviewers</h2><span class="count" id="cnt-users">0</span></div>
      <div style="overflow-x:auto"><table id="tbl-users"><thead><tr><th>Handle</th><th>Joined</th><th>Last seen</th><th>IP</th><th>Assignments</th><th>Done</th></tr></thead><tbody></tbody></table></div>
    </div>

    <div class="section">
      <div class="section-head"><h2>⚠️ Repost Inbox</h2><span class="count" id="cnt-reports">0</span></div>
      <div id="list-reports"></div>
    </div>

    <div class="section">
      <div class="section-head"><h2>📋 Login Events</h2><span class="count" id="cnt-logins">0</span></div>
      <div id="list-logins"></div>
    </div>

    <div class="section" id="bd-section">
      <div class="section-head">
        <h2>📊 Social Intelligence (Bright Data)</h2>
        <span class="count" id="bd-status">—</span>
      </div>
      <div style="padding:14px 18px">
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center">
          <input id="bd-profile" placeholder="IG username (e.g. duniameutya)" value="duniameutya" style="padding:8px 12px;border:1px solid var(--line);border-radius:8px;font-size:13px;width:180px">
          <input id="bd-start" type="text" placeholder="Start MM-DD-YYYY" style="padding:8px 12px;border:1px solid var(--line);border-radius:8px;font-size:13px;width:140px">
          <input id="bd-end" type="text" placeholder="End MM-DD-YYYY" style="padding:8px 12px;border:1px solid var(--line);border-radius:8px;font-size:13px;width:140px">
          <button onclick="bdLoadAnalytics()" style="padding:8px 16px;border-radius:8px;font-weight:600;font-size:13px;background:var(--accent);color:#fff;border:0">Load Analytics</button>
          <button onclick="bdTriggerScrape()" style="padding:8px 16px;border-radius:8px;font-weight:600;font-size:13px;background:var(--ink);color:#fff;border:0">⚡ Trigger Scrape</button>
        </div>

        <div class="kpis" id="bd-kpis" style="margin-bottom:14px"></div>

        <div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">
          <input id="bd-search" placeholder="Search commenter username..." style="flex:1;padding:8px 12px;border:1px solid var(--line);border-radius:8px;font-size:13px" onkeyup="if(event.key==='Enter')bdSearchCommenters()">
          <button onclick="bdSearchCommenters()" style="padding:8px 16px;border-radius:8px;font-weight:600;font-size:13px;background:#fff;border:1px solid var(--line)">Search</button>
        </div>
        <div id="bd-search-results" style="margin-bottom:14px"></div>

        <h3 style="font-size:13px;font-weight:700;text-transform:uppercase;color:var(--muted);margin:18px 0 8px">Posts Performance <span style="font-weight:400;text-transform:none;font-size:11px;color:var(--muted)">— click any row to load comments</span></h3>
        <div style="overflow-x:auto"><table id="bd-posts"><thead><tr><th>Date</th><th>Type</th><th>Likes</th><th>Comments</th><th>Description</th></tr></thead><tbody></tbody></table></div>

        <h3 style="font-size:13px;font-weight:700;text-transform:uppercase;color:var(--muted);margin:18px 0 8px">Top Commenters</h3>
        <div style="overflow-x:auto"><table id="bd-top-commenters"><thead><tr><th>#</th><th>Username</th><th>Comments</th><th>Total Likes</th><th>Last Active</th><th></th></tr></thead><tbody></tbody></table></div>

        <div id="bd-user-detail" style="margin-top:14px"></div>
      </div>
    </div>

    <div class="section">
      <div class="section-head"><h2>🔍 Assignment events</h2><span class="count" id="cnt-events">0</span></div>
      <div id="list-events"></div>
    </div>
  </div>
</div>

<script>
async function api(p,o={}){const r=await fetch(p,{...o,headers:{'Content-Type':'application/json',...(o.headers||{})}});const d=await r.json();if(!r.ok)throw new Error(d.error||'Request gagal');return d}
function $(id){return document.getElementById(id)}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function relTime(ts){if(!ts)return'—';try{const d=new Date(ts.replace(' ','T'));const s=(Date.now()-d)/1000;if(s<60)return Math.floor(s)+'s';if(s<3600)return Math.floor(s/60)+'m';if(s<86400)return Math.floor(s/3600)+'h';return Math.floor(s/86400)+'d'}catch(e){return ts}}

async function boot(){
  try{
    const m=await api('/api/admin/me');
    $('admin-auth').style.display='none';
    $('admin-app').style.display='block'.replace('block','')+'block';
    $('admin-app').style.display='block';
    $('adm-handle').textContent=m.admin.username;
    await refresh();
  }catch(_){$('admin-auth').style.display='flex'}
}

async function doAdminLogin(){
  const u=$('adm-user').value.trim(); const p=$('adm-pw').value;
  if(!u||!p){$('adm-err').textContent='Isi username & password';return}
  try{
    await api('/api/admin/login',{method:'POST',body:JSON.stringify({username:u,password:p})});
    location.reload();
  }catch(e){$('adm-err').textContent=e.message}
}
async function doAdminLogout(){await api('/api/admin/logout',{method:'POST'});location.reload()}

async function refresh(){
  const d=await api('/api/admin/dashboard');
  const s=d.summary;
  $('kpis').innerHTML=`
    <div class="kpi"><div class="lbl">Users</div><div class="val">${s.users_total}</div><div class="sub">${s.reviewers_active_24h} active 24h</div></div>
    <div class="kpi"><div class="lbl">Posts</div><div class="val">${s.posts_total}</div><div class="sub">${s.comments_total} total comments</div></div>
    <div class="kpi good"><div class="lbl">Done</div><div class="val">${s.assignments_done}</div><div class="sub">${Math.round(s.assignments_done/(s.assignments_total||1)*100)}% complete</div></div>
    <div class="kpi warn"><div class="lbl">In progress</div><div class="val">${s.assignments_in_progress}</div></div>
    <div class="kpi ${s.open_reports?'bad':''}"><div class="lbl">Open reports</div><div class="val">${s.open_reports}</div></div>
    <div class="kpi"><div class="lbl">Available comments</div><div class="val">${s.comments_available}</div></div>`;
  // Posts
  $('cnt-posts').textContent=d.posts.length;
  $('tbl-posts').querySelector('tbody').innerHTML=d.posts.map(p=>{
    const pct=p.comment_count?(p.done || 0)/p.comment_count*100:0;
    return `<tr><td>${p.id}</td><td><b>${esc(p.title)}</b></td><td>${p.comment_count}</td><td>${p.assigned||0}</td><td>${p.done||0}</td><td><div class="progress"><div class="fill" style="width:${pct}%"></div></div> ${Math.round(pct)}%</td></tr>`;
  }).join('')||'<tr><td colspan="6" class="empty">No posts yet</td></tr>';
  // Users
  $('cnt-users').textContent=d.users.length;
  $('tbl-users').querySelector('tbody').innerHTML=d.users.map(u=>{
    return `<tr><td><b>@${esc(u.handle)}</b></td><td>${(u.created_at||'').substring(0,16)}</td><td>${relTime(u.last_seen_at)}</td><td><span class="badge muted">${esc(u.last_ip||'—')}</span></td><td>${u.assignments||0}</td><td>${u.done||0}</td></tr>`;
  }).join('')||'<tr><td colspan="6" class="empty">No reviewers yet</td></tr>';
  // Reports
  $('cnt-reports').textContent=d.reports.filter(r=>!r.resolved).length;
  $('list-reports').innerHTML=d.reports.length?d.reports.map(r=>`
    <div class="report-row">
      <div><b>@${esc(r.handle)}</b> reported: <span class="badge ${r.resolved?'good':'warn'}">${r.resolved?'Resolved':'Open'}</span> · attempt #${r.attempt_count}</div>
      <div class="body">${esc(r.body||'').substring(0,140)}${(r.body||'').length>140?'…':''}</div>
      <div class="meta">📌 ${esc(r.title)} · 🕐 ${(r.created_at||'').substring(0,19)} · "${esc(r.note||'')}"</div>
    </div>`).join(''):'<div class="empty">No reports. Clean run.</div>';
  // Login events
  $('cnt-logins').textContent=d.login_events.length;
  $('list-logins').innerHTML=d.login_events.length?d.login_events.map(l=>`
    <div class="evt"><span class="ts">${(l.at||'').substring(0,19)}</span><span class="tag">login</span><b>@${esc(l.handle)}</b> · <span style="color:var(--muted)">${esc(l.ip||'')}</span></div>`).join(''):'<div class="empty">No logins yet</div>';
  // Assignment events
  $('cnt-events').textContent=d.assignment_events.length;
  $('list-events').innerHTML=d.assignment_events.length?d.assignment_events.map(e=>{
    const cls=e.event.includes('reject')?'r':e.event.includes('done')?'d':'p';
    return `<div class="evt"><span class="ts">${(e.at||'').substring(0,19)}</span><span class="tag ${cls}">${esc(e.event)}</span><b>@${esc(e.handle||'system')}</b> · <span style="color:var(--muted)">${esc(e.detail||'')}</span></div>`;
  }).join(''):'<div class="empty">No events yet</div>';
}

window.doAdminLogin=doAdminLogin; window.doAdminLogout=doAdminLogout;

// === BRIGHT DATA ANALYTICS ===
async function bdLoadAnalytics(){
  const profile=$('bd-profile').value.trim()||'duniameutya';
  const start=$('bd-start').value.trim();
  const end=$('bd-end').value.trim();
  $('bd-status').textContent='Loading...';
  try{
    const body={profile};
    if(start)body.start_date=start;
    if(end)body.end_date=end;
    const d=await api('/api/admin/bd-analytics',{method:'POST',body:JSON.stringify(body)});
    // KPIs
    $('bd-kpis').innerHTML=`
      <div class="kpi"><div class="lbl">Posts</div><div class="val">${d.total_posts}</div></div>
      <div class="kpi good"><div class="lbl">Comments</div><div class="val">${d.total_comments}</div></div>
      <div class="kpi"><div class="lbl">Unique Commenters</div><div class="val">${d.unique_commenters}</div></div>
      <div class="kpi"><div class="lbl">Avg Comments/Post</div><div class="val">${d.total_posts?Math.round(d.total_comments/d.total_posts):0}</div></div>`;
    // Top commenters
    $('bd-top-commenters').querySelector('tbody').innerHTML=d.top_commenters.map((u,i)=>
      `<tr><td>${i+1}</td><td><b>@${esc(u.username)}</b></td><td>${u.comment_count}</td><td>${u.total_likes||0}</td><td>${relTime(u.last_comment_date)}</td><td><button onclick="bdShowUser('${esc(u.username)}')" style="font-size:11px;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:#fff;font-weight:600">View</button></td></tr>`
    ).join('')||'<tr><td colspan="6" class="empty">No data. Run a scrape first.</td></tr>';
    // Posts
    $('bd-posts').querySelector('tbody').innerHTML=d.posts_summary.map(p=>{
      const desc=(p.description||'').substring(0,80);
      const dateStr=(p.date_posted||'').substring(0,10);
      const scraped=p.scraped_comments||0;
      const reported=p.num_comments||0;
      const commentBadge=scraped>0?`<b style="color:var(--accent)">${scraped}</b>`:`<span style="color:var(--muted)">0</span>`;
      return `<tr style="cursor:pointer" onclick="bdShowPost('${esc(p.url)}')"><td>${dateStr}</td><td><span class="badge muted">${esc(p.content_type||'')}</span></td><td>${p.likes||0}</td><td>${commentBadge}${reported?`<span style="color:var(--muted);font-size:11px">/${reported}</span>`:''}</td><td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(desc)}</td></tr>`;
    }).join('')||'<tr><td colspan="6" class="empty">No posts</td></tr>';
    $('bd-status').textContent='✓ Loaded';
  }catch(e){
    $('bd-status').textContent='Error';
    console.error(e);
  }
}

async function bdTriggerScrape(){
  const profile=$('bd-profile').value.trim()||'duniameutya';
  const start=$('bd-start').value.trim();
  const end=$('bd-end').value.trim();
  if(!confirm(`Scrape @{profile} (${start||'all'} to ${end||'all'})?\\nThis runs in background (~3-5 min).`))return;
  $('bd-status').textContent='Scraping...';
  try{
    const body={profile};
    if(start)body.start_date=start;
    if(end)body.end_date=end;
    const d=await api('/api/admin/bd-scrape',{method:'POST',body:JSON.stringify(body)});
    $('bd-status').textContent='⏳ '+d.message;
    // Poll analytics after 60s
    setTimeout(()=>bdLoadAnalytics(),60000);
  }catch(e){
    $('bd-status').textContent='Error: '+e.message;
  }
}

async function bdSearchCommenters(){
  const term=$('bd-search').value.trim();
  if(!term)return;
  try{
    const d=await api('/api/admin/bd-search-commenters',{method:'POST',body:JSON.stringify({search:term})});
    $('bd-search-results').innerHTML=d.results.length?d.results.map(u=>
      `<div class="evt" style="cursor:pointer" onclick="bdShowUser('${esc(u.username)}')"><span class="tag">@${esc(u.username)}</span> ${u.comment_count} comments · ${u.total_likes||0} likes · last: ${relTime(u.last_comment)}</div>`
    ).join(''):'<div class="empty">No matches</div>';
  }catch(e){console.error(e)}
}

async function bdShowUser(username){
  try{
    const d=await api('/api/admin/bd-user-comments',{method:'POST',body:JSON.stringify({username})});
    const html=d.comments.map(c=>{
      const date=(c.comment_date||'').substring(0,10);
      return `<div class="report-row"><div><b>@${esc(c.comment_user)}</b> · ${date} · ${c.likes_number||0} likes</div><div class="body">${esc(c.comment||'')}</div><div class="meta">📌 <a href="${esc(c.post_url)}" target="_blank" style="color:var(--accent)">${esc(c.post_url)}</a></div></div>`;
    }).join('');
    $('bd-user-detail').innerHTML=`<h3 style="font-size:14px;font-weight:700;margin:0 0 10px">Comments by @${esc(username)} (${d.comments.length})</h3>${html||'<div class="empty">No comments found</div>'}`;
    $('bd-user-detail').scrollIntoView({behavior:'smooth'});
  }catch(e){console.error(e)}
}

async function bdShowPost(postUrl){
  try{
    const d=await api('/api/admin/bd-post-comments',{method:'POST',body:JSON.stringify({post_url:postUrl})});
    const p=d.post||{};
    const dateStr=(p.date_posted||'').substring(0,10);
    const comments=d.comments||[];
    const html=comments.map(c=>{
      const cdate=(c.comment_date||'').substring(0,10);
      return `<div class="report-row"><div><b>@${esc(c.comment_user)}</b> · ${cdate} · ${c.likes_number||0} likes · ${c.replies_number||0} replies</div><div class="body">${esc(c.comment||'')}</div></div>`;
    }).join('');
    $('bd-user-detail').innerHTML=`
      <h3 style="font-size:14px;font-weight:700;margin:0 0 4px">Post Comments — ${dateStr} (${comments.length} scraped / ${p.num_comments||0} total)</h3>
      <p style="font-size:13px;color:var(--muted);margin:0 0 6px">${esc((p.description||'').substring(0,150))}${(p.description||'').length>150?'…':''}</p>
      <p style="margin:0 0 10px"><a href="${esc(p.url)}" target="_blank" style="color:var(--accent);font-size:13px">↗ Open on Instagram</a> · ${p.likes||0} likes · ${p.content_type||''}</p>
      ${html||'<div class="empty">No comments scraped for this post yet</div>'}`;
    $('bd-user-detail').scrollIntoView({behavior:'smooth'});
  }catch(e){console.error(e)}
}

window.bdLoadAnalytics=bdLoadAnalytics; window.bdTriggerScrape=bdTriggerScrape;
window.bdSearchCommenters=bdSearchCommenters; window.bdShowUser=bdShowUser;
window.bdShowPost=bdShowPost;

// Auto-load BD analytics on boot
const origRefresh=refresh;
refresh=async function(){await origRefresh();try{await bdLoadAnalytics()}catch(_){}};

boot();
</script>
</body></html>'''
HTML = r'''<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f172a">
<title>BUMEN — Reviewer</title>
<style>
*,*::before,*::after{box-sizing:border-box}
:root{
  --bg:#f7f8fa; --card:#ffffff; --ink:#0f172a; --muted:#64748b;
  --line:#e6e8ee; --accent:#3b5bdb; --accent-2:#1f3a8a;
  --good:#16a34a; --warn:#f59e0b; --bad:#dc2626;
  --r-lg:24px; --r-md:18px; --r-sm:12px;
  --sh-1:0 1px 2px rgba(15,23,42,.04), 0 1px 1px rgba(15,23,42,.03);
  --sh-2:0 8px 24px rgba(15,23,42,.06), 0 2px 6px rgba(15,23,42,.04);
  --sh-3:0 18px 40px rgba(15,23,42,.10), 0 6px 12px rgba(15,23,42,.06);
}
html,body{margin:0;padding:0;background:var(--bg);color:var(--ink);
  font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Rounded","Inter",
    "Nunito","Quicksand","Segoe UI",Roboto,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;min-height:100%}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
a{color:inherit}

/* ==== AUTH ==== */
.auth-pg{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}
.auth-box{background:#fff;border-radius:24px;padding:48px 40px;max-width:440px;width:100%;
  box-shadow:var(--sh-3);text-align:center}
.auth-box .logo{width:84px;height:84px;margin:0 auto 16px;display:block}
.auth-box h1{margin:0 0 4px;font-size:26px;font-weight:700;letter-spacing:-.02em}
.auth-box p.sub{margin:0 0 28px;color:var(--muted);font-size:14px}
.auth-box label{display:block;text-align:left;font-size:12px;font-weight:600;
  text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 4px 6px}
.auth-box input{width:100%;padding:14px 16px;border:1px solid var(--line);border-radius:14px;
  background:#f9fafc;font-size:16px;color:var(--ink);outline:none;transition:.15s}
.auth-box input:focus{border-color:var(--accent);background:#fff;
  box-shadow:0 0 0 4px rgba(59,91,219,.12)}
.auth-box .btn{margin-top:18px;width:100%;background:var(--ink);color:#fff;
  padding:14px;border-radius:14px;font-weight:600;font-size:15px;letter-spacing:.01em;
  transition:.15s}
.auth-box .btn:hover{transform:translateY(-1px);box-shadow:var(--sh-2)}
.auth-box .err{color:var(--bad);font-size:13px;margin-top:12px;min-height:18px}

/* ==== APP SHELL ==== */
.shell{max-width:760px;margin:0 auto;padding:18px 18px 120px;position:relative}
.topbar{display:flex;align-items:center;justify-content:space-between;padding:8px 4px 24px;
  position:relative}
.topbar-logo{width:48px;height:48px;object-fit:contain;
  filter:drop-shadow(0 2px 6px rgba(59,91,219,.18))}
.topbar .brand-fallback{font-weight:700;letter-spacing:-.01em;font-size:18px}
.brand-center{display:flex;flex-direction:column;align-items:center;gap:2px;
  position:absolute;left:50%;top:14px;transform:translateX(-50%)}
.brand-text{font-size:13px;font-weight:600;letter-spacing:-.02em;color:var(--ink)}
.topbar-spacer{width:38px;height:38px}
.close-btn{width:40px;height:40px;border-radius:50%;background:#1f2937;color:#fff;
  display:flex;align-items:center;justify-content:center;border:0;
  transition:.15s;flex-shrink:0}
.close-btn:hover{background:#111827;transform:scale(1.05)}

.hello{margin:14px 4px 4px;font-size:32px;font-weight:800;letter-spacing:-.03em;line-height:1.15;text-align:center}
#hello-handle{color:var(--accent);font-weight:800}
.hello .sub{display:block;font-size:14px;color:var(--muted);font-weight:400;margin-top:8px;text-align:center}

.tabs{display:flex;gap:6px;background:#eef0f4;padding:5px;border-radius:14px;margin:14px 0 14px}
.tab{flex:1;padding:9px 0;border-radius:10px;font-weight:600;font-size:14px;color:var(--muted);
  transition:.15s;text-align:center}
.tab.active{background:#fff;color:var(--ink);box-shadow:var(--sh-1)}
.tab .count{display:inline-block;background:#eef0f4;color:var(--muted);
  font-size:11px;font-weight:600;padding:2px 8px;border-radius:99px;margin-left:6px}
.tab.active .count{background:var(--ink);color:#fff}

/* ==== ACTIVE 1:1 CARD ==== */
.active-card{background:var(--card);border-radius:20px;box-shadow:var(--sh-2);
  overflow:hidden;margin-bottom:18px;border:1px solid var(--line)}
.active-card .thumb{aspect-ratio:16/10;width:100%;background:#e6e8ee;position:relative;
  background-size:cover;background-position:center;max-height:240px;cursor:pointer;
  transition:filter .2s}
.active-card .thumb:hover{filter:brightness(.92)}
.active-card .thumb::before{content:"🔍 Lihat post";position:absolute;left:50%;top:50%;
  transform:translate(-50%,-50%);background:rgba(15,23,42,.65);color:#fff;
  font-size:12px;font-weight:600;padding:7px 14px;border-radius:99px;
  opacity:0;transition:opacity .2s;pointer-events:none;backdrop-filter:blur(4px)}
.active-card .thumb:hover::before{opacity:1}
.thumb-placeholder{display:flex;align-items:center;justify-content:center;width:100%;height:100%;font-size:28px;color:#94a3b8}
.thumb{background:#e6e8ee}
.thumb:hover .thumb-placeholder{opacity:0.5}
.active-card .thumb.locked::after{content:"";position:absolute;inset:0;background:rgba(15,23,42,.55);
  backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center;
  color:#fff;font-size:14px}
.active-card .body{padding:16px 18px 18px}
.active-card .meta{display:flex;align-items:center;justify-content:space-between;
  margin-bottom:8px;gap:10px}
.active-card .meta .post{font-size:11px;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em}
.active-card .meta .state{font-size:10px;font-weight:700;padding:3px 9px;border-radius:99px;
  background:#eef2ff;color:var(--accent);text-transform:uppercase;letter-spacing:.04em;
  flex-shrink:0}
.active-card .meta .state.copied{background:#dcfce7;color:var(--good)}
.active-card .meta .state.reported{background:#fef3c7;color:var(--warn)}
.active-card .text{font-size:20px;line-height:1.42;font-weight:500;letter-spacing:-.01em;
  color:var(--ink);margin:6px 0 16px}
.active-card .copy{margin-top:4px;background:var(--ink);color:#fff;padding:14px 18px;
  border-radius:12px;font-weight:600;width:100%;font-size:14px;letter-spacing:.01em;
  display:flex;align-items:center;justify-content:center;gap:8px;transition:.15s}
.active-card .copy:hover{background:var(--accent-2);transform:translateY(-1px);
  box-shadow:var(--sh-2)}
.active-card .copy svg{width:16px;height:16px}
.active-card .verify{margin-top:10px;display:flex;gap:8px}
.active-card .verify button{flex:1;padding:12px;border-radius:12px;font-weight:600;font-size:13px;
  background:#fff;border:1px solid var(--line);transition:.15s}
.active-card .verify button:hover{background:#f3f4f6}
.active-card .verify .primary{background:var(--good);color:#fff;border-color:transparent}
.active-card .verify .primary:hover{background:#15803d}
.active-card .verify .warn{background:#fff;color:var(--warn);border-color:#fde68a}
.active-card .verify .warn:hover{background:#fffbeb}
.active-card .verify .bad{background:#fff;color:var(--bad);border-color:#fecaca}
.active-card .verify .bad:hover{background:#fef2f2}

/* ==== COMPACT QUEUE CARDS ==== */
.queue{display:flex;flex-direction:column;gap:10px;margin-top:6px}
.queue-title{font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;
  letter-spacing:.08em;margin:18px 0 12px;text-align:center}
.q-card{display:flex;gap:14px;background:#f1f3f7;border:1px solid #eceff4;
  border-radius:var(--r-md);padding:14px;align-items:center;opacity:.62;
  filter:grayscale(.6);transition:.15s;cursor:pointer}
.q-card:hover{opacity:.85;filter:none}
.q-card .thumb{width:84px;height:84px;border-radius:14px;flex-shrink:0;
  background-size:cover;background-position:center;background-color:#e6e8ee;position:relative}
.q-card .thumb::after{content:"🔒";position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;background:rgba(15,23,42,.35);border-radius:14px;color:#fff;
  font-size:22px;opacity:.9}
.q-card .info{flex:1;min-width:0}
.q-card .info .t{font-size:13px;font-weight:600;color:var(--ink);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.q-card .info .s{font-size:12px;color:var(--muted);margin-top:2px}

/* ==== SELESAI ==== */
.done-list{display:flex;flex-direction:column;gap:10px}
.done-card{display:flex;gap:14px;background:#fff;border:1px solid var(--line);
  border-radius:var(--r-md);padding:14px;box-shadow:var(--sh-1);align-items:center}
.done-card .thumb{width:64px;height:64px;border-radius:12px;flex-shrink:0;
  background-size:cover;background-position:center;background-color:#e6e8ee}
.done-card .info{flex:1;min-width:0}
.done-card .info .t{font-size:13px;font-weight:600;color:var(--ink);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.done-card .info .s{font-size:12px;color:var(--good);margin-top:2px;font-weight:500}
.done-card .info .b{font-size:12px;color:var(--muted);margin-top:4px;overflow:hidden;
  text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}

/* ==== EMPTY ==== */
.empty{text-align:center;padding:48px 24px;color:var(--muted)}
.empty .icon{font-size:42px;margin-bottom:10px;opacity:.5}
.empty h3{margin:0 0 4px;font-size:16px;color:var(--ink);font-weight:600}
.empty p{margin:0;font-size:13px}

/* ==== TOAST ==== */
.toast{position:fixed;left:50%;bottom:88px;transform:translateX(-50%) translateY(20px);
  background:var(--ink);color:#fff;padding:11px 18px;border-radius:99px;font-size:13px;
  font-weight:500;box-shadow:var(--sh-3);opacity:0;transition:.2s;pointer-events:none;
  z-index:1000;max-width:90vw}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

/* ==== VERIFY MODAL ==== */
.modal-bg{position:fixed;inset:0;background:rgba(15,23,42,.5);backdrop-filter:blur(4px);
  display:none;align-items:center;justify-content:center;padding:20px;z-index:999}
.modal-bg.open{display:flex}
.modal{background:#fff;border-radius:24px;padding:28px;max-width:440px;width:100%;
  box-shadow:var(--sh-3)}
.modal h3{margin:0 0 6px;font-size:18px;font-weight:700}
.modal p{margin:0 0 18px;font-size:14px;color:var(--muted)}
.modal .row{display:flex;gap:8px;margin-top:18px}
.modal .row button{flex:1;padding:13px;border-radius:12px;font-weight:600;font-size:14px}
.modal .row .ghost{background:#fff;border:1px solid var(--line);color:var(--ink)}
.modal .row .good{background:var(--good);color:#fff}
.modal .row .warn{background:var(--warn);color:#fff}

/* ==== ONBOARDING MODAL ==== */
.onboard-modal{max-width:480px;padding:0;overflow:hidden}
.onboard-hero{background:linear-gradient(135deg,#3b5bdb 0%,#1f3a8a 100%);
  color:#fff;padding:28px 24px 22px;text-align:center;position:relative}
.onboard-badge{width:54px;height:54px;border-radius:50%;background:rgba(255,255,255,.18);
  display:flex;align-items:center;justify-content:center;margin:0 auto 12px;
  backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.25)}
.onboard-hero h3{margin:0 0 6px;font-size:22px;font-weight:700;letter-spacing:-.02em}
.onboard-tag{margin:0;font-size:13px;color:#fff;font-weight:400}
.onboard-steps{padding:18px 22px;display:flex;flex-direction:column;gap:14px}
.step{display:flex;gap:12px;align-items:flex-start}
.step-num{flex-shrink:0;width:28px;height:28px;border-radius:50%;
  background:#eef2ff;color:var(--accent);font-weight:700;font-size:13px;
  display:flex;align-items:center;justify-content:center}
.step-body h4{margin:0 0 3px;font-size:14px;font-weight:600;color:var(--ink);
  letter-spacing:-.01em}
.step-body p{margin:0;font-size:13px;color:var(--muted);line-height:1.45}
.onboard-rule{margin:0 22px 16px;padding:12px 14px;background:#fff7ed;
  border:1px solid #fed7aa;border-radius:12px;font-size:13px;color:#92400e;
  line-height:1.45}
.onboard-rule b{color:#7c2d12}
.onboard-row{padding:0 22px 22px;display:flex;gap:8px}
.onboard-row button{flex:1;padding:13px;border-radius:12px;font-weight:600;font-size:14px;
  transition:.15s}
.onboard-row .ghost{background:#fff;border:1px solid var(--line);color:var(--ink)}
.onboard-row .ghost:hover{background:#f3f4f6}
.onboard-row .primary{background:var(--ink);color:#fff;border:0}
.onboard-row .primary:hover{background:var(--accent-2);transform:translateY(-1px)}
@keyframes onboard-in{
  from{opacity:0;transform:translateY(20px) scale(.96)}
  to{opacity:1;transform:translateY(0) scale(1)}
}
.onboard-bg .onboard-modal{animation:onboard-in .35s cubic-bezier(.2,.9,.3,1.1)}

/* ==== POST PREVIEW MODAL ==== */
.preview-modal{max-width:560px;width:100%;padding:0;overflow:hidden;position:relative;
  background:#fff;max-height:90vh;display:flex;flex-direction:column}
.preview-close{position:absolute;top:12px;right:12px;z-index:5;
  width:36px;height:36px;border-radius:50%;background:rgba(255,255,255,.95);
  color:var(--ink);display:flex;align-items:center;justify-content:center;
  box-shadow:0 4px 12px rgba(0,0,0,.18);border:0;backdrop-filter:blur(8px)}
.preview-close:hover{background:#fff;transform:scale(1.05)}
.preview-img-wrap{width:100%;background:#0f172a;display:flex;align-items:center;justify-content:center;
  max-height:55vh;overflow:hidden}
.preview-img{display:block;width:auto;height:auto;max-width:100%;max-height:55vh;
  object-fit:contain}
.preview-body{padding:18px 22px 22px;overflow-y:auto}
.preview-meta{display:flex;align-items:baseline;justify-content:space-between;
  gap:10px;margin-bottom:10px;flex-wrap:wrap}
.preview-title{font-size:13px;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em}
.preview-date{font-size:11px;font-weight:600;color:var(--muted);
  background:#eef0f4;padding:3px 9px;border-radius:99px}
.preview-desc{font-size:14px;line-height:1.55;color:var(--ink);white-space:pre-wrap;
  word-break:break-word}
.preview-desc:empty::before{content:"Tidak ada deskripsi post.";
  color:var(--muted);font-style:italic}

@media (max-width:520px){
  .shell{padding:16px 14px 100px}
  .active-card .text{font-size:24px}
  .hello{font-size:24px}
}
</style>
</head>
<body>

<!-- AUTH -->
<div id="auth-v" class="auth-pg">
  <div class="auth-box">
    <img class="logo" src="/bumen-logo.png" onerror="this.style.display='none'" alt="BUMEN">
    <h1>BUMEN Reviewer</h1>
    <p class="sub">Internal reviewer access · login with Instagram handle</p>
    <label for="hinp">Akun Instagram</label>
    <input id="hinp" placeholder="@senadavina" autocomplete="off" autocapitalize="off" spellcheck="false">
    <button class="btn" onclick="doLogin()">LOGIN</button>
    <div class="err" id="auth-err"></div>
  </div>
</div>

<!-- APP -->
<div id="app-v" style="display:none">
  <div class="shell">
    <div class="topbar">
      <div class="topbar-spacer"></div>
      <div class="brand-center">
        <img class="topbar-logo" src="/bumen-logo.png" onerror="this.outerHTML='<div class=&quot;brand-fallback&quot;>BUMEN</div>'" alt="BUMEN">
      </div>
      <button class="close-btn" onclick="doLogout()" aria-label="Keluar">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="7" y1="7" x2="17" y2="17"/><line x1="7" y1="17" x2="17" y2="7"/></svg>
      </button>
    </div>

    <div class="hello" id="hello-v">Selamat <span id="hello-time">pagi</span>, <span id="hello-handle"></span><span class="sub" id="hello-sub"></span></div>

    <div class="tabs">
      <div class="tab active" data-tab="tugas" onclick="switchTab('tugas')">
        TUGAS<span class="count" id="count-tugas">0</span>
      </div>
      <div class="tab" data-tab="selesai" onclick="switchTab('selesai')">
        SELESAI<span class="count" id="count-selesai">0</span>
      </div>
    </div>

    <!-- TUGAS TAB -->
    <div id="tugas-v">
      <div id="active-card"></div>
      <div class="queue-title" id="queue-title" style="display:none">TUGAS LAINNYA</div>
      <div class="queue" id="queue"></div>
      <div id="empty-tugas" class="empty" style="display:none">
        <div class="icon">✨</div>
        <h3>Semua tugas selesai</h3>
        <p>Tap "Ambil Tugas" untuk mulai lagi.</p>
        <button onclick="claimNext()" style="margin-top:18px;background:var(--accent);color:#fff;
          padding:12px 22px;border-radius:12px;font-weight:600">Ambil Tugas</button>
      </div>
    </div>

    <!-- SELESAI TAB -->
    <div id="selesai-v" style="display:none">
      <div id="done-list" class="done-list"></div>
      <div id="empty-selesai" class="empty" style="display:none">
        <div class="icon">📋</div>
        <h3>Belum ada tugas selesai</h3>
        <p>Tugas yang sudah terverifikasi akan muncul di sini.</p>
      </div>
    </div>
  </div>
</div>

<!-- ONBOARDING MODAL -->
<div id="onboard-modal" class="modal-bg onboard-bg">
  <div class="modal onboard-modal">
    <div class="onboard-hero">
      <div class="onboard-badge">
        <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>
      </div>
      <h3>Selamat datang di BUMEN</h3>
      <p class="onboard-tag">Pelajari alurnya sebelum mulai</p>
    </div>

    <div class="onboard-steps">
      <div class="step">
        <div class="step-num">1</div>
        <div class="step-body">
          <h4>1 tugas aktif</h4>
          <p>Hanya <b>1 post</b> yang terbuka pada satu waktu. Selesaikan dulu, baru lanjut ke berikutnya.</p>
        </div>
      </div>
      <div class="step">
        <div class="step-num">2</div>
        <div class="step-body">
          <h4>Copy &amp; buka Instagram</h4>
          <p>Ketuk tombol di kartu aktif. Komentar otomatis tersalin dan IG post terbuka di tab baru.</p>
        </div>
      </div>
      <div class="step">
        <div class="step-num">3</div>
        <div class="step-body">
          <h4>Paste &amp; posting di IG</h4>
          <p>Paste komentar di kolom komentar Instagram, lalu kirim. Pastikan sudah benar-benar live.</p>
        </div>
      </div>
      <div class="step">
        <div class="step-num">4</div>
        <div class="step-body">
          <h4>Konfirmasi</h4>
          <p>Setelah live, ketuk <b>Sudah Live</b>. Kalau belum terlihat, pilih <b>Lapor Admin</b>.</p>
        </div>
      </div>
    </div>

    <div class="onboard-rule">
      <b>Penting:</b> Pastikan bahwa isi komentar sudah sesuai dengan postingan yang ditugaskan. 1 komentar hanya untuk 1 postingan IG.
    </div>

    <div class="onboard-row">
      <button class="ghost" onclick="dismissOnboard(true)">Tampilkan lagi nanti</button>
      <button class="primary" onclick="dismissOnboard(false)">Oke siap!</button>
    </div>
  </div>
</div>

<!-- POST PREVIEW MODAL -->
<div id="preview-modal" class="modal-bg preview-bg" onclick="if(event.target===this)closePreview()">
  <div class="modal preview-modal">
    <button class="preview-close" onclick="closePreview()" aria-label="Tutup">
      <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="7" y1="7" x2="17" y2="17"/><line x1="7" y1="17" x2="17" y2="7"/></svg>
    </button>
    <div class="preview-img-wrap">
      <img id="preview-img" class="preview-img" alt="Post">
    </div>
    <div class="preview-body">
      <div class="preview-meta">
        <div class="preview-title" id="preview-title"></div>
        <div class="preview-date" id="preview-date"></div>
      </div>
      <div class="preview-desc" id="preview-desc"></div>
    </div>
  </div>
</div>

<!-- VERIFY MODAL -->
<div id="verify-modal" class="modal-bg">
  <div class="modal">
    <h3>Konfirmasi Komentar</h3>
    <p>Sudah posting komentar ini di Instagram? Buka Post IG untuk paste dari clipboard.</p>
    <div class="row">
      <button class="ghost" onclick="closeVerify()">Belum</button>
      <button class="warn" onclick="laporkan()">Lapor Admin</button>
      <button class="good" onclick="konfirmasi()">Sudah Live</button>
    </div>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
const S={tab:'tugas',user:null,active:null,queue:[],done:[],claimAfterCopy:false};

async function api(p,o={}){
  const r=await fetch(p,{...o,headers:{'Content-Type':'application/json',...(o.headers||{})}});
  const d=await r.json();
  if(!r.ok) throw new Error(d.error||'Request gagal');
  return d;
}
function $(id){return document.getElementById(id)}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function toast(m){const t=$('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2200)}

async function boot(){
  try{
    const m=await api('/api/me');
    if(!m.user){$('auth-v').style.display='flex';return}
    S.user=m.user; enterApp();
  }catch(_){$('auth-v').style.display='flex'}
}

async function doLogin(){
  const h=$('hinp').value.trim();
  if(!h){$('auth-err').textContent='Isi handle dulu';return}
  try{
    await api('/api/login',{method:'POST',body:JSON.stringify({handle:h})});
    location.reload();
  }catch(e){$('auth-err').textContent=e.message}
}
async function doLogout(){
  await api('/api/logout',{method:'POST'}); location.reload();
}

async function enterApp(){
  $('auth-v').style.display='none';
  $('app-v').style.display='block';
  $('hello-handle').textContent=S.user.handle;
  // Time-of-day greeting (WIB)
  const h=new Date().getHours();
  let t='pagi';
  if(h>=11&&h<15)t='siang';
  else if(h>=15&&h<18)t='sore';
  else if(h>=18||h<4)t='malam';
  $('hello-time').textContent=t;
  $('hello-sub').textContent='Tugas aktif akan tampil di sini. Selesaikan satu per satu.';
  await refresh();
  // Always ensure 1 active task exists; auto-promote if not
  if(!S.active){ await claimNext() }
  // Show onboarding modal — cached 2 hours per session
  showOnboardIfStale();
}

function showOnboardIfStale(){
  const KEY='bumen_onboard_ts';
  const last=parseInt(localStorage.getItem(KEY)||'0',10);
  const TWO_HOURS=2*60*60*1000;
  if(!last || (Date.now()-last) > TWO_HOURS){
    setTimeout(()=>$('onboard-modal').classList.add('open'), 350);
  }
}
function dismissOnboard(later){
  const KEY='bumen_onboard_ts';
  if(later){
    // "Tampilkan lagi nanti" = reset cache so it pops again next visit
    localStorage.removeItem(KEY);
  }else{
    // "Mengerti, mulai" = cache for 2 hours
    localStorage.setItem(KEY, String(Date.now()));
  }
  $('onboard-modal').classList.remove('open');
}

async function refresh(){
  const d=await api('/api/kanban');
  S.active=d.active; S.queue=d.queue; S.done=d.done;
  $('count-tugas').textContent=(S.active?1:0)+S.queue.length;
  $('count-selesai').textContent=S.done.length;
  renderActive(); renderQueue(); renderDone();
}

function renderActive(){
  const el=$('active-card');
  if(!S.active){
    el.innerHTML='';
    if(!S.queue.length){$('empty-tugas').style.display='block'}
    return;
  }
  $('empty-tugas').style.display='none';
  const a=S.active;
  const thumbUrl=a.thumbnail_url?`/api/image-proxy/${encodeURIComponent(a.thumbnail_url)}`:'';
  const thumbStyle=thumbUrl?`background-image:url('${esc(thumbUrl)}')`:'';
  const stateLabel={claimed:'BELUM',copied:'TERSALIN',reported:'DILAPORKAN'}[a.state]||a.state;
  const stateClass=a.state;
  el.innerHTML=`
    <div class="active-card">
      <div class="thumb" style="${thumbStyle}" onclick="openPreview(${a.assignment_id})" title="Lihat post lengkap" onerror="this.onerror=null;this.style.background='#e6e8ee';this.innerHTML='<div class=thumb-placeholder>🖼️</div>'"></div>
      <div class="body">
        <div class="meta">
          <div class="post">${esc(a.post_title)}</div>
          <div class="state ${stateClass}">${stateLabel}</div>
        </div>
        <div class="text">${esc(a.body)}</div>
        ${a.state==='claimed'?`
          <button class="copy" onclick="copyOpen(${a.assignment_id})">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            Copy &amp; Buka IG Post
          </button>
        `:`
          <button class="copy" onclick="openIG(${a.assignment_id})">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
            Buka Post di Instagram
          </button>
          <div class="verify">
            <button class="warn" onclick="showVerify()">Belum Live</button>
            <button class="primary" onclick="showVerify()">Sudah Live</button>
          </div>
        `}
      </div>
    </div>`;
}

function renderQueue(){
  const q=$('queue'),title=$('queue-title');
  if(!S.queue.length){q.innerHTML='';title.style.display='none';return}
  title.style.display='block';
  q.innerHTML=S.queue.map(a=>{
    const thumbUrl=a.thumbnail_url?`/api/image-proxy/${encodeURIComponent(a.thumbnail_url)}`:'';
    return `<div class="q-card" onclick="onLockedCard()">
      <div class="thumb" style="${thumbUrl?`background-image:url('${esc(thumbUrl)}')`:''}" onerror="this.onerror=null;this.style.background='#e6e8ee';this.innerHTML='<div class=thumb-placeholder>🖼️</div>'"></div>
      <div class="info">
        <div class="t">${esc(a.post_title)}</div>
        <div class="s">Tugas berikutnya · selesaikan yang aktif dulu</div>
      </div>
    </div>`;
  }).join('');
}

function renderDone(){
  const el=$('done-list');
  if(!S.done.length){$('empty-selesai').style.display='block';el.innerHTML='';return}
  $('empty-selesai').style.display='none';
  el.innerHTML=S.done.map(a=>{
    const thumbUrl=a.thumbnail_url?`/api/image-proxy/${encodeURIComponent(a.thumbnail_url)}`:'';
    return `<div class="done-card">
      <div class="thumb" style="${thumbUrl?`background-image:url('${esc(thumbUrl)}')`:''}" onerror="this.onerror=null;this.style.background='#e6e8ee';this.innerHTML='<div class=thumb-placeholder>🖼️</div>'"></div>
      <div class="info">
        <div class="t">${esc(a.post_title)}</div>
        <div class="s">✓ Sudah diverifikasi</div>
        <div class="b">${esc(a.body)}</div>
      </div>
    </div>`;
  }).join('');
}

function onLockedCard(){
  toast('Selesaikan tugas yang lain dulu...');
}

function openPreview(aid){
  const a=S.active; if(!a||a.assignment_id!==aid)return;
  $('preview-img').src=a.thumbnail_url||'';
  $('preview-img').alt=a.post_title||'Post';
  $('preview-title').textContent=a.post_title||'';
  // Format date_posted
  let dateStr='—';
  if(a.date_posted){
    try{
      const d=new Date(a.date_posted.replace(' ','T'));
      if(!isNaN(d.getTime())){
        const months=['Jan','Feb','Mar','Apr','Mei','Jun','Jul','Agu','Sep','Okt','Nov','Des'];
        dateStr=`${d.getDate()} ${months[d.getMonth()]} ${d.getFullYear()}`;
      } else { dateStr=a.date_posted; }
    }catch(_){ dateStr=a.date_posted; }
  }
  $('preview-date').textContent='📅 '+dateStr;
  $('preview-desc').textContent=a.description||'';
  $('preview-modal').classList.add('open');
}
function closePreview(){ $('preview-modal').classList.remove('open'); }

function switchTab(t){
  S.tab=t;
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.dataset.tab===t));
  $('tugas-v').style.display=t==='tugas'?'block':'none';
  $('selesai-v').style.display=t==='selesai'?'block':'none';
}

async function claimNext(){
  try{
    const d=await api('/api/claim',{method:'POST'});
    if(d.assignment){toast('Tugas baru diambil');await refresh()}
    else{toast('Tidak ada tugas tersedia')}
  }catch(e){toast(e.message)}
}

async function copyOpen(aid){
  try{
    const d=await api('/api/copy',{method:'POST',body:JSON.stringify({assignment_id:aid})});
    try{await navigator.clipboard.writeText(d.body)}catch(_){}
    toast('Tersalin ✓');
    setTimeout(()=>{window.open(d.source_url,'_blank')},250);
    setTimeout(()=>showVerify(),1500);
    await refresh();
  }catch(e){toast(e.message)}
}
function openIG(aid){
  const a=S.active; if(!a)return;
  window.open(a.source_url,'_blank');
}

function showVerify(){ $('verify-modal').classList.add('open') }
function closeVerify(){ $('verify-modal').classList.remove('open') }

async function konfirmasi(){
  try{
    await api('/api/verify',{method:'POST',body:JSON.stringify({
      assignment_id:S.active.assignment_id, claimed_seen:true
    })});
    closeVerify(); toast('Tugas selesai ✓');
    await refresh();
    // Auto-promote next task from queue (or seed fresh if exhausted)
    if(!S.active){ setTimeout(()=>claimNext(), 400) }
  }catch(e){toast(e.message)}
}
async function laporkan(){
  try{
    await api('/api/report',{method:'POST',body:JSON.stringify({
      assignment_id:S.active.assignment_id,
      note:'Reviewer says comment not visible live'
    })});
    closeVerify(); toast('Dilaporkan ke admin');
    await refresh();
  }catch(e){toast(e.message)}
}

window.doLogin=doLogin; window.doLogout=doLogout;
window.claimNext=claimNext; window.copyOpen=copyOpen; window.openIG=openIG;
window.showVerify=showVerify; window.closeVerify=closeVerify;
window.konfirmasi=konfirmasi; window.laporkan=laporkan;
window.switchTab=switchTab; window.onLockedCard=onLockedCard;
window.dismissOnboard=dismissOnboard;
window.openPreview=openPreview; window.closePreview=closePreview;

boot();
</script>
</body>
</html>'''

# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def _send(self, code, body, ctype="text/html", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body.encode("utf-8") if isinstance(body, str) else body)))
        for h, v in extra: self.send_header(h, v)
        self.end_headers()
        self.wfile.write(body.encode("utf-8") if isinstance(body, str) else body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def _file(self, path, ctype):
        try:
            data = path.read_bytes()
            self._send(200, data, ctype)
        except FileNotFoundError:
            self._send(404, "Not found", "text/plain")

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    def _set_cookie(self, name, val, max_age=60*60*24*30):
        c = cookies.SimpleCookie()
        c[name] = val; c[name]["path"] = "/"; c[name]["max-age"] = str(max_age)
        self.send_header("Set-Cookie", c.output(header="").strip())

    def _get_sid(self):
        for part in self.headers.get("Cookie", "").split(";"):
            if "bumen_sid=" in part:
                return part.split("bumen_sid=")[1].strip()
        return None

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        # Debug: log incoming path
        print(f"[GET] path={path}", flush=True)
        if path == "/health":
            print("[GET] health endpoint hit", flush=True)
            return self._json(200, {"status": "OK", "version": "2026-08-24-v2", "posts": 24})
        if path in ("/", "/index.html"):
            return self._send(200, HTML, "text/html; charset=utf-8")
        if path == "/admin":
            return self._send(200, ADMIN_HTML, "text/html; charset=utf-8")
        if path == "/bumen-logo.png":
            return self._file(ROOT / "bumen-logo.png", "image/png")
        if path.startswith("/api/thumbnail/"):
            return self._handle_thumbnail(path)
        if path.startswith("/api/image-proxy/"):
            return self._handle_image_proxy(path)
        if path.startswith("/api/admin/"):
            return self._handle_admin_get(path)
        if path == "/api/me":
            sid = self._get_sid()
            user = SESSIONS.get(sid) if sid else None
            return self._json(200, {"user": user})
        if path == "/api/kanban":
            return self._handle_kanban()
        return self._send(404, "Not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        try:
            if path == "/api/login":
                d = self._read_json()
                ip = self.client_address[0]
                ua = self.headers.get("User-Agent", "")
                sid = login(d.get("handle", ""), ip=ip, ua=ua)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._set_cookie("bumen_sid", sid)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "sid": sid}).encode())
                return
            if path == "/api/logout":
                sid = self._get_sid()
                if sid and sid in SESSIONS: del SESSIONS[sid]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._set_cookie("bumen_sid", "", max_age=0)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
                return

            # Admin login (no auth required)
            if path == "/api/admin/login":
                d = self._read_json()
                sid = admin_login(d.get("username", ""), d.get("password", ""))
                if not sid: return self._json(401, {"error": "Username/password salah"})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._set_cookie("bumen_admin_sid", sid)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
                return
            if path == "/api/admin/logout":
                sid = self._get_admin_sid()
                if sid and sid in ADMIN_SESSIONS: del ADMIN_SESSIONS[sid]
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._set_cookie("bumen_admin_sid", "", max_age=0)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
                return

            # Admin thumbnail refresh endpoint
            if path == "/api/admin/refresh-thumbnails":
                sid = self._get_admin_sid()
                admin = ADMIN_SESSIONS.get(sid) if sid else None
                if not admin:
                    return self._json(401, {"error": "Admin auth required"})
                # Run refresh in background
                import threading
                def do_refresh():
                    refresh_instagram_thumbnails()
                threading.Thread(target=do_refresh, daemon=True).start()
                return self._json(200, {"status": "started", "message": "Thumbnail refresh initiated"})

            # Admin bulk thumbnail update (for manual sync from local)
            if path == "/api/admin/update-thumbnails":
                d = self._read_json()
                # Allow auth via password in body OR admin session cookie
                admin = None
                if d.get("password") == ADMIN_PW_DEFAULT:
                    admin = {"username": "admin", "id": 1}
                else:
                    sid = self._get_admin_sid()
                    admin = ADMIN_SESSIONS.get(sid) if sid else None
                if not admin:
                    return self._json(401, {"error": "Admin auth required"})
                updates = d.get("updates", {})
                if not updates:
                    return self._json(400, {"error": "updates dict required"})
                c = db()
                updated = 0
                for post_id, thumb_url in updates.items():
                    cur = c.execute("UPDATE posts SET thumbnail_url = ? WHERE id = ?", (thumb_url, int(post_id)))
                    if cur.rowcount > 0:
                        updated += 1
                c.commit()
                c.close()
                return self._json(200, {"updated": updated})

            # Admin full sync from data.json (posts + comments + live_comments)
            if path == "/api/admin/full-sync":
                d = self._read_json()
                # Allow auth via password in body
                if d.get("password") != ADMIN_PW_DEFAULT:
                    return self._json(401, {"error": "Admin auth required"})
                if not DATA_JSON.exists():
                    return self._json(400, {"error": "data.json not found"})
                c = db()
                data = json.loads(DATA_JSON.read_text())
                posts_upserted = 0
                comments_upserted = 0
                for p in data:
                    c.execute(
                        "INSERT OR IGNORE INTO posts(id, source_url, title, thumbnail_url, description) VALUES(?,?,?,?,?)",
                        (p["id"], p.get("source_url", ""), p.get("title", ""), p.get("thumb", ""), p.get("description", "")),
                    )
                    if c.rowcount > 0:
                        posts_upserted += 1
                    for cmt in p.get("comments", []):
                        c.execute(
                            "INSERT OR IGNORE INTO comments(post_id, body, status) VALUES(?,?,'available')",
                            (p["id"], cmt),
                        )
                        if c.rowcount > 0:
                            comments_upserted += 1
                # Reseed live_comments
                c.execute("DELETE FROM live_comments WHERE username='__bumen_seed__'")
                live_seeded = 0
                for p in data:
                    for cmt in p.get("comments", []):
                        c.execute(
                            "INSERT INTO live_comments(post_id, username, body, scraped_at) VALUES(?, '__bumen_seed__', ?, CURRENT_TIMESTAMP)",
                            (p["id"], cmt),
                        )
                        live_seeded += 1
                c.commit()
                c.close()
                return self._json(200, {"posts_upserted": posts_upserted, "comments_upserted": comments_upserted, "live_seeded": live_seeded})

            # Admin manual reseed live_comments from data.json
            if path == "/api/admin/reseed-live-comments":
                print(f"[DEBUG] reseed endpoint hit, path={path}", flush=True)
                d = self._read_json()
                print(f"[DEBUG] request body: {d}", flush=True)
                # Allow auth via password in body OR admin session cookie
                admin = None
                if d.get("password") == ADMIN_PW_DEFAULT:
                    admin = {"username": "admin", "id": 1}
                    print("[DEBUG] password auth success", flush=True)
                else:
                    sid = self._get_admin_sid()
                    admin = ADMIN_SESSIONS.get(sid) if sid else None
                    print(f"[DEBUG] cookie auth: sid={sid}, admin={admin}", flush=True)
                if not admin:
                    return self._json(401, {"error": "Admin auth required"})
                if not DATA_JSON.exists():
                    return self._json(400, {"error": "data.json not found"})
                c = db()
                # Delete old seed rows
                c.execute("DELETE FROM live_comments WHERE username='__bumen_seed__'")
                # Insert fresh
                seeded = 0
                for p in json.loads(DATA_JSON.read_text()):
                    for cmt in p.get("comments", []):
                        c.execute(
                            "INSERT INTO live_comments(post_id, username, body, scraped_at) VALUES(?, '__bumen_seed__', ?, CURRENT_TIMESTAMP)",
                            (p["id"], cmt),
                        )
                        seeded += 1
                c.commit()
                total_seeded = c.execute("SELECT COUNT(*) FROM live_comments WHERE username='__bumen_seed__'").fetchone()[0]
                c.close()
                return self._json(200, {"seeded": seeded, "total_seeded": total_seeded})

            # Admin live-comments ingestion (password auth for scraper)
            if path == "/api/admin/live-comments":
                d = self._read_json()
                # Allow auth via password in body OR admin session cookie
                admin = None
                if d.get("password") == ADMIN_PW_DEFAULT:
                    admin = {"username": "admin", "id": 1}
                else:
                    sid = self._get_admin_sid()
                    admin = ADMIN_SESSIONS.get(sid) if sid else None
                if not admin:
                    return self._json(401, {"error": "Admin auth required"})
                post_id = int(d.get("post_id", 0))
                comments = d.get("comments", [])
                thumbnail = d.get("thumbnail", None)
                
                if not post_id or not comments:
                    return self._json(400, {"error": "post_id and comments required"})
                
                c = db()
                saved = 0
                for cm in comments:
                    body = cm.get("body", "").strip()
                    username = cm.get("username", "anonymous")
                    if not body: continue
                    try:
                        cur = c.execute("""
                            INSERT OR IGNORE INTO live_comments (post_id, username, body, scraped_at)
                            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                        """, (post_id, username, body))
                        if cur.rowcount > 0:
                            saved += 1
                    except sqlite3.IntegrityError:
                        pass
                
                if thumbnail:
                    c.execute("UPDATE posts SET thumbnail_url = ? WHERE id = ?", (thumbnail, post_id))
                
                c.commit(); c.close()
                return self._json(200, {"saved": saved})

            # === BRIGHT DATA SCRAPE ENDPOINTS ===

            # Trigger Bright Data scrape (async, runs in background)
            if path == "/api/admin/bd-scrape":
                d = self._read_json()
                admin = None
                if d.get("password") == ADMIN_PW_DEFAULT:
                    admin = {"username": "admin", "id": 1}
                else:
                    sid = self._get_admin_sid()
                    admin = ADMIN_SESSIONS.get(sid) if sid else None
                if not admin:
                    return self._json(401, {"error": "Admin auth required"})
                if not BD_AVAILABLE:
                    return self._json(500, {"error": "brightdata_scraper module not available"})

                profile = d.get("profile", "duniameutya")
                start_date = d.get("start_date")  # MM-DD-YYYY
                end_date = d.get("end_date")      # MM-DD-YYYY
                num_posts = d.get("num_posts", 100)

                # Run in background thread
                def do_scrape():
                    try:
                        result = run_full_scrape(
                            profile=profile,
                            start_date=start_date,
                            end_date=end_date,
                            num_posts=num_posts
                        )
                        print(f"[BD] Scrape result: {json.dumps(result)}", flush=True)
                    except Exception as e:
                        print(f"[BD] Scrape error: {e}", flush=True)

                threading.Thread(target=do_scrape, daemon=True).start()
                return self._json(200, {
                    "status": "started",
                    "message": f"Scraping @{profile} ({start_date or 'all'} to {end_date or 'all'}) in background"
                })

            # Get Bright Data analytics
            if path == "/api/admin/bd-analytics":
                d = self._read_json()
                admin = None
                if d.get("password") == ADMIN_PW_DEFAULT:
                    admin = {"username": "admin", "id": 1}
                else:
                    sid = self._get_admin_sid()
                    admin = ADMIN_SESSIONS.get(sid) if sid else None
                if not admin:
                    return self._json(401, {"error": "Admin auth required"})
                if not BD_AVAILABLE:
                    return self._json(500, {"error": "brightdata_scraper module not available"})

                profile = d.get("profile", "duniameutya")
                start_date = d.get("start_date")
                end_date = d.get("end_date")
                analytics = get_analytics(profile=profile, start_date=start_date, end_date=end_date)
                return self._json(200, analytics)

            # Search commenters
            if path == "/api/admin/bd-search-commenters":
                d = self._read_json()
                admin = None
                if d.get("password") == ADMIN_PW_DEFAULT:
                    admin = {"username": "admin", "id": 1}
                else:
                    sid = self._get_admin_sid()
                    admin = ADMIN_SESSIONS.get(sid) if sid else None
                if not admin:
                    return self._json(401, {"error": "Admin auth required"})
                if not BD_AVAILABLE:
                    return self._json(500, {"error": "brightdata_scraper module not available"})

                search_term = d.get("search", "")
                if not search_term:
                    return self._json(400, {"error": "search term required"})
                results = search_commenters(search_term, limit=50)
                return self._json(200, {"results": results})

            # Get comments by specific user
            if path == "/api/admin/bd-user-comments":
                d = self._read_json()
                admin = None
                if d.get("password") == ADMIN_PW_DEFAULT:
                    admin = {"username": "admin", "id": 1}
                else:
                    sid = self._get_admin_sid()
                    admin = ADMIN_SESSIONS.get(sid) if sid else None
                if not admin:
                    return self._json(401, {"error": "Admin auth required"})
                if not BD_AVAILABLE:
                    return self._json(500, {"error": "brightdata_scraper module not available"})

                username = d.get("username", "")
                if not username:
                    return self._json(400, {"error": "username required"})
                comments = get_comments_by_user(username)
                return self._json(200, {"username": username, "comments": comments})

            # Get comments by post URL
            if path == "/api/admin/bd-post-comments":
                d = self._read_json()
                admin = None
                if d.get("password") == ADMIN_PW_DEFAULT:
                    admin = {"username": "admin", "id": 1}
                else:
                    sid = self._get_admin_sid()
                    admin = ADMIN_SESSIONS.get(sid) if sid else None
                if not admin:
                    return self._json(401, {"error": "Admin auth required"})
                if not BD_AVAILABLE:
                    return self._json(500, {"error": "brightdata_scraper module not available"})

                post_url = d.get("post_url", "")
                if not post_url:
                    return self._json(400, {"error": "post_url required"})
                result = get_comments_by_post(post_url)
                return self._json(200, result)

            user = SESSIONS.get(self._get_sid() or "")
            if not user: return self._json(401, {"error": "Belum login"})

            if path == "/api/claim":
                a = claim_next_comment(user["id"])
                if a: return self._json(200, {"assignment": a})
                return self._json(200, {"assignment": None})
            if path == "/api/copy":
                d = self._read_json()
                r = copy_comment(user["id"], int(d["assignment_id"]))
                return self._json(200, r)
            if path == "/api/verify":
                d = self._read_json()
                r = verify_done(user["id"], int(d["assignment_id"]), bool(d.get("claimed_seen")))
                return self._json(200, r)
            if path == "/api/report":
                d = self._read_json()
                report_unconfirmed(user["id"], int(d["assignment_id"]), d.get("note", ""))
                return self._json(200, {"reported": True})
            return self._json(404, {"error": "Not found"})
        except Exception as e:
            return self._json(400, {"error": str(e)})

    def _handle_kanban(self):
        sid = self._get_sid()
        user = SESSIONS.get(sid) if sid else None
        if not user: return self._json(401, {"error": "Belum login"})
        c = db()
        active = None
        # 1) user's active (claimed/copied/reported)
        row = c.execute("""
            SELECT a.id AS aid, a.state, a.claimed_at, a.copied_at, a.verified_at,
                   cm.id AS cid, cm.body, cm.post_id,
                   p.title, p.thumbnail_url, p.source_url, p.description
            FROM assignments a
            JOIN comments cm ON cm.id = a.comment_id
            JOIN posts p ON p.id = cm.post_id
            WHERE a.user_id = ? AND a.state IN ('claimed','copied','reported')
            ORDER BY a.claimed_at DESC LIMIT 1
        """, (user["id"],)).fetchone()
        if row:
            # Date posted = earliest live_comment scraped_at, fall back to claimed_at
            date_posted = row["claimed_at"] or ""
            try:
                ts_row = c.execute("""
                    SELECT MIN(scraped_at) AS first_seen FROM live_comments WHERE post_id = ?
                """, (row["post_id"],)).fetchone()
                if ts_row and ts_row["first_seen"]:
                    date_posted = ts_row["first_seen"]
            except sqlite3.OperationalError:
                pass
            active = {
                "assignment_id": row["aid"], "state": row["state"],
                "comment_id": row["cid"], "body": row["body"],
                "post_id": row["post_id"], "post_title": row["title"],
                "thumbnail_url": row["thumbnail_url"], "source_url": normalize_ig_url(row["source_url"]),
                "description": row["description"] or "",
                "date_posted": date_posted or "",
            }
        # 2) user's own queue (waiting tasks, locked)
        qrows = c.execute("""
            SELECT cm.id AS cid, cm.body, p.title, p.thumbnail_url, p.source_url
            FROM assignments a
            JOIN comments cm ON cm.id = a.comment_id
            JOIN posts p ON p.id = cm.post_id
            WHERE a.user_id = ? AND a.state = 'queued'
            ORDER BY a.id ASC LIMIT 14
        """, (user["id"],)).fetchall()
        queue = [dict(r) for r in qrows]
        # 3) user's done
        drows = c.execute("""
            SELECT a.id AS aid, a.verified_at, cm.body, p.title, p.thumbnail_url, p.source_url
            FROM assignments a
            JOIN comments cm ON cm.id = a.comment_id
            JOIN posts p ON p.id = cm.post_id
            WHERE a.user_id = ? AND a.state = 'done'
            ORDER BY a.verified_at DESC LIMIT 100
        """, (user["id"],)).fetchall()
        done = [dict(r) for r in drows]
        c.close()
        return self._json(200, {"active": active, "queue": queue, "done": done})

    def _get_admin_sid(self):
        for part in self.headers.get("Cookie", "").split(";"):
            if "bumen_admin_sid=" in part:
                return part.split("bumen_admin_sid=")[1].strip()
        return None

    def _handle_admin_get(self, path):
        sid = self._get_admin_sid()
        admin = ADMIN_SESSIONS.get(sid) if sid else None
        if not admin: return self._json(401, {"error": "Admin belum login"})
        c = db()
        if path == "/api/admin/me":
            return self._json(200, {"admin": {"id": admin["id"], "username": admin["username"]}})
        if path == "/api/admin/dashboard":
            summary = {
                "users_total": c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                "reviewers_active_24h": c.execute(
                    "SELECT COUNT(DISTINCT user_id) FROM login_events WHERE at > datetime('now','-1 day')"
                ).fetchone()[0],
                "assignments_total": c.execute("SELECT COUNT(*) FROM assignments").fetchone()[0],
                "assignments_done": c.execute("SELECT COUNT(*) FROM assignments WHERE state='done'").fetchone()[0],
                "assignments_in_progress": c.execute(
                    "SELECT COUNT(*) FROM assignments WHERE state IN ('claimed','copied','reported')"
                ).fetchone()[0],
                "open_reports": c.execute("SELECT COUNT(*) FROM reports WHERE resolved=0").fetchone()[0],
                "posts_total": c.execute("SELECT COUNT(*) FROM posts").fetchone()[0],
                "comments_total": c.execute("SELECT COUNT(*) FROM comments").fetchone()[0],
                "comments_available": c.execute("SELECT COUNT(*) FROM comments WHERE status='available'").fetchone()[0],
            }
            users = [dict(r) for r in c.execute("""
                SELECT u.id, u.handle, u.created_at, u.last_seen_at, u.last_ip,
                       COUNT(a.id) AS assignments,
                       SUM(CASE WHEN a.state='done' THEN 1 ELSE 0 END) AS done
                FROM users u LEFT JOIN assignments a ON a.user_id = u.id
                GROUP BY u.id ORDER BY u.last_seen_at DESC NULLS LAST
            """)]
            posts = [dict(r) for r in c.execute("""
                SELECT p.id, p.title, p.source_url, p.thumbnail_url,
                       COUNT(DISTINCT cm.id) AS comment_count,
                       COUNT(DISTINCT a.id) AS assigned,
                       SUM(CASE WHEN a.state='done' THEN 1 ELSE 0 END) AS done
                FROM posts p
                LEFT JOIN comments cm ON cm.post_id = p.id
                LEFT JOIN assignments a ON a.comment_id = cm.id
                GROUP BY p.id ORDER BY p.id
            """)]
            reports = [dict(r) for r in c.execute("""
                SELECT r.id, r.note, r.attempt_count, r.created_at, r.resolved,
                       u.handle, cm.body, cm.post_id, p.title
                FROM reports r
                JOIN users u ON u.id = r.user_id
                JOIN comments cm ON cm.id = r.comment_id
                JOIN posts p ON p.id = cm.post_id
                ORDER BY r.created_at DESC LIMIT 50
            """)]
            login_events = [dict(r) for r in c.execute("""
                SELECT id, handle, ip, at FROM login_events ORDER BY at DESC LIMIT 30
            """)]
            assignment_events = [dict(r) for r in c.execute("""
                SELECT e.id, e.event, e.detail, e.at, u.handle
                FROM assignment_events e LEFT JOIN users u ON u.id = e.user_id
                ORDER BY e.at DESC LIMIT 50
            """)]
            c.close()
            return self._json(200, {
                "summary": summary, "users": users, "posts": posts,
                "reports": reports, "login_events": login_events,
                "assignment_events": assignment_events,
            })
        c.close()
        return self._json(404, {"error": "Not found"})


        return self._json(404, {"error": "Not found"})

    def _handle_thumbnail(self, path):
        """Serve cached post thumbnail from local disk cache or fetch & cache on demand."""
        try:
            post_id = int(path[len("/api/thumbnail/"):])
        except (ValueError, TypeError):
            return self._json(400, {"error": "Invalid post ID"})

        target_file = get_thumb_path(post_id)
        if target_file.exists() and target_file.stat().st_size > 500:
            return self._file(target_file, "image/jpeg")

        # Not cached: fetch from DB
        c = db()
        row = c.execute("SELECT thumbnail_url, source_url FROM posts WHERE id = ?", (post_id,)).fetchone()
        c.close()

        if not row:
            return self._json(404, {"error": "Post not found"})

        thumb_url = row["thumbnail_url"]
        if thumb_url and thumb_url.startswith("http"):
            if save_thumbnail_cache(post_id, thumb_url):
                if target_file.exists() and target_file.stat().st_size > 500:
                    return self._file(target_file, "image/jpeg")

        # Fallback to logo
        logo_path = ROOT / "bumen-logo.png"
        if logo_path.exists():
            return self._file(logo_path, "image/png")
        return self._json(404, {"error": "Thumbnail not found"})

    def _handle_image_proxy(self, path):
        """Proxy Instagram CDN images to avoid hotlink blocking."""
        # path format: /api/image-proxy/<encoded_url>
        import urllib.parse
        encoded = path[len("/api/image-proxy/"):]
        if not encoded:
            return self._json(400, {"error": "Missing image URL"})
        try:
            url = urllib.parse.unquote(encoded)
            if not url.startswith("http"):
                return self._json(400, {"error": "Invalid URL"})
            # Fetch with Instagram-friendly headers
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
                "Referer": "https://www.instagram.com/",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "image",
                "Sec-Fetch-Mode": "no-cors",
                "Sec-Fetch-Site": "cross-site",
                "Origin": "https://www.instagram.com",
                "Sec-Ch-Ua": "\"Not_A Brand\";v=\"8\", \"Chromium\";v=\"120\", \"Google Chrome\";v=\"120\"",
                "Sec-Ch-Ua-Mobile": "?1",
                "Sec-Ch-Ua-Platform": "\"iOS\""
            })
            # Add cookie handling for Instagram
            import http.cookiejar
            cookie_jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
            # First visit Instagram to set cookies
            try:
                opener.open("https://www.instagram.com/", timeout=5)
            except:
                pass
            with opener.open(req, timeout=10) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "image/jpeg")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
        except Exception as e:
            print(f"Image proxy error: {e}", flush=True)
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    import threading
    init_db()
    # Initialize Bright Data tables
    if BD_AVAILABLE:
        try:
            init_bd_tables()
            print("[DEBUG] Bright Data tables initialized", flush=True)
        except Exception as e:
            print(f"[DEBUG] BD table init error: {e}", flush=True)
    threading.Thread(target=refresh_instagram_thumbnails, daemon=True).start()
    print(f"BUMEN Reviewer —  http://127.0.0.1:{PORT}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    # Warm-up: serve a quick request to ensure server is bound
    def warmup():
        import time, urllib.request
        time.sleep(0.5)
        try: urllib.request.urlopen(f"http://localhost:{PORT}/health", timeout=2)
        except: pass
    threading.Thread(target=warmup, daemon=True).start()
    server.serve_forever()# FORCE BUILD Sun Aug 23 07:10:17 WIB 2026
# FORCE Sun Aug 23 07:38:35 WIB 2026
# force deploy Mon Aug 24 02:18:45 WIB 2026
# FINAL FORCE Mon Aug 24 02:48:34 WIB 2026
