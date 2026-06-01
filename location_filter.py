"""City/location filtering for accepted and excluded areas."""

from __future__ import annotations

import logging
import re

from scrapers.base import Listing
from config import ACCEPT_CITIES, EXCLUDE_CITIES_FINAL

logger = logging.getLogger(__name__)


def _normalize_city(name: str) -> str:
    """Normalize city name for comparison."""
    return name.strip().lower().replace("-", " ").replace("'", "")


def _city_in_list(haystack: str, city_list: list[str]) -> bool:
    """Check if any normalized city from list appears in haystack."""
    haystack_lower = haystack.lower()
    for city in city_list:
        norm = _normalize_city(city)
        if norm in haystack_lower:
            return True
    return False


def is_city_accepted(listing: Listing) -> bool:
    """Check if a listing is in an accepted city."""
    haystack = " ".join([
        listing.title or "",
        listing.address or "",
        listing.description[:500] if listing.description else "",
    ])
    return _city_in_list(haystack, ACCEPT_CITIES)


def is_city_excluded(listing: Listing) -> bool:
    """Check if a listing is in an excluded city."""
    if not EXCLUDE_CITIES_FINAL:
        return False
    haystack = " ".join([
        listing.title or "",
        listing.address or "",
        listing.description[:500] if listing.description else "",
    ])
    return _city_in_list(haystack, EXCLUDE_CITIES_FINAL)


def needs_renovation(listing: Listing) -> bool:
    """Check if listing description suggests renovation needed."""
    from config import SKIP_RENOVATION
    if not SKIP_RENOVATION:
        return False

    haystack = " ".join([
        listing.title or "",
        listing.description[:1000] if listing.description else "",
    ]).lower()

    renov_keywords = [
        "te renoveren", "op te frissen", "renovatie", "renoveren",
        "volledige renovatie", "dringend aan renovatie", "verouderd",
        "achterstallig onderhoud", "grondige renovatie", "onderhoudsgevoelig",
        "asbest", "vochtprobleem", "stabiliteitsproblemen",
    ]
    return any(kw in haystack for kw in renov_keywords)


def filter_listings_by_location(listings: list[Listing]) -> list[Listing]:
    """Filter listings: keep only accepted cities, remove excluded."""
    filtered: list[Listing] = []

    for listing in listings:
        if not is_city_accepted(listing):
            logger.debug(f"Skipping {listing.platform}:{listing.id} — not in accepted cities")
            continue
        if is_city_excluded(listing):
            logger.debug(f"Skipping {listing.platform}:{listing.id} — in excluded city")
            continue
        if needs_renovation(listing):
            logger.debug(f"Skipping {listing.platform}:{listing.id} — needs renovation")
            continue
        filtered.append(listing)

    logger.info(f"Location filter: kept {len(filtered)}/{len(listings)} listings")
    return filtered
