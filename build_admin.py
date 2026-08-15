import json
from pathlib import Path

# Read the admin HTML from app.py
app = Path('app.py').read_text()

# Extract ADMIN_HTML
import re
match = re.search(r'ADMIN_HTML = r\'\'\'(.*?)\'\'\'', app, re.DOTALL)
if not match:
    print("ADMIN_HTML not found")
    exit(1)

admin_html = match.group(1)

# The admin HTML uses /api/admin/* endpoints which point to Railway
# We need to make sure it points to the production API
# The current admin_html already uses relative /api/admin/* paths
# But on Vercel, those won't work unless we use vercel.json rewrites

# Write admin.html
Path('admin.html').write_text(admin_html)
print(f"Generated admin.html ({len(admin_html)} bytes)")
