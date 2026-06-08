#!/usr/bin/env python3
"""Run immoweb scraper with health tracking, timeouts, and partial data preservation."""
import sys, os, json, traceback, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/phases")

# Import health tracking — use relative import path
try:
    from scrapers.base import record_scraper_success, record_scraper_failure, is_scraper_skipped
except ImportError:
    def record_scraper_success(*a, **kw): pass
    def record_scraper_failure(*a, **kw): pass
    def is_scraper_skipped(*a, **kw): return False

try:
    # Check if scraper is in skip mode (3+ consecutive failures = rate limit protection)
    if is_scraper_skipped("immoweb"):
        print("IMMOWEB SKIPPED: rate limit protection active (24h skip)")
        with open("/tmp/domus-batches/immoweb.json", "w") as f:
            json.dump({"platform": "immoweb", "count": 0, "listings": [], "skipped": True}, f, indent=2)
        sys.exit(0)

    from scrapers.immoweb import ImmowebScraper
    s = ImmowebScraper()

    t0 = time.time()
    listings = s.scrape()
    elapsed = time.time() - t0

    out = [l.__dict__ if hasattr(l, '__dict__') else l for l in listings]

    # Write partial data as we go — this is the final output now
    with open("/tmp/domus-batches/immoweb.json", "w") as f:
        json.dump({"platform": "immoweb", "count": len(out), "listings": out}, f, indent=2, default=str)

    record_scraper_success("immoweb", elapsed)
    print(f"IMMOWEB OK: {len(out)} listings → /tmp/domus-batches/immoweb.json ({elapsed:.1f}s)")

except Exception as e:
    elapsed = time.time() - t0 if 't0' in dir() else 0
    print(f"IMMOWEB FAILED: {e}")
    traceback.print_exc()

    # ── Save partial data — whatever we have so far ──────────────
    partial_listings = []
    if 's' in dir() and hasattr(s, '_results_cache'):
        partial_listings = s._results_cache
    if not partial_listings:
        try:
            from scrapers.immoweb import ImmowebScraper
            # Try to read any partial output file the scraper might have written
            for fpath in ["/tmp/domus-immoweb-partial.json"]:
                if os.path.exists(fpath):
                    with open(fpath) as f:
                        partial = json.load(f)
                    if isinstance(partial, list):
                        partial_listings = partial
                        break
        except Exception:
            pass

    out = [l.__dict__ if hasattr(l, '__dict__') else l for l in partial_listings]
    with open("/tmp/domus-batches/immoweb.json", "w") as f:
        json.dump({
            "platform": "immoweb",
            "count": len(out),
            "listings": out,
            "partial": True,
            "error": str(e)[:300],
        }, f, indent=2, default=str)

    if out:
        print(f"IMMOWEB PARTIAL: {len(out)} listings saved (scraper failed mid-way)")
    else:
        record_scraper_failure("immoweb", str(e))
    sys.exit(1)
