#!/usr/bin/env python3
"""
Phase 1 — Collect: run all 5 scrapers in parallel, extract IDs only.

Output: /tmp/domus-collected-ids.json
  {"immoweb": ["id1", ...], "zimmo": [...], "immovlan": [...], "tweedehands": [...], "immoscoop": [...]}
"""

from __future__ import annotations

import json
import logging
import sys
import concurrent.futures

from dotenv import load_dotenv

from scrapers import ImmowebScraper, ImmovlanScraper, ImmoscoopScraper, TweeDeHandsScraper, ZimmoScraper

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

OUTPUT_PATH = "/tmp/domus-collected-ids.json"

SCRAPER_CLASSES = {
    "immoweb": ImmowebScraper,
    "zimmo": ZimmoScraper,
    "immoscoop": ImmoscoopScraper,
    "immovlan": ImmovlanScraper,
    "tweedehands": TweeDeHandsScraper,
}


def run_one_scraper(name: str) -> dict:
    """Run a single scraper and return {name: [ids...]}."""
    cls = SCRAPER_CLASSES[name]
    scraper = cls()
    try:
        listings = scraper.safe_scrape()
        ids = [str(l.id) for l in listings]
        logger.info("[collect] %s: %d IDs found", name, len(ids))
        return {name: ids}
    except Exception as e:
        logger.error("[collect] %s crashed: %s", name, e)
        return {name: []}


def collect() -> dict:
    """Run all scrapers in parallel with a per-scraper timeout."""
    all_ids: dict[str, list[str]] = {name: [] for name in SCRAPER_CLASSES}

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_map = {
            executor.submit(run_one_scraper, name): name
            for name in SCRAPER_CLASSES
        }
        for future in concurrent.futures.as_completed(future_map, timeout=120):
            name = future_map[future]
            try:
                result = future.result(timeout=60)
                all_ids.update(result)
            except concurrent.futures.TimeoutError:
                logger.error("[collect] %s timed out (>60s)", name)
                all_ids[name] = []
            except Exception as e:
                logger.error("[collect] %s failed: %s", name, e)
                all_ids[name] = []

    # Summary
    total = sum(len(v) for v in all_ids.values())
    logger.info("[collect] Total IDs collected: %d", total)
    for name, ids in sorted(all_ids.items()):
        logger.info("  %s: %d IDs", name, len(ids))

    return all_ids


def main() -> None:
    logger.info("=" * 50)
    logger.info("Phase 1 — Collect IDs")
    logger.info("=" * 50)

    all_ids = collect()

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(all_ids, f, indent=2)

    logger.info("Output written to %s", OUTPUT_PATH)


if __name__ == "__main__":
    main()
