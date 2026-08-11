#!/usr/bin/env python3
"""Build static index.html from data.json + static-ui.html template."""
from pathlib import Path
import json, re

ROOT = Path(__file__).resolve().parent

# Load data
DATA = json.loads((ROOT / "data.json").read_text())
JSON_EMBED = json.dumps(DATA, ensure_ascii=False)

# Load the static UI template (separate file, no API calls)
TEMPLATE = (ROOT / "static-ui.html").read_text()

# Replace placeholder
HTML = TEMPLATE.replace("__POSTS_JSON__", JSON_EMBED)

(ROOT / "index.html").write_text(HTML, encoding="utf-8")
print(f"Generated index.html ({len(HTML)} bytes)")
