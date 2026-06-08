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
from pathlib import Path

from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

_IMPERSONATE_BROWSERS = ["chrome124", "chrome123", "safari17_0"]

# ── Scaler Health self-learning systeem ──────────────────────────────
_HEALTH_FILE = Path("/tmp/domus-scraper-health.json")
_HEALTH_LOCK = threading.Lock()
_SKIP_HOURS = 24  # skip scraper for 24h after 3 consecutive failures
_MAX_CONSECUTIVE_FAILURES = 3


def _load_health() -> dict:
    """Load scraper health from disk."""
    if _HEALTH_FILE.exists():
        try:
            with open(_HEALTH_FILE) as f:
                return json_mod.load(f)
        except (json_mod.JSONDecodeError, OSError):
            pass
    return {}


def _save_health(health: dict):
    """Save scraper health to disk."""
    with _HEALTH_LOCK:
        with open(_HEALTH_FILE, "w") as f:
            json_mod.dump(health, f, indent=2)


def record_scraper_success(platform: str, run_time: float):
    """Record a successful scrape run. Resets consecutive failures counter."""
    with _HEALTH_LOCK:
        health = _load_health()
        entry = health.setdefault(platform, {
            "successes": 0,
            "failures": 0,
            "consecutive_failures": 0,
            "last_error": "",
            "total_run_time": 0.0,
            "run_count": 0,
            "skip_until": 0,
            "avg_run_time": 0.0,
        })
        entry["successes"] += 1
        entry["consecutive_failures"] = 0
        entry["skip_until"] = 0  # reset skip
        entry["total_run_time"] += run_time
        entry["run_count"] += 1
        entry["avg_run_time"] = round(entry["total_run_time"] / entry["run_count"], 1)
        entry["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save_health(health)


def record_scraper_failure(platform: str, error: str):
    """Record a failed scrape run. If 3 consecutive failures, set skip_until."""
    with _HEALTH_LOCK:
        health = _load_health()
        entry = health.setdefault(platform, {
            "successes": 0,
            "failures": 0,
            "consecutive_failures": 0,
            "last_error": "",
            "total_run_time": 0.0,
            "run_count": 0,
            "skip_until": 0,
            "avg_run_time": 0.0,
        })
        entry["failures"] += 1
        entry["consecutive_failures"] += 1
        entry["last_error"] = str(error)[:300]
        entry["last_run"] = time.strftime("%Y-%m-%dT%H:%M:%S")

        if entry["consecutive_failures"] >= _MAX_CONSECUTIVE_FAILURES:
            skip_ts = time.time() + (_SKIP_HOURS * 3600)
            entry["skip_until"] = skip_ts
            logger.warning(
                f"[{platform}] {entry['consecutive_failures']} consecutive failures → "
                f"skip for {_SKIP_HOURS}h (until {time.strftime('%Y-%m-%d %H:%M', time.localtime(skip_ts))})"
            )

        _save_health(health)


def is_scraper_skipped(platform: str) -> bool:
    """Check if a scraper is currently in skip mode due to repeated failures."""
    health = _load_health()
    entry = health.get(platform, {})
    skip_until = entry.get("skip_until", 0)
    if skip_until and skip_until > time.time():
        remaining = int(skip_until - time.time())
        remaining_h = remaining // 3600
        remaining_m = (remaining % 3600) // 60
        logger.info(f"[{platform}] Skipped (rate limit protection, {remaining_h}h{remaining_m}m remaining)")
        return True
    return False


def show_health_summary() -> str:
    """Return a human-readable summary of all scraper health."""
    health = _load_health()
    parts = []
    for platform, h in sorted(health.items()):
        status = "✅" if h.get("consecutive_failures", 0) == 0 else "⚠️"
        skip = ""
        if h.get("skip_until", 0) > time.time():
            skip = " 🔇 SKIPPED"
        parts.append(
            f"  {status} {platform}: "
            f"{h.get('successes', 0)}✓/{h.get('failures', 0)}✗ "
            f"(avg {h.get('avg_run_time', 0)}s){skip}"
        )
    return "Scraper Health:\n" + "\n".join(parts) if parts else "Scraper Health: (empty)"


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
    """Fetch URL using daemon thread with hard timeout and retry with exponential backoff.

    Wraps curl_cffi in a daemon thread so a stuck C-level SSL/network call
    can be abandoned after the grace period, preventing indefinite hangs.

    Retries on:
    - Timeout / ConnectionError / SSLError (transient)
    - HTTP 429 (rate limit — waits Retry-After header or exponential backoff)
    - HTTP 5xx (server errors, retryable)

    Does NOT retry: HTTP 4xx (except 429), DNS resolution failures after 1st attempt
    """
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    }
    if extra_headers:
        headers.update(extra_headers)

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        browser = random.choice(_IMPERSONATE_BROWSERS)
        result: list[curl_requests.Response] = []
        exception: list[Exception] = []

        def _do_fetch():
            sess = None
            try:
                sess = curl_requests.Session()
                sess.headers.update(headers)
                r = sess.get(url, timeout=timeout, impersonate=browser)
                r.raise_for_status()
                result.append(r)
            except Exception as e:
                exception.append(e)
            finally:
                if sess is not None:
                    try:
                        sess.close()
                    except Exception:
                        pass

        t = threading.Thread(target=_do_fetch, daemon=True)
        t.start()
        t.join(timeout=timeout + 5)  # extra 5s grace for connection setup

        if not exception and result:
            return result[0]

        if exception:
            exc = exception[0]
            # Determine if this is retryable
            retryable = False
            retry_delay = min(2 ** attempt + random.uniform(0.5, 2.0), 30)

            if hasattr(exc, "response") and exc.response is not None:
                status = exc.response.status_code
                if status == 429:
                    retryable = True
                    # Respect Retry-After
                    retry_after = exc.response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            retry_delay = max(int(retry_after), retry_delay)
                        except ValueError:
                            pass
                elif status >= 500:
                    retryable = True
            elif isinstance(exc, (TimeoutError, ConnectionError)):
                retryable = True
            elif "SSL" in type(exc).__name__ or "sslv3" in str(exc).lower():
                retryable = True

            if attempt < max_retries and retryable:
                logger.warning(f"Retry {attempt}/{max_retries} for {url} after {retry_delay:.0f}s: {type(exc).__name__}")
                time.sleep(retry_delay)
                continue

            raise exc

        if attempt < max_retries:
            retry_delay = min(2 ** attempt + random.uniform(0.5, 2.0), 30)
            logger.warning(f"Retry {attempt}/{max_retries} for {url} after timeout+{retry_delay:.0f}s")
            time.sleep(retry_delay)

    if exception:
        raise exception[0]
    raise TimeoutError(f"curl_cffi GET {url} timed out after {timeout}s (retried {max_retries}x)")


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
