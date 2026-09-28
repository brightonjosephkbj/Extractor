import requests
import json
import re
import sys

url = sys.argv[1]
match = re.search(r"playlist/([a-zA-Z0-9]+)", url)
if not match:
    print("Could not extract playlist ID")
    sys.exit(1)

playlist_id = match.group(1)
embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
headers = {"User-Agent": "Mozilla/5.0"}

resp = requests.get(embed_url, headers=headers, timeout=15)
match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', resp.text)

if not match:
    print("No __NEXT_DATA__ found, dumping first 2000 chars:")
    print(resp.text[:2000])
    sys.exit(1)

data = json.loads(match.group(1))
print(json.dumps(data, indent=2)[:3000])
