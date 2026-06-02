#!/usr/bin/env python3
"""
Phase 2 — Scrape Batch: fetch detail pages for up to 10 IDs from one scraper.

Usage:
  python3 phases/scrape_batch.py --scraper immoweb --ids id1,id2 --output /tmp/batch-1.json

Timeout: 60s per batch. Per-ID errors skip only that ID.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import urllib.request

from dotenv import load_dotenv

from scrapers.base import Listing
from scrapers import (
    ImmowebScraper,
    ImmovlanScraper,
    ImmoscoopScraper,
    TweeDeHandsScraper,
    ZimmoScraper,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

SCRAPER_LOOKUP = {
    "immoweb": ImmowebScraper,
    "zimmo": ZimmoScraper,
    "immoscoop": ImmoscoopScraper,
    "immovlan": ImmovlanScraper,
    "tweedehands": TweeDeHandsScraper,
}


def _build_url(platform: str, listing_id: str) -> str:
    """Construct a detail-page URL from the platform name and ID."""
    urls = {
        "immoweb": f"https://www.immoweb.be/en/classified/house/for-sale/{listing_id}",
        "zimmo": f"https://www.zimmo.be/nl/detail/{listing_id}",
        "immoscoop": f"https://www.immoscoop.be/detail/{listing_id}",
        "immovlan": f"https://www.immovlan.be/nl/detail/{listing_id}",
        "tweedehands": f"https://www.2dehands.be/l/immo/q/{listing_id}/",
    }
    return urls.get(platform, "")


def _og_image_fallback(listing: Listing) -> None:
    """Universal fallback: fetch og:image for listings without photos."""
    if listing.image_urls or not listing.url:
        return
    try:
        req = urllib.request.Request(listing.url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
            match = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
            if match:
                listing.image_urls = [match.group(1)]
    except Exception:
        pass


def scrape_batch(platform: str, ids: list[str], output_path: str) -> list[dict]:
    """Scrape detail pages for a batch of IDs and write results to JSON."""
    scraper_cls = SCRAPER_LOOKUP.get(platform)
    if not scraper_cls:
        logger.error("Unknown scraper: %s", platform)
        return []

    scraper = scraper_cls()
    results: list[Listing] = []

    for listing_id in ids:
        listing_id = listing_id.strip()
        if not listing_id:
            continue
        try:
            url = _build_url(platform, listing_id)
            listing = Listing(
                id=listing_id,
                platform=platform,
                title="",
                price=0,
                bedrooms=0,
                address="",
                url=url,
                description="",
                image_urls=[],
            )
            enriched = scraper.enrich_listing(listing)
            _og_image_fallback(enriched)
            results.append(enriched)
            logger.info("[batch] %s:%s — scraped OK (desc=%d, images=%d, type=%s)",
                        platform, listing_id,
                        len(enriched.description or ""),
                        len(enriched.image_urls or []),
                        enriched.property_type)
        except Exception as e:
            logger.warning("[batch] %s:%s — error: %s", platform, listing_id, e)
            # Still emit a skeleton record so orphan tracking is possible
            results.append(Listing(
                id=listing_id,
                platform=platform,
                title="",
                price=0,
                bedrooms=0,
                address="",
                url=_build_url(platform, listing_id),
                description="",
                image_urls=[],
            ))

    serialised = [l.to_dict() for l in results]
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(serialised, f, indent=2, ensure_ascii=False)

    logger.info("[batch] %s: %d/%d OK — written to %s", platform, len(results), len(ids), output_path)
    return serialised


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape one batch of detail pages")
    parser.add_argument("--scraper", required=True, choices=list(SCRAPER_LOOKUP))
    parser.add_argument("--ids", required=True, help="Comma-separated listing IDs")
    parser.add_argument("--output", required=True, help="Output JSON path")
    args = parser.parse_args()

    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    if len(ids) > 10:
        logger.warning("Batch truncated to 10 IDs (was %d)", len(ids))
        ids = ids[:10]

    logger.info("Scraping %s batch: %d IDs → %s", args.scraper, len(ids), args.output)
    scrape_batch(args.scraper, ids, args.output)


if __name__ == "__main__":
    main()


