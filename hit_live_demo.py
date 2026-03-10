"""Hit the live demo API endpoints to generate real traces."""
import urllib.request
import json
import time

BASE = "http://localhost:8000"

def hit(method, path):
    req = urllib.request.Request(f"{BASE}{path}", method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        print(f"  ✅ {method} {path} -> {resp.status}")
    except Exception as e:
        print(f"  ✖  {method} {path} -> {e}")

print("Hitting live demo endpoints...\n")

hit("GET", "/healthz")
time.sleep(0.3)

for i in range(3):
    hit("POST", f"/api/articles?title=Article+{i+1}&body=This+is+article+{i+1}+for+testing")
    time.sleep(0.5)

hit("GET", "/api/articles")
time.sleep(0.3)

hit("GET", "/api/articles/articles_1")
time.sleep(0.3)

hit("GET", "/api/users/admin")
time.sleep(0.3)

hit("GET", "/api/users/nobody")
time.sleep(0.3)

hit("GET", "/api/articles/fake_id")

print(f"\n✅ Done! Now run: curl -X POST http://localhost:4318/v1/analyze")
