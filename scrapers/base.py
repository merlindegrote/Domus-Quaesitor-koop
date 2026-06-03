"""Base scraper infrastructure and Listing dataclass."""

from __future__ import annotations

import json as json_mod
import logging
import os
import random
import subprocess
import sys
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
    "safari17_0",
]


@dataclass
class Listing:
    """Standardized rental listing across all platforms."""

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


class BaseScraper(ABC):
    """Base class for all rental listing scrapers."""

    PLATFORM_NAME: str = "base"
    REQUEST_DELAY: float = 1.5  # seconds between requests
    MAX_RETRIES: int = 2

    def __init__(self):
        self._last_request_time: float = 0

    def _rate_limited_get(self, url: str, timeout: int = 30, **kwargs) -> requests.Response:
        """Make a rate-limited GET request via subprocess.

        curl_cffi's C extension can hang in SSL/network code where Python
        signal handlers are never delivered.  By running the curl call in a
        separate subprocess we can kill it with OS-level SIGKILL via
        subprocess.TimeoutExpired.
        """
        elapsed = time.time() - self._last_request_time
        delay = self.REQUEST_DELAY + random.uniform(0.5, 2.0)
        if elapsed < delay:
            sleep_time = delay - elapsed
            logger.debug(f"[{self.PLATFORM_NAME}] Rate limiting: sleeping {sleep_time:.1f}s")
            time.sleep(sleep_time)

        logger.info(f"[{self.PLATFORM_NAME}] GET {url}")
        self._last_request_time = time.time()

        req_headers = {
            "Accept-Language": "nl-BE,nl;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        extra_headers = kwargs.pop("headers", {})
        if isinstance(extra_headers, dict):
            req_headers.update(extra_headers)

        runner = os.path.join(os.path.dirname(__file__), "curl_runner.py")
        if not os.path.exists(runner):
            raise RuntimeError(f"curl_runner.py not found at {runner}")

        args = [
            sys.executable, "-u", runner, url, str(timeout),
            json_mod.dumps(req_headers), json_mod.dumps(kwargs),
        ]

        try:
            proc = subprocess.run(
                args,
                capture_output=True, text=True,
                timeout=timeout + 10,
            )

            # Parse JSON output from subprocess
            out_line = proc.stdout.strip()
            if not out_line:
                lines = [l for l in proc.stdout.split("\n") if l.strip()]
                out_line = lines[-1] if lines else "{}"

            result = json_mod.loads(out_line)

            if result.get("ok"):
                resp = requests.Response()
                resp.status_code = 200
                resp._content = result["text"].encode("utf-8")
                resp.encoding = "utf-8"
                return resp
            else:
                err_msg = result.get("err", "Unknown subprocess error")
                raise RuntimeError(err_msg)

        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"Request timed out after {timeout + 10}s (subprocess killed)"
            )
        except json_mod.JSONDecodeError:
            raise RuntimeError(
                f"Bad JSON from subprocess: {proc.stdout[:300]}"
            )
        except Exception:
            raise

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
        """Wrapper that catches and logs all exceptions during scraping."""
        try:
            return self.scrape()
        except Exception as e:
            logger.error(f"[{self.PLATFORM_NAME}] Scrape failed: {e}", exc_info=True)
            return []
