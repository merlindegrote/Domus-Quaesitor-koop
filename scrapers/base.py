"""Base scraper infrastructure and Listing dataclass.

Worker: spawn a fresh subprocess per request (curl_runner.py) for reliable
OS-level SIGKILL when requests hang in C-level code (curl_cffi SSL/network).
"""
import json as json_mod
import logging
import os
import random
import signal
import subprocess
import sys
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
    """Spawn a fresh subprocess to curl an URL. Returns Response on success."""
    runner = os.path.join(os.path.dirname(__file__), "curl_runner.py")
    headers = json_mod.dumps({"Accept-Language": "nl-BE,nl;q=0.9"})
    kwargs = json_mod.dumps({})
    proc = subprocess.run(
        [sys.executable, "-u", runner, url, str(timeout), headers, kwargs],
        capture_output=True, text=True, timeout=timeout + 10,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()[:200]
        raise RuntimeError(f"curl_runner rc={proc.returncode}: {stderr}")

    result = json_mod.loads(proc.stdout.strip())
    if result.get("ok"):
        resp = requests.Response()
        resp.status_code = 200
        resp._content = result["text"].encode("utf-8")
        resp.encoding = "utf-8"
        return resp
    else:
        raise RuntimeError(result.get("err", "Unknown error"))


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
