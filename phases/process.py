#!/usr/bin/env python3
"""
Phase 3 — Process: merge batch outputs, dedup, score, save history.

Stages:
  1. Read all batch JSON files from /tmp/domus-batches/*.json
  2. Reconstruct Listing objects
  3. Filter: only houses (property_type + title check)
  4. Same-run dedup by fingerprint
  5. og:image fallback for listings without photos
  6. DeepSeek text scoring
  7. Save to history (storage.save_history)
  8. Output: /tmp/domus-processed.json
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime
from glob import glob
from pathlib import Path

from dotenv import load_dotenv

from location_filter import needs_renovation, filter_listings_by_location
from scrapers.base import Listing
from scoring.text_scorer import TextScorer

from storage import (
    build_sent_index,
    listing_fingerprint,
    load_history,
    load_seen_ids,
    save_history,
    save_seen_ids,
    upsert_listing_record,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
SEEN_FILE = DATA_DIR / "seen_listings.json"
HISTORY_FILE = DATA_DIR / "listing_history.json"

BATCHES_DIR = "/tmp/domus-batches"
OUTPUT_PATH = "/tmp/domus-processed.json"


def load_batches() -> list[Listing]:
    """Load all batch files from /tmp/domus-batches/ and return Listing objects."""
    batch_files = sorted(glob(os.path.join(BATCHES_DIR, "*.json")))
    if not batch_files:
        logger.warning("No batch files found in %s", BATCHES_DIR)
        return []

    all_listings: list[Listing] = []
    for fpath in batch_files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                logger.warning("Skipping %s: not a list", fpath)
                continue
            count = len(data)
            for item in data:
                if not isinstance(item, dict):
                    continue
                listing = Listing(
                    id=str(item.get("id", "")),
                    platform=item.get("platform", "unknown"),
                    title=item.get("title", ""),
                    price=int(item.get("price", 0) or 0),
                    bedrooms=int(item.get("bedrooms", 0) or 0),
                    address=item.get("address", ""),
                    url=item.get("url", ""),
                    description=item.get("description", ""),
                    image_urls=list(item.get("image_urls", [])),
                    epc_label=item.get("epc_label"),
                    surface_m2=item.get("surface_m2"),
                    lot_surface_m2=item.get("lot_surface_m2"),
                    posted_date=item.get("posted_date"),
                    property_type=item.get("property_type", "house"),
                )
                all_listings.append(listing)
            logger.info("[process] Loaded %d listings from %s", count, fpath)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[process] Skipping batch file %s: %s", fpath, e)

    logger.info("[process] Total loaded: %d listings from %d batch files", len(all_listings), len(batch_files))
    return all_listings


def filter_houses(listings: list[Listing]) -> list[Listing]:
    """Keep only houses (skip appartments)."""
    before = len(listings)

    # Step 1: renovation filter
    listings = [l for l in listings if not needs_renovation(l)]

    # Step 2: property_type check
    listings = [l for l in listings if l.property_type == "house"]

    # Step 3: title-based backstop for appartementen
    before2 = len(listings)
    listings = [l for l in listings if "appartement" not in l.title.lower()]

    logger.info("[process] House filter: kept %d/%d (title catch removed %d)",
                len(listings), before, before2 - len(listings))
    return listings


def same_run_dedup(listings: list[Listing]) -> list[Listing]:
    """Deduplicate within the same run using listing_fingerprint."""
    seen_fps: set[str] = set()
    deduped: list[Listing] = []
    for l in listings:
        fp = listing_fingerprint(l)
        if fp not in seen_fps:
            seen_fps.add(fp)
            deduped.append(l)
    logger.info("[process] Same-run dedup: %d → %d", len(listings), len(deduped))
    return deduped


def og_image_fallback(listings: list[Listing]) -> list[Listing]:
    """Fetch og:image for listings without photos."""
    import urllib.request

    for listing in listings:
        if listing.image_urls:
            continue
        try:
            req = urllib.request.Request(listing.url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
                match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
                if match:
                    listing.image_urls = [match.group(1)]
        except Exception:
            pass

    return listings


def process() -> list[Listing]:
    """Main processing pipeline."""
    logger.info("=" * 50)
    logger.info("Phase 3 — Process & Score")
    logger.info("=" * 50)

    discovered_at = datetime.now()

    # 1. Load batches
    all_listings = load_batches()
    if not all_listings:
        logger.warning("[process] No listings to process. Writing empty output.")
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)
        return []

    # 2. Filter houses
    all_listings = filter_houses(all_listings)
    if not all_listings:
        logger.warning("[process] No houses after filtering.")
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)
        return []

    # 3. Same-run dedup
    all_listings = same_run_dedup(all_listings)

    # 4. Location filter
    before_filter = len(all_listings)
    all_listings = filter_listings_by_location(all_listings)
    logger.info("[process] Location filter: kept %d/%d", len(all_listings), before_filter)

    # 5. og:image fallback
    all_listings = og_image_fallback(all_listings)

    # 6. Text scoring
    logger.info("[process] DeepSeek text scoring...")
    text_scorer = TextScorer()
    all_listings = text_scorer.score_listings(all_listings)

    # Final score = text_score (photo scoring disabled per spec)
    for l in all_listings:
        if l.final_score is None:
            l.final_score = l.text_score

    # 7. Save to history
    seen_ids = load_seen_ids(SEEN_FILE)
    history = load_history(HISTORY_FILE)
    sent_keys, sent_fps = build_sent_index(history, seen_ids)
    logger.info("[process] Previously emailed: %d ids, %d fingerprints", len(sent_keys), len(sent_fps))

    for listing in all_listings:
        was_emailed = False  # Will be set to True after email is sent in Phase 4
        upsert_listing_record(history, listing, discovered_at, was_emailed=False)

    save_history(HISTORY_FILE, history)
    logger.info("[process] History saved (%d records)", len(history.get("records", {})))

    # 8. Write output
    serialised = [l.to_dict() for l in all_listings]
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(serialised, f, indent=2, ensure_ascii=False)
    logger.info("[process] Written %d listings to %s", len(serialised), OUTPUT_PATH)

    return all_listings


def main() -> None:
    process()


if __name__ == "__main__":
    main()
