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
ADMIN_PASSWORD = "suluh2026"
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
        thumbnail_url TEXT DEFAULT ''
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
    c.close()

    # Migrate: add assigned_at if missing
    try:
        c = db()
        c.execute("ALTER TABLE assignments ADD COLUMN assigned_at TEXT DEFAULT CURRENT_TIMESTAMP")
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
HTML = r'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>Comment Desk</title>
<style>
:root{--bg:#f3f4f6;--surface:#fff;--ink:#111827;--muted:#6b7280;--brand:#4f46e5;--line:#e5e7eb;--radius:18px;--shadow:0 2px 20px rgba(0,0,0,.05)}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:400 15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased;min-height:100vh}
/* ------ auth ------ */
.auth-pg{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.auth-box{background:var(--surface);border-radius:var(--radius);box-shadow:var(--shadow);padding:36px 28px;max-width:440px;width:100%;text-align:center}
.auth-box .icon{font-size:42px;margin-bottom:6px}
.auth-box h1{font-size:26px;margin:0 0 4px;font-weight:800;letter-spacing:-.3px}
.auth-box .tag{color:var(--muted);font-size:14px;margin-bottom:22px}
.alert{background:#eef2ff;border-radius:12px;padding:14px 16px;color:#4338ca;font-size:13px;line-height:1.5;margin-bottom:22px;text-align:left}
input{font:inherit;width:100%;padding:13px 16px;border:2px solid var(--line);border-radius:14px;outline:none;margin-bottom:14px;transition:border-color .2s;font-size:15px}
input:focus{border-color:var(--brand);box-shadow:0 0 0 4px rgba(79,70,229,.08)}
.btn{font:inherit;font-weight:700;cursor:pointer;transition:all .15s;border:none}
.btn-primary{width:100%;padding:14px;background:var(--brand);color:#fff;border-radius:14px;font-size:15px;letter-spacing:-.2px}
.btn-primary:active{transform:scale(.98);opacity:.92}
.btn-ghost{background:0;color:var(--muted);padding:8px 12px;font-size:13px}
.btn-ghost:hover{color:var(--ink)}
.err-text{color:#ef4444;font-size:13px;margin-top:6px}
/* ------ workspace ------ */
.app-v{display:none}
.app-v.active{display:flex;flex-direction:column}
.toolbar{background:#0f172a;color:#fff;padding:14px 22px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:20}
.toolbar h1{font-size:17px;margin:0;font-weight:800;letter-spacing:-.2px}
.toolbar .hi{font-size:13px;opacity:.75}
.content-v{padding:20px;max-width:960px;margin:0 auto;width:100%;display:flex;flex-direction:column;gap:22px}
/* post grid */
.sec-label{font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin:0}
.pgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.pcard{background:var(--surface);border-radius:14px;overflow:hidden;border:2.5px solid transparent;cursor:pointer;box-shadow:0 1px 8px rgba(0,0,0,.03);transition:border-color .2s,box-shadow .2s,transform .15s}
.pcard:active{transform:scale(.97)}
.pcard.sel{border-color:var(--brand);box-shadow:0 0 0 5px rgba(79,70,229,.12)}
.pcard img{width:100%;aspect-ratio:1;object-fit:cover;display:block;background:#e5e7eb}
.pcard .lbl{padding:10px 12px 4px;font-size:13px;font-weight:700;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pcard .cnt{font-size:12px;color:var(--muted);padding:0 12px 10px}
/* hero */
.hero-block{background:var(--surface);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow);display:none;border:2px solid var(--line)}
.hero-block.on{display:block;border-color:var(--brand)}
.hero-block img{width:100%;max-height:360px;object-fit:cover;display:block}
.hero-info{padding:18px 22px}
.hero-info h2{margin:0 0 4px;font-size:20px;font-weight:800;letter-spacing:-.3px}
.hero-info .url-line{font-size:12px;color:var(--muted);word-break:break-all}
/* comment area */
.cmts{display:none}
.cmts.on{display:block}
.cmts-head{background:var(--surface);border-radius:var(--radius);padding:18px 22px;box-shadow:var(--shadow);border:2px solid var(--line);margin-bottom:12px}
.cmts-head .rule{font-size:14px;color:var(--muted);margin:0 0 14px;line-height:1.6}
.cmts-head .rule b{color:var(--brand);font-weight:700}
.cmts-btn-row{display:flex;gap:10px;flex-wrap:wrap}
.cmts-btn-row .btn-primary{width:auto;padding:12px 22px}
.cmts-btn-row .btn-primary:disabled{opacity:.45;cursor:not-allowed;background:#9ca3af}
.cmts-list{display:flex;flex-direction:column;gap:10px}
.cmt-card{background:var(--surface);border-radius:14px;padding:16px 18px;display:flex;align-items:flex-start;gap:14px;box-shadow:0 1px 10px rgba(0,0,0,.03);border:1.5px solid var(--line);transition:border-color .2s}
.cmt-card:hover{border-color:var(--brand)}
.cmt-card .body{padding:0;margin:0;flex:1;font-size:14px;line-height:1.7}
.cmt-card .tag{font-size:11px;background:#eef2ff;color:#4338ca;padding:5px 11px;border-radius:99px;white-space:nowrap;font-weight:600}
.cmt-card .cpy{padding:10px 18px;font-size:13px;background:var(--brand);color:#fff;border:none;border-radius:10px;font-weight:700;cursor:pointer;transition:all .15s}
.cmt-card .cpy:active{transform:scale(.95)}
.cpy.done{background:#059669 !important}
.empty-msg{padding:28px;text-align:center;color:var(--muted);font-size:14px;background:var(--surface);border-radius:var(--radius);border:2px dashed var(--line)}
@media(min-width:600px){.pgrid{grid-template-columns:repeat(3,1fr);gap:12px}}
@media(min-width:860px){.pgrid{grid-template-columns:repeat(4,1fr);gap:14px}}
</style></head><body>
<div id="auth-v" class="auth-pg">
  <div class="auth-box">
    <div class="icon">💬</div>
    <h1>Comment Desk</h1>
    <p class="tag">Masuk pakai handle tim internal</p>
    <div class="alert">Komentar cuma buat di-review & di-copy. Nggak ada yang diposting otomatis ke Instagram.</div>
    <input id="hinp" maxlength="40" placeholder="contoh: reviewer-01" autocomplete="off" enterkeyhint="go">
    <button class="btn btn-primary" onclick="doLogin()">Masuk ke Workspace</button>
    <p id="auth-err" class="err-text"></p>
  </div>
</div>
<div id="app-v" class="app-v">
  <div class="toolbar">
    <h1>💬 Comment Desk</h1>
    <span class="hi" id="hi"></span>
    <button class="btn btn-ghost" onclick="doLogout()">Keluar</button>
  </div>
  <div class="content-v">
    <section>
      <p class="sec-label">Pilih Postingan</p>
      <div id="pgrid" class="pgrid"></div>
    </section>
    <section id="hero" class="hero-block"></section>
    <section id="cmts" class="cmts">
      <div class="cmts-head">
        <p class="rule">📋 Kamu cuma bisa ambil <b>1 komentar</b> per post. Pilih post → ambil komentar → copy → lanjut ke post lain.</p>
        <div class="cmts-btn-row">
          <button class="btn btn-primary" id="abtn" onclick="doAssign()">🎲 Ambil 1 komentar acak</button>
        </div>
      </div>
      <div id="clist" class="cmts-list"></div>
    </section>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
const S={posts:[],post:null,has:false};

async function api(path,opt={}){const r=await fetch(path,{headers:{"Content-Type":"application/json"},...opt});const d=await r.json();if(!r.ok)throw Error(d.error||"Request failed");return d}

async function boot(){try{const m=await api("/api/me");if(m.user){$("auth-v").style.display="none";$("app-v").classList.add("active");$("hi").textContent="Halo, "+m.user.handle;S.posts=m.posts;renderGrid()}}catch(_){}}

function renderGrid(){$("pgrid").innerHTML=S.posts.map(p=>`<div class="pcard" id="pc-${p.id}" onclick="select(${p.id})"><img src="${esc(p.thumbnail)}" alt="${esc(p.title)}" loading="lazy" onerror="this.style.display='none';this.insertAdjacentHTML('afterend','<div style=width:100%;aspect-ratio:1;background:#e5e7eb;display:flex;align-items:center;justify-content:center;color:#9ca3af;font-size:24px;border-radius:14px>📷</div>')"><div class="lbl">${esc(p.title)}</div><div class="cnt">${p.count} komentar</div></div>`).join("")}

async function select(id){S.post=S.posts.find(x=>x.id===id);if(!S.post)return;document.querySelectorAll(".pcard").forEach(c=>c.classList.remove("sel"));$("pc-"+id).classList.add("sel");const h=$("hero");h.classList.add("on");h.innerHTML=`<img src="${esc(S.post.thumbnail)}" alt="${esc(S.post.title)}" onerror="this.style.display='none'"><div class="hero-info"><h2>${esc(S.post.title)}</h2><div class="url-line">${esc(S.post.source_url)}</div></div>`;setTimeout(()=>h.scrollIntoView({behavior:"smooth",block:"start"}),120);await reloadCmt()}

async function reloadCmt(){$("cmts").classList.add("on");try{const d=await api("/api/assignments?post_id="+S.post.id);S.has=d.items.length>0;$("abtn").disabled=S.has;$("abtn").textContent=S.has?"✓ Sudah ambil 1 komentar":"🎲 Ambil 1 komentar acak";$("clist").innerHTML=d.items.length?d.items.map(x=>`<div class="cmt-card"><p class="body">${esc(x.body)}</p><span class="tag">${x.status==="copied"?"✓ di-copy":"baru"}</span><button class="cpy" onclick="doCopy(${x.id},this)">Copy</button></div>`).join(""):'<div class="empty-msg">Klik tombol di atas buat dapat satu komentar acak ✨</div>'}catch(e){$("clist").innerHTML='<div class="empty-msg">Gagal memuat. Coba refresh.</div>'}}

async function doAssign(){if(S.has)return;try{await api("/api/assign",{method:"POST",body:JSON.stringify({post_id:S.post.id})});await reloadCmt()}catch(e){alert(e.message)}}

async function doCopy(id,btn){try{const d=await api("/api/copy",{method:"POST",body:JSON.stringify({assignment_id:id})});await navigator.clipboard.writeText(d.body);btn.textContent="Tersalin ✓";btn.classList.add("done");setTimeout(()=>{btn.textContent="Copy";btn.classList.remove("done")},1800);await reloadCmt()}catch(_){}}

async function doLogin(){const h=$("hinp").value.trim();if(!h)return $("auth-err").textContent="Isi handle dulu ya";try{await api("/api/login",{method:"POST",body:JSON.stringify({handle:h})});location.reload()}catch(e){$("auth-err").textContent=e.message}}

async function doLogout(){await api("/api/logout",{method:"POST"});location.reload()}

function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
boot();
</script></body></html>'''

# ---------------------------------------------------------------------------
# Admin HTML
# ---------------------------------------------------------------------------
ADMIN_HTML = r'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>Admin — Comment Desk</title>
<style>
:root{--bg:#0f172a;--surface:#1e293b;--ink:#f1f5f9;--muted:#94a3b8;--brand:#6366f1;--line:#334155;--radius:16px;--shadow:0 4px 24px rgba(0,0,0,.3)}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:400 14px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;min-height:100vh;-webkit-font-smoothing:antialiased}
/* auth */
.auth-pg{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.auth-box{background:var(--surface);border-radius:var(--radius);box-shadow:var(--shadow);padding:40px 32px;max-width:420px;width:100%;text-align:center;border:1px solid var(--line)}
.auth-box .icon{font-size:48px;margin-bottom:8px}
.auth-box h1{font-size:24px;margin:0 0 4px;font-weight:800;letter-spacing:-.3px}
.auth-box .tag{color:var(--muted);font-size:13px;margin-bottom:24px}
input{font:inherit;width:100%;padding:13px 16px;border:2px solid var(--line);border-radius:12px;outline:none;margin-bottom:14px;transition:border-color .2s;font-size:14px;background:var(--bg);color:var(--ink)}
input:focus{border-color:var(--brand);box-shadow:0 0 0 4px rgba(99,102,241,.15)}
.btn{font:inherit;font-weight:700;cursor:pointer;transition:all .15s;border:none}
.btn-primary{width:100%;padding:14px;background:var(--brand);color:#fff;border-radius:12px;font-size:15px;letter-spacing:-.2px}
.btn-primary:hover{opacity:.9}
.btn-ghost{background:0;color:var(--muted);padding:8px 14px;font-size:13px;border-radius:8px}
.btn-ghost:hover{color:var(--ink);background:var(--line)}
.err{color:#f87171;font-size:13px;margin-top:8px}
/* dashboard */
.dash{display:none;padding:24px;max-width:1200px;margin:0 auto}
.dash.active{display:block}
.dash-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px;flex-wrap:wrap;gap:12px}
.dash-header h1{font-size:22px;margin:0;font-weight:800;letter-spacing:-.3px}
.dash-header .sub{color:var(--muted);font-size:13px}
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:28px}
.stat-card{background:var(--surface);border-radius:var(--radius);padding:22px 20px;border:1px solid var(--line);display:flex;flex-direction:column;gap:4px}
.stat-card .val{font-size:32px;font-weight:800;letter-spacing:-1px}
.stat-card .lbl{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.stat-card .pct{font-size:13px;color:var(--brand);margin-top:4px}
.section{margin-bottom:28px}
.section h3{font-size:15px;font-weight:700;margin:0 0 14px;display:flex;align-items:center;gap:8px}
table{width:100%;border-collapse:collapse;background:var(--surface);border-radius:var(--radius);overflow:hidden;border:1px solid var(--line)}
th,td{padding:12px 16px;text-align:left;font-size:13px}
th{background:var(--line);font-weight:700;text-transform:uppercase;letter-spacing:.4px;font-size:11px;color:var(--muted)}
td{border-top:1px solid var(--line)}
tr:hover td{background:rgba(99,102,241,.05)}
.badge{display:inline-block;padding:3px 10px;border-radius:99px;font-size:11px;font-weight:700}
.badge-ok{background:#065f4620;color:#34d399}
.badge-pend{background:#92400e20;color:#fbbf24}
.badge-new{background:#4338ca20;color:#818cf8}
.prog-bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden;min-width:60px}
.prog-fill{height:100%;background:var(--brand);border-radius:3px;transition:width .3s}
.loading{padding:40px;text-align:center;color:var(--muted)}
@media(min-width:700px){.stats{grid-template-columns:repeat(4,1fr)}}
</style></head><body>
<div id="auth-v" class="auth-pg">
  <div class="auth-box">
    <div class="icon">🛡️</div>
    <h1>Admin Panel</h1>
    <p class="tag">Comment Desk — Akses terbatas</p>
    <input id="apwd" type="password" placeholder="Password admin" autocomplete="off" enterkeyhint="go">
    <button class="btn btn-primary" onclick="doAdminLogin()">Masuk</button>
    <p id="auth-err" class="err"></p>
  </div>
</div>
<div id="dash-v" class="dash">
  <div class="dash-header">
    <div>
      <h1>📊 Comment Desk Dashboard</h1>
      <span class="sub" id="dash-time"></span>
    </div>
    <button class="btn btn-ghost" onclick="doAdminLogout()">↩ Logout</button>
  </div>
  <div class="stats" id="stats"></div>
  <div class="section">
    <h3>👥 Aktivitas per User</h3>
    <div id="utable"></div>
  </div>
  <div class="section">
    <h3>📋 Progress per Post</h3>
    <div id="ptable"></div>
  </div>
  <div class="section">
    <h3>🕐 Aktivitas Terbaru</h3>
    <div id="feed"></div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);

function fmtTime(ts){if(!ts)return"-";const d=new Date(ts+"Z");return d.toLocaleString("id-ID",{day:"numeric",month:"short",hour:"2-digit",minute:"2-digit"})}

async function api(path,opt={}){
  const r=await fetch(path,{headers:{"Content-Type":"application/json"},...opt});
  const d=await r.json();
  if(!r.ok)throw Error(d.error||"Request failed");
  return d
}

async function loadDash(){
  try{
    const d=await api("/api/admin/dashboard");
    renderStats(d);
    renderUsers(d.users);
    renderPosts(d.posts);
    renderFeed(d.feed);
  }catch(e){$("dash-v").innerHTML='<div class="loading">Gagal memuat dashboard: '+e.message+'</div>'}
}

function renderStats(d){
  const total=d.users.length;
  const assigned=d.users.reduce((s,u)=>s+u.assigned,0);
  const copied=d.users.reduce((s,u)=>s+u.copied,0);
  const postsDone=d.posts.filter(p=>p.users_assigned>=total).length;
  $("stats").innerHTML=`
    <div class="stat-card"><div class="val">${total}</div><div class="lbl">Total Reviewer</div></div>
    <div class="stat-card"><div class="val">${assigned}</div><div class="lbl">Komentar Di-assign</div><div class="pct">${copied} sudah di-copy</div></div>
    <div class="stat-card"><div class="val">${copied}</div><div class="lbl">Komentar Di-copy</div><div class="pct">${total?(copied/(assigned||1)*100).toFixed(0):0}% dari assigned</div></div>
    <div class="stat-card"><div class="val">${postsDone}/${d.posts.length}</div><div class="lbl">Post Tuntas</div><div class="pct">${(postsDone/d.posts.length*100).toFixed(0)}% selesai</div></div>`;
}

function renderUsers(users){
  if(!users.length){$("utable").innerHTML='<div class="loading">Belum ada user.</div>';return}
  $("utable").innerHTML='<table><thead><tr><th>Handle</th><th>Bergabung</th><th>Assigned</th><th>Di-copy</th><th>Progress</th></tr></thead><tbody>'
    +users.map(u=>`<tr><td><strong>${esc(u.handle)}</strong></td><td>${fmtTime(u.created_at)}</td><td>${u.assigned}</td><td>${u.copied}</td><td><div class="prog-bar"><div class="prog-fill" style="width:${u.assigned?(u.copied/u.assigned*100):0}%"></div></div> ${u.assigned?(u.copied/u.assigned*100).toFixed(0):0}%</td></tr>`).join("")
    +'</tbody></table>';
}

function renderPosts(posts){
  if(!posts.length){$("ptable").innerHTML='<div class="loading">Belum ada post.</div>';return}
  $("ptable").innerHTML='<table><thead><tr><th>Post</th><th>Komentar</th><th>User Assigned</th><th>User Copy</th><th>Progress</th></tr></thead><tbody>'
    +posts.map(p=>`<tr><td><strong>${esc(p.title)}</strong></td><td>${p.comment_count}</td><td>${p.users_assigned}</td><td>${p.users_copied}</td><td><div class="prog-bar"><div class="prog-fill" style="width:${p.users_assigned?(p.users_copied/p.users_assigned*100):0}%"></div></div> ${p.users_assigned?(p.users_copied/p.users_assigned*100).toFixed(0):0}%</td></tr>`).join("")
    +'</tbody></table>';
}

function renderFeed(feed){
  if(!feed.length){$("feed").innerHTML='<div class="loading">Belum ada aktivitas.</div>';return}
  $("feed").innerHTML='<table><thead><tr><th>Waktu</th><th>User</th><th>Post</th><th>Aksi</th></tr></thead><tbody>'
    +feed.map(f=>`<tr><td>${fmtTime(f.ts)}</td><td>${esc(f.handle)}</td><td>${esc(f.post_title)}</td><td><span class="badge badge-${f.action==='copied'?'ok':'new'}">${f.action==='copied'?'✓ Copy':'📋 Assign'}</span></td></tr>`).join("")
    +'</tbody></table>';
}

async function doAdminLogin(){
  const pwd=$("apwd").value.trim();
  if(!pwd)return $("auth-err").textContent="Masukkan password";
  try{
    await api("/api/admin/login",{method:"POST",body:JSON.stringify({password:pwd})});
    $("auth-v").style.display="none";$("dash-v").classList.add("active");
    $("dash-time").textContent="Data real-time • "+new Date().toLocaleString("id-ID");
    loadDash();
  }catch(e){$("auth-err").textContent=e.message}
}

async function boot(){
  try{await api("/api/admin/me");$("auth-v").style.display="none";$("dash-v").classList.add("active");$("dash-time").textContent="Data real-time • "+new Date().toLocaleString("id-ID");loadDash()}catch(_){}
}

async function doAdminLogout(){
  try{await api("/api/admin/logout",{method:"POST"});location.reload()}catch(_){location.reload()}
}

function esc(s){return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
boot();
</script></body></html>'''

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
                    "SELECT p.id, p.title, p.source_url, p.thumbnail_url, COUNT(cm.id) AS count FROM posts p LEFT JOIN comments cm ON cm.post_id = p.id GROUP BY p.id ORDER BY p.id"
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
