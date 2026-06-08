#!/usr/bin/env python3
"""Enrich worker — draait in GEÏSOLEERD subprocess per listing.
Na elk proces wordt ALLE curl_cffi C-state opgeruimd door de OS."""

import sys
import json
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import Listing
from scrapers.immoweb import ImmowebScraper
from scrapers.immoscoop import ImmoscoopScraper
from scrapers.immovlan import ImmovlanScraper

PLATFORM_CLASSES = {
    "immoweb": ImmowebScraper,
    "immoscoop": ImmoscoopScraper,
    "immovlan": ImmovlanScraper,
}

# Alleen velden die Listing.__init__ accepteert
LISTING_FIELDS = [
    "id", "platform", "title", "price", "bedrooms", "address", "url",
    "description", "image_urls", "epc_label", "surface_m2", "lot_surface_m2",
    "posted_date", "property_type", "status", "text_score", "photo_score",
    "final_score", "score_reasoning",
]


def dict_to_listing(d: dict) -> Listing:
    kw = {k: d.get(k) for k in LISTING_FIELDS if k in d}
    return Listing(**kw)


def enrich_one() -> None:
    raw = json.loads(sys.stdin.read())
    listing_dict = raw["listing"]
    platform = listing_dict.get("platform", "")

    scraper_class = PLATFORM_CLASSES.get(platform)
    if not scraper_class:
        json.dump({"ok": True, "listing": listing_dict}, sys.stdout)
        return

    try:
        scraper = scraper_class()
        listing = dict_to_listing(listing_dict)
        enriched = scraper.enrich_listing(listing)
    except Exception as e:
        json.dump({"ok": False, "error": str(e), "listing": listing_dict}, sys.stdout)
        return

    result = {k: getattr(enriched, k, None) for k in LISTING_FIELDS}
    json.dump({"ok": True, "listing": result}, sys.stdout)


if __name__ == "__main__":
    enrich_one()
