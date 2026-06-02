#!/usr/bin/env python3
"""Run immoweb scraper, output to /tmp/domus-batches/"""
import sys, os, json, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/phases")

try:
    from scrapers.immoweb import ImmowebScraper
    s = ImmowebScraper()
    listings = s.scrape()
    out = [l.__dict__ if hasattr(l, '__dict__') else l for l in listings]
    with open("/tmp/domus-batches/immoweb.json", "w") as f:
        json.dump({"platform": "immoweb", "count": len(out), "listings": out}, f, indent=2, default=str)
    print(f"IMMOWEB OK: {len(out)} listings → /tmp/domus-batches/immoweb.json")
except Exception as e:
    print(f"IMMOWEB FAILED: {e}")
    traceback.print_exc()
    with open("/tmp/domus-batches/immoweb.json", "w") as f:
        json.dump({"platform": "immoweb", "count": 0, "listings": [], "error": str(e)}, f)
    sys.exit(1)
