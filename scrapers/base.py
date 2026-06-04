"""Base scraper infrastructure and Listing dataclass.

Worker: spawn a fresh subprocess per request (curl_runner.py) for reliable
OS-level SIGKILL when requests hang in C-level code (curl_cffi SSL/network).
"""
import json as json_mod
import logging
import os
import random
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

_IMPERSONATE_BROWSERS = ["chrome124", "chrome123", "safari17_0"]


@dataclass
class Listing:
    id: str
    platform: str
    title: str
    price: float
    bedrooms: int
    address: str
    url: str
    description: str = ""
    image_urls: list[str] = field(default_factory=list)
    epc_label: str = ""
    surface_m2: float = 0.0
    lot_surface_m2: float = 0.0
    posted_date: str = ""
    property_type: str = "house"
    status: str = ""
    text_score: float = 0.0
    photo_score: float = 0.0
    final_score: float = 0.0
    score_reasoning: str = ""

    @property
    def unique_key(self) -> str:
        return f"{self.platform}:{self.id}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "platform": self.platform,
            "title": self.title,
            "price": self.price,
            "bedrooms": self.bedrooms,
            "address": self.address,
            "url": self.url,
            "description": self.description,
            "image_urls": self.image_urls,
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


def _spawn_fetch(url: str, timeout: int = 15, extra_headers: dict | None = None) -> curl_requests.Response:
    """Fetch URL using daemon thread with hard timeout.

    Wraps curl_cffi in a daemon thread so a stuck C-level SSL/network call
    can be abandoned after the grace period, preventing indefinite hangs.
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    }
    if extra_headers:
        headers.update(extra_headers)

    browser = random.choice(_IMPERSONATE_BROWSERS)

    result: list[curl_requests.Response] = []
    exception: list[Exception] = []

    def _do_fetch():
        try:
            sess = curl_requests.Session()
            sess.headers.update(headers)
            r = sess.get(url, timeout=timeout, impersonate=browser)
            r.raise_for_status()
            result.append(r)
        except Exception as e:
            exception.append(e)

    t = threading.Thread(target=_do_fetch, daemon=True)
    t.start()
    t.join(timeout=timeout + 5)  # extra 5s grace for connection setup

    if exception:
        raise exception[0]
    if not result:
        raise TimeoutError(f"curl_cffi GET {url} timed out after {timeout}s")
    return result[0]


class BaseScraper(ABC):
    PLATFORM_NAME: str = "base"
    REQUEST_DELAY: float = 1.5
    MAX_RETRIES: int = 2

    def __init__(self):
        self._last_request_time: float = 0

    def _rate_limited_get(self, url: str, timeout: int = 15, **kwargs) -> curl_requests.Response:
        elapsed = time.time() - self._last_request_time
        delay = self.REQUEST_DELAY + random.uniform(0.5, 2.0)
        if elapsed < delay:
            sleep_time = delay - elapsed
            logger.debug(f"[{self.PLATFORM_NAME}] Rate limiting: sleeping {sleep_time:.1f}s")
            time.sleep(sleep_time)

        logger.info(f"[{self.PLATFORM_NAME}] GET {url}")
        self._last_request_time = time.time()

        extra_headers = kwargs.pop("headers", {})
        return _spawn_fetch(url, timeout, extra_headers=extra_headers)

    def _get_with_fallback(self, url: str, **kwargs) -> curl_requests.Response | None:
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
        ...

    def safe_scrape(self) -> list[Listing]:
        try:
            return self.scrape()
        except Exception as e:
            logger.error(f"[{self.PLATFORM_NAME}] Scrape failed: {e}", exc_info=True)
            return []
