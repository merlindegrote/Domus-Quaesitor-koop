#!/usr/bin/env python3
import sys, os, json, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.zimmo import ZimmoScraper

try:
    s = ZimmoScraper()
    listings = s.scrape()
    out = [l.__dict__ if hasattr(l, '__dict__') else l for l in listings]
    with open("/tmp/domus-batches/zimmo.json", "w") as f:
        json.dump({"platform": "zimmo", "count": len(out), "listings": out}, f, indent=2, default=str)
    print(f"ZIMMO OK: {len(out)} listings")
except Exception as e:
    print(f"ZIMMO FAILED: {e}")
    traceback.print_exc()
    with open("/tmp/domus-batches/zimmo.json", "w") as f:
        json.dump({"platform": "zimmo", "count": 0, "listings": [], "error": str(e)}, f)
    sys.exit(1)
