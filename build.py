#!/usr/bin/env python3
"""Convert data.json + template into a self-contained static index.html."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent
DATA = json.loads((ROOT / "data.json").read_text())
JSON_EMBED = json.dumps(DATA, ensure_ascii=False)

HTML_TEMPLATE = r'''<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>Comment Desk</title>
<style>
:root{--bg:#f3f4f6;--surface:#fff;--ink:#111827;--muted:#6b7280;--brand:#4f46e5;--line:#e5e7eb;--radius:18px;--shadow:0 2px 20px rgba(0,0,0,.05)}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:400 15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased;min-height:100vh}
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
.app-v{display:none}
.app-v.active{display:flex;flex-direction:column}
.toolbar{background:#0f172a;color:#fff;padding:14px 22px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:20}
.toolbar h1{font-size:17px;margin:0;font-weight:800;letter-spacing:-.2px}
.toolbar .hi{font-size:13px;opacity:.75}
.content-v{padding:20px;max-width:960px;margin:0 auto;width:100%;display:flex;flex-direction:column;gap:22px}
.sec-label{font-size:14px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin:0}
.pgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.pcard{background:var(--surface);border-radius:14px;overflow:hidden;border:2.5px solid transparent;cursor:pointer;box-shadow:0 1px 8px rgba(0,0,0,.03);transition:border-color .2s,box-shadow .2s,transform .15s}
.pcard:active{transform:scale(.97)}
.pcard.sel{border-color:var(--brand);box-shadow:0 0 0 5px rgba(79,70,229,.12)}
.pcard img{width:100%;aspect-ratio:1;object-fit:cover;display:block;background:#e5e7eb}
.pcard .lbl{padding:10px 12px 4px;font-size:13px;font-weight:700;line-height:1.3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pcard .cnt{font-size:12px;color:var(--muted);padding:0 12px 10px}
.hero-block{background:var(--surface);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow);display:none;border:2px solid var(--line)}
.hero-block.on{display:block;border-color:var(--brand)}
.hero-block img{width:100%;max-height:360px;object-fit:cover;display:block}
.hero-info{padding:18px 22px}
.hero-info h2{margin:0 0 4px;font-size:20px;font-weight:800;letter-spacing:-.3px}
.hero-info .url-line{font-size:12px;color:var(--muted);word-break:break-all}
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
var POSTS = __POSTS_JSON__;

(function(){
var S={user:null,post:null,has:false};
var $=function(id){return document.getElementById(id)};
var LS=function(k,v){try{return v===void 0?JSON.parse(localStorage.getItem('cd_'+k)):localStorage.setItem('cd_'+k,JSON.stringify(v))}catch(e){return v===void 0?null:void 0}};
var ALL=LS('all')||{};

function boot(){
  S.user=LS('user');
  if(S.user){
    $('auth-v').style.display='none';
    $('app-v').classList.add('active');
    $('hi').textContent='Halo, '+S.user;
    renderGrid();
  }
}

function renderGrid(){
  $('pgrid').innerHTML=POSTS.map(function(p){
    return '<div class="pcard" id="pc-'+p.id+'" onclick="sel('+p.id+')"><img src="'+esc(p.thumb||'')+'" alt="'+esc(p.title)+'" loading="lazy" onerror="this.style.display=\'none\';this.insertAdjacentHTML(\'afterend\',\'<div style=width:100%;aspect-ratio:1;background:#e5e7eb;display:flex;align-items:center;justify-content:center;color:#9ca3af;font-size:24px;border-radius:14px>\u{1f4f7}</div>\')"><div class="lbl">'+esc(p.title)+'</div><div class="cnt">'+p.comments.length+' komentar</div></div>';
  }).join('');
}

function sel(id){
  S.post=POSTS.find(function(x){return x.id===id});
  if(!S.post)return;
  var cards=document.querySelectorAll('.pcard');
  for(var i=0;i<cards.length;i++)cards[i].classList.remove('sel');
  $('pc-'+id).classList.add('sel');
  var h=$('hero');
  h.classList.add('on');
  h.innerHTML='<img src="'+esc(S.post.thumb||'')+'" alt="'+esc(S.post.title)+'" onerror="this.style.display=\'none\'"><div class="hero-info"><h2>'+esc(S.post.title)+'</h2><div class="url-line">'+esc(S.post.source_url)+'</div></div>';
  setTimeout(function(){h.scrollIntoView({behavior:'smooth',block:'start'})},100);
  reloadCmt();
}

function reloadCmt(){
  $('cmts').classList.add('on');
  var key=S.user+'_'+S.post.id;
  var assign=ALL[key];
  S.has=!!assign;
  $('abtn').disabled=S.has;
  $('abtn').textContent=S.has?'\u2713 Sudah ambil 1 komentar':'\u{1f3b2} Ambil 1 komentar acak';
  if(assign){
    var c=S.post.comments[assign];
    $('clist').innerHTML='<div class="cmt-card"><p class="body">'+esc(c)+'</p><span class="tag">'+(assign!==null&&ALL[key+'_copied']?'\u2713 di-copy':'baru')+'</span><button class="cpy" onclick="doCopy(this)">Copy</button></div>';
  } else {
    $('clist').innerHTML='<div class="empty-msg">Klik tombol di atas buat dapat satu komentar acak \u2728</div>';
  }
}

function doAssign(){
  if(S.has)return;
  // user can only get one random comment per post
  var pool=[];
  for(var i=0;i<S.post.comments.length;i++){ pool.push(i) }
  // shuffle
  for(var j=pool.length-1;j>0;j--){var k=Math.floor(Math.random()*(j+1));var t=pool[j];pool[j]=pool[k];pool[k]=t}
  var pick=pool[0];
  ALL[S.user+'_'+S.post.id]=pick;
  LS('all',ALL);
  reloadCmt();
}

function doCopy(btn){
  var assign=ALL[S.user+'_'+S.post.id];
  if(assign===void 0)return;
  var text=S.post.comments[assign];
  try{navigator.clipboard.writeText(text).then(function(){
    ALL[S.user+'_'+S.post.id+'_copied']=true;
    LS('all',ALL);
    btn.textContent='Tersalin \u2713';
    btn.classList.add('done');
    setTimeout(function(){btn.textContent='Copy';btn.classList.remove('done')},1800);
    reloadCmt();
  })}catch(e){}
}

function doLogin(){
  var h=$('hinp').value.trim();
  if(!h)return $('auth-err').textContent='Isi handle dulu ya';
  if(h.length>40)return $('auth-err').textContent='Maks 40 karakter';
  S.user=h;
  LS('user',h);
  $('auth-v').style.display='none';
  $('app-v').classList.add('active');
  $('hi').textContent='Halo, '+h;
  renderGrid();
}

function doLogout(){
  LS('user',null);
  location.reload();
}

function esc(s){
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

window.sel=sel;
window.doAssign=doAssign;
window.doCopy=doCopy;
window.doLogin=doLogin;
window.doLogout=doLogout;
boot();
})();
</script></body></html>'''

HTML = HTML_TEMPLATE.replace('__POSTS_JSON__', JSON_EMBED)
(ROOT / "index.html").write_text(HTML, encoding="utf-8")
print(f"Generated index.html ({len(HTML)} bytes)")
