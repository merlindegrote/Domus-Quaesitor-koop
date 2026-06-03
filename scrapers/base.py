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

import requests

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


def _spawn_fetch(url: str, timeout: int = 15) -> requests.Response:
    """Fetch URL using in-process requests with daemon thread timeout.
    
    Replaced subprocess-based curl_runner.py (was causing hang after ~40 calls
    due to pipe deadlock / orphan processes / FD leak). Uses a daemon thread
    with join timeout to ensure reliable timeout even when C-level SSL/network
    calls block the GIL.
    """
    result: list[requests.Response] = []
    exception: list[Exception] = []

    def _do():
        try:
            headers = {"Accept-Language": "nl-BE,nl;q=0.9"}
            sess = requests.Session()
            sess.headers.update(headers)
            r = sess.get(url, timeout=timeout)
            r.raise_for_status()
            result.append(r)
        except Exception as e:
            exception.append(e)

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=timeout + 10)

    if exception:
        raise exception[0]
    if not result:
        raise RuntimeError(f"Timeout fetching {url[:100]} after {timeout + 10}s")
    return result[0]


class BaseScraper(ABC):
    PLATFORM_NAME: str = "base"
    REQUEST_DELAY: float = 1.5
    MAX_RETRIES: int = 2

    def __init__(self):
        self._last_request_time: float = 0

    def _rate_limited_get(self, url: str, timeout: int = 15, **kwargs) -> requests.Response:
        elapsed = time.time() - self._last_request_time
        delay = self.REQUEST_DELAY + random.uniform(0.5, 2.0)
        if elapsed < delay:
            sleep_time = delay - elapsed
            logger.debug(f"[{self.PLATFORM_NAME}] Rate limiting: sleeping {sleep_time:.1f}s")
            time.sleep(sleep_time)

        logger.info(f"[{self.PLATFORM_NAME}] GET {url}")
        self._last_request_time = time.time()

        return _spawn_fetch(url, timeout)

    def _get_with_fallback(self, url: str, **kwargs) -> requests.Response | None:
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
