#!/usr/bin/env python3
import sys, os, json, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.immoscoop import ImmoscoopScraper

try:
    s = ImmoscoopScraper()
    listings = s.scrape()
    out = [l.__dict__ if hasattr(l, '__dict__') else l for l in listings]
    with open("/tmp/domus-batches/immoscoop.json", "w") as f:
        json.dump({"platform": "immoscoop", "count": len(out), "listings": out}, f, indent=2, default=str)
    print(f"IMMOSCOOP OK: {len(out)} listings")
except Exception as e:
    print(f"IMMOSCOOP FAILED: {e}")
    traceback.print_exc()
    with open("/tmp/domus-batches/immoscoop.json", "w") as f:
        json.dump({"platform": "immoscoop", "count": 0, "listings": [], "error": str(e)}, f)
    sys.exit(1)
