#!/usr/bin/env python3
"""
BUMEN — Social Media Intelligence Platform
============================================
- Comment Desk for team-based Instagram comment management
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
    '🔥', '🙏', '✨', '💪', '👏', '❤️', '💚', '🏆', '🌟', '🙌', '🇮🇩', '👍', '😊',
]
NEG_WORDS = [
    'kecewa', 'buruk', 'parah', 'gagal', 'bohong', 'basi', 'percuma', 'pencitraan',
    'janji doang', 'omong doang', 'gk kayak', 'nggak kayak', 'beda sama',
    'plindung', 'judol', 'mentri apa', 'guna gak', 'tugasnya ap',
    'kocak', 'blokir', 'darurat', 'tolol', 'bodoh', 'goblok',
]

def score_sentiment(text):
    score, t = 70, text.lower()
    for w in POS_WORDS:
        if w.lower() in t: score += 2
    for w in NEG_WORDS:
        if w.lower() in t: score -= 20
    if re.search(r'\b(gak|nggak|tdk|tidak)\s+(guna|berguna|jalan|bener|benar)', t): score -= 15
    return max(0, min(100, score))

def sentiment_label(score):
    if score >= 80: return "Sangat Positif"
    elif score >= 65: return "Positif"
    elif score >= 50: return "Netral"
    elif score >= 35: return "Negatif"
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
HTML = r'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"><title>Comment Desk</title>
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
.ccard.done::after{content:'✓';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:64px;font-weight:900;color:#22c55e;z-index:5;text-shadow:0 0 40px rgba(34,197,94,.5);animation:popIn .4s cubic-bezier(.34,1.56,.64,1)}
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
</style></head><body>
<div id="auth-v" class="auth-pg"><div class="auth-card"><div class="icon">💬</div><h1>Comment Desk</h1><p class="tag">by BUMEN Intelligence</p><div class="alert">Komentar cuma buat di-review &amp; di-copy. Nggak ada yang diposting otomatis ke Instagram.</div><input id="hinp" maxlength="40" placeholder="contoh: reviewer-01" autocomplete="off" enterkeyhint="go"><button class="btn btn-primary" onclick="doLogin()">Masuk ke Workspace</button><p id="auth-err" class="err-text"></p></div></div>
<div id="app-v" class="app-v"><div class="topbar"><h1>💬 Comment Desk</h1><span class="hi" id="hi"></span><button class="btn btn-ghost" onclick="doLogout()">Keluar</button></div>
<section class="carousel-section"><p class="carousel-label">Pilih Postingan</p><div class="carousel-viewport" id="carousel-vp"><div class="carousel-track" id="carousel-track"></div></div></section>
<div class="stats-bar" id="stats-bar"></div>
<div class="modal-overlay" id="modal-overlay"><div class="modal-sheet"><div class="modal-topbar"><button class="mbtn" onclick="closeModal()">← Kembali</button><span class="mtitle" id="modal-title"></span><button class="mbtn" onclick="closeModal()">✕ Tutup</button></div><div class="modal-hero"><img id="modal-img" src="" alt=""></div><div class="modal-body"><p class="info-line" id="modal-url"></p><div class="post-desc" id="modal-desc"></div><div class="cmt-section"><p class="cmt-section-label">💬 Komentar</p><div id="modal-cmt"></div></div></div><div class="dominant-cta"><button class="btn-ambil" id="btn-ambil" onclick="doAssign()">🎲 Ambil &amp; Copy Komentar</button></div></div></div></div>
<script>
const $=id=>document.getElementById(id);const S={posts:[],post:null,has:false,done:new Set()};
async function api(path,opt={}){const r=await fetch(path,{headers:{"Content-Type":"application/json"},...opt});const d=await r.json();if(!r.ok)throw Error(d.error||"Request failed");return d}
async function boot(){try{const m=await api("/api/me");if(m.user){$("auth-v").style.display="none";$("app-v").classList.add("active");$("hi").textContent="Halo, "+m.user.handle;S.posts=m.posts;for(const p of S.posts){try{const a=await api("/api/assignments?post_id="+p.id);if(a.items.some(x=>x.status==="copied"))S.done.add(p.id)}catch(_){}}renderCarousel();renderStats()}}catch(_){}}
function renderCarousel(){const track=$("carousel-track"),cw=window.innerWidth<600?300:window.innerWidth<860?320:340;track.innerHTML=S.posts.map(p=>{const isDone=S.done.has(p.id);return '<div class="ccard'+(isDone?' done':'')+'" id="cc-'+p.id+'" onclick="openModal('+p.id+')"><img src="'+esc(p.thumbnail)+'" alt="'+esc(p.title)+'" loading="lazy" onerror="this.style.display=\\'none\\';this.insertAdjacentHTML(\\'afterend\\',\\'<div style=height:62%;background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:36px>📷</div>\\')"><div class="card-body"><div class="card-title">'+esc(p.title)+'</div><div class="card-meta">'+p.count+' komentar'+(isDone?' • ✓':'')+'</div></div></div>'}).join("");const vp=$("carousel-vp");vp.onscroll=()=>updateActive();if(S.posts.length>2){const mid=Math.floor(S.posts.length/2);setTimeout(()=>vp.scrollTo({left:mid*(cw+16),behavior:'smooth'}),400)}setTimeout(updateActive,600)}
function updateActive(){const cards=document.querySelectorAll('.ccard'),vp=$("carousel-vp"),vpr=vp.getBoundingClientRect();let best=null,bestDist=Infinity;cards.forEach(c=>{const cr=c.getBoundingClientRect(),ccx=cr.left+cr.width/2,vcx=vpr.left+vpr.width/2,dist=Math.abs(ccx-vcx);c.classList.remove('active');if(dist<bestDist){bestDist=dist;best=c}});if(best)best.classList.add('active')}
function renderStats(){const t=S.posts.length,d=S.done.size,r=t-d;$("stats-bar").innerHTML='<div class="stat-item"><div class="val">'+t+'</div><div class="lbl">Total Post</div></div><div class="stat-item"><div class="val">'+d+'</div><div class="lbl">Selesai</div></div><div class="stat-item"><div class="val">'+r+'</div><div class="lbl">Tersisa</div></div>'}
async function openModal(id){S.post=S.posts.find(x=>x.id===id);if(!S.post)return;$("modal-title").textContent=S.post.title;$("modal-img").src=S.post.thumbnail||'';$("modal-img").onerror=function(){this.style.display='none'};$("modal-url").textContent=S.post.source_url||'';$("modal-desc").textContent=S.post.description||'';$("modal-overlay").classList.add("open");document.body.style.overflow='hidden';await reloadModalCmt()}
async function reloadModalCmt(){try{const d=await api("/api/assignments?post_id="+S.post.id);S.has=d.items.length>0;const btn=$("btn-ambil"),copied=d.items.length&&d.items[0].status==="copied";if(copied){btn.textContent="🔗 Buka Post di IG";btn.disabled=false;btn.className="btn-ambil done";btn.onclick=()=>window.open(S.post.source_url,"_blank")}else if(S.has){btn.textContent="Tersalin ✓";btn.disabled=true;btn.className="btn-ambil done"}else{btn.textContent="🎲 Ambil & Copy Komentar";btn.disabled=false;btn.className="btn-ambil";btn.onclick=doAssign}$("modal-cmt").innerHTML=d.items.length?d.items.map(x=>'<div class="cmt-card"><p class="cmt-body">'+esc(x.body)+'</p><span class="cmt-tag '+(x.status==='copied'?'copied':'assigned')+'">'+(x.status==='copied'?'✓ Sudah di-copy':'📋 Baru di-assign')+'</span></div>').join(""):'<div class="empty-msg">Klik tombol di bawah untuk dapat satu komentar acak ✨</div>'}catch(e){$("modal-cmt").innerHTML='<div class="empty-msg">Gagal memuat.</div>'}}
function closeModal(){$("modal-overlay").classList.remove("open");document.body.style.overflow='';S.done.forEach(pid=>{const c=document.getElementById("cc-"+pid);if(c)c.classList.add("done")});renderStats()}
async function doAssign(){if(S.has||!S.post)return;try{const d=await api("/api/assign",{method:"POST",body:JSON.stringify({post_id:S.post.id})});const a=await api("/api/assignments?post_id="+S.post.id);if(a.items.length){const cmt=a.items[0];await api("/api/copy",{method:"POST",body:JSON.stringify({assignment_id:cmt.id})});try{await navigator.clipboard.writeText(cmt.body)}catch(_){}S.done.add(S.post.id);const card=document.getElementById("cc-"+S.post.id);if(card)card.classList.add("done");renderStats()}await reloadModalCmt()}catch(e){alert(e.message)}}
async function doLogin(){const h=$("hinp").value.trim();if(!h)return $("auth-err").textContent="Isi handle dulu ya";try{await api("/api/login",{method:"POST",body:JSON.stringify({handle:h})});location.reload()}catch(e){$("auth-err").textContent=e.message}}
async function doLogout(){await api("/api/logout",{method:"POST"});location.reload()}
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
boot();
</script></body></html>'''

# Admin HTML (intelligence dashboard)
ADMIN_HTML = r'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>BUMEN Intelligence</title>
<style>
:root{--bg:#0f172a;--surface:#1e293b;--ink:#f1f5f9;--muted:#94a3b8;--brand:#818cf8;--brand2:#c084fc;--line:#334155;--radius:16px;--green:#34d399;--amber:#fbbf24;--red:#f87171}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:400 14px/1.5 system-ui,-apple-system,sans-serif;min-height:100vh}
.auth-pg{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px;background:#ffffff}
.auth-box{background:#ffffff;border-radius:var(--radius);padding:40px 32px;max-width:420px;width:100%;text-align:center;border:1px solid #e5e7eb;box-shadow:0 4px 24px rgba(0,0,0,.06)}
.auth-box .icon{font-size:48px;margin-bottom:8px}
.auth-box .logo{width:120px;height:auto;object-fit:contain;margin-bottom:20px}
.auth-box h1{font-size:24px;margin:0;font-weight:800;color:#0f172a}
.auth-box .tag{color:#64748b;font-size:13px;margin-bottom:24px}
input,textarea{font:inherit;width:100%;padding:13px 16px;border:2px solid #e5e7eb;border-radius:12px;outline:none;margin-bottom:14px;font-size:14px;background:#f8fafc;color:#0f172a}
input:focus{border-color:var(--brand);box-shadow:0 0 0 4px rgba(129,140,248,.1)}
.btn{font:inherit;font-weight:700;cursor:pointer;border:none}
.btn-primary{width:100%;padding:14px;background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;border-radius:12px;font-size:15px}
.btn-primary:hover{opacity:.9}
.btn-ghost{background:0;color:var(--muted);padding:8px 14px;font-size:13px;border-radius:8px}
.btn-sm{padding:10px 22px;font-size:13px;border-radius:10px;width:auto}
.btn-xs{padding:6px 14px;font-size:11px;border-radius:8px;width:auto;background:var(--brand);color:#fff}
.btn-xs:hover{opacity:.8}
.err{color:var(--red);font-size:13px;margin-top:8px}.ok{color:var(--green);font-size:13px}
.dash{display:none;padding:20px;max-width:1400px;margin:0 auto}.dash.active{display:block}
.dash-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;flex-wrap:wrap;gap:12px}
.dash-header h1{font-size:22px;margin:0;font-weight:800;background:linear-gradient(135deg,var(--green),var(--brand));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.dash-header .sub{color:var(--muted);font-size:13px}
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:24px}
.stat-card{background:var(--surface);border-radius:var(--radius);padding:20px 18px;border:1px solid var(--line)}
.stat-card .val{font-size:28px;font-weight:800}.stat-card .lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.stat-card .sub{font-size:12px;color:var(--muted);margin-top:4px}
.section{margin-bottom:24px}.section h3{font-size:15px;font-weight:700;margin:0 0 14px;display:flex;align-items:center;gap:8px}
table{width:100%;border-collapse:collapse;background:var(--surface);border-radius:var(--radius);overflow:hidden;border:1px solid var(--line)}
th,td{padding:10px 14px;text-align:left;font-size:12px}
th{background:var(--line);font-weight:700;text-transform:uppercase;font-size:10px;color:var(--muted)}
td{border-top:1px solid var(--line)}tr:hover td{background:rgba(129,140,248,.05)}
.badge{display:inline-block;padding:3px 10px;border-radius:99px;font-size:11px;font-weight:700}
.badge-ok{background:#065f4620;color:var(--green)}.badge-new{background:#4338ca20;color:var(--brand)}.badge-warn{background:#92400e20;color:var(--amber)}.badge-neg{background:#7f1d1d20;color:var(--red)}
.sent-meter{display:flex;align-items:center;gap:8px;min-width:160px}
.sent-bar{flex:1;height:10px;border-radius:5px;background:var(--line);overflow:hidden}
.sent-fill{height:100%;border-radius:5px;transition:width .4s}
.sent-label{font-size:11px;font-weight:700;min-width:95px}
.prog-bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden;min-width:60px}
.prog-fill{height:100%;background:var(--brand);border-radius:3px}
.loading{padding:40px;text-align:center;color:var(--muted)}
.expand-btn{cursor:pointer;color:var(--brand);font-size:12px;font-weight:700;background:0;border:none}
.cmt-preview{display:none;padding:8px 14px;background:rgba(0,0,0,.3);border-radius:8px;margin-top:4px;max-height:250px;overflow-y:auto}
.cmt-preview.open{display:block}
.cmt-line{font-size:11px;color:var(--muted);padding:5px 0;border-bottom:1px solid rgba(255,255,255,.04);line-height:1.5;display:flex;gap:6px}
.cmt-line .s{font-weight:700;min-width:18px}.cmt-line .u{color:var(--brand);min-width:80px;font-size:10px}
.cmt-line.pos .s{color:var(--green)}.cmt-line.neg .s{color:var(--red)}.cmt-line.neu .s{color:var(--amber)}
.tabs{display:flex;gap:4px;margin-bottom:20px;background:var(--surface);border-radius:12px;padding:4px;border:1px solid var(--line)}
.tab{padding:10px 20px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;border:none;background:0;color:var(--muted);transition:all .2s}
.tab.active{background:var(--brand);color:#fff}
.add-post-box{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:22px;margin-bottom:24px}
.add-post-row{display:flex;gap:10px}.add-post-row input{flex:1;margin-bottom:0}
@media(min-width:700px){.stats{grid-template-columns:repeat(6,1fr)}}
</style></head><body>
<div id="auth-v" class="auth-pg"><div class="auth-box"><img src="data:image/png;base64,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" alt="BuMen" class="logo"><h1>BUMEN Intelligence</h1><p class="tag">Social Media Intelligence Platform</p><input id="apwd" type="password" placeholder="Password admin" autocomplete="off"><button class="btn btn-primary" onclick="doAdminLogin()">Masuk</button><p id="auth-err" class="err"></p></div></div>
<div id="dash-v" class="dash">
<div class="dash-header"><div><h1>📊 BUMEN Intelligence</h1><span class="sub" id="dash-time"></span></div><button class="btn btn-ghost" onclick="doAdminLogout()">↩ Logout</button></div>
<div class="add-post-box"><h3>➕ Tambah Post + Scrape</h3><div class="add-post-row"><input id="post-url" placeholder="https://www.instagram.com/p/..."><button class="btn btn-primary btn-sm" onclick="addPost()">Tambah & Scrape</button></div><p id="add-msg" style="margin-top:8px;font-size:13px"></p></div>
<div class="stats" id="stats"></div>
<div class="tabs"><button class="tab active" onclick="switchTab('posts')">📋 Posts + Sentiment</button><button class="tab" onclick="switchTab('comments')">💬 Live Comments</button><button class="tab" onclick="switchTab('reviewers')">👥 Reviewers</button></div>
<div id="tab-posts"><div class="section"><div id="ptable"></div></div></div>
<div id="tab-comments" style="display:none"><div class="section"><div id="ctable"></div></div></div>
<div id="tab-reviewers" style="display:none"><div class="section"><div id="utable"></div><div id="feed-section" class="section" style="margin-top:24px"><h3>🕐 Aktivitas Terbaru</h3><div id="feed"></div></div></div></div>
</div>
<script>
const $=id=>document.getElementById(id);let currentTab='posts',allData=null;
const fmtTime=ts=>{if(!ts)return"-";return new Date(ts+"Z").toLocaleString("id-ID",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})};
async function api(p,o={}){const r=await fetch(p,{headers:{"Content-Type":"application/json"},...o});const d=await r.json();if(!r.ok)throw Error(d.error||"Failed");return d}
async function loadDash(){try{allData=await api("/api/admin/dashboard");renderAll()}catch(e){$("dash-v").innerHTML='<div class="loading">Error: '+e.message+'</div>'}}
function renderAll(){const d=allData;renderStats(d);renderPosts(d.posts);renderComments(d.posts);renderReviewers(d.users,d.feed)}
function renderStats(d){const t=d.users.length,a=d.users.reduce((s,u)=>s+u.assigned,0),c=d.users.reduce((s,u)=>s+u.copied,0),p=d.posts.filter(p=>p.users_assigned>=t).length,avgSent=d.posts.length?Math.round(d.posts.reduce((s,p)=>s+(p.sentiment?p.sentiment.score:0),0)/d.posts.length):0,totalLive=d.posts.reduce((s,p)=>s+(p.live_comments||0),0),avgLiveSent=d.posts.reduce((s,p)=>s+(p.live_sentiment?p.live_sentiment.score:0),0);$("stats").innerHTML='<div class="stat-card"><div class="val">'+t+'</div><div class="lbl">Reviewer</div></div><div class="stat-card"><div class="val">'+a+'</div><div class="lbl">Assigned</div><div class="sub">'+c+' copied</div></div><div class="stat-card"><div class="val">'+p+'/'+d.posts.length+'</div><div class="lbl">Tuntas</div></div><div class="stat-card"><div class="val">'+avgSent+'%</div><div class="lbl">Gen Comment Sentiment</div></div><div class="stat-card"><div class="val">'+totalLive+'</div><div class="lbl">Live Comments</div></div><div class="stat-card"><div class="val">'+(totalLive?Math.round(avgLiveSent/totalLive):0)+'%</div><div class="lbl">Live Sentiment</div></div>'}
function sentColor(s){return s>=80?'var(--green)':s>=65?'var(--brand)':s>=50?'var(--amber)':'var(--red)'}
function renderPosts(p){if(!p.length){$("ptable").innerHTML='<div class="loading">-</div>';return}$("ptable").innerHTML='<table><tr><th>Post</th><th>Gen Komen</th><th>Live Komen</th><th>Live Sentiment</th><th>Gen Sentiment</th><th></th></tr>'+p.map(p=>{const ls=p.live_sentiment||{},lsc=ls.score||0,gs=p.sentiment||{},gsc=gs.score||0;return'<tr><td><b>'+esc(p.title)+'</b></td><td>'+p.comment_count+'</td><td>'+(p.live_comments||0)+'</td><td><div class="sent-meter"><div class="sent-bar"><div class="sent-fill" style="width:'+lsc+'%;background:'+sentColor(lsc)+'"></div></div><span class="sent-label" style="color:'+sentColor(lsc)+'">'+lsc+'% '+(ls.label||'')+'</span></div></td><td><div class="sent-meter"><div class="sent-bar"><div class="sent-fill" style="width:'+gsc+'%;background:'+sentColor(gsc)+'"></div></div><span class="sent-label" style="color:'+sentColor(gsc)+'">'+gsc+'% '+(gs.label||'')+'</span></div></td><td><button class="btn btn-xs" onclick="scrapePost('+p.id+',this)">🔄 Scrape</button></td></tr>'}).join("")+'</table>'}
function renderComments(p){const all=[];p.forEach(p=>{(p.live_comment_list||[]).forEach(c=>all.push({...c,post_title:p.title}))});if(!all.length){$("ctable").innerHTML='<div class="loading">No live comments yet. Click 🔄 Scrape on a post.</div>';return}all.sort((a,b)=>b.sentiment_score-a.sentiment_score);$("ctable").innerHTML='<table><tr><th>Post</th><th>User</th><th>Comment</th><th>Sentiment</th></tr>'+all.map(c=>{const sc=c.sentiment_score||0,cls=sc>=65?'pos':sc>=50?'neu':'neg';return'<tr><td style="font-size:10px">'+esc(c.post_title||'')+'</td><td style="font-size:10px;color:var(--brand)">'+esc(c.username||'anon')+'</td><td>'+esc(c.body)+'</td><td><div class="sent-meter"><div class="sent-bar"><div class="sent-fill" style="width:'+sc+'%;background:'+sentColor(sc)+'"></div></div><span class="sent-label" style="color:'+sentColor(sc)+'">'+sc+'% '+(c.sentiment_label||'')+'</span></div></td></tr>'}).join("")+'</table>'}
function renderReviewers(u,feed){if(!u.length){$("utable").innerHTML='<div class="loading">-</div>'}else{$("utable").innerHTML='<table><tr><th>Handle</th><th>Join</th><th>Assigned</th><th>Copied</th><th>%</th><th>Last Active</th></tr>'+u.map(u=>'<tr><td><b>'+esc(u.handle)+'</b></td><td>'+fmtTime(u.created_at)+'</td><td>'+u.assigned+'</td><td>'+u.copied+'</td><td><div class="prog-bar"><div class="prog-fill" style="width:'+(u.assigned?(u.copied/u.assigned*100):0)+'%"></div></div></td><td>'+(u.last_activity?fmtTime(u.last_activity):'-')+'</td></tr>').join("")+'</table>'}if(!feed||!feed.length){$("feed").innerHTML='<div class="loading">-</div>';return}$("feed").innerHTML='<table><tr><th>Waktu</th><th>User</th><th>Post</th><th>Aksi</th></tr>'+feed.map(f=>'<tr><td>'+fmtTime(f.ts)+'</td><td>'+esc(f.handle)+'</td><td>'+esc(f.post_title)+'</td><td><span class="badge badge-'+(f.action==='copied'?'ok':'new')+'">'+(f.action==='copied'?'✓ Copy':'📋 Assign')+'</span></td></tr>').join("")+'</table>'}
function switchTab(t){currentTab=t;document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.textContent.includes(t==='posts'?'Posts':t==='comments'?'Comments':'Reviewers')));$("tab-posts").style.display=t==='posts'?'block':'none';$("tab-comments").style.display=t==='comments'?'block':'none';$("tab-reviewers").style.display=t==='reviewers'?'block':'none'}
async function scrapePost(pid,btn){btn.textContent='...';btn.disabled=true;try{const d=await api("/api/admin/scrape",{method:"POST",body:JSON.stringify({post_id:pid})});btn.textContent='✅ '+d.count;await loadDash()}catch(e){btn.textContent='❌';btn.disabled=false}}
async function addPost(){const u=$("post-url").value.trim(),m=$("add-msg");if(!u)return m.innerHTML='<span class="err">URL required</span>';m.innerHTML='<span class="ok">Adding + scraping...</span>';try{const d=await api("/api/admin/add-post",{method:"POST",body:JSON.stringify({url:u})});m.innerHTML='<span class="ok">✅ '+esc(d.post.title)+' — '+d.comments_generated+' generated, '+d.live_scraped+' live comments</span>';$("post-url").value='';await loadDash()}catch(e){m.innerHTML='<span class="err">❌ '+e.message+'</span>'}}
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
        if path == "/admin": return send(self, 200, ADMIN_HTML, "text/html")
        if path == "/logo-transparent.png":
            logo = (ROOT / "logo-transparent.png").read_bytes()
            return send(self, 200, logo, "image/png")
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
            for p in posts:
                pid = p["id"]
                cmts = [dict(r) for r in c.execute("SELECT body FROM comments WHERE post_id=?", (pid,))]
                p["sentiment"] = {"score": round(sum(score_sentiment(cm["body"]) for cm in cmts)/len(cmts)) if cmts else 0, "label": sentiment_label(round(sum(score_sentiment(cm["body"]) for cm in cmts)/len(cmts))) if cmts else "N/A"}
                live = [dict(r) for r in c.execute("SELECT id, username, body, sentiment_score, sentiment_label, scraped_at FROM live_comments WHERE post_id=? ORDER BY sentiment_score DESC", (pid,))]
                p["live_comments"] = len(live)
                if live:
                    p["live_sentiment"] = {"score": round(sum(lc["sentiment_score"] for lc in live)/len(live)), "label": "Mixed"}
                    if p["live_sentiment"]["score"] >= 65: p["live_sentiment"]["label"] = "Positif"
                    elif p["live_sentiment"]["score"] >= 50: p["live_sentiment"]["label"] = "Netral"
                    else: p["live_sentiment"]["label"] = "Negatif"
                else:
                    p["live_sentiment"] = {"score": 0, "label": "No Data"}
                p["live_comment_list"] = live
            c.close()
            return send(self, 200, json.dumps({"users": users, "posts": posts, "feed": feed}))
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
                if not h or len(h) > 40: return send(self, 400, e("Masukkan handle yang valid (maks 40 karakter)"))
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
    print("Fetching Instagram thumbnails for posts…")
    try: fetch_thumbnails()
    except Exception as ex: print(f"  thumbnail fetch error (non-fatal): {ex}")
    print(f"BUMEN Intelligence ➜  http://0.0.0.0:{PORT}")
    print(f"  Admin: http://0.0.0.0:{PORT}/admin")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
