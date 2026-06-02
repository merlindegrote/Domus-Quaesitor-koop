#!/usr/bin/env python3
import sys, os, json, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.tweedehands import TweeDeHandsScraper

try:
    s = TweeDeHandsScraper()
    listings = s.scrape()
    out = [l.__dict__ if hasattr(l, '__dict__') else l for l in listings]
    with open("/tmp/domus-batches/2dehands.json", "w") as f:
        json.dump({"platform": "2dehands", "count": len(out), "listings": out}, f, indent=2, default=str)
    print(f"2DEHANDS OK: {len(out)} listings")
except Exception as e:
    print(f"2DEHANDS FAILED: {e}")
    traceback.print_exc()
    with open("/tmp/domus-batches/2dehands.json", "w") as f:
        json.dump({"platform": "2dehands", "count": 0, "listings": [], "error": str(e)}, f)
    sys.exit(1)
