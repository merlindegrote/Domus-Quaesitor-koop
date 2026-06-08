#!/usr/bin/env python3
"""Run Immoscoop scraper with health tracking and partial data preservation."""
import sys, os, json, traceback, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.base import record_scraper_success, record_scraper_failure, is_scraper_skipped

PLATFORM = "immoscoop"
OUTFILE = "/tmp/domus-batches/immoscoop.json"

try:
    if is_scraper_skipped(PLATFORM):
        print(f"IMMOSCOOP SKIPPED: rate limit protection active (24h skip)")
        with open(OUTFILE, "w") as f:
            json.dump({"platform": PLATFORM, "count": 0, "listings": [], "skipped": True}, f, indent=2)
        sys.exit(0)

    from scrapers.immoscoop import ImmoscoopScraper
    s = ImmoscoopScraper()
    t0 = time.time()
    listings = s.scrape()
    elapsed = time.time() - t0
    out = [l.__dict__ if hasattr(l, '__dict__') else l for l in listings]
    with open(OUTFILE, "w") as f:
        json.dump({"platform": PLATFORM, "count": len(out), "listings": out}, f, indent=2, default=str)
    record_scraper_success(PLATFORM, elapsed)
    print(f"IMMOSCOOP OK: {len(out)} listings ({elapsed:.1f}s)")
except Exception as e:
    elapsed = time.time() - t0 if 't0' in dir() else 0
    print(f"IMMOSCOOP FAILED: {e}")
    traceback.print_exc()
    partial = []
    if 's' in dir() and hasattr(s, '_results_cache'):
        partial = s._results_cache
    out = [l.__dict__ if hasattr(l, '__dict__') else l for l in partial]
    with open(OUTFILE, "w") as f:
        json.dump({"platform": PLATFORM, "count": len(out), "listings": out, "partial": True, "error": str(e)[:300]}, f, indent=2, default=str)
    if not out:
        record_scraper_failure(PLATFORM, str(e))
    sys.exit(1)
