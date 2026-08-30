import instaloader
import json
import os
import sys

# URLs to fetch
urls = [
    'https://www.instagram.com/reel/DcWNUDiNPDq/?igsi=MWh1cHc0bXF6b20zcw==',
    'https://www.instagram.com/reel/DcWChJNghdW/?igsi=MTNnNWsxZ3BpZ3duag==',
    'https://www.instagram.com/p/DcVveSYmqMv/?igsi=dHlqN2p4NGJwZXZs',
    'https://www.instagram.com/p/DcVGrq4RnOG/?igsi=NXI3cDE5a3Z5ejB3',
    'https://www.instagram.com/reel/DcSPHHczYJ1/?igsi=NnRodmdmaGNncG51',
    'https://www.instagram.com/reel/DcOO3P6yWMq/?igsi=aTVtczMxbWV4b2Zk',
    'https://www.instagram.com/p/DcOHPTckjJq/?igsi=MW55MTR3bTliaWY4Yw==',
    'https://www.instagram.com/p/DcIpQK5kb_k/?img_index=10&igsh=MXJ5ZHVwbzZxYTM0OQ==',
    'https://www.instagram.com/reel/DcIanKgxtVo/?igsh=MXV2bWZ1bGhjZDEydg==',
    'https://www.instagram.com/reel/DcH0-2Yz9Xx/?igsh=MWMwdzV5M2tjcGdveg=='
]

# Load existing data
if os.path.exists('data.json'):
    with open('data.json', 'r') as f:
        data = json.load(f)
else:
    data = []

# Get max post id
max_id = max([p['id'] for p in data]) if data else 0

# Initialize instaloader
L = instaloader.Instaloader()

# Process each URL
new_posts = []
failed_urls = []

for i, url in enumerate(urls):
    try:
        print(f"Fetching {i+1}/{len(urls)}: {url}")
        shortcode = url.split("/")[4]  # Extract shortcode from URL
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        # Check if post already exists
        existing = [p for p in data if p.get('shortcode') == post.shortcode]
        if existing:
            print(f"  Skipping duplicate: {post.shortcode}")
            continue
            
        max_id += 1
        new_post = {
            "id": max_id,
            "shortcode": post.shortcode,
            "url": url,
            "title": f"{max_id:02d} {post.owner_username} Post",
            "caption": post.caption or "",
            "comments": []
        }
        new_posts.append(new_post)
        print(f"  Added post {max_id}: {post.owner_username}")
        
    except Exception as e:
        print(f"  Failed to fetch {url}: {e}")
        failed_urls.append((url, str(e)))

# Save new posts to data.json
data.extend(new_posts)
with open('data.json', 'w') as f:
    json.dump(data, f, indent=2, ensure_ascii=False)

print(f"\nSummary:")
print(f"  Existing posts: {len(data) - len(new_posts)}")
print(f"  New posts added: {len(new_posts)}")
print(f"  Failed URLs: {len(failed_urls)}")

if failed_urls:
    print("\nFailed URLs:")
    for url, err in failed_urls:
        print(f"  {url}: {err}")

print(f"\nUpdated data.json with {len(data)} total posts")
