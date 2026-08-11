#!/usr/bin/env python3
"""
BUMEN — Social Media Intelligence Platform
============================================
- BUMEN Fans Club for team-based Instagram comment management
- Live comment scraping + sentiment analysis
- Admin dashboard with real-time intelligence
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib import request as urlreq, error as urlerr
from pathlib import Path
import sqlite3, json, secrets, os, re, hashlib, time, random
from http import cookies
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent
DB   = ROOT / "bumen.db"
XLSX = ROOT / "duniameutya_last_10_posts_comments.xlsx"
PORT = int(os.environ.get("PORT", "8765"))
SESSIONS = {}
ADMIN_SESSIONS = {}
ADMIN_PASSWORD = "@poji#1"

# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, handle TEXT UNIQUE NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS posts(
        id INTEGER PRIMARY KEY, sheet TEXT UNIQUE NOT NULL,
        source_url TEXT, title TEXT, thumbnail_url TEXT DEFAULT '',
        description TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS comments(
        id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id),
        body TEXT NOT NULL, UNIQUE(post_id, body));
    CREATE TABLE IF NOT EXISTS live_comments(
        id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id),
        username TEXT DEFAULT '', body TEXT NOT NULL,
        sentiment_score INTEGER DEFAULT 0, sentiment_label TEXT DEFAULT '',
        scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(post_id, username, body));
    CREATE TABLE IF NOT EXISTS assignments(
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
        comment_id INTEGER NOT NULL REFERENCES comments(id),
        status TEXT DEFAULT 'assigned', assigned_at TEXT DEFAULT CURRENT_TIMESTAMP,
        copied_at TEXT, UNIQUE(user_id, comment_id));
    CREATE INDEX IF NOT EXISTS idx_live_post ON live_comments(post_id);
    CREATE INDEX IF NOT EXISTS idx_live_sent ON live_comments(sentiment_score);
    """)
    c.commit()

    # Seed from XLSX
    if XLSX.exists() and not c.execute("SELECT 1 FROM posts LIMIT 1").fetchone():
        wb = load_workbook(XLSX, read_only=True, data_only=True)
        for ws in wb.worksheets:
            source = ""
            for row in list(ws.values)[:4]:
                if row and len(row) > 7 and row[6] == "Source URL": source = (row[7] or "").strip()
            cur = c.execute("INSERT INTO posts(sheet, source_url, title) VALUES(?,?,?)", (ws.title, source, ws.title.replace("_", " ")))
            pid = cur.lastrowid
            for row in list(ws.values)[1:]:
                if row and isinstance(row[0], int) and row[1]:
                    c.execute("INSERT OR IGNORE INTO comments(post_id, body) VALUES(?,?)", (pid, str(row[1])))
        wb.close()
    c.commit()

    # Import from data.json for thumbnails, descriptions, clean comments
    DATA_JSON = ROOT / "data.json"
    if DATA_JSON.exists():
        _data = json.loads(DATA_JSON.read_text())
        for p in _data:
            pid = p["id"]
            if p.get("thumb"): c.execute("UPDATE posts SET thumbnail_url=? WHERE id=?", (p["thumb"], pid))
            if p.get("description"): c.execute("UPDATE posts SET description=? WHERE id=?", (p["description"], pid))
        c.execute("DELETE FROM assignments")
        c.execute("DELETE FROM comments")
        for p in _data:
            for cmt in p.get("comments", []):
                c.execute("INSERT INTO comments(post_id, body) VALUES(?,?)", (p["id"], cmt))
    c.commit()
    c.close()

# ---------------------------------------------------------------------------
# Sentiment engine
# ---------------------------------------------------------------------------
POS_WORDS = [
    'keren', 'mantap', 'salut', 'bangga', 'terima kasih', 'makasih', 'semangat',
    'lanjutkan', 'maju', 'sukses', 'hebat', 'luar biasa', 'inspiratif', 'totalitas',
    'transparan', 'peduli', 'aman', 'percaya', 'banggakan', 'visioner', 'cerdas',
    'tulus', 'adil', 'baik', 'solusi', 'harapan', 'optimis', 'setuju', 'mantap jiwa',
]
NEG_WORDS_MODERATE = [
    'kecewa', 'buruk', 'parah', 'gagal', 'bohong', 'basi', 'percuma', 'pencitraan',
    'janji doang', 'omong doang', 'gk kayak', 'nggak kayak', 'beda sama',
    'plindung', 'judol', 'mentri apa', 'guna gak', 'tugasnya ap',
    'kocak', 'blokir', 'darurat',
]
NEG_WORDS_SEVERE = [
    'tolol', 'bodoh', 'goblok', 'bangsat', 'anjing', 'bajingan', 'brengsek',
    'sampah', 'busuk',
]
POS_EMOJI = ['🔥', '🙏', '✨', '💪', '👏', '❤️', '💚', '🏆', '🌟', '🙌', '🇮🇩', '👍', '😊', '😍', '🥰', '😭', '🤩', '🥳']
NEG_EMOJI = ['😡', '🤬', '👎', '💩', '😤', '🤮', '😠', '😒']

def score_sentiment(text):
    t = text.lower()
    score = 60  # base neutral

    # Positive keywords: +2 each
    for w in POS_WORDS:
        if w.lower() in t:
            score += 2

    # Negative keywords: moderate -15, severe -25 each
    for w in NEG_WORDS_MODERATE:
        if w.lower() in t:
            score -= 15
    for w in NEG_WORDS_SEVERE:
        if w.lower() in t:
            score -= 25

    # Emoji sentiment (original text, not lowered)
    for e in POS_EMOJI:
        if e in text:
            score += 3
    for e in NEG_EMOJI:
        if e in text:
            score -= 5

    # Comment length bonus
    l = len(text)
    if l > 200:
        score += 3
    elif l > 100:
        score += 2
    elif l > 50:
        score += 1

    # Spam detection: URLs = heavily penalised
    if re.search(r'https?://', t):
        score -= 40

    # Question detection: questions with negative context get extra penalty
    if re.search(r'\?', t):
        has_neg = any(w in t for w in NEG_WORDS_MODERATE + NEG_WORDS_SEVERE)
        if has_neg:
            score -= 5

    # gak/nggak negation patterns
    if re.search(r'\b(gak|nggak|tdk|tidak)\s+(guna|berguna|jalan|bener|benar)', t):
        score -= 15

    # Random variance +/- 5% for organic feel
    score += random.randint(-5, 5)

    return max(10, min(95, score))

def sentiment_label(score):
    if score >= 80: return "Sangat Positif"
    elif score >= 65: return "Positif"
    elif score >= 50: return "Netral"
    elif score >= 30: return "Negatif"
    return "Sangat Negatif"

# ---------------------------------------------------------------------------
# Comment scraper (browser-based)
# ---------------------------------------------------------------------------
def scrape_live_comments(post_url, post_id):
    """Extract visible comments from Instagram page HTML."""
    UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15"
    try:
        req = urlreq.Request(post_url, headers={"User-Agent": UA})
        html = urlreq.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
    except Exception:
        return 0

    comments_found = []
    
    # Method 1: Try __INITIAL_STATE__ JSON
    m = re.search(r'<script[^>]*>\s*window\.__INITIAL_STATE__\s*=\s*({.+?});\s*</script>', html, re.DOTALL)
    if not m:
        m = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.+?});', html, re.DOTALL)
    
    if m:
        try:
            data = json.loads(m.group(1))
            def dig(obj, depth=0):
                if depth > 12: return
                if isinstance(obj, dict):
                    if 'text' in obj and isinstance(obj.get('text'), str) and 3 < len(obj['text']) < 2000:
                        u = ''
                        if 'owner' in obj and isinstance(obj['owner'], dict):
                            u = obj['owner'].get('username', '')
                        comments_found.append({'username': u, 'body': obj['text']})
                    for v in obj.values(): dig(v, depth+1)
                elif isinstance(obj, list):
                    for item in obj[:80]: dig(item, depth+1)
            dig(data)
        except: pass

    # Method 2: If JSON didn't work, try extracting from HTML content using regex
    if not comments_found:
        # Extract comment-like text from the page
        # Instagram comments appear in spans after usernames
        pattern = re.findall(r'"text":"([^"]{10,500})"', html)
        for t in pattern[:50]:
            t = t.replace('\\n', ' ').replace('\\"', '"').replace('\\\\', '\\')
            if len(t) > 10 and not t.startswith('http') and 'Instagram' not in t:
                comments_found.append({'username': '', 'body': t})

    # Save to DB
    if comments_found:
        c = db()
        saved = 0
        for cmt in comments_found[:50]:
            body = cmt['body'].strip()
            if len(body) < 5 or len(body) > 2000: continue
            score = score_sentiment(body)
            label = sentiment_label(score)
            try:
                c.execute(
                    "INSERT OR IGNORE INTO live_comments(post_id, username, body, sentiment_score, sentiment_label) VALUES(?,?,?,?,?)",
                    (post_id, cmt.get('username', ''), body, score, label))
                if c.rowcount > 0: saved += 1
            except: pass
        c.commit()
        c.close()
        return saved
    return 0

# ---------------------------------------------------------------------------
# Fetch thumbnails
# ---------------------------------------------------------------------------
def fetch_thumbnails():
    c = db()
    posts = c.execute("SELECT id, source_url, thumbnail_url FROM posts WHERE thumbnail_url='' OR thumbnail_url IS NULL").fetchall()
    c.close()
    if not posts: return
    UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15"
    fetched = 0
    for p in posts:
        if not p["source_url"]: continue
        try:
            req = urlreq.Request(p["source_url"], headers={"User-Agent": UA})
            html = urlreq.urlopen(req, timeout=12).read().decode("utf-8", errors="ignore")
            m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
            if not m: m = re.search(r'og:image[^>]+content="([^"]+)"', html)
            if m:
                thumb = m.group(1).replace("&amp;", "&")
                c = db(); c.execute("UPDATE posts SET thumbnail_url=? WHERE id=?", (thumb, p["id"])); c.commit(); c.close()
                fetched += 1
        except: pass
        time.sleep(0.6)
    print(f"  Thumbnails fetched: {fetched}/{len(posts)}")

# ---------------------------------------------------------------------------
# Comment generator
# ---------------------------------------------------------------------------
def generate_comments(description, title):
    words = re.findall(r'\b[A-Za-z]{4,}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*', description)
    topics = [w for w in words if len(w) > 5][:5] if words else ["program", "masyarakat", "digital"]
    templates = [
        lambda: f"keren bgt... {random.choice(['akhirnya','seneng liat','bangga'])} {random.choice(topics)} {random.choice(['berjalan','dijalankan','dilaksanakan'])} dgn serius... semoga manfaatnya nyampe ke masyarakat luas ya 🙏",
        lambda: f"nahh gini dong... {random.choice(topics)} yg pro rakyat... lanjutkan terus bu, semangat 🔥",
        lambda: f"setuju bgt... emang harus {random.choice(['konsisten','serius','fokus'])} kalo urusan {random.choice(topics)}... salut sama kerja kerasnya 🙌",
        lambda: f"waduh keren sih... pelaksanaannya mantap... ditunggu inovasi selanjutnya 🙏",
        lambda: f"mantappp... {random.choice(['akhirnya ada','baru kali ini','ini baru namanya'])} {random.choice(topics)} nyata... gaskeun terus bu 🔥🔥",
        lambda: f"buibu pasti setuju... {random.choice(topics)} kaya gini yg dibutuhin keluarga kita... semoga berjalan lancar dan tepat sasaran 🙏",
        lambda: f"seneng bgt liat berita begini... sebagai ibu, pengen anak2 kita tumbuh di Indonesia yg makin maju dan peduli sama rakyatnya 😭❤️",
        lambda: f"jadi inget perjuangan kita sebagai ibu... semoga {random.choice(topics)} ini bener2 bantu meringankan beban keluarga ya bu 🙏",
        lambda: f"Ini langkah yang baik untuk memperkuat {random.choice(topics)}. Semoga konsisten dan memberikan dampak yang berkelanjutan bagi masyarakat.",
        lambda: f"Semoga program ini menjadi fondasi kuat untuk Indonesia yang lebih maju. Kolaborasi dan transparansi jadi kunci keberhasilannya.",
        lambda: f"bagus sih... pelaksanaannya udah keliatan nyata... rakyat nunggu kelanjutannya 🙏",
        lambda: f"semoga terus direalisasikan... programnya udah oke... tinggal eksekusi berkelanjutan aja nih 🔥",
    ]
    comments, used = [], set()
    for _ in range(15):
        for _ in range(5):
            c = random.choice(templates)().strip()
            if c not in used:
                used.add(c); comments.append(c); break
        else:
            comments.append(f"lanjutkan bu... semoga {random.choice(topics)} berjalan lancar dan manfaatnya dirasakan masyarakat... amin 🙏")
    return comments

# ===========================================================================
# HTML TEMPLATES
# ===========================================================================

# Main app HTML (inline SPA)
HTML = r'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><title>BUMEN Fans Club</title>
<style>
:root{--bg:#09090b;--bg2:#121217;--surface:rgba(255,255,255,.04);--surface2:rgba(255,255,255,.07);--ink:#fafafa;--muted:#a1a1aa;--line:rgba(255,255,255,.06);--brand:#818cf8;--brand2:#c084fc;--brand3:#f472b6;--radius:24px;--radius-sm:16px}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:400 15px/1.5 system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased;min-height:100vh;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;z-index:-1;background:radial-gradient(ellipse 80% 50% at 20% 0%,rgba(99,102,241,.12),transparent),radial-gradient(ellipse 60% 40% at 80% 100%,rgba(192,132,252,.08),transparent),radial-gradient(ellipse 50% 30% at 50% 50%,rgba(244,114,182,.05),transparent)}
.auth-pg{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}
.auth-card{background:var(--surface);backdrop-filter:blur(40px);-webkit-backdrop-filter:blur(40px);border:1px solid var(--line);border-radius:var(--radius);padding:48px 32px;max-width:420px;width:100%;text-align:center;animation:fadeUp .6s ease}
@keyframes fadeUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}
.auth-card .icon{font-size:56px;margin-bottom:8px;filter:drop-shadow(0 0 20px rgba(99,102,241,.4))}
.auth-card h1{font-size:28px;margin:0 0 4px;font-weight:800;letter-spacing:-.5px;background:linear-gradient(135deg,var(--brand),var(--brand2),var(--brand3));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.auth-card .tag{color:var(--muted);font-size:14px;margin-bottom:28px}
.auth-card .alert{background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.15);border-radius:var(--radius-sm);padding:14px 18px;color:#a5b4fc;font-size:13px;line-height:1.5;margin-bottom:24px;text-align:left}
.auth-card input{font:inherit;width:100%;padding:15px 18px;background:var(--surface2);border:1px solid var(--line);border-radius:var(--radius-sm);outline:none;margin-bottom:16px;color:var(--ink);font-size:15px;transition:all .2s}
.auth-card input:focus{border-color:var(--brand);box-shadow:0 0 0 4px rgba(99,102,241,.12)}
.btn{font:inherit;font-weight:700;cursor:pointer;transition:all .2s;border:none;outline:none}.btn:active{transform:scale(.97)}
.btn-primary{width:100%;padding:16px;border-radius:var(--radius-sm);font-size:16px;letter-spacing:-.2px;background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;box-shadow:0 4px 24px rgba(99,102,241,.25)}
.btn-primary:hover{box-shadow:0 6px 32px rgba(99,102,241,.35)}
.err-text{color:#f87171;font-size:13px;margin-top:8px}
.app-v{display:none;flex-direction:column;min-height:100vh}.app-v.active{display:flex}
.topbar{display:flex;align-items:center;justify-content:space-between;padding:14px 24px;background:rgba(9,9,11,.85);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:50}
.topbar h1{font-size:17px;margin:0;font-weight:800;letter-spacing:-.2px}
.topbar .hi{font-size:13px;color:var(--muted)}
.btn-ghost{background:0;color:var(--muted);padding:8px 16px;font-size:13px;border-radius:12px}.btn-ghost:hover{color:var(--ink);background:var(--surface2)}
.carousel-section{padding:12px 0 8px;flex:0 0 auto;position:relative}
.carousel-label{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);padding:0 24px 12px;text-align:center}
.carousel-viewport{overflow-x:auto;overflow-y:hidden;scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;padding:0 calc(50vw - 150px);scrollbar-width:none}
.carousel-viewport::-webkit-scrollbar{display:none}
.carousel-track{display:flex;gap:16px;padding:12px 0;align-items:center}
.ccard{flex:0 0 280px;scroll-snap-align:center;border-radius:var(--radius);overflow:hidden;background:var(--surface);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border:1px solid var(--line);cursor:pointer;transition:all .4s cubic-bezier(.4,0,.2,1);position:relative;height:58vh;display:flex;flex-direction:column;transform:scale(.82);opacity:.4;filter:brightness(.6)}
.ccard.active{transform:scale(1);opacity:1;filter:brightness(1);border-color:rgba(129,140,248,.4);box-shadow:0 12px 60px rgba(99,102,241,.2);z-index:2}
.ccard:hover:not(.active){transform:scale(.87);opacity:.6}
.ccard img{width:100%;height:62%;object-fit:cover;display:block;background:var(--surface2)}
.ccard .card-body{padding:14px;flex:1;display:flex;flex-direction:column;justify-content:space-between}
.ccard .card-title{font-size:13px;font-weight:700;line-height:1.3;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.ccard .card-meta{font-size:11px;color:var(--muted);margin-top:4px}
.ccard.active .card-title{font-size:15px}.ccard.active .card-meta{font-size:12px}
.ccard.done{opacity:.35!important;filter:grayscale(.7)!important;transform:scale(.82)!important}
.ccard.done.active{transform:scale(.88)!important;opacity:.5!important}
.ccard.done::after{content:'\2713';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:64px;font-weight:900;color:#22c55e;z-index:5;text-shadow:0 0 40px rgba(34,197,94,.5);animation:popIn .4s cubic-bezier(.34,1.56,.64,1)}
.ccard.done::before{content:'';position:absolute;inset:0;background:rgba(9,9,11,.5);z-index:4}
@keyframes popIn{0%{opacity:0;transform:translate(-50%,-50%) scale(.3)}100%{opacity:1;transform:translate(-50%,-50%) scale(1)}}
.stats-bar{flex:1;display:flex;align-items:center;justify-content:center;gap:12px;padding:0 24px;max-width:500px;margin:0 auto;width:100%}
.stat-item{flex:1;background:rgba(255,255,255,.03);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--line);border-radius:20px;padding:18px 14px;text-align:center;transition:all .3s}
.stat-item:nth-child(1){border-color:rgba(129,140,248,.15)}.stat-item:nth-child(2){border-color:rgba(192,132,252,.15)}.stat-item:nth-child(3){border-color:rgba(244,114,182,.15)}
.stat-item .val{font-size:28px;font-weight:800;letter-spacing:-.5px}
.stat-item:nth-child(1) .val{background:linear-gradient(135deg,#818cf8,#a5b4fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-item:nth-child(2) .val{background:linear-gradient(135deg,#a78bfa,#c4b5fd);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-item:nth-child(3) .val{background:linear-gradient(135deg,#f472b6,#f9a8d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-item .lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-top:4px}
.modal-overlay{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.85);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);display:none;align-items:flex-end;justify-content:center}
.modal-overlay.open{display:flex;animation:fadeIn .3s ease}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.modal-sheet{width:100%;max-width:560px;max-height:94vh;background:var(--bg2);border-radius:var(--radius) var(--radius) 0 0;overflow:hidden;display:flex;flex-direction:column;border:1px solid var(--line);border-bottom:none;animation:slideUp .4s cubic-bezier(.16,1,.3,1)}
@keyframes slideUp{from{transform:translateY(100%)}to{transform:translateY(0)}}
.modal-topbar{display:flex;align-items:center;justify-content:space-between;padding:12px 18px;border-bottom:1px solid var(--line);background:rgba(18,18,23,.95);backdrop-filter:blur(20px);flex-shrink:0}
.modal-topbar .mbtn{background:0;color:var(--muted);border:none;font:inherit;font-size:14px;font-weight:600;cursor:pointer;padding:8px 12px;border-radius:12px;transition:all .2s}
.modal-topbar .mbtn:hover{color:var(--ink);background:var(--surface2)}
.modal-topbar .mtitle{font-size:14px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;text-align:center;padding:0 6px}
.modal-hero{position:relative;overflow:hidden;flex:0 0 44vh;min-height:200px}
.modal-hero img{width:100%;height:100%;object-fit:cover;display:block}
.modal-hero::after{content:'';position:absolute;bottom:0;left:0;right:0;height:40px;background:linear-gradient(transparent,var(--bg2));pointer-events:none}
.modal-body{flex:1;overflow-y:auto;padding:0}
.modal-body .post-desc{padding:16px 20px 8px;font-size:13px;line-height:1.7;color:var(--muted);border-bottom:1px solid var(--line);white-space:pre-line}
.modal-body .info-line{font-size:11px;color:var(--muted);word-break:break-all;padding:8px 20px 0}
.modal-body .cmt-section{padding:16px 20px 20px}
.cmt-section-label{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:12px}
.cmt-card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius-sm);padding:18px;margin-bottom:12px;animation:fadeUp .4s ease}
.cmt-card .cmt-body{font-size:14px;line-height:1.7;margin:0 0 14px;color:var(--ink)}
.cmt-card .cmt-tag{display:inline-block;font-size:11px;font-weight:700;padding:4px 12px;border-radius:99px;margin-bottom:14px}
.cmt-tag.copied{background:rgba(34,197,94,.12);color:#22c55e}.cmt-tag.assigned{background:rgba(99,102,241,.12);color:#818cf8}
.dominant-cta{padding:14px 20px;border-top:1px solid var(--line);background:rgba(18,18,23,.95);backdrop-filter:blur(20px);flex-shrink:0}
.dominant-cta .btn-ambil{width:100%;padding:16px;font:inherit;font-size:16px;font-weight:800;letter-spacing:-.2px;border:none;border-radius:16px;cursor:pointer;transition:all .2s;background:linear-gradient(135deg,var(--brand),var(--brand2),var(--brand3));color:#fff;box-shadow:0 4px 30px rgba(99,102,241,.3)}
.dominant-cta .btn-ambil:hover{box-shadow:0 6px 40px rgba(99,102,241,.45)}
.dominant-cta .btn-ambil:active{transform:scale(.97)}
.dominant-cta .btn-ambil:disabled{opacity:.5;cursor:not-allowed;background:var(--surface2);box-shadow:none;color:var(--muted)}
.dominant-cta .btn-ambil.done{background:linear-gradient(135deg,#22c55e,#16a34a)!important}
.empty-msg{padding:32px;text-align:center;color:var(--muted);font-size:14px}
@media(min-width:600px){.ccard{flex:0 0 300px;height:62vh}.carousel-viewport{padding:0 calc(50vw - 165px)}}
@media(min-width:860px){.ccard{flex:0 0 320px}.carousel-viewport{padding:0 calc(50vw - 175px)}}

.login-page{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px;background:#f8fafc}
.login-card{background:#fff;border-radius:20px;padding:48px 36px 40px;max-width:440px;width:100%;text-align:center;border:1px solid #e2e8f0;box-shadow:0 1px 3px rgba(0,0,0,.04),0 8px 32px rgba(0,0,0,.06)}
.login-card .logo{width:64px;height:auto;margin-bottom:20px}
.login-card h1{font-size:22px;font-weight:800;letter-spacing:-.3px;margin-bottom:4px;color:#0f172a}
.login-card .subtitle{color:#64748b;font-size:14px;margin-bottom:28px}
.login-card .alert{background:#eef2ff;border:1px solid #c7d2fe;border-radius:12px;padding:14px 18px;color:#4338ca;font-size:13px;line-height:1.6;margin-bottom:24px;text-align:left;display:flex;gap:10px;align-items:flex-start}
.login-card .alert-icon{font-size:18px;flex-shrink:0;margin-top:1px}
.login-card .field{margin-bottom:16px;text-align:left}
.login-card .field label{display:block;font-size:13px;font-weight:600;color:#475569;margin-bottom:6px}
.login-card .field input{font:inherit;width:100%%;padding:13px 16px;border:2px solid #e2e8f0;border-radius:12px;outline:none;font-size:15px;color:#0f172a;background:#f8fafc;transition:all .2s}
.login-card .field input:focus{border-color:#818cf8;box-shadow:0 0 0 4px rgba(129,140,248,.1);background:#fff}
.login-card .btn{font:inherit;font-weight:700;cursor:pointer;border:none;transition:all .2s;width:100%%;padding:15px;border-radius:12px;font-size:15px;background:linear-gradient(135deg,#6366f1,#818cf8);color:#fff;box-shadow:0 4px 16px rgba(99,102,241,.25)}
.login-card .btn:active{transform:scale(.97)}
.login-card .btn:hover{box-shadow:0 6px 24px rgba(99,102,241,.35)}
.login-card .error{color:#ef4444;font-size:13px;margin-top:10px;min-height:20px}
.login-card .divider{display:flex;align-items:center;margin:28px 0;color:#cbd5e1;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
.login-card .divider::before,.login-card .divider::after{content:'';flex:1;height:1px;background:#e2e8f0}
.login-card .divider span{padding:0 16px}
.auth-pg{display:none}
</style></head><body>
<div id="auth-v" class="login-page"><div class="login-card"><img class="logo" src="/logo.png" alt="BuMen"><h1>BUMEN Fans Club</h1><p class="subtitle">Community by BUMEN Intelligence</p><div class="alert"><span class="alert-icon">\U0001f4a1</span><span>Komentar hanya untuk direview &amp; dicopy. Tidak ada yang diposting otomatis ke Instagram.</span></div><div class="field"><label for="hinp">Akun Instagram</label><input id="hinp" placeholder="contoh: @senadavina" maxlength="40" autocomplete="off"></div><button class="btn btn-primary" onclick="doLogin()">Gabung Fans Club</button><p id="auth-err" class="error"></p></div></div>
<div id="app-v" class="app-v"><div class="topbar"><h1>\U0001f4ac BUMEN Fans Club</h1><span class="hi" id="hi"></span><button class="btn btn-ghost" onclick="doLogout()">Keluar</button></div>
<section class="carousel-section"><p class="carousel-label">Pilih Postingan</p><div class="carousel-viewport" id="carousel-vp"><div class="carousel-track" id="carousel-track"></div></div></section>
<div class="stats-bar" id="stats-bar"></div>
<div class="modal-overlay" id="modal-overlay"><div class="modal-sheet"><div class="modal-topbar"><button class="mbtn" onclick="closeModal()">\u2190 Kembali</button><span class="mtitle" id="modal-title"></span><button class="mbtn" onclick="closeModal()">\u2715 Tutup</button></div><div class="modal-hero"><img id="modal-img" src="" alt=""></div><div class="modal-body"><p class="info-line" id="modal-url"></p><div class="post-desc" id="modal-desc"></div><div class="cmt-section"><p class="cmt-section-label">\U0001f4ac Komentar</p><div id="modal-cmt"></div></div></div><div class="dominant-cta"><button class="btn-ambil" id="btn-ambil" onclick="doAssign()">\U0001f3b2 Ambil &amp; Copy Komentar</button></div></div></div></div>
<script>
const $=id=>document.getElementById(id);const S={posts:[],post:null,has:false,done:new Set()};
async function api(path,opt={}){const r=await fetch(path,{headers:{"Content-Type":"application/json"},...opt});const d=await r.json();if(!r.ok)throw Error(d.error||"Request failed");return d}
async function boot(){try{const m=await api("/api/me");if(m.user){$("auth-v").style.display="none";$("app-v").classList.add("active");$("hi").textContent="Halo, "+m.user.handle;S.posts=m.posts;for(const p of S.posts){try{const a=await api("/api/assignments?post_id="+p.id);if(a.items.some(x=>x.status==="copied"))S.done.add(p.id)}catch(_){}}renderCarousel();renderStats()}}catch(_){}}
function renderCarousel(){const track=$("carousel-track"),cw=window.innerWidth<600?300:window.innerWidth<860?320:340;track.innerHTML=S.posts.map(p=>{const isDone=S.done.has(p.id);return '<div class="ccard'+(isDone?' done':'')+'" id="cc-'+p.id+'" onclick="openModal('+p.id+')"><img src="'+esc(p.thumbnail)+'" alt="'+esc(p.title)+'" loading="lazy" onerror="this.style.display=\\'none\\';this.insertAdjacentHTML(\\'afterend\\',\\'<div style=height:62%;background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:36px>\ud83d\udcf7</div>\\')"><div class="card-body"><div class="card-title">'+esc(p.title)+'</div><div class="card-meta">'+p.count+' komentar'+(isDone?' \u2022 \u2713':'')+'</div></div></div>'}).join("");const vp=$("carousel-vp");vp.onscroll=()=>updateActive();if(S.posts.length>2){const mid=Math.floor(S.posts.length/2);setTimeout(()=>vp.scrollTo({left:mid*(cw+16),behavior:'smooth'}),400)}setTimeout(updateActive,600)}
function updateActive(){const cards=document.querySelectorAll('.ccard'),vp=$("carousel-vp"),vpr=vp.getBoundingClientRect();let best=null,bestDist=Infinity;cards.forEach(c=>{const cr=c.getBoundingClientRect(),ccx=cr.left+cr.width/2,vcx=vpr.left+vpr.width/2,dist=Math.abs(ccx-vcx);c.classList.remove('active');if(dist<bestDist){bestDist=dist;best=c}});if(best)best.classList.add('active')}
function renderStats(){const t=S.posts.length,d=S.done.size,r=t-d;$("stats-bar").innerHTML='<div class="stat-item"><div class="val">'+t+'</div><div class="lbl">Total Post</div></div><div class="stat-item"><div class="val">'+d+'</div><div class="lbl">Selesai</div></div><div class="stat-item"><div class="val">'+r+'</div><div class="lbl">Tersisa</div></div>'}
async function openModal(id){S.post=S.posts.find(x=>x.id===id);if(!S.post)return;$("modal-title").textContent=S.post.title;$("modal-img").src=S.post.thumbnail||'';$("modal-img").onerror=function(){this.style.display='none'};$("modal-url").textContent=S.post.source_url||'';$("modal-desc").textContent=S.post.description||'';$("modal-overlay").classList.add("open");document.body.style.overflow='hidden';await reloadModalCmt()}
async function reloadModalCmt(){try{const d=await api("/api/assignments?post_id="+S.post.id);S.has=d.items.length>0;const btn=$("btn-ambil"),copied=d.items.length&&d.items[0].status==="copied";if(copied){btn.textContent="\ud83d\udd17 Buka Post di IG";btn.disabled=false;btn.className="btn-ambil done";btn.onclick=()=>window.open(S.post.source_url,"_blank")}else if(S.has){btn.textContent="Tersalin \u2713";btn.disabled=true;btn.className="btn-ambil done"}else{btn.textContent="\ud83c\udfb2 Ambil & Copy Komentar";btn.disabled=false;btn.className="btn-ambil";btn.onclick=doAssign}$("modal-cmt").innerHTML=d.items.length?d.items.map(x=>'<div class="cmt-card"><p class="cmt-body">'+esc(x.body)+'</p><span class="cmt-tag '+(x.status==='copied'?'copied':'assigned')+'">'+(x.status==='copied'?'\u2713 Sudah di-copy':'\ud83d\udccb Baru di-assign')+'</span></div>').join(""):'<div class="empty-msg">Klik tombol di bawah untuk dapat satu komentar acak \u2728</div>'}catch(e){$("modal-cmt").innerHTML='<div class="empty-msg">Gagal memuat.</div>'}}
function closeModal(){$("modal-overlay").classList.remove("open");document.body.style.overflow='';S.done.forEach(pid=>{const c=document.getElementById("cc-"+pid);if(c)c.classList.add("done")});renderStats()}
async function doAssign(){if(S.has||!S.post)return;try{const d=await api("/api/assign",{method:"POST",body:JSON.stringify({post_id:S.post.id})});const a=await api("/api/assignments?post_id="+S.post.id);if(a.items.length){const cmt=a.items[0];await api("/api/copy",{method:"POST",body:JSON.stringify({assignment_id:cmt.id})});try{await navigator.clipboard.writeText(cmt.body)}catch(_){}S.done.add(S.post.id);const card=document.getElementById("cc-"+S.post.id);if(card)card.classList.add("done");renderStats()}await reloadModalCmt()}catch(e){alert(e.message)}}
async function doLogin(){const h=$("hinp").value.trim();if(!h)return $("auth-err").textContent="Isi akun Instagram dulu ya";try{await api("/api/login",{method:"POST",body:JSON.stringify({handle:h})});location.reload()}catch(e){$("auth-err").textContent=e.message}}
async function doLogout(){await api("/api/logout",{method:"POST"});location.reload()}
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
boot();
</script></body></html>'''

# Admin HTML — Enterprise Social Media Intelligence Dashboard
ADMIN_HTML = r'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>BUMEN Intelligence | Enterprise Dashboard</title>
<style>
:root{--bg:#0a0f1e;--surface:#111827;--surface2:#1a2332;--ink:#e2e8f0;--muted:#64748b;--brand:#6366f1;--brand2:#818cf8;--line:#1e293b;--radius:14px;--green:#22c55e;--amber:#f59e0b;--red:#ef4444;--indigo:#4f46e5;--pink:#ec4899;--cyan:#06b6d4}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:400 14px/1.5 system-ui,-apple-system,sans-serif;min-height:100vh}
body::before{content:'';position:fixed;inset:0;z-index:-1;background:radial-gradient(ellipse 60% 50% at 30% 0%,rgba(99,102,241,.08),transparent),radial-gradient(ellipse 50% 40% at 70% 100%,rgba(192,132,252,.05),transparent)}
.login-page{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px;background:#f8fafc}
.login-card{background:#fff;border-radius:20px;padding:48px 36px 40px;max-width:440px;width:100%;text-align:center;border:1px solid #e2e8f0;box-shadow:0 1px 3px rgba(0,0,0,.04),0 8px 32px rgba(0,0,0,.06)}
.login-card .logo{width:120px;height:auto;object-fit:contain;margin-bottom:20px}
.login-card h1{font-size:24px;font-weight:800;margin:0 0 4px;color:#0f172a}
.login-card .subtitle{color:#64748b;font-size:14px;margin-bottom:28px}
.login-card .field{margin-bottom:16px;text-align:left}
.login-card .field label{display:block;font-size:13px;font-weight:600;color:#475569;margin-bottom:6px}
.login-card .field input{font:inherit;width:100%%;padding:13px 16px;border:2px solid #e2e8f0;border-radius:12px;outline:none;font-size:15px;color:#0f172a;background:#f8fafc;transition:all .2s}
.login-card .field input:focus{border-color:var(--brand);box-shadow:0 0 0 4px rgba(129,140,248,.1);background:#fff}
.btn{font:inherit;font-weight:700;cursor:pointer;border:none;transition:all .2s}.btn:active{transform:scale(.97)}
.btn-primary{width:100%%;padding:15px;border-radius:12px;font-size:15px;background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;box-shadow:0 4px 16px rgba(99,102,241,.25)}
.btn-primary:hover{box-shadow:0 6px 24px rgba(99,102,241,.35)}
.btn-ghost{background:0;color:var(--muted);padding:8px 14px;font-size:13px;border-radius:8px}.btn-ghost:hover{color:var(--ink);background:var(--surface2)}
.btn-sm{padding:10px 22px;font-size:13px;border-radius:10px;width:auto}
.btn-xs{padding:6px 14px;font-size:11px;border-radius:8px;width:auto;background:var(--brand);color:#fff;cursor:pointer}
.btn-xs:hover{opacity:.8}
.login-card .error{color:var(--red);font-size:13px;margin-top:10px;min-height:20px}
.login-card .divider{display:flex;align-items:center;margin:28px 0;color:#cbd5e1;font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.5px}
.login-card .divider::before,.login-card .divider::after{content:'';flex:1;height:1px;background:#e2e8f0}
.login-card .divider span{padding:0 16px}
.dash{display:none;padding:20px;max-width:1440px;margin:0 auto}.dash.active{display:block}
.dash-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap;gap:12px}
.dash-header h1{font-size:24px;margin:0;font-weight:800;background:linear-gradient(135deg,var(--cyan),var(--brand),var(--brand2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.dash-header .sub{color:var(--muted);font-size:12px}

/* Crisis banner */
.crisis-banner{display:none;padding:16px 20px;border-radius:var(--radius);margin-bottom:20px;font-size:14px;font-weight:600;align-items:center;gap:12px;animation:slideDown .4s ease}
.crisis-banner.show{display:flex}
.crisis-banner.warning{background:linear-gradient(135deg,rgba(245,158,11,.15),rgba(245,158,11,.05));border:1px solid rgba(245,158,11,.3);color:var(--amber)}
.crisis-banner.critical{background:linear-gradient(135deg,rgba(239,68,68,.15),rgba(239,68,68,.05));border:1px solid rgba(239,68,68,.3);color:var(--red)}
.crisis-banner .crisis-icon{font-size:24px;flex-shrink:0}
.crisis-banner .crisis-close{background:0;border:none;color:inherit;cursor:pointer;margin-left:auto;font-size:18px;opacity:.6}
@keyframes slideDown{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:translateY(0)}}

/* KPI Cards */
.kpi-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:20px}
.kpi-card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:20px 18px;position:relative;overflow:hidden}
.kpi-card::before{content:'';position:absolute;top:0;left:0;width:4px;height:100%;border-radius:4px 0 0 4px}
.kpi-card.posts::before{background:var(--brand)}.kpi-card.live::before{background:var(--cyan)}.kpi-card.sent::before{background:var(--green)}.kpi-card.crisis::before{background:var(--red)}.kpi-card.engage::before{background:var(--amber)}.kpi-card.sov::before{background:var(--pink)}
.kpi-card .kpi-val{font-size:28px;font-weight:800;line-height:1.2}
.kpi-card.posts .kpi-val{color:#a5b4fc}.kpi-card.live .kpi-val{color:#67e8f9}.kpi-card.sent .kpi-val{color:#6ee7b7}.kpi-card.crisis .kpi-val{color:#fca5a5}.kpi-card.engage .kpi-val{color:#fcd34d}.kpi-card.sov .kpi-val{color:#f9a8d4}
.kpi-card .kpi-lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-top:4px}
.kpi-card .kpi-sub{font-size:11px;color:var(--muted);margin-top:2px}

/* Tabs */
.tabs{display:flex;gap:4px;margin-bottom:20px;background:var(--surface);border-radius:12px;padding:4px;border:1px solid var(--line)}
.tab{padding:10px 20px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;border:none;background:0;color:var(--muted);transition:all .2s}
.tab.active{background:var(--brand);color:#fff}
.tab:hover:not(.active){color:var(--ink);background:var(--surface2)}

/* Posts table */
.post-row{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:16px 20px;margin-bottom:10px;transition:all .2s}
.post-row:hover{border-color:rgba(129,140,248,.25)}
.post-main{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.post-title{flex:1;min-width:180px;font-weight:700;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.post-meta{display:flex;align-items:center;gap:24px;flex-wrap:wrap;font-size:12px}
.post-stat{text-align:center}.post-stat .n{font-size:18px;font-weight:800}.post-stat .l{font-size:10px;color:var(--muted);text-transform:uppercase}
.sent-bar-wrap{width:120px}.sent-bar-bg{height:8px;border-radius:4px;background:var(--line);overflow:hidden}.sent-bar-fill{height:100%;border-radius:4px;transition:width .5s}
.sent-pct{font-size:11px;font-weight:700;margin-top:3px}

/* Risk badges */
.risk-badge{display:inline-flex;align-items:center;gap:4px;padding:4px 12px;border-radius:99px;font-size:11px;font-weight:700}
.risk-stable{background:rgba(34,197,94,.12);color:var(--green)}
.risk-warning{background:rgba(245,158,11,.12);color:var(--amber)}
.risk-critical{background:rgba(239,68,68,.12);color:var(--red)}

/* Share of Voice tags */
.sov-tags{display:flex;gap:4px;flex-wrap:wrap;max-width:300px}
.sov-tag{background:var(--surface2);border:1px solid var(--line);padding:2px 8px;border-radius:6px;font-size:10px;color:var(--muted);white-space:nowrap}
.sov-tag span{color:var(--brand);font-weight:600}

/* AI Tip tooltip */
.ai-tip-wrap{position:relative;display:inline-block}
.ai-tip-btn{cursor:pointer;background:var(--surface2);border:1px solid var(--line);border-radius:8px;padding:4px 10px;font-size:11px;color:var(--brand2);transition:all .2s}
.ai-tip-btn:hover{background:rgba(99,102,241,.15);color:#a5b4fc}
.ai-tip-pop{display:none;position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);background:#1e293b;border:1px solid var(--line);border-radius:10px;padding:12px 16px;font-size:12px;color:var(--ink);white-space:normal;width:280px;z-index:10;box-shadow:0 8px 32px rgba(0,0,0,.4);line-height:1.6}
.ai-tip-wrap:hover .ai-tip-pop,.ai-tip-pop.show{display:block}
.ai-tip-pop::after{content:'';position:absolute;top:100%;left:50%;transform:translateX(-50%);border:6px solid transparent;border-top-color:var(--line)}

/* Expandable comments */
.expand-btn{background:0;border:none;color:var(--brand);font-size:12px;font-weight:600;cursor:pointer;padding:4px 0}
.expand-btn:hover{color:var(--brand2)}
.cmt-preview{display:none;margin-top:10px;padding:0;border-top:1px solid var(--line);padding-top:10px;max-height:300px;overflow-y:auto}
.cmt-preview.open{display:block}
.cmt-line{font-size:12px;color:var(--muted);padding:6px 8px;margin-bottom:4px;border-radius:6px;display:flex;gap:8px;align-items:flex-start;line-height:1.5}
.cmt-line:nth-child(odd){background:rgba(255,255,255,.02)}
.cmt-line .cs{font-weight:700;min-width:30px;font-size:11px}
.cmt-line .cb{flex:1}.cmt-line .cu{color:var(--brand);font-size:10px;min-width:70px}
.cmt-line.pos .cs{color:var(--green)}.cmt-line.neg .cs{color:var(--red)}.cmt-line.neu .cs{color:var(--amber)}

/* Comments tab table */
table{width:100%;border-collapse:collapse;background:var(--surface);border-radius:var(--radius);overflow:hidden;border:1px solid var(--line)}
th,td{padding:10px 14px;text-align:left;font-size:12px}
th{background:var(--line);font-weight:700;text-transform:uppercase;font-size:10px;color:var(--muted);letter-spacing:.3px}
td{border-top:1px solid var(--line)}
tr:hover td{background:rgba(129,140,248,.04)}
.loading{padding:40px;text-align:center;color:var(--muted);font-size:14px}
.add-post-box{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:22px;margin-bottom:20px}
.add-post-row{display:flex;gap:10px}.add-post-row input{flex:1;margin-bottom:0;font:inherit;padding:12px 14px;border:1px solid var(--line);border-radius:10px;outline:none;font-size:13px;background:var(--bg);color:var(--ink)}
.add-post-row input:focus{border-color:var(--brand)}
.add-post-row .btn-sm{flex-shrink:0}
.section{margin-bottom:20px}.section h3{font-size:15px;font-weight:700;margin:0 0 12px}
.empty-state{padding:48px 20px;text-align:center;color:var(--muted)}
.empty-state .icon{font-size:40px;margin-bottom:10px}
.sent-label{font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px;display:inline-block}
.sent-pos{background:rgba(34,197,94,.1);color:var(--green)}
.sent-neu{background:rgba(245,158,11,.1);color:var(--amber)}
.sent-neg{background:rgba(239,68,68,.1);color:var(--red)}

/* Alert tab */
.alert-row{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:16px 20px;margin-bottom:10px}
.alert-row.critical{border-color:rgba(239,68,68,.3);background:linear-gradient(135deg,rgba(239,68,68,.06),var(--surface))}
.alert-row .alert-head{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.alert-row .alert-title{font-weight:700;font-size:14px}
.alert-row .alert-detail{font-size:12px;color:var(--muted);line-height:1.6}

@media(min-width:700px){.kpi-grid{grid-template-columns:repeat(3,1fr)}}
@media(min-width:1024px){.kpi-grid{grid-template-columns:repeat(6,1fr)}}
.ov-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px;margin-bottom:20px}
.ov-card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:18px}
.ov-card h4{font-size:13px;font-weight:700;margin:0 0 10px;color:var(--ink)}
.ov-card .ov-stat{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.03);font-size:12px}
.ov-card .ov-stat:last-child{border-bottom:0}
.ov-card .ov-stat .ov-k{color:var(--muted)}.ov-card .ov-stat .ov-v{font-weight:700}
</style></head><body>
<!-- Auth Page -->
<div id="auth-v" class="login-page"><div class="login-card"><img class="logo" src="/logo.png" alt="BuMen"><h1>BUMEN Intelligence</h1><p class="subtitle">Enterprise Social Media Intelligence Platform</p><div class="field"><label for="apwd">Password Admin</label><input id="apwd" type="password" placeholder="Masukkan password" autocomplete="off"></div><button class="btn btn-primary" onclick="doAdminLogin()">Masuk ke Dashboard</button><p id="auth-err" class="error"></p><div class="divider"><span>atau</span></div><p style="font-size:12px;color:#94a3b8;text-align:center">Butuh akses admin? Hubungi <a href="#" style="color:#6366f1;text-decoration:none;font-weight:600">ops@bumen.id</a></p></div></div>

<!-- Dashboard -->
<div id="dash-v" class="dash">
<div class="dash-header"><div><h1>\U0001f4ca BUMEN Intelligence</h1><span class="sub" id="dash-time"></span></div><button class="btn btn-ghost" onclick="doAdminLogout()">\u21a9 Logout</button></div>

<!-- Crisis Banner -->
<div id="crisis-banner" class="crisis-banner"><span class="crisis-icon" id="crisis-icon"></span><span id="crisis-msg"></span><button class="crisis-close" onclick="$('crisis-banner').classList.remove('show')">\u2715</button></div>

<!-- Add Post -->
<div class="add-post-box"><h3>\u2795 Tambah Post + Scrape</h3><div class="add-post-row"><input id="post-url" placeholder="https://www.instagram.com/p/..."><button class="btn btn-primary btn-sm" onclick="addPost()">Tambah &amp; Scrape</button></div><p id="add-msg" style="margin-top:8px;font-size:13px"></p></div>

<!-- KPI Cards -->
<div class="kpi-grid" id="kpi-grid"></div>

<!-- Tabs -->
<div class="tabs"><button class="tab active" onclick="switchTab('overview')">\U0001f4ca Overview</button><button class="tab" onclick="switchTab('posts')">\U0001f4cb Posts</button><button class="tab" onclick="switchTab('comments')">\U0001f4ac Comments</button><button class="tab" onclick="switchTab('alerts')">\U0001f6a8 Alerts</button></div>

<!-- Tab: Overview -->
<div id="tab-overview"><div class="ov-cards" id="overview-cards"></div><div class="section"><h3>\U0001f50d Top Keywords Global</h3><div id="global-kw"></div></div></div>

<!-- Tab: Posts -->
<div id="tab-posts" style="display:none"><div id="posts-container"></div></div>

<!-- Tab: Comments -->
<div id="tab-comments" style="display:none"><div id="ctable"></div></div>

<!-- Tab: Alerts -->
<div id="tab-alerts" style="display:none"><div id="alerts-container"></div></div>
</div>

<script>
const $=id=>document.getElementById(id);let currentTab='overview',allData=null;
const fmtTime=ts=>{if(!ts)return"-";return new Date(ts+"Z").toLocaleString("id-ID",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})};
async function api(p,o={}){const r=await fetch(p,{headers:{"Content-Type":"application/json"},...o});const d=await r.json();if(!r.ok)throw Error(d.error||"Failed");return d}
async function loadDash(){try{allData=await api("/api/admin/dashboard");renderAll()}catch(e){$("dash-v").innerHTML='<div class="loading">Error: '+e.message+'</div>'}}

function renderAll(){const d=allData;renderKPIs(d);renderCrisis(d);renderOverview(d);renderPosts(d.posts);renderComments(d.posts);renderAlerts(d.posts)}

function sentColor(s){return s>=80?'#22c55e':s>=65?'#818cf8':s>=50?'#f59e0b':'#ef4444'}
function sentLabelClass(s){return s>=65?'sent-pos':s>=50?'sent-neu':'sent-neg'}

function renderKPIs(d){
  const s=d.summary||{};
  const crisisPosts=d.posts.filter(p=>p.crisis_alert).length;
  const totalEngage=d.posts.reduce((acc,p)=>acc+(p.engagement_rate||0),0);
  const avgEngage=d.posts.length?(totalEngage/d.posts.length).toFixed(1):'0';
  const topKw=(s.top_keywords_global||[]).slice(0,3).map(k=>k.keyword).join(', ')||'-';
  $("kpi-grid").innerHTML=
    '<div class="kpi-card posts"><div class="kpi-val">'+(s.total_posts||d.posts.length)+'</div><div class="kpi-lbl">Total Posts</div></div>'+
    '<div class="kpi-card live"><div class="kpi-val">'+(s.total_live_comments||0)+'</div><div class="kpi-lbl">Live Comments</div></div>'+
    '<div class="kpi-card sent"><div class="kpi-val">'+(s.avg_sentiment||0)+'%</div><div class="kpi-lbl">Avg Sentiment</div></div>'+
    '<div class="kpi-card crisis"><div class="kpi-val">'+(s.crisis_count||crisisPosts)+'</div><div class="kpi-lbl">Crisis Alerts</div></div>'+
    '<div class="kpi-card engage"><div class="kpi-val">'+avgEngage+'%</div><div class="kpi-lbl">Engagement Rate</div></div>'+
    '<div class="kpi-card sov"><div class="kpi-val" style="font-size:16px">'+topKw+'</div><div class="kpi-lbl">Share of Voice</div></div>'
}

function renderCrisis(d){
  const criticalPosts=d.posts.filter(p=>p.risk_level==='critical');
  const warnPosts=d.posts.filter(p=>p.risk_level==='warning');
  const banner=$("crisis-banner"),icon=$("crisis-icon"),msg=$("crisis-msg");
  if(criticalPosts.length>0){
    banner.className='crisis-banner critical show';
    icon.textContent='\u26a0\ufe0f';
    msg.innerHTML='<b>PR CRISIS ALERT</b> \u2014 '+criticalPosts.length+' post'+(criticalPosts.length>1?'s':'')+' with critical sentiment: '+criticalPosts.map(p=>esc(p.title)).join(', ');
  }else if(warnPosts.length>0){
    banner.className='crisis-banner warning show';
    icon.textContent='\u26a0\ufe0f';
    msg.innerHTML='<b>Warning</b> \u2014 '+warnPosts.length+' post'+(warnPosts.length>1?'s':'')+' with elevated negative sentiment. Monitor closely.';
  }else{
    banner.className='crisis-banner';banner.classList.remove('show');
  }
}

function renderOverview(d){
  let cards='';
  d.posts.forEach(p=>{
    const ls=p.live_sentiment||{},sb=p.sentiment_breakdown||{};
    const risk=p.risk_level||'stable';
    const riskIcon=risk==='critical'?'\ud83d\udd34':risk==='warning'?'\ud83d\udfe1':'\ud83d\udfe2';
    const riskText=risk.charAt(0).toUpperCase()+risk.slice(1);
    const sovTags=(p.share_of_voice||[]).slice(0,4).map(k=>'<span class="sov-tag">'+esc(k.keyword)+' <span>'+k.count+'</span></span>').join('');
    cards+='<div class="ov-card">'+
      '<h4>'+esc(p.title)+'</h4>'+
      '<div class="ov-stat"><span class="ov-k">Live Comments</span><span class="ov-v">'+(p.live_comments||0)+'</span></div>'+
      '<div class="ov-stat"><span class="ov-k">Sentiment</span><span class="ov-v" style="color:'+sentColor(ls.score||0)+'">'+(ls.score||0)+'% '+(ls.label||'')+'</span></div>'+
      '<div class="ov-stat"><span class="ov-k">Breakdown</span><span class="ov-v"><span style="color:#22c55e">'+(sb.positive||0)+'%</span> / <span style="color:#f59e0b">'+(sb.neutral||0)+'%</span> / <span style="color:#ef4444">'+(sb.negative||0)+'%</span></span></div>'+
      '<div class="ov-stat"><span class="ov-k">Risk</span><span class="ov-v">'+riskIcon+' '+riskText+' ('+(p.risk_score||0)+')</span></div>'+
      '<div class="ov-stat"><span class="ov-k">Engagement</span><span class="ov-v">'+(p.engagement_rate||0)+'%</span></div>'+
      (sovTags?'<div class="ov-stat"><span class="ov-k">Keywords</span><span class="ov-v"><div class="sov-tags">'+sovTags+'</div></span></div>':'')+
    '</div>';
  });
  if(!cards)cards='<div class="empty-state"><div class="icon">\ud83d\udcca</div><p>No data yet. Add posts to see analytics.</p></div>';
  $("overview-cards").innerHTML=cards;

  // Global keywords
  const gkw=(d.summary||{}).top_keywords_global||[];
  $("global-kw").innerHTML=gkw.length?
    '<div style="display:flex;gap:6px;flex-wrap:wrap">'+gkw.map(k=>'<span class="sov-tag" style="font-size:12px;padding:4px 12px">'+esc(k.keyword)+' <span>'+k.count+'</span></span>').join('')+'</div>':
    '<div class="empty-state"><p>No keywords extracted yet.</p></div>';
}

function renderPosts(posts){
  if(!posts.length){$("posts-container").innerHTML='<div class="empty-state"><div class="icon">\ud83d\udccb</div><p>No posts with live comments yet.</p></div>';return}
  let html='';
  posts.forEach(p=>{
    const ls=p.live_sentiment||{},sb=p.sentiment_breakdown||{};
    const risk=p.risk_level||'stable';
    const riskBadge=risk==='critical'?'risk-critical':risk==='warning'?'risk-warning':'risk-stable';
    const riskIcon=risk==='critical'?'\ud83d\udd34':risk==='warning'?'\ud83d\udfe1':'\ud83d\udfe2';
    const sovTags=(p.share_of_voice||[]).slice(0,5).map(k=>'<span class="sov-tag">'+esc(k.keyword)+' <span>'+k.count+'</span></span>').join('');
    const comments=(p.live_comment_list||[]).slice(0,10);
    const cmtHtml=comments.length?comments.map(c=>{
      const sc=c.sentiment_score||0,cls=sc>=65?'pos':sc>=50?'neu':'neg';
      return '<div class="cmt-line '+cls+'"><span class="cs">'+sc+'%</span><span class="cu">'+esc(c.username||'anon')+'</span><span class="cb">'+esc(c.body)+'</span></div>'
    }).join(''):'<div style="font-size:11px;color:var(--muted)">No comments</div>';
    html+= '<div class="post-row">'+
      '<div class="post-main">'+
        '<div class="post-title">'+esc(p.title)+'</div>'+
        '<div class="post-stat"><div class="n">'+(p.live_comments||0)+'</div><div class="l">comments</div></div>'+
        '<div class="sent-bar-wrap"><div class="sent-bar-bg"><div class="sent-bar-fill" style="width:'+(ls.score||0)+'%;background:'+sentColor(ls.score||0)+'"></div></div><div class="sent-pct" style="color:'+sentColor(ls.score||0)+'">'+(ls.score||0)+'% '+(ls.label||'')+'</div></div>'+
        '<div><span class="risk-badge '+riskBadge+'">'+riskIcon+' '+risk.charAt(0).toUpperCase()+risk.slice(1)+'</span></div>'+
        '<div class="ai-tip-wrap"><button class="ai-tip-btn">\U0001f9e0 AI Tip</button><div class="ai-tip-pop">'+(p.ai_tip||'')+'</div></div>'+
        '<div><button class="btn btn-xs" onclick="scrapePost('+p.id+',this)">\U0001f504 Scrape</button></div>'+
      '</div>'+
      (sovTags?'<div style="margin-top:10px"><div class="sov-tags">'+sovTags+'</div></div>':'')+
      '<div style="margin-top:8px"><span style="font-size:11px;color:var(--muted)">Breakdown: <span style="color:#22c55e">'+(sb.positive||0)+'% pos</span> / <span style="color:#f59e0b">'+(sb.neutral||0)+'% neutral</span> / <span style="color:#ef4444">'+(sb.negative||0)+'% neg</span> \u2022 Engagement: '+(p.engagement_rate||0)+'%</span></div>'+
      '<div style="margin-top:10px"><button class="expand-btn" onclick="toggleComments(this)">\u25bc Show Comments ('+comments.length+')</button>'+
      '<div class="cmt-preview">'+cmtHtml+'</div></div>'+
    '</div>';
  });
  $("posts-container").innerHTML=html;
}

function toggleComments(btn){
  const preview=btn.nextElementSibling;
  const isOpen=preview.classList.contains('open');
  preview.classList.toggle('open');
  btn.textContent=(isOpen?'\u25bc':'\u25b2')+' '+(isOpen?'Show':'Hide')+' Comments';
}

function renderComments(posts){
  const all=[];
  posts.forEach(p=>{(p.live_comment_list||[]).forEach(c=>all.push({...c,post_title:p.title}))});
  if(!all.length){$("ctable").innerHTML='<div class="empty-state"><div class="icon">\U0001f4ac</div><p>No live comments yet. Click \U0001f504 Scrape on a post to fetch comments.</p></div>';return}
  all.sort((a,b)=>b.sentiment_score-a.sentiment_score);
  $("ctable").innerHTML='<table><tr><th>Post</th><th>User</th><th>Comment</th><th>Score</th></tr>'+all.map(c=>{
    const sc=c.sentiment_score||0;
    return '<tr><td style="font-size:10px;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(c.post_title||'')+'</td><td style="font-size:10px;color:var(--brand)">'+esc(c.username||'anon')+'</td><td>'+esc(c.body)+'</td><td><span class="sent-label '+sentLabelClass(sc)+'">'+sc+'% '+(c.sentiment_label||'')+'</span></td></tr>'
  }).join('')+'</table>'
}

function renderAlerts(posts){
  const alerts=[];
  posts.forEach(p=>{
    if(p.risk_level==='critical'||p.risk_level==='warning'){
      alerts.push({
        title:p.title,
        level:p.risk_level,
        score:p.risk_score||0,
        tip:p.ai_tip||'',
        negPct:(p.sentiment_breakdown||{}).negative||0,
        comments:p.live_comments||0,
        crisis:p.crisis_alert||false
      });
    }
  });
  if(!alerts.length){$("alerts-container").innerHTML='<div class="empty-state"><div class="icon">\u2705</div><p>All clear! No posts with warning or critical risk levels.</p></div>';return}
  $("alerts-container").innerHTML=alerts.map(a=>{
    const isCrit=a.level==='critical';
    return '<div class="alert-row'+(isCrit?' critical':'')+'"><div class="alert-head"><span style="font-size:18px">'+(isCrit?'\ud83d\udd34':'\ud83d\udfe1')+'</span><span class="alert-title">'+esc(a.title)+'</span>'+(a.crisis?'<span style="color:var(--red);font-size:11px;font-weight:700">CRISIS</span>':'')+'</div><div class="alert-detail">Risk Score: <b>'+a.score+'</b> \u2022 Negative Comments: <b>'+a.negPct+'%</b> \u2022 Live Comments: <b>'+a.comments+'</b><br><span style="color:var(--brand2)">\U0001f9e0 AI: '+esc(a.tip)+'</span></div></div>';
  }).join('');
}

function switchTab(t){
  currentTab=t;
  document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.textContent.toLowerCase().includes(t)));
  $("tab-overview").style.display=t==='overview'?'block':'none';
  $("tab-posts").style.display=t==='posts'?'block':'none';
  $("tab-comments").style.display=t==='comments'?'block':'none';
  $("tab-alerts").style.display=t==='alerts'?'block':'none';
}
async function scrapePost(pid,btn){btn.textContent='...';btn.disabled=true;try{const d=await api("/api/admin/scrape",{method:"POST",body:JSON.stringify({post_id:pid})});btn.textContent='\u2705 '+d.count;await loadDash()}catch(e){btn.textContent='\u274c';btn.disabled=false}}
async function addPost(){const u=$("post-url").value.trim(),m=$("add-msg");if(!u)return m.innerHTML='<span style="color:var(--red);font-size:13px">URL required</span>';m.innerHTML='<span style="color:var(--green);font-size:13px">Adding + scraping...</span>';try{const d=await api("/api/admin/add-post",{method:"POST",body:JSON.stringify({url:u})});m.innerHTML='<span style="color:var(--green);font-size:13px">\u2705 '+esc(d.post.title)+' \u2014 '+d.comments_generated+' generated, '+d.live_scraped+' live comments</span>';$("post-url").value='';await loadDash()}catch(e){m.innerHTML='<span style="color:var(--red);font-size:13px">\u274c '+e.message+'</span>'}}
async function doAdminLogin(){const p=$("apwd").value.trim();if(!p)return $("auth-err").textContent="Password required";try{await api("/api/admin/login",{method:"POST",body:JSON.stringify({password:p})});$("auth-v").style.display="none";$("dash-v").classList.add("active");$("dash-time").textContent=new Date().toLocaleString("id-ID");loadDash()}catch(e){$("auth-err").textContent=e.message}}
async function boot(){try{await api("/api/admin/me");$("auth-v").style.display="none";$("dash-v").classList.add("active");$("dash-time").textContent=new Date().toLocaleString("id-ID");loadDash()}catch(_){}}
async function doAdminLogout(){try{await api("/api/admin/logout",{method:"POST"});location.reload()}catch(_){location.reload()}}
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
boot();
</script></body></html>'''

# ===========================================================================
# HTTP Handler
# ===========================================================================
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _admin_user(self):
        jar = cookies.SimpleCookie(); jar.load(self.headers.get("Cookie", ""))
        tok = jar.get("admin_session")
        return tok and tok.value in ADMIN_SESSIONS

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/": return send(self, 200, HTML, "text/html")
        if path == "/health": return send(self, 200, "OK", "text/plain")
        if path == "/admin": return send(self, 200, ADMIN_HTML, "text/html")
        if path == "/logo.png":
            logo = (ROOT / "bumen-logo.png").read_bytes()
            return send(self, 200, logo, "image/png")
        u = current_user(self)
        if path == "/api/admin/me":
            if not self._admin_user(): return send(self, 401, e("Not signed in"))
            return send(self, 200, json.dumps({"admin": True}))
        if path == "/api/admin/dashboard":
            if not self._admin_user(): return send(self, 401, e("Not signed in"))
            c = db()
            users = [dict(r) for r in c.execute("""SELECT u.id, u.handle, u.created_at, COUNT(a.id) AS assigned, COUNT(CASE WHEN a.status='copied' THEN 1 END) AS copied FROM users u LEFT JOIN assignments a ON a.user_id=u.id GROUP BY u.id ORDER BY u.created_at""")]
            posts = [dict(r) for r in c.execute("""SELECT p.id, p.title, COUNT(DISTINCT cm.id) AS comment_count, COUNT(DISTINCT a.user_id) AS users_assigned, COUNT(DISTINCT CASE WHEN a.status='copied' THEN a.user_id END) AS users_copied FROM posts p LEFT JOIN comments cm ON cm.post_id=p.id LEFT JOIN assignments a ON a.comment_id=cm.id GROUP BY p.id ORDER BY p.id""")]
            feed = [dict(r) for r in c.execute("""SELECT a.assigned_at AS ts, u.handle, p.title AS post_title, 'assigned' AS action FROM assignments a JOIN users u ON u.id=a.user_id JOIN comments cm ON cm.id=a.comment_id JOIN posts p ON p.id=cm.post_id UNION ALL SELECT a.copied_at AS ts, u.handle, p.title AS post_title, 'copied' AS action FROM assignments a JOIN users u ON u.id=a.user_id JOIN comments cm ON cm.id=a.comment_id JOIN posts p ON p.id=cm.post_id WHERE a.copied_at IS NOT NULL ORDER BY ts DESC LIMIT 40""")]
            for u in users:
                r = c.execute("SELECT MAX(assigned_at) FROM assignments WHERE user_id=?", (u["id"],)).fetchone()
                u["last_activity"] = r[0] if r else None

            # Stop words for keyword extraction
            STOP_WORDS = {'yang','dan','ini','itu','dengan','untuk','tidak','akan','ada','dari','juga','saya','kami','kita','mereka','bisa','kalau','atau','saja','tapi','karena','sudah','telah','lebih','masih','hanya','lagi','pak','bu','iya','aja','deh','dong','sih','nah','nih','kok','loh','kan','ya','yah','ayo','nih','tuh','mah','dong','gak','nggak','tdk','gg','wkwk','haha','hehe','wow','buset','busyet'}

            all_live_keywords = []
            crisis_count = 0

            for p in posts:
                pid = p["id"]
                cmts = [dict(r) for r in c.execute("SELECT body FROM comments WHERE post_id=?", (pid,))]
                p["sentiment"] = {"score": round(sum(score_sentiment(cm["body"]) for cm in cmts)/len(cmts)) if cmts else 0, "label": sentiment_label(round(sum(score_sentiment(cm["body"]) for cm in cmts)/len(cmts))) if cmts else "N/A"}
                live = [dict(r) for r in c.execute("SELECT id, username, body, sentiment_score, sentiment_label, scraped_at FROM live_comments WHERE post_id=? ORDER BY sentiment_score DESC", (pid,))]
                p["live_comments"] = len(live)

                if live:
                    # Live sentiment average
                    avg_score = round(sum(lc["sentiment_score"] for lc in live)/len(live))
                    p["live_sentiment"] = {"score": avg_score, "label": sentiment_label(avg_score)}

                    # Sentiment breakdown (percentages)
                    pos_count = sum(1 for lc in live if lc["sentiment_score"] >= 65)
                    neg_count = sum(1 for lc in live if lc["sentiment_score"] < 50)
                    neu_count = len(live) - pos_count - neg_count
                    total_live = len(live)
                    p["sentiment_breakdown"] = {
                        "positive": round(pos_count / total_live * 100),
                        "negative": round(neg_count / total_live * 100),
                        "neutral": round(neu_count / total_live * 100)
                    }

                    # Risk assessment based on negative comment percentage
                    neg_pct = neg_count / total_live * 100
                    if neg_pct > 60:
                        p["risk_level"] = "critical"
                        p["risk_score"] = min(100, int(neg_pct + 20))
                    elif neg_pct > 30:
                        p["risk_level"] = "warning"
                        p["risk_score"] = int(neg_pct + 10)
                    else:
                        p["risk_level"] = "stable"
                        p["risk_score"] = max(0, int(neg_pct))

                    # Crisis alert
                    p["crisis_alert"] = (p["risk_level"] == "critical" and len(live) > 20)
                    if p["crisis_alert"]:
                        crisis_count += 1

                    # Share of voice: top keywords from live comments
                    keyword_freq = {}
                    for lc in live:
                        words = re.findall(r'\b[a-z]{4,}\b', lc["body"].lower())
                        for w in words:
                            if w not in STOP_WORDS:
                                keyword_freq[w] = keyword_freq.get(w, 0) + 1
                    sorted_kw = sorted(keyword_freq.items(), key=lambda x: -x[1])[:8]
                    p["share_of_voice"] = [{"keyword": kw, "count": cnt} for kw, cnt in sorted_kw]

                    # Collect for global keywords
                    for kw, cnt in sorted_kw:
                        all_live_keywords.append((kw, cnt))

                    # AI tip
                    if avg_score >= 80:
                        p["ai_tip"] = "Sentimen sangat positif. Pertahankan tone personal dan autentik."
                    elif avg_score >= 65:
                        p["ai_tip"] = "Sentimen positif tinggi. Tingkatkan engagement dengan konten behind-the-scenes."
                    elif avg_score >= 50:
                        p["ai_tip"] = "Sentimen netral. Coba konten yang lebih emosional atau story-driven."
                    elif avg_score >= 30:
                        p["ai_tip"] = "Sentimen mulai negatif. Evaluasi narasi dan respons cepat terhadap kritik."
                    else:
                        p["ai_tip"] = "\u26a0\ufe0f Sentimen sangat negatif. Segera siapkan crisis communication plan."

                    # Engagement rate
                    p["engagement_rate"] = round(len(live) / max(1, p["comment_count"]) * 100, 1)
                else:
                    p["live_sentiment"] = {"score": 0, "label": "No Data"}
                    p["sentiment_breakdown"] = {"positive": 0, "negative": 0, "neutral": 0}
                    p["risk_level"] = "stable"
                    p["risk_score"] = 0
                    p["crisis_alert"] = False
                    p["share_of_voice"] = []
                    p["ai_tip"] = "Belum ada live comment. Lakukan scraping untuk melihat insight."
                    p["engagement_rate"] = 0.0

                p["live_comment_list"] = live

            # Global keyword aggregation
            global_kw_freq = {}
            for kw, cnt in all_live_keywords:
                global_kw_freq[kw] = global_kw_freq.get(kw, 0) + cnt
            top_keywords_global = sorted(global_kw_freq.items(), key=lambda x: -x[1])[:10]
            top_keywords_global = [{"keyword": kw, "count": cnt} for kw, cnt in top_keywords_global]

            # Summary
            total_posts = len(posts)
            total_live = sum(p["live_comments"] for p in posts)
            avg_sentiment = round(sum(p["live_sentiment"]["score"] for p in posts) / max(1, total_posts))
            summary = {
                "total_posts": total_posts,
                "total_live_comments": total_live,
                "avg_sentiment": avg_sentiment,
                "crisis_count": crisis_count,
                "top_keywords_global": top_keywords_global
            }

            c.close()
            return send(self, 200, json.dumps({"users": users, "posts": posts, "feed": feed, "summary": summary}))
        if path == "/api/me":
            if not u: return send(self, 401, e("Not signed in"))
            c = db()
            posts = [dict(r) for r in c.execute("SELECT p.id, p.title, p.source_url, p.thumbnail_url, p.description, COUNT(cm.id) AS count FROM posts p LEFT JOIN comments cm ON cm.post_id=p.id GROUP BY p.id ORDER BY p.id")]
            c.close()
            for p in posts: p["thumbnail"] = p.get("thumbnail_url") or ""
            return send(self, 200, json.dumps({"user": dict(u), "posts": posts}))
        if path == "/api/assignments" and u:
            q = parse_qs(urlparse(self.path).query); pid = int(q.get("post_id", ["0"])[0])
            c = db()
            rows = [dict(r) for r in c.execute("SELECT a.id, a.status, cm.body FROM assignments a JOIN comments cm ON cm.id=a.comment_id WHERE a.user_id=? AND cm.post_id=? ORDER BY a.id DESC", (u["id"], pid))]
            c.close()
            return send(self, 200, json.dumps({"items": rows}))
        return send(self, 404, e("Not found"))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/login":
                d = json_body(self); h = " ".join(str(d.get("handle", "")).strip().split())
                if not h or len(h) > 40: return send(self, 400, e("Masukkan akun Instagram yang valid (maks 40 karakter)"))
                c = db(); n = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
                row = c.execute("SELECT * FROM users WHERE handle=?", (h,)).fetchone()
                if not row:
                    if n >= 10: c.close(); return send(self, 403, e("Workspace penuh (maks 10 orang)"))
                    c.execute("INSERT INTO users(handle) VALUES(?)", (h,)); c.commit()
                    row = c.execute("SELECT * FROM users WHERE handle=?", (h,)).fetchone()
                c.close(); tok = secrets.token_urlsafe(24); SESSIONS[tok] = row["id"]
                return send(self, 200, json.dumps({"user": dict(row)}), extra={"Set-Cookie": f"session={tok}; HttpOnly; SameSite=Lax; Path=/"})
            if path == "/api/logout":
                jar = cookies.SimpleCookie(); jar.load(self.headers.get("Cookie", ""))
                t = jar.get("session")
                if t: SESSIONS.pop(t.value, None)
                return send(self, 200, "{}", extra={"Set-Cookie": "session=; Max-Age=0; Path=/"})
            if path == "/api/admin/login":
                d = json_body(self)
                if d.get("password") != ADMIN_PASSWORD: return send(self, 403, e("Password salah"))
                tok = secrets.token_urlsafe(24); ADMIN_SESSIONS[tok] = True
                return send(self, 200, json.dumps({"admin": True}), extra={"Set-Cookie": f"admin_session={tok}; HttpOnly; SameSite=Lax; Path=/"})
            if path == "/api/admin/logout":
                jar = cookies.SimpleCookie(); jar.load(self.headers.get("Cookie", ""))
                t = jar.get("admin_session")
                if t: ADMIN_SESSIONS.pop(t.value, None)
                return send(self, 200, "{}", extra={"Set-Cookie": "admin_session=; Max-Age=0; Path=/"})
            if path == "/api/admin/add-post":
                if not self._admin_user(): return send(self, 401, e("Not signed in"))
                d = json_body(self); url = (d.get("url") or "").strip()
                if not url or "instagram.com/p/" not in url: return send(self, 400, e("URL Instagram tidak valid"))
                UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15"
                try:
                    req = urlreq.Request(url, headers={"User-Agent": UA})
                    html = urlreq.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
                except Exception as ex: return send(self, 500, e(f"Gagal fetch: {ex}"))
                m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
                if not m: m = re.search(r'og:image[^>]+content="([^"]+)"', html)
                thumb = m.group(1).replace("&amp;", "&") if m else ""
                m = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', html)
                if not m: m = re.search(r'og:description[^>]+content="([^"]+)"', html)
                desc_raw = m.group(1).replace("&amp;", "&") if m else ""
                desc = re.sub(r'^\d[\d,]* [Ll]ikes?.*?on Instagram: "?', '', desc_raw).strip().strip('"')
                m = re.search(r'<title>([^<]+)</title>', html)
                title_raw = m.group(1) if m else "New Post"
                title = re.sub(r' on Instagram:.*$', '', title_raw).strip()[:100]
                comments = generate_comments(desc, title)
                c = db(); sheet = re.sub(r'[^a-zA-Z0-9_]', '_', title)[:50]
                cur = c.execute("INSERT INTO posts(sheet, source_url, title, thumbnail_url, description) VALUES(?,?,?,?,?)", (sheet, url, title, thumb, desc[:2000]))
                pid = cur.lastrowid
                for cmt in comments: c.execute("INSERT OR IGNORE INTO comments(post_id, body) VALUES(?,?)", (pid, cmt))
                c.commit()
                # Also try scraping live comments
                live_count = scrape_live_comments(url, pid)
                row = dict(c.execute("SELECT p.*, COUNT(cm.id) AS count FROM posts p LEFT JOIN comments cm ON cm.post_id=p.id WHERE p.id=? GROUP BY p.id", (pid,)).fetchone())
                c.close()
                return send(self, 200, json.dumps({"post": row, "comments_generated": len(comments), "live_scraped": live_count}))
            if path == "/api/admin/scrape":
                if not self._admin_user(): return send(self, 401, e("Not signed in"))
                d = json_body(self); pid = int(d.get("post_id", 0))
                c = db(); post = c.execute("SELECT source_url FROM posts WHERE id=?", (pid,)).fetchone(); c.close()
                if not post: return send(self, 404, e("Post not found"))
                count = scrape_live_comments(post["source_url"], pid)
                return send(self, 200, json.dumps({"count": count}))
            if path == "/api/admin/live-comments":
                if not self._admin_user(): return send(self, 401, e("Not signed in"))
                d = json_body(self)
                pid = int(d.get("post_id", 0))
                comments = d.get("comments", [])
                c = db(); saved = 0
                for cmt in comments:
                    body = cmt.get("body", "").strip()
                    if len(body) < 3: continue
                    score = score_sentiment(body)
                    label = sentiment_label(score)
                    try:
                        c.execute("INSERT OR IGNORE INTO live_comments(post_id, username, body, sentiment_score, sentiment_label) VALUES(?,?,?,?,?)",
                            (pid, cmt.get("username", ""), body, score, label))
                        if c.rowcount > 0: saved += 1
                    except: pass
                c.commit(); c.close()
                return send(self, 200, json.dumps({"saved": saved}))
            u = current_user(self)
            if not u: return send(self, 401, e("Not signed in"))
            d = json_body(self); c = db()
            if path == "/api/assign":
                pid = int(d["post_id"])
                if c.execute("SELECT 1 FROM assignments a JOIN comments cm ON cm.id=a.comment_id WHERE a.user_id=? AND cm.post_id=? LIMIT 1", (u["id"], pid)).fetchone():
                    return send(self, 409, e("Kamu sudah ambil 1 komentar untuk post ini"))
                rows = c.execute("""SELECT cm.id FROM comments cm WHERE cm.post_id=? AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.comment_id=cm.id AND a.user_id=?) ORDER BY RANDOM() LIMIT 1""", (pid, u["id"])).fetchall()
                for r in rows: c.execute("INSERT OR IGNORE INTO assignments(user_id, comment_id) VALUES(?,?)", (u["id"], r["id"]))
                c.commit(); c.close()
                return send(self, 200, json.dumps({"assigned": len(rows)}))
            if path == "/api/copy":
                aid = int(d["assignment_id"])
                r = c.execute("SELECT a.id, cm.body FROM assignments a JOIN comments cm ON cm.id=a.comment_id WHERE a.id=? AND a.user_id=?", (aid, u["id"])).fetchone()
                if not r: return send(self, 404, e("Assignment not found"))
                c.execute("UPDATE assignments SET status='copied', copied_at=CURRENT_TIMESTAMP WHERE id=?", (aid,))
                c.commit(); c.close()
                return send(self, 200, json.dumps({"body": r["body"]}))
            return send(self, 404, e("Not found"))
        except Exception as ex:
            return send(self, 400, json.dumps({"error": str(ex)}))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def current_user(handler):
    jar = cookies.SimpleCookie(); jar.load(handler.headers.get("Cookie", ""))
    tok = jar.get("session")
    if not tok or tok.value not in SESSIONS: return None
    c = db(); row = c.execute("SELECT * FROM users WHERE id=?", (SESSIONS[tok.value],)).fetchone(); c.close()
    return row

def json_body(handler):
    n = int(handler.headers.get("Content-Length", "0"))
    return json.loads(handler.rfile.read(n) or b"{}")

def send(handler, status, body, content_type="application/json", extra=None):
    data = body if isinstance(body, bytes) else body.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", content_type + "; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    for k, v in (extra or {}).items(): handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(data)

def e(msg):
    return json.dumps({"error": msg})

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    print("Fetching Instagram thumbnails for posts\u2026")
    try: fetch_thumbnails()
    except Exception as ex: print(f"  thumbnail fetch error (non-fatal): {ex}")
    print(f"BUMEN Intelligence \u279c  http://0.0.0.0:{PORT}")
    print(f"  Admin: http://0.0.0.0:{PORT}/admin")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
