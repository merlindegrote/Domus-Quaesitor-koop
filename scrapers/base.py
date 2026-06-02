"""Base scraper infrastructure and Listing dataclass."""

from __future__ import annotations

import logging
import random
import signal
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import requests
from curl_cffi import requests as curl_requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# Browser versions to impersonate (curl_cffi TLS fingerprints)
_IMPERSONATE_BROWSERS = [
    "chrome124",
    "chrome123",
    "chrome120",
    "safari17_0",
]


@dataclass
class Listing:
    """Standardized rental listing across all platforms."""

    id: str                           # platform-specific unique ID
    platform: str                     # "immoweb" | "zimmo" | "immoscoop"
    title: str
    price: int                        # monthly rent in EUR
    bedrooms: int
    address: str
    url: str                          # direct link to listing
    description: str                  # full description text (usually Dutch)
    image_urls: list[str] = field(default_factory=list)
    epc_label: Optional[str] = None   # energy label if available
    surface_m2: Optional[int] = None
    lot_surface_m2: Optional[int] = None
    posted_date: Optional[str] = None
    property_type: str = "house"
    status: Optional[str] = None  # "under_option" | "life_annuity" | None

    # Scoring fields (populated later)
    text_score: Optional[float] = None
    photo_score: Optional[float] = None
    final_score: Optional[float] = None
    score_reasoning: Optional[str] = None

    @property
    def unique_key(self) -> str:
        """Unique identifier across platforms."""
        return f"{self.platform}_{self.id}"

    def to_dict(self) -> dict:
        """Serialize for JSON storage."""
        return {
            "id": self.id,
            "platform": self.platform,
            "title": self.title,
            "price": self.price,
            "bedrooms": self.bedrooms,
            "address": self.address,
            "url": self.url,
            "description": self.description[:500],  # truncate for storage
            "image_urls": self.image_urls[:5],
            "epc_label": self.epc_label,
            "surface_m2": self.surface_m2,
            "lot_surface_m2": self.lot_surface_m2,
            "posted_date": self.posted_date,
            "property_type": self.property_type,
            "status": self.status,
            "text_score": self.text_score,
            "photo_score": self.photo_score,
            "final_score": self.final_score,
            "score_reasoning": self.score_reasoning,
        }


class BaseScraper(ABC):
    """Base class for all rental listing scrapers."""

    PLATFORM_NAME: str = "base"
    REQUEST_DELAY: float = 1.5  # seconds between requests
    MAX_RETRIES: int = 2

    def __init__(self):
        self._last_request_time: float = 0

    def _rate_limited_get(self, url: str, timeout: int = 30, **kwargs) -> requests.Response:
        """Make a rate-limited GET request using curl_cffi with TLS impersonation.

        curl_cffi mimics a real browser's TLS fingerprint, which is the primary
        signal Cloudflare and CloudFront use to detect bots.

        Uses SIGALRM as a hard timeout to prevent curl_cffi from hanging indefinitely.
        """
        elapsed = time.time() - self._last_request_time
        delay = self.REQUEST_DELAY + random.uniform(0.5, 2.0)
        if elapsed < delay:
            sleep_time = delay - elapsed
            logger.debug(f"[{self.PLATFORM_NAME}] Rate limiting: sleeping {sleep_time:.1f}s")
            time.sleep(sleep_time)

        logger.info(f"[{self.PLATFORM_NAME}] GET {url}")
        self._last_request_time = time.time()

        class TimeoutError(Exception):
            pass

        def _handler(_signum, _frame):
            raise TimeoutError(f"Request timed out after {timeout + 5}s")

        # Set hard alarm: timeout + 5s grace for the curl call itself
        signal.signal(signal.SIGALRM, _handler)
        signal.alarm(timeout + 5)

        try:
            browser = random.choice(_IMPERSONATE_BROWSERS)
            req_headers = {
                "Accept-Language": "nl-BE,nl;q=0.9,en-US;q=0.8,en;q=0.7",
            }
            # Merge extra headers uit kwargs (bv. Accept, X-Requested-With)
            extra_headers = kwargs.pop("headers", {})
            if isinstance(extra_headers, dict):
                req_headers.update(extra_headers)
            response = curl_requests.get(
                url,
                impersonate=browser,
                timeout=timeout,
                headers=req_headers,
                **kwargs,
            )
            response.raise_for_status()
            return response
        finally:
            signal.alarm(0)  # disarm

    def _get_with_fallback(self, url: str, **kwargs) -> requests.Response | None:
        """Fetch a URL with error handling. Returns None on failure."""
        try:
            return self._rate_limited_get(url, **kwargs)
        except Exception as e:
            status = ""
            if hasattr(e, "response") and e.response is not None:
                status = f" (HTTP {e.response.status_code})"
            logger.warning(f"[{self.PLATFORM_NAME}] Failed to fetch{status}: {e}")
            return None

    @abstractmethod
    def scrape(self) -> list[Listing]:
        """Scrape listings from the platform. Returns a list of Listing objects."""
        ...

    def safe_scrape(self) -> list[Listing]:
        """Scrape with error handling — never crashes the pipeline."""
        try:
            listings = self.scrape()
            logger.info(f"[{self.PLATFORM_NAME}] ✅ Found {len(listings)} listings")
            return listings
        except Exception as e:
            logger.error(f"[{self.PLATFORM_NAME}] ❌ Scraping failed: {e}", exc_info=True)
            return []
