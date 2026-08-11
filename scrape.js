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

const API = 'https://bumen-production.up.railway.app';
const ADMIN_PASSWORD = '@poji#1';
const DATA_FILE = path.join(__dirname, 'data.json');

// ============================================================================
// Helpers
// ============================================================================

async function apiCall(endpoint, options = {}) {
  const url = `${API}${endpoint}`;
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
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
    body: JSON.stringify({ password: ADMIN_PASSWORD }),
  });
  if (!res.ok) throw new Error(`Login failed: ${res.status}`);
  // Extract cookie from set-cookie header
  const setCookie = res.headers.get('set-cookie') || '';
  const match = setCookie.match(/admin_session=([^;]+)/);
  if (!match) throw new Error('No session cookie in response');
  return `admin_session=${match[1]}`;
}

async function saveComments(cookie, postId, comments) {
  const res = await apiCall('/api/admin/live-comments', {
    method: 'POST',
    headers: { 'Cookie': cookie },
    body: JSON.stringify({ post_id: postId, comments }),
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

  // Scroll to load comments
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
  return comments;
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
        const comments = await scrapePost(page, post.source_url, post.id);
        
        if (comments.length > 0) {
          // Save to API
          console.log(`  Saving ${comments.length} comments...`);
          const result = await saveComments(cookie, post.id, comments);
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
