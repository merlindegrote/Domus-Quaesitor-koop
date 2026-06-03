"""Base scraper infrastructure and Listing dataclass.

Uses a persistent curl_cffi worker subprocess to avoid per-request spawn overhead.
"""
import json as json_mod
import logging
import os
import random
import subprocess
import sys
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


class _CurlWorker:
    """Persistent subprocess worker for curl_cffi requests.

    Avoids spawning a new process for every request, reducing memory pressure.
    One worker process handles all requests sequentially via stdin/stdout.
    """

    _instance = None
    _proc: subprocess.Popen | None = None
    _dirty: bool = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def start(self):
        if self._dirty:
            self.stop()
            self._dirty = False
        if self._proc is not None and self._proc.poll() is None:
            return  # already running
        # Kill any stale worker
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None
        runner = os.path.join(os.path.dirname(__file__), "curl_worker.py")
        self._proc = subprocess.Popen(
            [sys.executable, "-u", runner, '{"Accept-Language": "nl-BE,nl;q=0.9"}'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.stdin.write("__EXIT__\n")
            self._proc.stdin.flush()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._proc = None

    def fetch(self, url: str, timeout: int = 15) -> requests.Response:
        self.start()
        req_id = str(abs(hash(url + str(timeout))))
        req = json_mod.dumps({"url": url, "timeout": timeout, "id": req_id})
        self._proc.stdin.write(req + "\n")
        self._proc.stdin.flush()

        # Daemon thread for readline timeout (avoids ThreadPoolExecutor deadlock)
        _out_line = None
        _exc = None
        def _reader():
            nonlocal _out_line, _exc
            try:
                _out_line = self._proc.stdout.readline()
            except Exception as e:
                _exc = e
        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=timeout + 15)
        if t.is_alive():
            self._dirty = True
            self.stop()
            raise RuntimeError(f"Request timed out after {timeout + 15}s")
        if _exc:
            self.stop()
            raise RuntimeError(f"Worker read error: {_exc}")
        out_line = (_out_line or "").strip()

        try:
            result = json_mod.loads(out_line)
        except json_mod.JSONDecodeError:
            self.stop()
            raise RuntimeError(f"Worker returned garbage: {out_line[:100]}")

        if result.get("ok"):
            filepath = result.get("file")
            text = ""
            if filepath and os.path.exists(filepath):
                with open(filepath) as f:
                    full = json_mod.load(f)
                text = full.get("text", "")
                try:
                    os.unlink(filepath)
                except OSError:
                    pass
            else:
                text = result.get("text", "")
            resp = requests.Response()
            resp.status_code = 200
            resp._content = text.encode("utf-8")
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

        try:
            return _CurlWorker().fetch(url, timeout)
        except subprocess.TimeoutExpired:
            # Worker was killed by timeout
            raise RuntimeError(f"Request timed out after {timeout}s")
        except Exception as e:
            raise

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
