#!/usr/bin/env python3
"""Run Zimmo scraper with health tracking and partial data preservation."""
import sys, os, json, traceback, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.base import record_scraper_success, record_scraper_failure, is_scraper_skipped

PLATFORM = "zimmo"
OUTFILE = "/tmp/domus-batches/zimmo.json"

try:
    if is_scraper_skipped(PLATFORM):
        print(f"ZIMMO SKIPPED: rate limit protection active (24h skip)")
        with open(OUTFILE, "w") as f:
            json.dump({"platform": PLATFORM, "count": 0, "listings": [], "skipped": True}, f, indent=2)
        sys.exit(0)

    from scrapers.zimmo import ZimmoScraper
    s = ZimmoScraper()
    t0 = time.time()
    listings = s.scrape()
    elapsed = time.time() - t0
    out = [l.__dict__ if hasattr(l, '__dict__') else l for l in listings]
    with open(OUTFILE, "w") as f:
        json.dump({"platform": PLATFORM, "count": len(out), "listings": out}, f, indent=2, default=str)
    record_scraper_success(PLATFORM, elapsed)
    print(f"ZIMMO OK: {len(out)} listings ({elapsed:.1f}s)")
except Exception as e:
    elapsed = time.time() - t0 if 't0' in dir() else 0
    print(f"ZIMMO FAILED: {e}")
    traceback.print_exc()
    # Save partial data
    partial = []
    if 's' in dir() and hasattr(s, '_results_cache'):
        partial = s._results_cache
    out = [l.__dict__ if hasattr(l, '__dict__') else l for l in partial]
    with open(OUTFILE, "w") as f:
        json.dump({"platform": PLATFORM, "count": len(out), "listings": out, "partial": True, "error": str(e)[:300]}, f, indent=2, default=str)
    if not out:
        record_scraper_failure(PLATFORM, str(e))
    sys.exit(1)
