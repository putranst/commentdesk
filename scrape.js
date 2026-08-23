#!/usr/bin/env node
/**
 * BUMEN Intelligence — Instagram Comment Scraper
 * ==============================================
 * Uses Puppeteer + Chrome to scrape visible comments from Instagram posts
 * and POSTs them to the BUMEN API for sentiment analysis.
 *
 * Usage:
 *   node scrape.js                    # scrape all posts from data.json
 *   node scrape.js --post 5           # scrape single post ID
 *   node scrape.js --headless=false   # show browser for debugging
 */

const puppeteer = require('puppeteer');
const fs = require('fs');
const path = require('path');

const API = process.env.API_URL || 'http://localhost:8765';
const ADMIN_PASSWORD = '@poji#1';
const ADMIN_PW_DEFAULT = '@poji#1';
const DATA_FILE = path.join(__dirname, 'data.json');

// Simple cookie jar for the scraper
let cookieJar = '';

// ============================================================================
// Image Download Helper
// ============================================================================

async function downloadImage(page, url, postId) {
  try {
    const filename = `thumb_${postId}_${Date.now()}.jpg`;
    const filepath = path.join(__dirname, 'thumbnails', filename);
    
    // Create thumbnails directory
    await fs.promises.mkdir(path.join(__dirname, 'thumbnails'), { recursive: true });
    
    // Use the page's context to download (has Instagram cookies/auth)
    const buffer = await page.evaluate(async (url) => {
      const response = await fetch(url, {
        headers: {
          'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
          'Referer': 'https://www.instagram.com/',
        },
        credentials: 'include'
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const arrayBuffer = await response.arrayBuffer();
      return Array.from(new Uint8Array(arrayBuffer));
    }, url);
    
    await fs.promises.writeFile(filepath, Buffer.from(buffer));
    return `/thumbnails/${filename}`;
  } catch (e) {
    console.log(`    Download failed: ${e.message}`);
    return null;
  }
}

async function apiCall(endpoint, options = {}) {
  const url = `${API}${endpoint}`;
  // Use cookieJar if available
  const headers = { 'Content-Type': 'application/json', ...options.headers };
  if (cookieJar) {
    headers['Cookie'] = cookieJar;
  }
  const res = await fetch(url, {
    headers,
    ...options,
  });
  if (!res.ok) {
    const err = await res.text();
    throw new Error(`API ${res.status}: ${err}`);
  }
  return res.json();
}

async function apiLogin() {
  const url = `${API}/api/admin/login`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'admin', password: ADMIN_PASSWORD }),
  });
  if (!res.ok) throw new Error(`Login failed: ${res.status}`);
  // Extract cookie from set-cookie header
  const setCookie = res.headers.get('set-cookie') || '';
  const match = setCookie.match(/bumen_admin_sid=([^;]+)/);
  if (!match) throw new Error('No session cookie in response');
  cookieJar = `bumen_admin_sid=${match[1]}`;
  return cookieJar;
}

async function saveComments(postId, comments, thumbnailUrl = null) {
  const res = await apiCall('/api/admin/live-comments', {
    method: 'POST',
    body: JSON.stringify({ post_id: postId, comments, thumbnail: thumbnailUrl, password: ADMIN_PW_DEFAULT }),
  });
  return res;
}

// ============================================================================
// Instagram Scraper
// ============================================================================

async function scrapePost(page, url, postId) {
  console.log(`  Navigating to ${url}...`);
  await page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });
  await new Promise(r => setTimeout(r, 2000));

  // Dismiss any dialogs
  try {
    const closeBtn = await page.$('[role="dialog"] button, [aria-label="Close"]');
    if (closeBtn) await closeBtn.click();
    await new Promise(r => setTimeout(r, 500));
  } catch (_) {}

  // Extract post thumbnail/image
  let thumbnailUrl = null;
  try {
    // Try multiple selectors for the main post image
    const imgSelectors = [
      'article img[style*="object-fit: cover"]',
      'article img[decoding="auto"]',
      'article header + div img',
      'article img:first-of-type',
    ];
    
    for (const selector of imgSelectors) {
      const img = await page.$(selector);
      if (img) {
        thumbnailUrl = await page.evaluate(el => el.src, img);
        if (thumbnailUrl && thumbnailUrl.startsWith('https://scontent')) break;
      }
    }
    
    // Fallback: try meta og:image
    if (!thumbnailUrl) {
      thumbnailUrl = await page.$eval('meta[property="og:image"]', el => el.content).catch(() => null);
    }
    
    if (thumbnailUrl) {
      console.log(`  📸 Thumbnail: ${thumbnailUrl.substring(0,80)}...`);
    }
  } catch (e) {
    console.log(`  ⚠️  Could not extract thumbnail: ${e.message}`);
  }

  // Download thumbnail if found
  let localThumbnail = null;
  if (thumbnailUrl) {
    try {
      localThumbnail = await downloadImage(page, thumbnailUrl, postId);
      if (localThumbnail) {
        console.log(`  💾 Thumbnail saved: ${localThumbnail}`);
      } else {
        console.log(`  ⚠️  Thumbnail download returned null, proceeding without local thumbnail`);
        localThumbnail = null;
      }
    } catch (e) {
      console.log(`  ⚠️  Could not download thumbnail: ${e.message}`);
      localThumbnail = null;
    }
  } else {
    localThumbnail = null;
  }

  // Scroll to load comments...
  console.log('  Loading comments...');
  let prevCount = 0;
  for (let i = 0; i < 5; i++) {
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await new Promise(r => setTimeout(r, 1500));
    // Click "Load more comments" if present
    const buttons = await page.$$('button, span');
    for (const btn of buttons) {
      const text = await page.evaluate(el => el.textContent, btn);
      if (text && (text.includes('more') || text.includes('Load'))) {
        try { await btn.click(); await new Promise(r => setTimeout(r, 800)); } catch (_) {}
        break;
      }
    }
    // Check if new comments loaded
    const spans = await page.$$('span');
    if (spans.length === prevCount && i > 2) break;
    prevCount = spans.length;
  }

  // Extract comments
  console.log('  Extracting comments...');
  const comments = await page.evaluate(() => {
    const results = [];
    const seen = new Set();
    document.querySelectorAll('span').forEach(span => {
      const text = span.textContent.trim();
      if (text.length < 4 || text.length > 2000) return;
      if (seen.has(text)) return;
      // Skip known non-comment text
      const skip = ['Sign up', 'Terms', 'Privacy', 'Never miss', 'See more',
                    'Log in', 'Instagram', 'Meta', 'English', 'About', 'Blog',
                    'Jobs', 'Help', 'API', 'Locations'];
      if (skip.some(s => text.includes(s))) return;
      if (text.startsWith('#') && text.length < 30) return;

      // Try to find username from nearby elements
      let username = 'anonymous';
      const parent = span.closest('li, div');
      if (parent) {
        const links = parent.querySelectorAll('a[href]');
        for (const link of links) {
          const u = link.textContent.trim();
          if (u && !u.includes('duniameutya') && u.length > 1 && u.length < 35 && !u.includes(' ')) {
            username = u;
            break;
          }
        }
      }
      
      seen.add(text);
      results.push({ username, body: text });
    });
    return results;
  });

  console.log(`  ✓ Found ${comments.length} comments`);
  return { comments, localThumbnail };
}

// ============================================================================
// Main
// ============================================================================

async function main() {
  const args = process.argv.slice(2);
  const singlePost = args.includes('--post') ? parseInt(args[args.indexOf('--post') + 1]) : null;
  const headless = !args.includes('--headless=false');

  // Load posts
  const data = JSON.parse(fs.readFileSync(DATA_FILE, 'utf8'));
  const posts = singlePost 
    ? data.filter(p => p.id === singlePost)
    : data;

  if (posts.length === 0) {
    console.error('No posts found');
    process.exit(1);
  }

  console.log(`\n🔍 BUMEN Scraper — ${posts.length} posts to scrape\n`);

  // Launch browser
  const browser = await puppeteer.launch({
    headless: headless ? 'new' : false,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-blink-features=AutomationControlled',
      '--window-size=1280,800',
    ],
  });

  let totalSaved = 0;

  try {
    const page = await browser.newPage();
    await page.setUserAgent(
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36'
    );
    await page.setViewport({ width: 1280, height: 800 });

    // Authenticate with API
    console.log('🔑 Authenticating with BUMEN API...');
    const cookie = await apiLogin();
    console.log('  ✓ Authenticated\n');

    for (const post of posts) {
      console.log(`📋 Post ${post.id}: ${post.title}`);
      
      try {
        const result = await scrapePost(page, post.source_url, post.id);
        const comments = result.comments;
        const localThumbnail = result.localThumbnail;
        
        if (comments.length > 0) {
          // Save to API with thumbnail
          console.log(`  Saving ${comments.length} comments...`);
          const result = await saveComments(post.id, comments, localThumbnail || null);
          totalSaved += comments.length;
          console.log(`  ✅ Saved. Total: ${totalSaved}\n`);
        } else {
          console.log(`  ⚠️  No comments found\n`);
        }
      } catch (err) {
        console.error(`  ❌ Error: ${err.message}\n`);
      }

      // Rate limit between posts
      await new Promise(r => setTimeout(r, 3000));
    }

  } finally {
    await browser.close();
  }

  console.log(`\n✨ Done! ${totalSaved} comments saved across ${posts.length} posts.`);
  console.log(`📊 View dashboard: ${API}/admin\n`);
}

main().catch(console.error);
