#!/usr/bin/env python3
"""Scrape 1 batch Immovlan detail pages. Run: python3 scrape_batch.py --ids A1,A2,A3 --batch 0
Output: /tmp/domus-batches/immovlan_batch_0.json"""
import sys, os, json, argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scrapers.immovlan import ImmovlanScraper

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", required=True, help="Comma-separated property IDs")
    parser.add_argument("--batch", required=True, type=int, help="Batch number")
    args = parser.parse_args()

    ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    s = ImmovlanScraper()
    
    listings = []
    for prop_id in ids:
        try:
            listing = s._fetch_detail(prop_id)
            if listing:
                listings.append(listing.__dict__)
                print(f"  ✅ {prop_id}")
            else:
                print(f"  ⬜ {prop_id}: no listing")
        except Exception as e:
            print(f"  ❌ {prop_id}: {e}")
    
    outpath = f"/tmp/domus-batches/immovlan_batch_{args.batch}.json"
    with open(outpath, "w") as f:
        json.dump({"platform": "immovlan", "batch": args.batch, "count": len(listings), "listings": listings}, f, indent=2, default=str)
    print(f"  💾 Batch {args.batch}: {len(listings)}/{len(ids)} → {outpath}")

if __name__ == "__main__":
    main()
