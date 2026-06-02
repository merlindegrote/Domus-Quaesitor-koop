#!/usr/bin/env python3
"""Extract Immovlan search IDs ONLY — geen detail pages, <5s werk"""
import sys, os, re, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers.immovlan import ImmovlanScraper

def main():
    s = ImmovlanScraper()
    all_ids = []
    seen_ids = set()

    for page in range(1, 6):
        page_ids = s._fetch_search_page(page)
        if not page_ids:
            break
        short_ids = [pid for pid in page_ids if re.match(r"^[A-Z]", pid)]
        new_ids = [pid for pid in short_ids if pid not in seen_ids]
        if not new_ids:
            break
        all_ids.extend(new_ids)
        seen_ids.update(new_ids)
        print(f"  Page {page}: {len(new_ids)} new IDs")
        if len(page_ids) < 20:
            break

    with open("/tmp/domus-immovlan-ids.json", "w") as f:
        json.dump(all_ids, f)
    print(f"\n✅ {len(all_ids)} Immovlan IDs → /tmp/domus-immovlan-ids.json")

if __name__ == "__main__":
    main()
