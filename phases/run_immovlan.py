#!/usr/bin/env python3
"""Immovlan scraper - apart proces want 91 detailpages duurt ~7 min"""
import sys, os, json, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.immovlan import ImmovlanScraper

try:
    s = ImmovlanScraper()
    listings = s.scrape()
    out = [l.__dict__ if hasattr(l, '__dict__') else l for l in listings]
    with open("/tmp/domus-batches/immovlan.json", "w") as f:
        json.dump({"platform": "immovlan", "count": len(out), "listings": out}, f, indent=2, default=str)
    print(f"IMMOVLAN OK: {len(out)} listings → /tmp/domus-batches/immovlan.json")
except Exception as e:
    print(f"IMMOVLAN FAILED: {e}")
    traceback.print_exc()
    with open("/tmp/domus-batches/immovlan.json", "w") as f:
        json.dump({"platform": "immovlan", "count": 0, "listings": [], "error": str(e)}, f)
    sys.exit(1)
