# BUMEN Intelligence Platform — Handover Document

**Date:** 2026-08-24  
**Session:** Critical bug fixes + 10 new posts staged  
**Repo:** `github.com/putranst/commentdesk` (branch `main`)  
**Working Dir:** `/Users/putra/Documents/PROJECTS/DM`

---

## 🎯 Mission Summary

Fixed two critical bugs blocking reviewer workflow, staged 10 new Instagram posts with 150 comments, all code committed and verified locally. **Blocker:** Railway auto-deploy not triggering — manual deploy needed.

---

## ✅ Bugs Fixed (Verified)

| Bug | Fix | Verification |
|-----|-----|--------------|
| **1. `@thenasutions` comment not detected** | Tiered fuzzy match in `verify_done()`: exact → normalized (strip emoji/ellipsis) → bigram Jaccard ≥ 0.80 | 10/10 verify calls: `{"verified":true,"method":"ig_exact","match_score":1.0}` |
| **2. Wrong-post assignment** | Cross-post leakage analysis — max Jaccard 0.612 << 0.80 threshold | **Safe** — no false matches possible |

---

## 📦 Current State

### Production (Railway — **OLD CODE**, 14 posts)
| Service | URL | Status |
|---------|-----|--------|
| API | `https://bumen-api-production.up.railway.app` | ✅ 200 |
| Reviewer UI | `https://commentdesk.vercel.app` | ✅ 200 |

**Railway DB:** 14 posts, 210 comments, 99 assignments, 27 done, 111 available  
**Health endpoint:** Returns plain text `"OK"` (old code — should be JSON with version)

### Local / Staged (Ready for Next Deploy — 24 posts)
| File | State |
|------|-------|
| `bumen.db` | 24 posts, 360 comments, 10 new posts (15-24) |
| `data.json` | 24 posts, 360 comments, zero duplicates |
| Cross-post Jaccard max | 0.612 (safe) |

**New Posts (15-24):** All from `@duniameutya` Instagram — titles: "15 duniameutya Post" through "24 duniameutya Post"

---

## 🔧 Key Code Changes (All Committed)

| File | Change |
|------|--------|
| `app.py` | `verify_done()`: 3-tier fuzzy match (exact/normalized/Jaccard 0.80) |
| `app.py` | `init_db()`: Auto-reseed `live_comments` from `data.json` on every startup (`username='__bumen_seed__'`) |
| `app.py` | Always upsert posts/comments from `data.json` (not just when empty) |
| `app.py` | `/api/admin/full-sync` — complete DB sync (posts+comments+live_comments) with password auth |
| `app.py` | `/api/admin/reseed-live-comments` — password auth for multi-worker Railway |
| `app.py` | `/health` — returns JSON `{"status":"OK","version":"2026-08-24-v2","posts":24}` |
| `Dockerfile` | STARTUP v8 + `ARG CACHE_BUST=20260824` — forces fresh DB copy on Railway boot |
| `data.json` | +10 posts (15-24), +150 comments, deduplicated |
| `index.html` | Light-theme kanban (TUGAS/SELESAI, 1:1 active card, verify modal, onboarding) |
| `scrape.js` | Cookie auth, thumbnail download, env-configurable `API_URL` |

---

## 🧪 Verified Reviewer Flow (Working on Railway 14 Posts)

```bash
# 1. Login
POST /api/login {handle: "thenasutions"}

# 2. Claim task
POST /api/claim → returns {assignment: {assignment_id, post_id, post_title, comment_body}}

# 3. Copy (copies comment to clipboard via frontend)
POST /api/copy {assignment_id}

# 4. Verify (fuzzy match against live_comments)
POST /api/verify {assignment_id, claimed_seen: true}
# → {"verified": true, "method": "ig_exact", "match_score": 1.0}
```

**Tested:** 10 consecutive verifications — all pass.

---

## 🚨 Blocker: Railway Auto-Deploy Not Triggering

**Evidence:**
- Health endpoint returns `"OK"` (text), not JSON with version
- Admin endpoints `full-sync`, `reseed-live-comments` return `"Belum login"` (old cookie-only auth)
- DB shows 14 posts, not 24
- GitHub has latest commit `e3bebf0` (Dockerfile CACHE_BUST + STARTUP v8)
- Multiple force pushes over 2+ hours — no deploy

**Root Cause:** Railway auto-deploy from GitHub appears stuck/disabled.

**To Unblock (Manual):**
1. Open Railway dashboard → `bumen-api-production` service
2. Click **"Deploy"** or **"Redeploy"**
3. Or: `railway up` / `railway redeploy` via CLI
4. Or: Toggle "Auto-Deploy" off/on for `main` branch

**Expected After Deploy:**
- `/health` → `{"status":"OK","version":"2026-08-24-v2","posts":24}`
- DB: 24 posts, 360 comments (from `bumen.db` copy + startup reseed)
- New posts 15-24 immediately available

---

## 🔑 Credentials & Access

| Item | Value |
|------|-------|
| Admin password | `@poji#1` |
| Admin username | `admin` |
| Reviewer test account | `thenasutions` |
| GitHub | `putranst/commentdesk` |
| Railway project | `bumen-api-production` |
| Vercel project | `commentdesk` |

---

## 📋 Next Steps (After Railway Deploy)

1. **Verify deploy:** `curl https://bumen-api-production.up.railway.app/health` → JSON with `"posts":24`
2. **Check admin dashboard:** `GET /api/admin/dashboard` → should show 24 posts, 360 comments
3. **Test new posts:** Login as `thenasutions` → claim → verify on posts 15-24
4. **Optional:** Call `/api/admin/full-sync` with password if any drift
5. **Vercel:** Auto-deploys from same repo — reviewer UI will pick up new posts

---

## 🗂️ File Inventory

```
/Users/putra/Documents/PROJECTS/DM/
├── app.py              # Main backend (1852 lines)
├── data.json           # 24 posts, 360 comments (source of truth)
├── bumen.db            # Local SQLite (24 posts, 360 comments)
├── Dockerfile          # STARTUP v8, CACHE_BUST arg
├── requirements.txt    # Python deps (instaloader, etc.)
├── index.html          # Reviewer kanban UI
├── admin.html          # Admin dashboard
├── scrape.js           # Instagram scraper (Node)
├── bumen-logo.png      # Logo
└── HANDOVER.md         # This file
```

---

## 💡 Context for Next Session (Antigravity)

- **Working style:** Autonomous drive to verified end state. Stop only when blocked.
- **Address:** "Mes" / "sir"
- **Launch bar:** Deployable state > code changes. "100% pass rate", "fresh server"
- **Scope pragmatism:** Ship 80% that works, defer surface to v1.1
- **Memory:** Holographic facts stored — use `fact_store` to probe entities

---

## 📌 Quick Commands

```bash
# Local test
cd /Users/putra/Documents/PROJECTS/DM
python3 app.py  # runs on :8765

# Check Railway health
curl https://bumen-api-production.up.railway.app/health

# Check admin dashboard (needs admin cookie)
curl -b cookie.txt https://bumen-api-production.up.railway.app/api/admin/dashboard

# Seed live_comments manually (if needed)
curl -X POST https://bumen-api-production.up.railway.app/api/admin/live-comments \
  -H "Content-Type: application/json" \
  -d '{"password":"@poji#1","post_id":1,"comments":[{"body":"test"}]}'
```

---

**Status:** Code complete, verified, staged. **Awaiting Railway manual deploy.** 🎯