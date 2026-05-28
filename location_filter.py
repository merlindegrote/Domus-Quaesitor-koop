"""Heuristic location filtering for proximity preferences."""

from __future__ import annotations

import os
from dataclasses import dataclass

from scrapers.base import Listing
from config import (
    ENABLE_STATION_FILTER,
    STATION_NEAR_KEYWORDS,
    STATION_FAR_KEYWORDS,
)


@dataclass(frozen=True)
class StationFilterResult:
    keep: bool
    score: int
    reason: str


def station_filter_enabled() -> bool:
    """Check whether location filtering is enabled."""
    return ENABLE_STATION_FILTER


def assess_station_proximity(listing: Listing) -> StationFilterResult:
    """Estimate how suitable a listing is based on location keywords."""
    if not station_filter_enabled():
        return StationFilterResult(keep=True, score=0, reason="location filtering disabled")

    haystack = " ".join(
        [
            listing.title or "",
            listing.address or "",
            listing.description[:400] if listing.description else "",
        ]
    ).lower()

    near_hits = [keyword for keyword in STATION_NEAR_KEYWORDS if keyword in haystack]
    far_hits = [keyword for keyword in STATION_FAR_KEYWORDS if keyword in haystack]

    score = 0
    if near_hits:
        score += 2
    if far_hits:
        score -= 2

    keep = score >= 0 and not far_hits
    if near_hits:
        return StationFilterResult(keep=keep, score=score, reason=f"near preferred location: {near_hits[0]}")
    if far_hits:
        return StationFilterResult(keep=False, score=score, reason=f"in excluded location: {far_hits[0]}")
    return StationFilterResult(keep=True, score=score, reason="no location preference matches")


def filter_listings_for_station(listings: list[Listing]) -> list[Listing]:
    """Keep only listings that fit the proximity preference."""
    if not station_filter_enabled():
        return listings

    filtered: list[Listing] = []
    for listing in listings:
        result = assess_station_proximity(listing)
        if result.keep:
            filtered.append(listing)
    return filtered
