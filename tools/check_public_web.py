#!/usr/bin/env python3
import urllib.request, sys
url="https://storage.yandexcloud.net/plant-diary-photos/web/2026-08-29/IMG_9701.webp"
try:
    with urllib.request.urlopen(url, timeout=15) as r:
        print(r.status, r.headers.get("Content-Type"), r.headers.get("Content-Length"), url)
        if r.status==200: print("OK: web images are publicly readable")
except Exception as e:
    print("NOT READY:",e); sys.exit(1)
