#!/usr/bin/env python3
"""Comment Desk – internal tool, copy-only. Posts loaded from XLSX."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib import request as urlreq, error as urlerr
from pathlib import Path
import sqlite3, json, secrets, os, re, hashlib, time
from http import cookies
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent
DB   = ROOT / "app.db"
XLSX = ROOT / "duniameutya_last_10_posts_comments.xlsx"
PORT = int(os.environ.get("PORT", "8765"))
SESSIONS = {}
ADMIN_SESSIONS = {}
ADMIN_PASSWORD = "@poji#1"
THUMB_CACHE = ROOT / "thumbs"
THUMB_CACHE.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# database helpers
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
        id         INTEGER PRIMARY KEY,
        handle     TEXT UNIQUE NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS posts(
        id            INTEGER PRIMARY KEY,
        sheet         TEXT UNIQUE NOT NULL,
        source_url    TEXT,
        title         TEXT,
        thumbnail_url TEXT DEFAULT '',
        description   TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS comments(
        id      INTEGER PRIMARY KEY,
        post_id INTEGER NOT NULL REFERENCES posts(id),
        body    TEXT    NOT NULL,
        UNIQUE(post_id, body)
    );
    CREATE TABLE IF NOT EXISTS assignments(
        id         INTEGER PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        comment_id INTEGER NOT NULL REFERENCES comments(id),
        status     TEXT    DEFAULT 'assigned',
        assigned_at TEXT   DEFAULT CURRENT_TIMESTAMP,
        copied_at  TEXT,
        UNIQUE(user_id, comment_id)
    );
    CREATE INDEX IF NOT EXISTS idx_assign_user  ON assignments(user_id);
    CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
    """)
    c.commit()

    # Import XLSX if DB is empty
    if XLSX.exists() and not c.execute("SELECT 1 FROM posts LIMIT 1").fetchone():
        wb = load_workbook(XLSX, read_only=True, data_only=True)
        for ws in wb.worksheets:
            source = ""
            for row in list(ws.values)[:4]:
                if row and len(row) > 7 and row[6] == "Source URL":
                    source = (row[7] or "").strip()
            cur = c.execute(
                "INSERT INTO posts(sheet, source_url, title) VALUES(?,?,?)",
                (ws.title, source, ws.title.replace("_", " ")),
            )
            pid = cur.lastrowid
            for row in list(ws.values)[1:]:
                if row and isinstance(row[0], int) and row[1]:
                    c.execute(
                        "INSERT OR IGNORE INTO comments(post_id, body) VALUES(?,?)",
                        (pid, str(row[1])),
                    )
        wb.close()
    c.commit()

    # Also import from data.json if it exists — replaces old comments with new ones
    DATA_JSON = ROOT / "data.json"
    if DATA_JSON.exists():
        import json as _json
        _data = _json.loads(DATA_JSON.read_text())
        for p in _data:
            pid = p["id"]
            if p.get("thumb"):
                c.execute("UPDATE posts SET thumbnail_url = ? WHERE id = ?", (p["thumb"], pid))
            if p.get("description"):
                c.execute("UPDATE posts SET description = ? WHERE id = ?", (p["description"], pid))
        # Replace all comments: delete old, insert new from data.json
        c.execute("DELETE FROM assignments")
        c.execute("DELETE FROM comments")
        for p in _data:
            for cmt in p.get("comments", []):
                c.execute("INSERT INTO comments(post_id, body) VALUES(?,?)", (p["id"], cmt))
    c.commit()
    c.close()

    # Migrate: add assigned_at if missing
    try:
        c = db()
        c.execute("ALTER TABLE assignments ADD COLUMN assigned_at TEXT DEFAULT CURRENT_TIMESTAMP")
        c.commit()
        c.close()
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migrate: add description column to posts
    try:
        c = db()
        c.execute("ALTER TABLE posts ADD COLUMN description TEXT DEFAULT ''")
        c.commit()
        c.close()
    except sqlite3.OperationalError:
        pass  # column already exists

# ---------------------------------------------------------------------------
# fetch Instagram thumbnails via og:image
# ---------------------------------------------------------------------------
def fetch_thumbnails():
    """Fetch og:image from each post page and store CDN URL in DB."""
    c = db()
    posts = c.execute(
        "SELECT id, source_url, thumbnail_url FROM posts WHERE thumbnail_url = '' OR thumbnail_url IS NULL"
    ).fetchall()
    c.close()
    if not posts:
        return

    UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
    fetched = 0
    for p in posts:
        url = p["source_url"]
        if not url:
            continue
        try:
            req = urlreq.Request(url, headers={"User-Agent": UA})
            html = urlreq.urlopen(req, timeout=12).read().decode("utf-8", errors="ignore")
            m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
            if m:
                thumb_url = m.group(1).replace("&amp;", "&")
                c = db()
                c.execute("UPDATE posts SET thumbnail_url = ? WHERE id = ?", (thumb_url, p["id"]))
                c.commit()
                c.close()
                fetched += 1
            else:
                # Try alternate pattern
                m = re.search(r'og:image[^>]+content="([^"]+)"', html)
                if m:
                    thumb_url = m.group(1).replace("&amp;", "&")
                    c = db()
                    c.execute("UPDATE posts SET thumbnail_url = ? WHERE id = ?", (thumb_url, p["id"]))
                    c.commit()
                    c.close()
                    fetched += 1
        except Exception as e:
            # Leave thumbnail empty; UI will use placeholder
            print(f"  thumb fetch failed for post {p['id']}: {type(e).__name__}")
        time.sleep(0.6)  # polite rate limit

    print(f"  Thumbnails fetched: {fetched}/{len(posts)}")

# ---------------------------------------------------------------------------
# request helpers
# ---------------------------------------------------------------------------
def current_user(handler):
    jar = cookies.SimpleCookie()
    jar.load(handler.headers.get("Cookie", ""))
    tok = jar.get("session")
    if not tok or tok.value not in SESSIONS:
        return None
    c = db()
    row = c.execute("SELECT * FROM users WHERE id = ?", (SESSIONS[tok.value],)).fetchone()
    c.close()
    return row

def json_body(handler):
    n = int(handler.headers.get("Content-Length", "0"))
    return json.loads(handler.rfile.read(n) or b"{}")

def send(handler, status, body, content_type="application/json", extra=None):
    data = body if isinstance(body, bytes) else body.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", content_type + "; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    for k, v in (extra or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(data)

# ---------------------------------------------------------------------------
# HTML (single-page SPA injected inline)
# ---------------------------------------------------------------------------
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
/* ── Center-peek Carousel ── */
.carousel-section{padding:12px 0 8px;flex:0 0 auto;position:relative}
.carousel-label{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);padding:0 24px 12px;text-align:center}
.carousel-viewport{overflow-x:auto;overflow-y:hidden;scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;padding:0 calc(50vw - 150px);scrollbar-width:none}
.carousel-viewport::-webkit-scrollbar{display:none}
.carousel-track{display:flex;gap:16px;padding:12px 0;align-items:center}
/* Card */
.ccard{flex:0 0 280px;scroll-snap-align:center;border-radius:var(--radius);overflow:hidden;background:var(--surface);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border:1px solid var(--line);cursor:pointer;transition:all .4s cubic-bezier(.4,0,.2,1);position:relative;height:58vh;display:flex;flex-direction:column;transform:scale(.82);opacity:.4;filter:brightness(.6)}
.ccard.active{transform:scale(1);opacity:1;filter:brightness(1);border-color:rgba(129,140,248,.4);box-shadow:0 12px 60px rgba(99,102,241,.2);z-index:2}
.ccard:hover:not(.active){transform:scale(.87);opacity:.6}
.ccard img{width:100%;height:62%;object-fit:cover;display:block;background:var(--surface2)}
.ccard .card-body{padding:14px;flex:1;display:flex;flex-direction:column;justify-content:space-between}
.ccard .card-title{font-size:13px;font-weight:700;line-height:1.3;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.ccard .card-meta{font-size:11px;color:var(--muted);margin-top:4px}
.ccard.active .card-title{font-size:15px}.ccard.active .card-meta{font-size:12px}
/* Done state */
.ccard.done{opacity:.35!important;filter:grayscale(.7)!important;transform:scale(.82)!important}
.ccard.done.active{transform:scale(.88)!important;opacity:.5!important}
.ccard.done::after{content:'✓';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:64px;font-weight:900;color:#22c55e;z-index:5;text-shadow:0 0 40px rgba(34,197,94,.5);animation:popIn .4s cubic-bezier(.34,1.56,.64,1)}
.ccard.done::before{content:'';position:absolute;inset:0;background:rgba(9,9,11,.5);z-index:4}
@keyframes popIn{0%{opacity:0;transform:translate(-50%,-50%) scale(.3)}100%{opacity:1;transform:translate(-50%,-50%) scale(1)}}
/* ── Stats Bar ── */
.stats-bar{flex:1;display:flex;align-items:center;justify-content:center;gap:12px;padding:0 24px;max-width:500px;margin:0 auto;width:100%}
.stat-item{flex:1;background:rgba(255,255,255,.03);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--line);border-radius:20px;padding:18px 14px;text-align:center;transition:all .3s}
.stat-item:nth-child(1){border-color:rgba(129,140,248,.15)}.stat-item:nth-child(2){border-color:rgba(192,132,252,.15)}.stat-item:nth-child(3){border-color:rgba(244,114,182,.15)}
.stat-item .val{font-size:28px;font-weight:800;letter-spacing:-.5px}
.stat-item:nth-child(1) .val{background:linear-gradient(135deg,#818cf8,#a5b4fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-item:nth-child(2) .val{background:linear-gradient(135deg,#a78bfa,#c4b5fd);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-item:nth-child(3) .val{background:linear-gradient(135deg,#f472b6,#f9a8d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-item .lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-top:4px}
/* ── Modal ── */
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
.cmt-card .btn-copy{width:100%;padding:13px;font:inherit;font-size:14px;font-weight:700;border:none;border-radius:12px;cursor:pointer;transition:all .2s;background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff}
.cmt-card .btn-copy:active{transform:scale(.97)}.cmt-card .btn-copy.done{background:linear-gradient(135deg,#22c55e,#16a34a)!important}
.dominant-cta{padding:14px 20px;border-top:1px solid var(--line);background:rgba(18,18,23,.95);backdrop-filter:blur(20px);flex-shrink:0}
.dominant-cta .btn-ambil{width:100%;padding:16px;font:inherit;font-size:16px;font-weight:800;letter-spacing:-.2px;border:none;border-radius:16px;cursor:pointer;transition:all .2s;background:linear-gradient(135deg,var(--brand),var(--brand2),var(--brand3));color:#fff;box-shadow:0 4px 30px rgba(99,102,241,.3)}
.dominant-cta .btn-ambil:hover{box-shadow:0 6px 40px rgba(99,102,241,.45)}
.dominant-cta .btn-ambil:active{transform:scale(.97)}
.dominant-cta .btn-ambil:disabled{opacity:.5;cursor:not-allowed;background:var(--surface2);box-shadow:none;color:var(--muted)}
.empty-msg{padding:32px;text-align:center;color:var(--muted);font-size:14px}
@media(min-width:600px){.ccard{flex:0 0 300px;height:62vh}.carousel-viewport{padding:0 calc(50vw - 165px)}}
@media(min-width:860px){.ccard{flex:0 0 320px}.carousel-viewport{padding:0 calc(50vw - 175px)}}
</style></head><body>

<div id="auth-v" class="auth-pg"><div class="auth-card">
<div class="icon">💬</div><h1>Comment Desk</h1><p class="tag">Masuk pakai handle tim internal</p>
<div class="alert">Komentar cuma buat di-review &amp; di-copy. Nggak ada yang diposting otomatis ke Instagram.</div>
<input id="hinp" maxlength="40" placeholder="contoh: reviewer-01" autocomplete="off" enterkeyhint="go">
<button class="btn btn-primary" onclick="doLogin()">Masuk ke Workspace</button>
<p id="auth-err" class="err-text"></p>
</div></div>

<div id="app-v" class="app-v"><div class="topbar"><h1>💬 Comment Desk</h1><span class="hi" id="hi"></span><button class="btn btn-ghost" onclick="doLogout()">Keluar</button></div>
<section class="carousel-section"><p class="carousel-label">Pilih Postingan</p>
<div class="carousel-viewport" id="carousel-vp"><div class="carousel-track" id="carousel-track"></div></div></section>
<div class="stats-bar" id="stats-bar"></div>
<div class="modal-overlay" id="modal-overlay"><div class="modal-sheet"><div class="modal-topbar">
<button class="mbtn" onclick="closeModal()">← Kembali</button><span class="mtitle" id="modal-title"></span><button class="mbtn" onclick="closeModal()">✕ Tutup</button>
</div><div class="modal-hero"><img id="modal-img" src="" alt=""></div><div class="modal-body">
<p class="info-line" id="modal-url"></p><div class="post-desc" id="modal-desc"></div>
<div class="cmt-section"><p class="cmt-section-label">💬 Komentar</p><div id="modal-cmt"></div></div>
</div><div class="dominant-cta"><button class="btn-ambil" id="btn-ambil" onclick="doAssign()">🎲 Ambil &amp; Copy Komentar</button></div></div></div>
</div>

<script>
const $=id=>document.getElementById(id);
const S={posts:[],post:null,has:false,done:new Set()};

async function api(path,opt={}){const r=await fetch(path,{headers:{"Content-Type":"application/json"},...opt});const d=await r.json();if(!r.ok)throw Error(d.error||"Request failed");return d}

async function boot(){
  try{const m=await api("/api/me");
    if(m.user){$("auth-v").style.display="none";$("app-v").classList.add("active");$("hi").textContent="Halo, "+m.user.handle;S.posts=m.posts;
    for(const p of S.posts){try{const a=await api("/api/assignments?post_id="+p.id);if(a.items.some(x=>x.status==="copied"))S.done.add(p.id)}catch(_){}}
    renderCarousel();renderStats()}
  }catch(_){}}

function renderCarousel(){
  const track=$("carousel-track"),cw=window.innerWidth<600?300:window.innerWidth<860?320:340;
  track.innerHTML=S.posts.map(p=>{const isDone=S.done.has(p.id);return '<div class="ccard'+(isDone?' done':'')+'" id="cc-'+p.id+'" onclick="openModal('+p.id+')"><img src="'+esc(p.thumbnail)+'" alt="'+esc(p.title)+'" loading="lazy" onerror="this.style.display=\'none\';this.insertAdjacentHTML(\'afterend\',\'<div style=height:62%;background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:36px>📷</div>\')"><div class="card-body"><div class="card-title">'+esc(p.title)+'</div><div class="card-meta">'+p.count+' komentar'+(isDone?' • ✓':'')+'</div></div></div>'}).join("");
  const vp=$("carousel-vp");vp.onscroll=()=>updateActive();
  if(S.posts.length>2){const mid=Math.floor(S.posts.length/2);setTimeout(()=>vp.scrollTo({left:mid*(cw+16),behavior:'smooth'}),400)}
  setTimeout(updateActive,600);
}

function updateActive(){
  const cards=document.querySelectorAll('.ccard'),vp=$("carousel-vp"),vpr=vp.getBoundingClientRect();
  let best=null,bestDist=Infinity;
  cards.forEach(c=>{const cr=c.getBoundingClientRect(),ccx=cr.left+cr.width/2,vcx=vpr.left+vpr.width/2,dist=Math.abs(ccx-vcx);c.classList.remove('active');if(dist<bestDist){bestDist=dist;best=c}});
  if(best)best.classList.add('active');
}

function renderStats(){const t=S.posts.length,d=S.done.size,r=t-d;$("stats-bar").innerHTML='<div class="stat-item"><div class="val">'+t+'</div><div class="lbl">Total Post</div></div><div class="stat-item"><div class="val">'+d+'</div><div class="lbl">Selesai</div></div><div class="stat-item"><div class="val">'+r+'</div><div class="lbl">Tersisa</div></div>'}

async function openModal(id){
  S.post=S.posts.find(x=>x.id===id);if(!S.post)return;$("modal-title").textContent=S.post.title;
  $("modal-img").src=S.post.thumbnail||'';$("modal-img").onerror=function(){this.style.display='none'};
  $("modal-url").textContent=S.post.source_url||'';$("modal-desc").textContent=S.post.description||'';
  $("modal-overlay").classList.add("open");document.body.style.overflow='hidden';await reloadModalCmt()}

async function reloadModalCmt(){
  try{const d=await api("/api/assignments?post_id="+S.post.id);S.has=d.items.length>0;const btn=$("btn-ambil");btn.disabled=S.has;btn.textContent=S.has?"✓ Sudah Ambil 1 Komentar":"🎲 Ambil & Copy Komentar";
  $("modal-cmt").innerHTML=d.items.length?d.items.map(x=>'<div class="cmt-card"><p class="cmt-body">'+esc(x.body)+'</p><span class="cmt-tag '+x.status+'">'+(x.status==='copied'?'✓ Sudah di-copy':'📋 Baru di-assign')+'</span><button class="btn-copy'+(x.status==='copied'?' done':'')+'" onclick="doCopy('+x.id+',this)">'+(x.status==='copied'?'Tersalin ✓':'Copy ke Clipboard')+'</button></div>').join(""):'<div class="empty-msg">Klik tombol di bawah untuk dapat satu komentar acak ✨</div>'}catch(e){$("modal-cmt").innerHTML='<div class="empty-msg">Gagal memuat.</div>'}}

function closeModal(){$("modal-overlay").classList.remove("open");document.body.style.overflow='';S.done.forEach(pid=>{const c=document.getElementById("cc-"+pid);if(c)c.classList.add("done")});renderStats()}
async function doAssign(){if(S.has||!S.post)return;try{await api("/api/assign",{method:"POST",body:JSON.stringify({post_id:S.post.id})});await reloadModalCmt()}catch(e){alert(e.message)}}
async function doCopy(id,btn){try{const d=await api("/api/copy",{method:"POST",body:JSON.stringify({assignment_id:id})});try{await navigator.clipboard.writeText(d.body)}catch(_){}btn.textContent="Tersalin ✓";btn.classList.add("done");S.done.add(S.post.id);const card=document.getElementById("cc-"+S.post.id);if(card)card.classList.add("done");renderStats();setTimeout(()=>{btn.textContent="Copy ke Clipboard";btn.classList.remove("done")},2000);await reloadModalCmt()}catch(_){}}
async function doLogin(){const h=$("hinp").value.trim();if(!h)return $("auth-err").textContent="Isi handle dulu ya";try{await api("/api/login",{method:"POST",body:JSON.stringify({handle:h})});location.reload()}catch(e){$("auth-err").textContent=e.message}}
async function doLogout(){await api("/api/logout",{method:"POST"});location.reload()}
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
window.openModal=openModal;window.doAssign=doAssign;window.doCopy=doCopy;window.doLogin=doLogin;window.doLogout=doLogout;window.closeModal=closeModal;
boot();
</script></body></html>
'''"Comment Desk – internal tool, copy-only. Posts loaded from XLSX."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib import request as urlreq, error as urlerr
from pathlib import Path
import sqlite3, json, secrets, os, re, hashlib, time
from http import cookies
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent
DB   = ROOT / "app.db"
XLSX = ROOT / "duniameutya_last_10_posts_comments.xlsx"
PORT = int(os.environ.get("PORT", "8765"))
SESSIONS = {}
ADMIN_SESSIONS = {}
ADMIN_PASSWORD = "@poji#1"
THUMB_CACHE = ROOT / "thumbs"
THUMB_CACHE.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# database helpers
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
        id         INTEGER PRIMARY KEY,
        handle     TEXT UNIQUE NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS posts(
        id            INTEGER PRIMARY KEY,
        sheet         TEXT UNIQUE NOT NULL,
        source_url    TEXT,
        title         TEXT,
        thumbnail_url TEXT DEFAULT '',
        description   TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS comments(
        id      INTEGER PRIMARY KEY,
        post_id INTEGER NOT NULL REFERENCES posts(id),
        body    TEXT    NOT NULL,
        UNIQUE(post_id, body)
    );
    CREATE TABLE IF NOT EXISTS assignments(
        id         INTEGER PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        comment_id INTEGER NOT NULL REFERENCES comments(id),
        status     TEXT    DEFAULT 'assigned',
        assigned_at TEXT   DEFAULT CURRENT_TIMESTAMP,
        copied_at  TEXT,
        UNIQUE(user_id, comment_id)
    );
    CREATE INDEX IF NOT EXISTS idx_assign_user  ON assignments(user_id);
    CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
    """)
    c.commit()

    # Import XLSX if DB is empty
    if XLSX.exists() and not c.execute("SELECT 1 FROM posts LIMIT 1").fetchone():
        wb = load_workbook(XLSX, read_only=True, data_only=True)
        for ws in wb.worksheets:
            source = ""
            for row in list(ws.values)[:4]:
                if row and len(row) > 7 and row[6] == "Source URL":
                    source = (row[7] or "").strip()
            cur = c.execute(
                "INSERT INTO posts(sheet, source_url, title) VALUES(?,?,?)",
                (ws.title, source, ws.title.replace("_", " ")),
            )
            pid = cur.lastrowid
            for row in list(ws.values)[1:]:
                if row and isinstance(row[0], int) and row[1]:
                    c.execute(
                        "INSERT OR IGNORE INTO comments(post_id, body) VALUES(?,?)",
                        (pid, str(row[1])),
                    )
        wb.close()
    c.commit()

    # Also import from data.json if it exists — replaces old comments with new ones
    DATA_JSON = ROOT / "data.json"
    if DATA_JSON.exists():
        import json as _json
        _data = _json.loads(DATA_JSON.read_text())
        for p in _data:
            pid = p["id"]
            if p.get("thumb"):
                c.execute("UPDATE posts SET thumbnail_url = ? WHERE id = ?", (p["thumb"], pid))
            if p.get("description"):
                c.execute("UPDATE posts SET description = ? WHERE id = ?", (p["description"], pid))
        # Replace all comments: delete old, insert new from data.json
        c.execute("DELETE FROM assignments")
        c.execute("DELETE FROM comments")
        for p in _data:
            for cmt in p.get("comments", []):
                c.execute("INSERT INTO comments(post_id, body) VALUES(?,?)", (p["id"], cmt))
    c.commit()
    c.close()

    # Migrate: add assigned_at if missing
    try:
        c = db()
        c.execute("ALTER TABLE assignments ADD COLUMN assigned_at TEXT DEFAULT CURRENT_TIMESTAMP")
        c.commit()
        c.close()
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migrate: add description column to posts
    try:
        c = db()
        c.execute("ALTER TABLE posts ADD COLUMN description TEXT DEFAULT ''")
        c.commit()
        c.close()
    except sqlite3.OperationalError:
        pass  # column already exists

# ---------------------------------------------------------------------------
# fetch Instagram thumbnails via og:image
# ---------------------------------------------------------------------------
def fetch_thumbnails():
    """Fetch og:image from each post page and store CDN URL in DB."""
    c = db()
    posts = c.execute(
        "SELECT id, source_url, thumbnail_url FROM posts WHERE thumbnail_url = '' OR thumbnail_url IS NULL"
    ).fetchall()
    c.close()
    if not posts:
        return

    UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
    fetched = 0
    for p in posts:
        url = p["source_url"]
        if not url:
            continue
        try:
            req = urlreq.Request(url, headers={"User-Agent": UA})
            html = urlreq.urlopen(req, timeout=12).read().decode("utf-8", errors="ignore")
            m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
            if m:
                thumb_url = m.group(1).replace("&amp;", "&")
                c = db()
                c.execute("UPDATE posts SET thumbnail_url = ? WHERE id = ?", (thumb_url, p["id"]))
                c.commit()
                c.close()
                fetched += 1
            else:
                # Try alternate pattern
                m = re.search(r'og:image[^>]+content="([^"]+)"', html)
                if m:
                    thumb_url = m.group(1).replace("&amp;", "&")
                    c = db()
                    c.execute("UPDATE posts SET thumbnail_url = ? WHERE id = ?", (thumb_url, p["id"]))
                    c.commit()
                    c.close()
                    fetched += 1
        except Exception as e:
            # Leave thumbnail empty; UI will use placeholder
            print(f"  thumb fetch failed for post {p['id']}: {type(e).__name__}")
        time.sleep(0.6)  # polite rate limit

    print(f"  Thumbnails fetched: {fetched}/{len(posts)}")

# ---------------------------------------------------------------------------
# request helpers
# ---------------------------------------------------------------------------
def current_user(handler):
    jar = cookies.SimpleCookie()
    jar.load(handler.headers.get("Cookie", ""))
    tok = jar.get("session")
    if not tok or tok.value not in SESSIONS:
        return None
    c = db()
    row = c.execute("SELECT * FROM users WHERE id = ?", (SESSIONS[tok.value],)).fetchone()
    c.close()
    return row

def json_body(handler):
    n = int(handler.headers.get("Content-Length", "0"))
    return json.loads(handler.rfile.read(n) or b"{}")

def send(handler, status, body, content_type="application/json", extra=None):
    data = body if isinstance(body, bytes) else body.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", content_type + "; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    for k, v in (extra or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(data)

# ---------------------------------------------------------------------------
# HTML (single-page SPA injected inline)
# ---------------------------------------------------------------------------
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
/* ── Center-peek Carousel ── */
.carousel-section{padding:12px 0 8px;flex:0 0 auto;position:relative}
.carousel-label{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);padding:0 24px 12px;text-align:center}
.carousel-viewport{overflow-x:auto;overflow-y:hidden;scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;padding:0 calc(50vw - 150px);scrollbar-width:none}
.carousel-viewport::-webkit-scrollbar{display:none}
.carousel-track{display:flex;gap:16px;padding:12px 0;align-items:center}
/* Card */
.ccard{flex:0 0 280px;scroll-snap-align:center;border-radius:var(--radius);overflow:hidden;background:var(--surface);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);border:1px solid var(--line);cursor:pointer;transition:all .4s cubic-bezier(.4,0,.2,1);position:relative;height:58vh;display:flex;flex-direction:column;transform:scale(.82);opacity:.4;filter:brightness(.6)}
.ccard.active{transform:scale(1);opacity:1;filter:brightness(1);border-color:rgba(129,140,248,.4);box-shadow:0 12px 60px rgba(99,102,241,.2);z-index:2}
.ccard:hover:not(.active){transform:scale(.87);opacity:.6}
.ccard img{width:100%;height:62%;object-fit:cover;display:block;background:var(--surface2)}
.ccard .card-body{padding:14px;flex:1;display:flex;flex-direction:column;justify-content:space-between}
.ccard .card-title{font-size:13px;font-weight:700;line-height:1.3;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.ccard .card-meta{font-size:11px;color:var(--muted);margin-top:4px}
.ccard.active .card-title{font-size:15px}.ccard.active .card-meta{font-size:12px}
/* Done state */
.ccard.done{opacity:.35!important;filter:grayscale(.7)!important;transform:scale(.82)!important}
.ccard.done.active{transform:scale(.88)!important;opacity:.5!important}
.ccard.done::after{content:'✓';position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:64px;font-weight:900;color:#22c55e;z-index:5;text-shadow:0 0 40px rgba(34,197,94,.5);animation:popIn .4s cubic-bezier(.34,1.56,.64,1)}
.ccard.done::before{content:'';position:absolute;inset:0;background:rgba(9,9,11,.5);z-index:4}
@keyframes popIn{0%{opacity:0;transform:translate(-50%,-50%) scale(.3)}100%{opacity:1;transform:translate(-50%,-50%) scale(1)}}
/* ── Stats Bar ── */
.stats-bar{display:flex;gap:12px;padding:0 24px 16px;flex:0 0 auto;max-width:500px;margin:0 auto;width:100%}
.stat-item{flex:1;background:rgba(255,255,255,.03);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--line);border-radius:20px;padding:18px 14px;text-align:center;transition:all .3s}
.stat-item:nth-child(1){border-color:rgba(129,140,248,.15)}.stat-item:nth-child(2){border-color:rgba(192,132,252,.15)}.stat-item:nth-child(3){border-color:rgba(244,114,182,.15)}
.stat-item .val{font-size:28px;font-weight:800;letter-spacing:-.5px}
.stat-item:nth-child(1) .val{background:linear-gradient(135deg,#818cf8,#a5b4fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-item:nth-child(2) .val{background:linear-gradient(135deg,#a78bfa,#c4b5fd);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-item:nth-child(3) .val{background:linear-gradient(135deg,#f472b6,#f9a8d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.stat-item .lbl{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-top:4px}
/* ── Modal ── */
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
.cmt-card .btn-copy{width:100%;padding:13px;font:inherit;font-size:14px;font-weight:700;border:none;border-radius:12px;cursor:pointer;transition:all .2s;background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff}
.cmt-card .btn-copy:active{transform:scale(.97)}.cmt-card .btn-copy.done{background:linear-gradient(135deg,#22c55e,#16a34a)!important}
.dominant-cta{padding:14px 20px;border-top:1px solid var(--line);background:rgba(18,18,23,.95);backdrop-filter:blur(20px);flex-shrink:0}
.dominant-cta .btn-ambil{width:100%;padding:16px;font:inherit;font-size:16px;font-weight:800;letter-spacing:-.2px;border:none;border-radius:16px;cursor:pointer;transition:all .2s;background:linear-gradient(135deg,var(--brand),var(--brand2),var(--brand3));color:#fff;box-shadow:0 4px 30px rgba(99,102,241,.3)}
.dominant-cta .btn-ambil:hover{box-shadow:0 6px 40px rgba(99,102,241,.45)}
.dominant-cta .btn-ambil:active{transform:scale(.97)}
.dominant-cta .btn-ambil:disabled{opacity:.5;cursor:not-allowed;background:var(--surface2);box-shadow:none;color:var(--muted)}
.empty-msg{padding:32px;text-align:center;color:var(--muted);font-size:14px}
@media(min-width:600px){.ccard{flex:0 0 300px;height:62vh}.carousel-viewport{padding:0 calc(50vw - 165px)}}
@media(min-width:860px){.ccard{flex:0 0 320px}.carousel-viewport{padding:0 calc(50vw - 175px)}}
</style></head><body>

<div id="auth-v" class="auth-pg"><div class="auth-card">
<div class="icon">💬</div><h1>Comment Desk</h1><p class="tag">Masuk pakai handle tim internal</p>
<div class="alert">Komentar cuma buat di-review &amp; di-copy. Nggak ada yang diposting otomatis ke Instagram.</div>
<input id="hinp" maxlength="40" placeholder="contoh: reviewer-01" autocomplete="off" enterkeyhint="go">
<button class="btn btn-primary" onclick="doLogin()">Masuk ke Workspace</button>
<p id="auth-err" class="err-text"></p>
</div></div>

<div id="app-v" class="app-v"><div class="topbar"><h1>💬 Comment Desk</h1><span class="hi" id="hi"></span><button class="btn btn-ghost" onclick="doLogout()">Keluar</button></div>
<section class="carousel-section"><p class="carousel-label">Pilih Postingan</p>
<div class="carousel-viewport" id="carousel-vp"><div class="carousel-track" id="carousel-track"></div></div></section>
<div class="stats-bar" id="stats-bar"></div>
<div class="modal-overlay" id="modal-overlay"><div class="modal-sheet"><div class="modal-topbar">
<button class="mbtn" onclick="closeModal()">← Kembali</button><span class="mtitle" id="modal-title"></span><button class="mbtn" onclick="closeModal()">✕ Tutup</button>
</div><div class="modal-hero"><img id="modal-img" src="" alt=""></div><div class="modal-body">
<p class="info-line" id="modal-url"></p><div class="post-desc" id="modal-desc"></div>
<div class="cmt-section"><p class="cmt-section-label">💬 Komentar</p><div id="modal-cmt"></div></div>
</div><div class="dominant-cta"><button class="btn-ambil" id="btn-ambil" onclick="doAssign()">🎲 Ambil &amp; Copy Komentar</button></div></div></div>
</div>

<script>
const $=id=>document.getElementById(id);
const S={posts:[],post:null,has:false,done:new Set()};

async function api(path,opt={}){const r=await fetch(path,{headers:{"Content-Type":"application/json"},...opt});const d=await r.json();if(!r.ok)throw Error(d.error||"Request failed");return d}

async function boot(){
  try{const m=await api("/api/me");
    if(m.user){$("auth-v").style.display="none";$("app-v").classList.add("active");$("hi").textContent="Halo, "+m.user.handle;S.posts=m.posts;
    for(const p of S.posts){try{const a=await api("/api/assignments?post_id="+p.id);if(a.items.some(x=>x.status==="copied"))S.done.add(p.id)}catch(_){}}
    renderCarousel();renderStats()}
  }catch(_){}}

function renderCarousel(){
  const track=$("carousel-track"),cw=window.innerWidth<600?300:window.innerWidth<860?320:340;
  track.innerHTML=S.posts.map(p=>{const isDone=S.done.has(p.id);return '<div class="ccard'+(isDone?' done':'')+'" id="cc-'+p.id+'" onclick="openModal('+p.id+')"><img src="'+esc(p.thumbnail)+'" alt="'+esc(p.title)+'" loading="lazy" onerror="this.style.display=\'none\';this.insertAdjacentHTML(\'afterend\',\'<div style=height:62%;background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:36px>📷</div>\')"><div class="card-body"><div class="card-title">'+esc(p.title)+'</div><div class="card-meta">'+p.count+' komentar'+(isDone?' • ✓':'')+'</div></div></div>'}).join("");
  const vp=$("carousel-vp");vp.onscroll=()=>updateActive();
  if(S.posts.length>2){const mid=Math.floor(S.posts.length/2);setTimeout(()=>vp.scrollTo({left:mid*(cw+16),behavior:'smooth'}),400)}
  setTimeout(updateActive,600);
}

function updateActive(){
  const cards=document.querySelectorAll('.ccard'),vp=$("carousel-vp"),vpr=vp.getBoundingClientRect();
  let best=null,bestDist=Infinity;
  cards.forEach(c=>{const cr=c.getBoundingClientRect(),ccx=cr.left+cr.width/2,vcx=vpr.left+vpr.width/2,dist=Math.abs(ccx-vcx);c.classList.remove('active');if(dist<bestDist){bestDist=dist;best=c}});
  if(best)best.classList.add('active');
}

function renderStats(){const t=S.posts.length,d=S.done.size,r=t-d;$("stats-bar").innerHTML='<div class="stat-item"><div class="val">'+t+'</div><div class="lbl">Total Post</div></div><div class="stat-item"><div class="val">'+d+'</div><div class="lbl">Selesai</div></div><div class="stat-item"><div class="val">'+r+'</div><div class="lbl">Tersisa</div></div>'}

async function openModal(id){
  S.post=S.posts.find(x=>x.id===id);if(!S.post)return;$("modal-title").textContent=S.post.title;
  $("modal-img").src=S.post.thumbnail||'';$("modal-img").onerror=function(){this.style.display='none'};
  $("modal-url").textContent=S.post.source_url||'';$("modal-desc").textContent=S.post.description||'';
  $("modal-overlay").classList.add("open");document.body.style.overflow='hidden';await reloadModalCmt()}

async function reloadModalCmt(){
  try{const d=await api("/api/assignments?post_id="+S.post.id);S.has=d.items.length>0;const btn=$("btn-ambil");btn.disabled=S.has;btn.textContent=S.has?"✓ Sudah Ambil 1 Komentar":"🎲 Ambil & Copy Komentar";
  $("modal-cmt").innerHTML=d.items.length?d.items.map(x=>'<div class="cmt-card"><p class="cmt-body">'+esc(x.body)+'</p><span class="cmt-tag '+x.status+'">'+(x.status==='copied'?'✓ Sudah di-copy':'📋 Baru di-assign')+'</span><button class="btn-copy'+(x.status==='copied'?' done':'')+'" onclick="doCopy('+x.id+',this)">'+(x.status==='copied'?'Tersalin ✓':'Copy ke Clipboard')+'</button></div>').join(""):'<div class="empty-msg">Klik tombol di bawah untuk dapat satu komentar acak ✨</div>'}catch(e){$("modal-cmt").innerHTML='<div class="empty-msg">Gagal memuat.</div>'}}

function closeModal(){$("modal-overlay").classList.remove("open");document.body.style.overflow='';S.done.forEach(pid=>{const c=document.getElementById("cc-"+pid);if(c)c.classList.add("done")});renderStats()}
async function doAssign(){if(S.has||!S.post)return;try{await api("/api/assign",{method:"POST",body:JSON.stringify({post_id:S.post.id})});await reloadModalCmt()}catch(e){alert(e.message)}}
async function doCopy(id,btn){try{const d=await api("/api/copy",{method:"POST",body:JSON.stringify({assignment_id:id})});try{await navigator.clipboard.writeText(d.body)}catch(_){}btn.textContent="Tersalin ✓";btn.classList.add("done");S.done.add(S.post.id);const card=document.getElementById("cc-"+S.post.id);if(card)card.classList.add("done");renderStats();setTimeout(()=>{btn.textContent="Copy ke Clipboard";btn.classList.remove("done")},2000);await reloadModalCmt()}catch(_){}}
async function doLogin(){const h=$("hinp").value.trim();if(!h)return $("auth-err").textContent="Isi handle dulu ya";try{await api("/api/login",{method:"POST",body:JSON.stringify({handle:h})});location.reload()}catch(e){$("auth-err").textContent=e.message}}
async function doLogout(){await api("/api/logout",{method:"POST"});location.reload()}
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
window.openModal=openModal;window.doAssign=doAssign;window.doCopy=doCopy;window.doLogin=doLogin;window.doLogout=doLogout;window.closeModal=closeModal;
boot();
</script></body></html>
'''

# ---------------------------------------------------------------------------
ADMIN_HTML = r'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>Admin — Comment Desk</title>
<style>
:root{--bg:#0f172a;--surface:#1e293b;--ink:#f1f5f9;--muted:#94a3b8;--brand:#818cf8;--brand2:#c084fc;--line:#334155;--radius:16px}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:400 14px/1.5 system-ui,-apple-system,sans-serif;min-height:100vh}
.auth-pg{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.auth-box{background:var(--surface);border-radius:var(--radius);padding:40px 32px;max-width:420px;width:100%;text-align:center;border:1px solid var(--line);box-shadow:0 4px 24px rgba(0,0,0,.3)}
.auth-box .icon{font-size:48px;margin-bottom:8px}
.auth-box h1{font-size:24px;margin:0 0 4px;font-weight:800}
.auth-box .tag{color:var(--muted);font-size:13px;margin-bottom:24px}
input{font:inherit;width:100%;padding:13px 16px;border:2px solid var(--line);border-radius:12px;outline:none;margin-bottom:14px;font-size:14px;background:var(--bg);color:var(--ink)}
input:focus{border-color:var(--brand);box-shadow:0 0 0 4px rgba(129,140,248,.15)}
.btn{font:inherit;font-weight:700;cursor:pointer;border:none}
.btn-primary{width:100%;padding:14px;background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;border-radius:12px;font-size:15px}
.btn-ghost{background:0;color:var(--muted);padding:8px 14px;font-size:13px;border-radius:8px}
.btn-sm{padding:10px 22px;font-size:13px;border-radius:10px;width:auto}
.err{color:#f87171;font-size:13px;margin-top:8px}.ok{color:#34d399;font-size:13px}
.dash{display:none;padding:24px;max-width:1200px;margin:0 auto}.dash.active{display:block}
.dash-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}
.dash-header h1{font-size:22px;margin:0;font-weight:800}.dash-header .sub{color:var(--muted);font-size:13px}
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:28px}
.stat-card{background:var(--surface);border-radius:var(--radius);padding:22px 20px;border:1px solid var(--line)}
.stat-card .val{font-size:32px;font-weight:800}.stat-card .lbl{font-size:12px;color:var(--muted);text-transform:uppercase}
.stat-card .pct{font-size:13px;color:var(--brand);margin-top:4px}
.section{margin-bottom:28px}.section h3{font-size:15px;font-weight:700;margin:0 0 14px}
table{width:100%;border-collapse:collapse;background:var(--surface);border-radius:var(--radius);overflow:hidden;border:1px solid var(--line)}
th,td{padding:10px 14px;text-align:left;font-size:12px}
th{background:var(--line);font-weight:700;text-transform:uppercase;font-size:10px;color:var(--muted)}
td{border-top:1px solid var(--line)}
.badge{display:inline-block;padding:3px 10px;border-radius:99px;font-size:11px;font-weight:700}
.badge-ok{background:#065f4620;color:#34d399}.badge-new{background:#4338ca20;color:#818cf8}
.prog-bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden;min-width:60px}
.prog-fill{height:100%;background:var(--brand);border-radius:3px}
.loading{padding:40px;text-align:center;color:var(--muted)}
.add-post-box{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:22px;margin-bottom:28px}
.add-post-row{display:flex;gap:10px}.add-post-row input{flex:1;margin-bottom:0}
@media(min-width:700px){.stats{grid-template-columns:repeat(4,1fr)}}
</style></head><body>
<div id="auth-v" class="auth-pg"><div class="auth-box">
<div class="icon">🛡️</div><h1>Admin Panel</h1><p class="tag">Comment Desk</p>
<input id="apwd" type="password" placeholder="Password admin" autocomplete="off">
<button class="btn btn-primary" onclick="doAdminLogin()">Masuk</button><p id="auth-err" class="err"></p>
</div></div>
<div id="dash-v" class="dash">
<div class="dash-header"><div><h1>📊 Dashboard</h1><span class="sub" id="dash-time"></span></div><button class="btn btn-ghost" onclick="doAdminLogout()">↩ Logout</button></div>
<div class="add-post-box"><h3>➕ Tambah Post (URL)</h3><div class="add-post-row"><input id="post-url" placeholder="https://www.instagram.com/p/..."><button class="btn btn-primary btn-sm" onclick="addPost()">Tambah</button></div><p id="add-msg" style="margin-top:8px;font-size:13px"></p></div>
<div class="stats" id="stats"></div>
<div class="section"><h3>👥 User</h3><div id="utable"></div></div>
<div class="section"><h3>📋 Post</h3><div id="ptable"></div></div>
<div class="section"><h3>🕐 Aktivitas</h3><div id="feed"></div></div>
</div>
<script>
const $=id=>document.getElementById(id);
function fmtTime(ts){if(!ts)return"-";return new Date(ts+"Z").toLocaleString("id-ID",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})}
async function api(p,o={}){const r=await fetch(p,{headers:{"Content-Type":"application/json"},...o});const d=await r.json();if(!r.ok)throw Error(d.error||"Failed");return d}
async function loadDash(){try{const d=await api("/api/admin/dashboard");renderStats(d);renderUsers(d.users);renderPosts(d.posts);renderFeed(d.feed)}catch(e){$("dash-v").innerHTML='<div class="loading">Error: '+e.message+'</div>'}}
function renderStats(d){const t=d.users.length,a=d.users.reduce((s,u)=>s+u.assigned,0),c=d.users.reduce((s,u)=>s+u.copied,0),p=d.posts.filter(p=>p.users_assigned>=t).length;$("stats").innerHTML='<div class="stat-card"><div class="val">'+t+'</div><div class="lbl">Reviewer</div></div><div class="stat-card"><div class="val">'+a+'</div><div class="lbl">Assigned</div><div class="pct">'+c+' copied</div></div><div class="stat-card"><div class="val">'+c+'</div><div class="lbl">Copied</div><div class="pct">'+(t?(c/(a||1)*100).toFixed(0):0)+'%</div></div><div class="stat-card"><div class="val">'+p+'/'+d.posts.length+'</div><div class="lbl">Tuntas</div></div>'}
function renderUsers(u){if(!u.length){$("utable").innerHTML='<div class="loading">-</div>';return}$("utable").innerHTML='<table><tr><th>Handle</th><th>Join</th><th>Assigned</th><th>Copied</th><th>%</th></tr>'+u.map(u=>'<tr><td><b>'+esc(u.handle)+'</b></td><td>'+fmtTime(u.created_at)+'</td><td>'+u.assigned+'</td><td>'+u.copied+'</td><td><div class="prog-bar"><div class="prog-fill" style="width:'+(u.assigned?(u.copied/u.assigned*100):0)+'%"></div></div></td></tr>').join("")+'</table>'}
function renderPosts(p){if(!p.length){$("ptable").innerHTML='<div class="loading">-</div>';return}$("ptable").innerHTML='<table><tr><th>Post</th><th>Komen</th><th>Users</th><th>Copy</th><th>%</th></tr>'+p.map(p=>'<tr><td><b>'+esc(p.title)+'</b></td><td>'+p.comment_count+'</td><td>'+p.users_assigned+'</td><td>'+p.users_copied+'</td><td><div class="prog-bar"><div class="prog-fill" style="width:'+(p.users_assigned?(p.users_copied/p.users_assigned*100):0)+'%"></div></div></td></tr>').join("")+'</table>'}
function renderFeed(f){if(!f.length){$("feed").innerHTML='<div class="loading">-</div>';return}$("feed").innerHTML='<table><tr><th>Waktu</th><th>User</th><th>Post</th><th>Aksi</th></tr>'+f.map(f=>'<tr><td>'+fmtTime(f.ts)+'</td><td>'+esc(f.handle)+'</td><td>'+esc(f.post_title)+'</td><td><span class="badge badge-'+(f.action==='copied'?'ok':'new')+'">'+(f.action==='copied'?'✓ Copy':'📋 Assign')+'</span></td></tr>').join("")+'</table>'}
async function addPost(){const u=$("post-url").value.trim(),m=$("add-msg");if(!u)return m.innerHTML='<span class="err">URL required</span>';m.innerHTML='<span class="ok">Adding...</span>';try{const d=await api("/api/admin/add-post",{method:"POST",body:JSON.stringify({url:u})});m.innerHTML='<span class="ok">✅ '+esc(d.post.title)+' — '+d.comments_generated+' comments</span>';$("post-url").value='';loadDash()}catch(e){m.innerHTML='<span class="err">❌ '+e.message+'</span>'}}
async function doAdminLogin(){const p=$("apwd").value.trim();if(!p)return $("auth-err").textContent="Password required";try{await api("/api/admin/login",{method:"POST",body:JSON.stringify({password:p})});$("auth-v").style.display="none";$("dash-v").classList.add("active");$("dash-time").textContent=new Date().toLocaleString("id-ID");loadDash()}catch(e){$("auth-err").textContent=e.message}}
async function boot(){try{await api("/api/admin/me");$("auth-v").style.display="none";$("dash-v").classList.add("active");$("dash-time").textContent=new Date().toLocaleString("id-ID");loadDash()}catch(_){}}
async function doAdminLogout(){try{await api("/api/admin/logout",{method:"POST"});location.reload()}catch(_){location.reload()}}
function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
boot();
</script></body></html>
'''

# ---------------------------------------------------------------------------
# Comment generator (template-based, generates varied Indonesian comments)
# ---------------------------------------------------------------------------
def generate_comments(description, title):
    """Generate 15 varied Indonesian comments based on post context."""
    import random
    # Extract key phrases from description
    words = re.findall(r'\b[A-Za-z]{4,}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*', description)
    topics = [w for w in words if len(w) > 5][:5] if words else ["program", "masyarakat", "digital"]
    short_title = title[:30] if title else "ini"

    templates = [
        # Casual netizen (40%)
        lambda: f"keren bgt... {random.choice(['akhirnya','seneng liat','bangga'])} {random.choice(topics) if topics else 'program'} {random.choice(['berjalan','dijalankan','dilaksanakan'])} dgn serius... semoga manfaatnya nyampe ke masyarakat luas ya 🙏",
        lambda: f"nahh gini dong... {random.choice(topics) if topics else 'kebijakan'} yg pro rakyat... lanjutkan terus bu, jgn setengah2 🔥",
        lambda: f"setuju bgt... emang harus {random.choice(['konsisten','serius','fokus'])} kalo urusan {random.choice(topics) if topics else 'ini'}... jgn cuma pas rame doang 😅",
        lambda: f"waduh keren sih... tp pelaksanaannya gmn ya? semoga ga cuma seremoni aja... ditunggu hasil nyatanya 🙏",
        lambda: f"mantappp... {random.choice(['akhirnya ada','baru kali ini','ini baru namanya'])} {random.choice(topics) if topics else 'langkah'} nyata... gaskeun terus bu 🔥🔥",
        # Young mom (25%)
        lambda: f"buibu pasti setuju... {random.choice(topics) if topics else 'program'} kaya gini yg dibutuhin keluarga kita... semoga berjalan lancar dan tepat sasaran 🙏",
        lambda: f"seneng bgt liat berita begini... sebagai ibu, pengen anak2 kita tumbuh di Indonesia yg makin maju dan peduli sama rakyatnya 😭❤️",
        lambda: f"jadi inget perjuangan kita sebagai ibu... semoga {random.choice(topics) if topics else 'program'} ini bener2 bantu meringankan beban keluarga ya bu 🙏",
        # Slightly polished (15%)
        lambda: f"Ini langkah yang baik untuk memperkuat {random.choice(topics) if topics else 'ekosistem'}. Semoga konsisten dan memberikan dampak yang berkelanjutan bagi masyarakat.",
        lambda: f"Semoga program ini menjadi fondasi kuat untuk Indonesia yang lebih maju. Kolaborasi dan transparansi jadi kunci keberhasilannya.",
        # Critical/questioning (10%)
        lambda: f"bagus sih... tp gmn dgn implementasinya di lapangan? jgn sampe bagus di atas kertas doang... rakyat nunggu bukti nyata 🙏",
        lambda: f"semoga beneran direalisasikan... seringnya program bagus tp eksekusi di lapangan suka beda... ditunggu follow up nya 😅",
    ]

    comments = []
    used = set()
    for _ in range(15):
        t = random.choice(templates)
        for attempt in range(5):
            c = t().strip()
            if c not in used:
                used.add(c)
                comments.append(c)
                break
        else:
            comments.append(f"lanjutkan bu... semoga {random.choice(topics) if topics else 'semua'} berjalan lancar dan manfaatnya dirasakan masyarakat... amin 🙏")
    return comments

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _admin_user(self):
        jar = cookies.SimpleCookie()
        jar.load(self.headers.get("Cookie", ""))
        tok = jar.get("admin_session")
        if tok and tok.value in ADMIN_SESSIONS:
            return True
        return None

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return send(self, 200, HTML, "text/html")
        if path == "/admin":
            return send(self, 200, ADMIN_HTML, "text/html")
        u = current_user(self)
        if path == "/api/admin/me":
            if not self._admin_user():
                return send(self, 401, json.dumps({"error": "Not signed in"}))
            return send(self, 200, json.dumps({"admin": True}))
        if path == "/api/admin/dashboard":
            if not self._admin_user():
                return send(self, 401, json.dumps({"error": "Not signed in"}))
            c = db()
            # users with activity counts
            users = [
                dict(r)
                for r in c.execute("""
                    SELECT u.id, u.handle, u.created_at,
                           COUNT(a.id) AS assigned,
                           COUNT(CASE WHEN a.status = 'copied' THEN 1 END) AS copied
                    FROM users u
                    LEFT JOIN assignments a ON a.user_id = u.id
                    GROUP BY u.id ORDER BY u.created_at
                """)
            ]
            # post stats
            posts = [
                dict(r)
                for r in c.execute("""
                    SELECT p.id, p.title, COUNT(DISTINCT cm.id) AS comment_count,
                           COUNT(DISTINCT a.user_id) AS users_assigned,
                           COUNT(DISTINCT CASE WHEN a.status = 'copied' THEN a.user_id END) AS users_copied
                    FROM posts p
                    LEFT JOIN comments cm ON cm.post_id = p.id
                    LEFT JOIN assignments a ON a.comment_id = cm.id
                    GROUP BY p.id ORDER BY p.id
                """)
            ]
            # recent activity feed
            feed = [
                dict(r)
                for r in c.execute("""
                    SELECT a.assigned_at AS ts, u.handle, p.title AS post_title,
                           'assigned' AS action
                    FROM assignments a
                    JOIN users u ON u.id = a.user_id
                    JOIN comments cm ON cm.id = a.comment_id
                    JOIN posts p ON p.id = cm.post_id
                    UNION ALL
                    SELECT a.copied_at AS ts, u.handle, p.title AS post_title,
                           'copied' AS action
                    FROM assignments a
                    JOIN users u ON u.id = a.user_id
                    JOIN comments cm ON cm.id = a.comment_id
                    JOIN posts p ON p.id = cm.post_id
                    WHERE a.copied_at IS NOT NULL
                    ORDER BY ts DESC LIMIT 40
                """)
            ]
            c.close()
            return send(self, 200, json.dumps({"users": users, "posts": posts, "feed": feed}))
        if path == "/api/me":
            if not u:
                return send(self, 401, json.dumps({"error": "Not signed in"}))
            c = db()
            posts = [
                dict(r)
                for r in c.execute(
                    "SELECT p.id, p.title, p.source_url, p.thumbnail_url, p.description, COUNT(cm.id) AS count FROM posts p LEFT JOIN comments cm ON cm.post_id = p.id GROUP BY p.id ORDER BY p.id"
                )
            ]
            c.close()
            for p in posts:
                p["thumbnail"] = p.get("thumbnail_url") or ""
            return send(self, 200, json.dumps({"user": dict(u), "posts": posts}))
        if path == "/api/assignments" and u:
            q = parse_qs(urlparse(self.path).query)
            pid = int(q.get("post_id", ["0"])[0])
            c = db()
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT a.id, a.status, cm.body FROM assignments a JOIN comments cm ON cm.id = a.comment_id WHERE a.user_id = ? AND cm.post_id = ? ORDER BY a.id DESC",
                    (u["id"], pid),
                )
            ]
            c.close()
            return send(self, 200, json.dumps({"items": rows}))
        return send(self, 404, json.dumps({"error": "Not found"}))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/login":
                d = json_body(self)
                h = " ".join(str(d.get("handle", "")).strip().split())
                if not h or len(h) > 40:
                    return send(self, 400, json.dumps({"error": "Masukkan handle yang valid (maks 40 karakter)"}))
                c = db()
                n = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
                row = c.execute("SELECT * FROM users WHERE handle = ?", (h,)).fetchone()
                if not row:
                    if n >= 10:
                        c.close()
                        return send(self, 403, json.dumps({"error": "Workspace penuh (maks 10 orang)"}))
                    c.execute("INSERT INTO users(handle) VALUES(?)", (h,))
                    c.commit()
                    row = c.execute("SELECT * FROM users WHERE handle = ?", (h,)).fetchone()
                c.close()
                tok = secrets.token_urlsafe(24)
                SESSIONS[tok] = row["id"]
                return send(
                    self, 200, json.dumps({"user": dict(row)}),
                    extra={"Set-Cookie": f"session={tok}; HttpOnly; SameSite=Lax; Path=/"},
                )
            if path == "/api/logout":
                jar = cookies.SimpleCookie(); jar.load(self.headers.get("Cookie", ""))
                t = jar.get("session")
                if t: SESSIONS.pop(t.value, None)
                return send(self, 200, "{}", extra={"Set-Cookie": "session=; Max-Age=0; Path=/"})
            if path == "/api/admin/login":
                d = json_body(self)
                if d.get("password") != ADMIN_PASSWORD:
                    return send(self, 403, json.dumps({"error": "Password salah"}))
                tok = secrets.token_urlsafe(24)
                ADMIN_SESSIONS[tok] = True
                return send(
                    self, 200, json.dumps({"admin": True}),
                    extra={"Set-Cookie": f"admin_session={tok}; HttpOnly; SameSite=Lax; Path=/"},
                )
            if path == "/api/admin/logout":
                jar = cookies.SimpleCookie(); jar.load(self.headers.get("Cookie", ""))
                t = jar.get("admin_session")
                if t: ADMIN_SESSIONS.pop(t.value, None)
                return send(self, 200, "{}", extra={"Set-Cookie": "admin_session=; Max-Age=0; Path=/"})
            if path == "/api/admin/add-post":
                if not self._admin_user():
                    return send(self, 401, json.dumps({"error": "Not signed in"}))
                d = json_body(self)
                url = (d.get("url") or "").strip()
                if not url or "instagram.com/p/" not in url:
                    return send(self, 400, json.dumps({"error": "URL Instagram tidak valid"}))
                # Fetch post metadata
                UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15"
                try:
                    req = urlreq.Request(url, headers={"User-Agent": UA})
                    html = urlreq.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
                except Exception as e:
                    return send(self, 500, json.dumps({"error": f"Gagal fetch post: {e}"}))
                # Extract thumbnail
                m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html)
                if not m: m = re.search(r'og:image[^>]+content="([^"]+)"', html)
                thumb = m.group(1).replace("&amp;", "&") if m else ""
                # Extract description
                m = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', html)
                if not m: m = re.search(r'og:description[^>]+content="([^"]+)"', html)
                desc_raw = m.group(1).replace("&amp;", "&") if m else ""
                # Clean description: strip like counts
                desc = re.sub(r'^\d[\d,]* [Ll]ikes?.*?on Instagram: "?', '', desc_raw).strip().strip('"')
                # Extract title from og:title or URL
                m = re.search(r'<title>([^<]+)</title>', html)
                title_raw = m.group(1) if m else "New Post"
                # Clean title
                title = re.sub(r' on Instagram:.*$', '', title_raw).strip()
                if len(title) > 100: title = title[:97] + "..."
                # Generate comments
                comments = generate_comments(desc, title)
                # Insert into DB
                c = db()
                sheet = re.sub(r'[^a-zA-Z0-9_]', '_', title)[:50]
                cur = c.execute("INSERT INTO posts(sheet, source_url, title, thumbnail_url, description) VALUES(?,?,?,?,?)",
                    (sheet, url, title, thumb, desc[:2000]))
                pid = cur.lastrowid
                for cmt in comments:
                    c.execute("INSERT OR IGNORE INTO comments(post_id, body) VALUES(?,?)", (pid, cmt))
                c.commit()
                row = dict(c.execute("SELECT p.*, COUNT(cm.id) AS count FROM posts p LEFT JOIN comments cm ON cm.post_id = p.id WHERE p.id = ? GROUP BY p.id", (pid,)).fetchone())
                c.close()
                return send(self, 200, json.dumps({"post": row, "comments_generated": len(comments)}))
            u = current_user(self)
            if not u: return send(self, 401, json.dumps({"error": "Not signed in"}))
            d = json_body(self); c = db()
            if path == "/api/assign":
                pid = int(d["post_id"])
                if c.execute(
                    "SELECT 1 FROM assignments a JOIN comments cm ON cm.id = a.comment_id WHERE a.user_id = ? AND cm.post_id = ? LIMIT 1",
                    (u["id"], pid),
                ).fetchone():
                    return send(self, 409, json.dumps({"error": "Kamu sudah ambil 1 komentar untuk post ini"}))
                rows = c.execute(
                    """SELECT cm.id FROM comments cm
                       WHERE cm.post_id = ?
                         AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.comment_id = cm.id AND a.user_id = ?)
                       ORDER BY RANDOM() LIMIT 1""",
                    (pid, u["id"]),
                ).fetchall()
                for r in rows:
                    c.execute("INSERT OR IGNORE INTO assignments(user_id, comment_id) VALUES(?,?)", (u["id"], r["id"]))
                c.commit(); c.close()
                return send(self, 200, json.dumps({"assigned": len(rows)}))
            if path == "/api/copy":
                aid = int(d["assignment_id"])
                r = c.execute(
                    "SELECT a.id, cm.body FROM assignments a JOIN comments cm ON cm.id = a.comment_id WHERE a.id = ? AND a.user_id = ?",
                    (aid, u["id"]),
                ).fetchone()
                if not r: return send(self, 404, json.dumps({"error": "Assignment not found"}))
                c.execute("UPDATE assignments SET status = 'copied', copied_at = CURRENT_TIMESTAMP WHERE id = ?", (aid,))
                c.commit(); c.close()
                return send(self, 200, json.dumps({"body": r["body"]}))
            return send(self, 404, json.dumps({"error": "Not found"}))
        except Exception as e:
            return send(self, 400, json.dumps({"error": str(e)}))


if __name__ == "__main__":
    init_db()
    print("Fetching Instagram thumbnails for posts…")
    try:
        fetch_thumbnails()
    except Exception as e:
        print(f"  thumbnail fetch error (non-fatal): {e}")
    print(f"Comment Desk ➜  http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
