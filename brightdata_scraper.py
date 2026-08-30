#!/usr/bin/env python3
"""
BUMEN Bright Data Scraper Module
Discovers Instagram posts from a profile, scrapes all comments, and ingests
into the BUMEN SQLite database with full metadata.

Replaces the fragile Puppeteer/instaloader approach with clean server-side API calls.

Usage (standalone test):
    python brightdata_scraper.py

Usage (from app.py):
    from brightdata_scraper import discover_posts, scrape_comments, ingest_posts, ingest_comments, run_full_scrape
"""

import sqlite3
import json
import os
import re
import time
import requests
from pathlib import Path
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("BRIGHTDATA_API_KEY", "")
BASE_URL = "https://api.brightdata.com/datasets/v3"

DATASET_IDS = {
    "profiles": "gd_l1vikfch901nx3by4",
    "posts": "gd_lk5ns7kz21pck8jpis",
    "reels": "gd_lyclm20il4r5helnj",
    "comments": "gd_ltppn085pokosxh13",
}

# Default profile to scrape
DEFAULT_PROFILE = "duniameutya"

# Sync request timeout (Bright Data can take 60-90s on first requests)
SYNC_TIMEOUT = 120
# Async poll interval
POLL_INTERVAL = 15
# Max poll attempts (40 * 15s = 10 min max wait)
MAX_POLLS = 40


def _headers():
    if not API_KEY:
        raise ValueError("BRIGHTDATA_API_KEY environment variable not set")
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Core API functions
# ---------------------------------------------------------------------------

def trigger_async(dataset_key, input_data, discover=False):
    """Trigger an async scraping job. Returns snapshot_id.
    
    Args:
        dataset_key: One of 'posts', 'comments', 'profiles', 'reels'
        input_data: List of input objects (each with 'url' and optional params)
        discover: If True, use discovery mode (for posts/reels from profile URL)
    
    Returns:
        snapshot_id string
    """
    dataset_id = DATASET_IDS[dataset_key]
    params = {"dataset_id": dataset_id, "format": "json"}
    if discover:
        params["type"] = "discover_new"
        params["discover_by"] = "url"
    
    resp = requests.post(
        f"{BASE_URL}/trigger",
        params=params,
        headers=_headers(),
        json={"input": input_data},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    snapshot_id = data.get("snapshot_id")
    if not snapshot_id:
        raise ValueError(f"No snapshot_id in response: {data}")
    return snapshot_id


def poll_snapshot(snapshot_id):
    """Poll until snapshot is ready. Returns the progress dict."""
    for attempt in range(MAX_POLLS):
        time.sleep(POLL_INTERVAL)
        resp = requests.get(
            f"{BASE_URL}/progress/{snapshot_id}",
            headers=_headers(),
            timeout=30,
        )
        data = resp.json()
        status = data.get("status", "unknown")
        records = data.get("records", 0)
        print(f"  [Bright Data] Poll {attempt+1}/{MAX_POLLS}: status={status}, records={records}", flush=True)
        if status == "ready":
            return data
        if status == "failed":
            raise RuntimeError(f"Snapshot {snapshot_id} failed: {data}")
    raise TimeoutError(f"Snapshot {snapshot_id} did not complete after {MAX_POLLS * POLL_INTERVAL}s")


def download_snapshot(snapshot_id):
    """Download snapshot results as JSON list."""
    resp = requests.get(
        f"{BASE_URL}/snapshot/{snapshot_id}",
        params={"format": "json"},
        headers=_headers(),
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# High-level scraping functions
# ---------------------------------------------------------------------------

def discover_posts(profile_url_or_username, num_posts=100, start_date=None, end_date=None):
    """Discover posts from an Instagram profile URL within a date range.
    
    Args:
        profile_url_or_username: e.g. 'duniameutya' or full URL
        num_posts: Max posts to discover (default 100)
        start_date: 'MM-DD-YYYY' format
        end_date: 'MM-DD-YYYY' format
    
    Returns:
        List of post dicts with url, description, date_posted, num_comments, likes, etc.
    """
    # Normalize to full URL
    username = profile_url_or_username.lstrip("@")
    if not username.startswith("http"):
        profile_url = f"https://www.instagram.com/{username}"
    else:
        profile_url = username
    
    input_obj = {"url": profile_url, "num_of_posts": num_posts}
    if start_date:
        input_obj["start_date"] = start_date
    if end_date:
        input_obj["end_date"] = end_date
    
    print(f"[Bright Data] Discovering posts for {profile_url} ({start_date} to {end_date})...", flush=True)
    snapshot_id = trigger_async("posts", [input_obj], discover=True)
    print(f"[Bright Data] Snapshot: {snapshot_id}", flush=True)
    poll_snapshot(snapshot_id)
    posts = download_snapshot(snapshot_id)
    print(f"[Bright Data] Discovered {len(posts)} posts", flush=True)
    return posts


def scrape_comments(post_urls):
    """Scrape all comments from a list of Instagram post/reel URLs.
    
    Args:
        post_urls: List of Instagram post/reel URLs
    
    Returns:
        List of comment dicts with comment_user, comment_date, comment, likes_number, etc.
    """
    if not post_urls:
        return []
    
    # Batch in groups of 100 (async supports up to 5000, but 100 is manageable)
    all_comments = []
    batch_size = 100
    
    for i in range(0, len(post_urls), batch_size):
        batch = post_urls[i:i+batch_size]
        input_data = [{"url": u} for u in batch]
        
        print(f"[Bright Data] Scraping comments batch {i//batch_size + 1} ({len(batch)} posts)...", flush=True)
        snapshot_id = trigger_async("comments", input_data)
        print(f"[Bright Data] Snapshot: {snapshot_id}", flush=True)
        poll_snapshot(snapshot_id)
        comments = download_snapshot(snapshot_id)
        print(f"[Bright Data] Got {len(comments)} comments in batch", flush=True)
        all_comments.extend(comments)
    
    print(f"[Bright Data] Total comments scraped: {len(all_comments)}", flush=True)
    return all_comments


# ---------------------------------------------------------------------------
# Database ingestion
# ---------------------------------------------------------------------------

def get_db_path():
    """Get DB path matching app.py convention."""
    root = Path(__file__).resolve().parent
    return Path(os.environ.get("DB_PATH", str(root / "bumen.db")))


def db():
    c = sqlite3.connect(get_db_path())
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_bd_tables():
    """Create Bright Data tables if they don't exist."""
    c = db()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS bd_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT,
            url TEXT UNIQUE NOT NULL,
            shortcode TEXT,
            user_posted TEXT,
            description TEXT,
            date_posted TEXT,
            content_type TEXT,
            num_comments INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            hashtags TEXT,
            thumbnail TEXT,
            images TEXT,
            followers INTEGER,
            is_verified INTEGER,
            profile_url TEXT,
            scraped_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        
        CREATE TABLE IF NOT EXISTS bd_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id TEXT UNIQUE,
            post_url TEXT NOT NULL,
            comment_user TEXT,
            comment_user_url TEXT,
            comment_date TEXT,
            comment TEXT,
            likes_number INTEGER DEFAULT 0,
            replies_number INTEGER DEFAULT 0,
            scraped_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        
        CREATE INDEX IF NOT EXISTS idx_bd_comments_user ON bd_comments(comment_user);
        CREATE INDEX IF NOT EXISTS idx_bd_comments_post ON bd_comments(post_url);
        CREATE INDEX IF NOT EXISTS idx_bd_comments_date ON bd_comments(comment_date);
        CREATE INDEX IF NOT EXISTS idx_bd_posts_date ON bd_posts(date_posted);
        CREATE INDEX IF NOT EXISTS idx_bd_posts_user ON bd_posts(user_posted);
    """)
    c.commit()
    c.close()


def ingest_posts(posts_data, profile_username=None):
    """Ingest discovered posts into bd_posts table.
    
    Args:
        posts_data: List of post dicts from discover_posts()
    
    Returns:
        Dict with counts: {inserted, updated, total}
    """
    c = db()
    inserted = 0
    updated = 0
    
    for p in posts_data:
        url = p.get("url", "")
        if not url:
            continue
        
        # Extract shortcode from URL
        shortcode = ""
        m = re.search(r'instagram\.com/(?:p|reel)/([A-Za-z0-9_-]+)', url)
        if m:
            shortcode = m.group(1)
        
        values = (
            p.get("post_id") or p.get("id"),
            url,
            shortcode,
            p.get("user_posted") or profile_username,
            p.get("description") or p.get("caption") or "",
            p.get("date_posted") or p.get("datetime") or "",
            p.get("content_type") or "",
            p.get("num_comments") or p.get("comments") or 0,
            p.get("likes") or 0,
            json.dumps(p.get("hashtags") or p.get("post_hashtags") or []),
            p.get("thumbnail") or p.get("image_url") or "",
            json.dumps(p.get("images") or []),
            p.get("followers"),
            1 if p.get("is_verified") else 0,
            p.get("profile_url") or f"https://www.instagram.com/{p.get('user_posted', '')}",
        )
        
        cur = c.execute("""
            INSERT INTO bd_posts (post_id, url, shortcode, user_posted, description,
                                   date_posted, content_type, num_comments, likes,
                                   hashtags, thumbnail, images, followers, is_verified, profile_url)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
                post_id=excluded.post_id,
                description=excluded.description,
                date_posted=excluded.date_posted,
                num_comments=excluded.num_comments,
                likes=excluded.likes,
                hashtags=excluded.hashtags,
                thumbnail=excluded.thumbnail,
                scraped_at=CURRENT_TIMESTAMP
        """, values)
        
        if cur.rowcount == 1:
            inserted += 1
        else:
            updated += 1
    
    c.commit()
    c.close()
    return {"inserted": inserted, "updated": updated, "total": len(posts_data)}


def ingest_comments(comments_data):
    """Ingest scraped comments into bd_comments table.
    
    Args:
        comments_data: List of comment dicts from scrape_comments()
    
    Returns:
        Dict with counts: {inserted, duplicates, total}
    """
    c = db()
    inserted = 0
    duplicates = 0
    
    for cm in comments_data:
        comment_id = cm.get("comment_id", "")
        post_url = cm.get("post_url") or cm.get("url") or ""
        comment_text = (cm.get("comment") or "").strip()
        
        if not comment_text or not post_url:
            continue
        
        # Generate a fallback ID if missing
        if not comment_id:
            comment_id = f"{post_url}:{comment_text[:50]}:{cm.get('comment_user','')}"
        
        try:
            cur = c.execute("""
                INSERT OR IGNORE INTO bd_comments
                    (comment_id, post_url, comment_user, comment_user_url,
                     comment_date, comment, likes_number, replies_number)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                comment_id,
                post_url,
                cm.get("comment_user") or "",
                cm.get("comment_user_url") or "",
                cm.get("comment_date") or "",
                comment_text,
                cm.get("likes_number") or 0,
                cm.get("replies_number") or 0,
            ))
            if cur.rowcount > 0:
                inserted += 1
            else:
                duplicates += 1
        except sqlite3.IntegrityError:
            duplicates += 1
    
    c.commit()
    c.close()
    return {"inserted": inserted, "duplicates": duplicates, "total": len(comments_data)}


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def run_full_scrape(profile=DEFAULT_PROFILE, start_date=None, end_date=None, num_posts=100):
    """Run the full scrape pipeline: discover posts → scrape comments → ingest to DB.
    
    Args:
        profile: Instagram username (without @)
        start_date: 'MM-DD-YYYY' format (optional)
        end_date: 'MM-DD-YYYY' format (optional)
        num_posts: Max posts to discover
    
    Returns:
        Dict with summary: {posts_discovered, comments_scraped, posts_ingested, comments_ingested}
    """
    print(f"\n{'='*60}", flush=True)
    print(f"[BUMEN Bright Data] Full scrape for @{profile}", flush=True)
    print(f"  Date range: {start_date or 'all'} to {end_date or 'all'}", flush=True)
    print(f"  Max posts: {num_posts}", flush=True)
    print(f"{'='*60}\n", flush=True)
    
    # 1) Ensure tables exist
    init_bd_tables()
    
    # 2) Discover posts
    posts = discover_posts(profile, num_posts=num_posts, start_date=start_date, end_date=end_date)
    
    # 3) Ingest posts
    posts_result = ingest_posts(posts, profile_username=profile)
    print(f"\n[BUMEN] Posts ingested: {posts_result}", flush=True)
    
    # 4) Scrape comments for all discovered posts
    post_urls = [p["url"] for p in posts if p.get("url")]
    comments = scrape_comments(post_urls)
    
    # 5) Ingest comments
    comments_result = ingest_comments(comments)
    print(f"\n[BUMEN] Comments ingested: {comments_result}", flush=True)
    
    summary = {
        "profile": profile,
        "date_range": f"{start_date} to {end_date}" if start_date else "all",
        "posts_discovered": len(posts),
        "comments_scraped": len(comments),
        "posts_ingested": posts_result,
        "comments_ingested": comments_result,
    }
    print(f"\n{'='*60}", flush=True)
    print(f"[BUMEN Bright Data] Scrape complete!", flush=True)
    print(json.dumps(summary, indent=2), flush=True)
    print(f"{'='*60}\n", flush=True)
    return summary


# ---------------------------------------------------------------------------
# Analytics queries
# ---------------------------------------------------------------------------

def get_analytics(profile=None, start_date=None, end_date=None):
    """Get analytics summary from bd_comments and bd_posts.
    
    Returns dict with:
        - total_posts, total_comments, unique_commenters
        - top_commenters: [{username, comment_count, total_likes}]
        - posts_summary: [{url, date, num_comments, likes, description}]
        - comments_timeline: [{date, count}]
    """
    c = db()
    
    where_parts = []
    params = []
    if profile:
        where_parts.append("p.user_posted = ?")
        params.append(profile)
    if start_date:
        where_parts.append("p.date_posted >= ?")
        params.append(start_date)
    if end_date:
        where_parts.append("p.date_posted <= ?")
        params.append(end_date)
    
    where_clause = " AND ".join(where_parts) if where_parts else "1=1"
    
    # Total posts
    total_posts = c.execute(f"""
        SELECT COUNT(*) FROM bd_posts p WHERE {where_clause}
    """, params).fetchone()[0]
    
    # Total comments (join to filter by profile/date)
    total_comments = c.execute(f"""
        SELECT COUNT(*) FROM bd_comments cm
        JOIN bd_posts p ON p.url = cm.post_url
        WHERE {where_clause}
    """, params).fetchone()[0]
    
    # Unique commenters
    unique_commenters = c.execute(f"""
        SELECT COUNT(DISTINCT LOWER(cm.comment_user)) FROM bd_comments cm
        JOIN bd_posts p ON p.url = cm.post_url
        WHERE {where_clause} AND cm.comment_user != ''
    """, params).fetchone()[0]
    
    # Top commenters
    top_commenters = [dict(r) for r in c.execute(f"""
        SELECT LOWER(cm.comment_user) as username,
               COUNT(*) as comment_count,
               SUM(cm.likes_number) as total_likes,
               MAX(cm.comment_date) as last_comment_date
        FROM bd_comments cm
        JOIN bd_posts p ON p.url = cm.post_url
        WHERE {where_clause} AND cm.comment_user != ''
        GROUP BY LOWER(cm.comment_user)
        ORDER BY comment_count DESC, total_likes DESC
        LIMIT 20
    """, params)]
    
    # Posts summary
    posts_summary = [dict(r) for r in c.execute(f"""
        SELECT p.url, p.shortcode, p.date_posted, p.content_type,
               p.num_comments, p.likes, p.description, p.thumbnail,
               COUNT(cm.id) as scraped_comments
        FROM bd_posts p
        LEFT JOIN bd_comments cm ON cm.post_url = p.url
        WHERE {where_clause}
        GROUP BY p.id
        ORDER BY p.date_posted DESC
    """, params)]
    
    # Comments timeline (by day)
    timeline = [dict(r) for r in c.execute(f"""
        SELECT DATE(cm.comment_date) as date, COUNT(*) as count
        FROM bd_comments cm
        JOIN bd_posts p ON p.url = cm.post_url
        WHERE {where_clause} AND cm.comment_date != ''
        GROUP BY DATE(cm.comment_date)
        ORDER BY date
    """, params)]
    
    c.close()
    
    return {
        "total_posts": total_posts,
        "total_comments": total_comments,
        "unique_commenters": unique_commenters,
        "top_commenters": top_commenters,
        "posts_summary": posts_summary,
        "comments_timeline": timeline,
    }


def search_commenters(search_term, limit=50):
    """Search commenters by username. Returns list of matches with stats."""
    c = db()
    term = f"%{search_term.lower()}%"
    results = [dict(r) for r in c.execute("""
        SELECT LOWER(comment_user) as username,
               COUNT(*) as comment_count,
               SUM(likes_number) as total_likes,
               MIN(comment_date) as first_comment,
               MAX(comment_date) as last_comment,
               GROUP_CONCAT(DISTINCT post_url) as posts_commented_on
        FROM bd_comments
        WHERE LOWER(comment_user) LIKE ?
        GROUP BY LOWER(comment_user)
        ORDER BY comment_count DESC
        LIMIT ?
    """, (term, limit))]
    c.close()
    return results


def get_comments_by_user(username):
    """Get all comments by a specific user."""
    c = db()
    results = [dict(r) for r in c.execute("""
        SELECT cm.*, p.description as post_description, p.date_posted as post_date
        FROM bd_comments cm
        LEFT JOIN bd_posts p ON p.url = cm.post_url
        WHERE LOWER(cm.comment_user) = LOWER(?)
        ORDER BY cm.comment_date DESC
    """, (username,))]
    c.close()
    return results


def get_comments_by_post(post_url):
    """Get all comments for a specific post, with post metadata.
    
    Args:
        post_url: Instagram post/reel URL
    
    Returns:
        Dict with post info and list of comments
    """
    c = db()
    post = c.execute("""
        SELECT * FROM bd_posts WHERE url = ?
    """, (post_url,)).fetchone()
    
    if not post:
        c.close()
        return {"post": None, "comments": []}
    
    comments = [dict(r) for r in c.execute("""
        SELECT * FROM bd_comments WHERE post_url = ?
        ORDER BY comment_date DESC
    """, (post_url,))]
    c.close()
    return {"post": dict(post), "comments": comments}


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    
    # Test: ingest the August data we already scraped
    init_bd_tables()
    
    # Load pre-scraped data
    posts_file = "/tmp/duniameutya_aug_posts.json"
    comments_file = "/tmp/duniameutya_aug_comments.json"
    
    if os.path.exists(posts_file):
        with open(posts_file) as f:
            posts = json.load(f)
        print(f"Loading {len(posts)} posts from {posts_file}")
        result = ingest_posts(posts, profile_username="duniameutya")
        print(f"Posts: {result}")
    
    if os.path.exists(comments_file):
        with open(comments_file) as f:
            comments = json.load(f)
        print(f"Loading {len(comments)} comments from {comments_file}")
        result = ingest_comments(comments)
        print(f"Comments: {result}")
    
    # Print analytics
    print("\n--- ANALYTICS ---")
    analytics = get_analytics(profile="duniameutya")
    print(f"Total posts: {analytics['total_posts']}")
    print(f"Total comments: {analytics['total_comments']}")
    print(f"Unique commenters: {analytics['unique_commenters']}")
    print(f"\nTop 10 commenters:")
    for u in analytics["top_commenters"][:10]:
        print(f"  @{u['username']}: {u['comment_count']} comments, {u['total_likes']} likes")
