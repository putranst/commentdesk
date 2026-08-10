#!/usr/bin/env python3
"""Convert data.json + template into a self-contained static index.html."""
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent
DATA = json.loads((ROOT / "data.json").read_text())
JSON_EMBED = json.dumps(DATA, ensure_ascii=False)

# Load the static UI template from file
UI = (ROOT / "new-ui.html").read_text()

# Replace API-based JS with localStorage-based JS for static deployment
OLD_JS = """async function api(path,opt={}){const r=await fetch(path,{headers:{"Content-Type":"application/json"},...opt});const d=await r.json();if(!r.ok)throw Error(d.error||"Request failed");return d}

async function boot(){
  try{const m=await api("/api/me");
    if(m.user){$("auth-v").style.display="none";$("app-v").classList.add("active");$("hi").textContent="Halo, "+m.user.handle;S.posts=m.posts;
    for(const p of S.posts){try{const a=await api("/api/assignments?post_id="+p.id);if(a.items.some(x=>x.status==="copied"))S.done.add(p.id)}catch(_){}}
    renderCarousel();renderStats()}
  }catch(_){}}

function renderCarousel(){
  const track=$("carousel-track"),cw=window.innerWidth<600?300:window.innerWidth<860?320:340;
  track.innerHTML=S.posts.map(p=>{const isDone=S.done.has(p.id);return '<div class="ccard'+(isDone?' done':'')+'" id="cc-'+p.id+'" onclick="openModal('+p.id+')"><img src="'+esc(p.thumbnail)+'" alt="'+esc(p.title)+'" loading="lazy" onerror="this.style.display=\\'none\\';this.insertAdjacentHTML(\\'afterend\\',\\'<div style=height:62%;background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:36px>📷</div>\\')"><div class="card-body"><div class="card-title">'+esc(p.title)+'</div><div class="card-meta">'+p.count+' komentar'+(isDone?' • ✓':'')+'</div></div></div>'}).join("");
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
boot();"""

NEW_JS = """var POSTS=__POSTS_JSON__;
(function(){
var $=function(id){return document.getElementById(id)};
var LS=function(k,v){try{return v===void 0?JSON.parse(localStorage.getItem('cd_'+k)):localStorage.setItem('cd_'+k,JSON.stringify(v))}catch(e){return v===void 0?null:void 0}};
var S={user:null,post:null,has:false,done:new Set()},ALL=LS('all')||{};
function boot(){S.user=LS('user');if(S.user){$('auth-v').style.display='none';$('app-v').classList.add('active');$('hi').textContent='Halo, '+S.user;POSTS.forEach(function(p){var key=S.user+'_'+p.id;if(ALL[key+'_copied'])S.done.add(p.id)});renderCarousel();renderStats()}}
function renderCarousel(){var track=$('carousel-track'),cw=window.innerWidth<600?300:window.innerWidth<860?320:340;track.innerHTML=POSTS.map(function(p){var isDone=S.done.has(p.id);return '<div class=\"ccard'+(isDone?' done':'')+'\" id=\"cc-'+p.id+'\" onclick=\"openModal('+p.id+')\"><img src=\"'+esc(p.thumb||'')+'\" alt=\"'+esc(p.title)+'\" loading=\"lazy\" onerror=\"this.style.display=\\'none\\';this.insertAdjacentHTML(\\'afterend\\',\\'<div style=height:62%;background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:36px>📷</div>\\')\"><div class=\"card-body\"><div class=\"card-title\">'+esc(p.title)+'</div><div class=\"card-meta\">'+p.comments.length+' komentar'+(isDone?' • ✓':'')+'</div></div></div>'}).join('');var vp=$('carousel-vp');vp.onscroll=function(){updateActive()};if(POSTS.length>2){var mid=Math.floor(POSTS.length/2);setTimeout(function(){vp.scrollTo({left:mid*(cw+16),behavior:'smooth'})},400)}setTimeout(updateActive,600)}
function updateActive(){var cards=document.querySelectorAll('.ccard'),vp=$('carousel-vp'),vpr=vp.getBoundingClientRect(),best=null,bestDist=Infinity;cards.forEach(function(c){var cr=c.getBoundingClientRect(),ccx=cr.left+cr.width/2,vcx=vpr.left+vpr.width/2,dist=Math.abs(ccx-vcx);c.classList.remove('active');if(dist<bestDist){bestDist=dist;best=c}});if(best)best.classList.add('active')}
function renderStats(){var t=POSTS.length,d=S.done.size,r=t-d;$('stats-bar').innerHTML='<div class=\"stat-item\"><div class=\"val\">'+t+'</div><div class=\"lbl\">Total Post</div></div><div class=\"stat-item\"><div class=\"val\">'+d+'</div><div class=\"lbl\">Selesai</div></div><div class=\"stat-item\"><div class=\"val\">'+r+'</div><div class=\"lbl\">Tersisa</div></div>'}
function openModal(id){S.post=POSTS.find(function(x){return x.id===id});if(!S.post)return;$('modal-title').textContent=S.post.title;$('modal-img').src=S.post.thumb||'';$('modal-img').onerror=function(){this.style.display='none'};$('modal-url').textContent=S.post.source_url||'';$('modal-desc').textContent=S.post.description||'';$('modal-overlay').classList.add('open');document.body.style.overflow='hidden';reloadModalCmt()}
function reloadModalCmt(){var key=S.user+'_'+S.post.id,assign=ALL[key];S.has=!!(assign!==void 0);var btn=$('btn-ambil');btn.disabled=S.has;btn.textContent=S.has?'✓ Sudah Ambil 1 Komentar':'🎲 Ambil & Copy Komentar';if(assign!==void 0){var c=S.post.comments[assign],copied=ALL[key+'_copied'];$('modal-cmt').innerHTML='<div class=\"cmt-card\"><p class=\"cmt-body\">'+esc(c)+'</p><span class=\"cmt-tag '+(copied?'copied':'assigned')+'\">'+(copied?'✓ Sudah di-copy':'📋 Baru di-assign')+'</span><button class=\"btn-copy'+(copied?' done':'')+'\" onclick=\"doCopy(this)\">'+(copied?'Tersalin ✓':'Copy ke Clipboard')+'</button></div>'}else{$('modal-cmt').innerHTML='<div class=\"empty-msg\">Klik tombol di bawah untuk dapat satu komentar acak ✨</div>'}}
function closeModal(){$('modal-overlay').classList.remove('open');document.body.style.overflow='';S.done.forEach(function(pid){var c=document.getElementById('cc-'+pid);if(c)c.classList.add('done')});renderStats()}
function doAssign(){if(S.has||!S.post)return;var pool=[];for(var i=0;i<S.post.comments.length;i++)pool.push(i);for(var j=pool.length-1;j>0;j--){var k=Math.floor(Math.random()*(j+1));var t=pool[j];pool[j]=pool[k];pool[k]=t}ALL[S.user+'_'+S.post.id]=pool[0];LS('all',ALL);reloadModalCmt()}
function doCopy(btn){var assign=ALL[S.user+'_'+S.post.id];if(assign===void 0)return;var text=S.post.comments[assign];try{navigator.clipboard.writeText(text)}catch(_){}ALL[S.user+'_'+S.post.id+'_copied']=true;LS('all',ALL);S.done.add(S.post.id);var card=document.getElementById('cc-'+S.post.id);if(card)card.classList.add('done');renderStats();btn.textContent='Tersalin ✓';btn.classList.add('done');setTimeout(function(){btn.textContent='Copy ke Clipboard';btn.classList.remove('done')},2000);reloadModalCmt()}
function doLogin(){var h=$('hinp').value.trim();if(!h)return $('auth-err').textContent='Isi handle dulu ya';if(h.length>40)return $('auth-err').textContent='Maks 40 karakter';LS('user',h);location.reload()}
function doLogout(){LS('user',null);location.reload()}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
window.openModal=openModal;window.doAssign=doAssign;window.doCopy=doCopy;window.doLogin=doLogin;window.doLogout=doLogout;window.closeModal=closeModal;
boot();
})();"""

# Replace JS
assert OLD_JS in UI, "OLD_JS not found in template"
STATIC_UI = UI.replace(OLD_JS, NEW_JS)

# Build HTML
HTML = STATIC_UI.replace("__POSTS_JSON__", JSON_EMBED)
(ROOT / "index.html").write_text(HTML, encoding="utf-8")
print(f"Generated index.html ({len(HTML)} bytes)")
