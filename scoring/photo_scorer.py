"""AI photo scoring using OpenRouter (Gemini 2.5 Flash Lite) for house quality.

Cost optimization:
  - Enkel top 25 listings scoren (na text scoring, wat in email komt)
  - Alle fotos van 1 huis in 1 API call (max 8 fotos, zelfde prijs als 1)
  - Geen delay tussen calls (OpenRouter heeft geen rate limit probleem)
  - Max 1 retry (niet blijven proberen als het faalt)
  - EPC D/E/F/G overslaan (slecht energielabel = slechte foto's gegarandeerd)
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from scrapers.base import Listing

logger = logging.getLogger(__name__)

DEFAULT_SCORE = 5.0
OPENROUTER_BASE = "https://openrouter.ai/api/v1/chat/completions"

# EPC-labels die we overslaan — geen credits verspillen aan bouwvallig
_BAD_EPC = {"d", "e", "f", "g", "h"}


class PhotoScorer:
    """Score listing photos via OpenRouter + Gemini 2.5 Flash Lite."""

    MODEL = "google/gemini-2.5-flash-lite"
    MAX_PHOTOS = 8          # alle fotos in 1 call
    SCORE_TOP_N = 25        # enkel de top-N na text scoring
    MAX_RETRIES = 1
    RETRY_DELAY = 2

    PROMPT = """Rate the quality of this house 1-10 based on these photos.

1-2: very old/dirty, full renovation needed
3-4: dated, needs work
5-6: average, some modern touches
7-8: good condition, clean, well-maintained
9-10: excellent, modern, high quality finishes

Respond ONLY valid JSON, no markdown:
{"photo_score": number, "reasoning": "korte observatie in Nederlands"}"""

    def __init__(self):
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.client_available = bool(self.api_key)
        logger.info(f"📸 OpenRouter: {self.MODEL}, top {self.SCORE_TOP_N}, EPC D+ skip")

    @property
    def is_available(self) -> bool:
        return self.client_available

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _bad_epc(epc: str | None) -> bool:
        """Skip photo scoring for bad energy labels (D/E/F/G/H)."""
        if not epc:
            return False
        epc = epc.strip().lower().replace("+", "").replace("-", "")
        return epc in _BAD_EPC

    @staticmethod
    def _compute_final(listing: Listing) -> float:
        text = listing.text_score or DEFAULT_SCORE
        photo = listing.photo_score
        if photo is not None:
            return round(text * 0.6 + photo * 0.4, 1)
        return text

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.MULTILINE).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        match = re.search(r'\{[^{}]*"photo_score"[^{}]*\}', cleaned)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return None

    @staticmethod
    def _is_placeholder(url: str) -> bool:
        indicators = ["placeholder", "no-image", "noimage", "default", "missing", "blank", "1x1", "pixel"]
        return any(ind in url.lower() for ind in indicators)

    # ── core ─────────────────────────────────────────────────

    def score_listing(self, listing: Listing) -> float | None:
        """Score 1 listing. Returns None if no valid images or bad EPC."""
        if self._bad_epc(listing.epc_label):
            logger.debug(f"[photo] EPC {listing.epc_label} — overslaan")
            return None

        urls = [u for u in listing.image_urls[:self.MAX_PHOTOS]
                if u.startswith("http") and not self._is_placeholder(u)]
        if not urls:
            return None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                score = self._call_openrouter(urls)
                if score is not None:
                    return score
                return DEFAULT_SCORE
            except Exception as e:
                if "rate" in str(e).lower() or "429" in str(e):
                    logger.warning(f"Rate limit, retry {attempt + 1}")
                    continue
                logger.warning(f"Photo score failed: {e}")
                if attempt < self.MAX_RETRIES:
                    continue
                return None
        return None

    def score_listings(self, listings: list[Listing]) -> list[Listing]:
        """Score top N listings. EPC D/E/F/G en listings zonder fotos overgeslagen."""
        to_score = listings[:self.SCORE_TOP_N]
        skipped = max(0, len(listings) - self.SCORE_TOP_N)
        skipped_epc = 0
        skipped_noimg = 0
        scored = 0

        # Pre-processing: EPC D+ / geen fotos skippen
        photo_tasks = []
        for listing in to_score:
            addr = listing.address or listing.title or "?"
            if self._bad_epc(listing.epc_label):
                listing.photo_score = None
                listing.final_score = self._compute_final(listing)
                skipped_epc += 1
                print(f"  photo ⏭ EPC {listing.epc_label} — {addr[:50]}")
                continue

            valid_urls = [u for u in listing.image_urls[:self.MAX_PHOTOS]
                          if u.startswith("http") and not self._is_placeholder(u)]
            if not valid_urls:
                listing.photo_score = None
                listing.final_score = self._compute_final(listing)
                skipped_noimg += 1
                print(f"  photo ⏭ geen fotos — {addr[:50]}")
                continue

            photo_tasks.append((listing, valid_urls))

        # Parallelle OpenRouter calls — geen rate limit, geen delay
        if photo_tasks:
            print(f"  photo 📸 {len(photo_tasks)} listings parallel via OpenRouter...", flush=True)
            with ThreadPoolExecutor(max_workers=5) as pool:
                fut_map = {
                    pool.submit(self._call_openrouter, urls): (listing, urls)
                    for listing, urls in photo_tasks
                }
                for fut in as_completed(fut_map):
                    listing, _ = fut_map[fut]
                    score = fut.result()
                    if score is not None:
                        scored += 1
                    listing.photo_score = score
                    listing.final_score = self._compute_final(listing)

        parts = [f"📸 {scored} gescoord"]
        if skipped_epc:
            parts.append(f"{skipped_epc} EPC D/E/F/G overgeslagen")
        if skipped_noimg:
            parts.append(f"{skipped_noimg} geen fotos")
        if skipped:
            parts.append(f"{skipped} buiten top {self.SCORE_TOP_N}")
        logger.info(f"  — {' | '.join(parts)}")
        return listings

    def _call_openrouter(self, image_urls: list[str]) -> float | None:
        import requests
        content = [{"type": "text", "text": self.PROMPT}]
        for url in image_urls:
            content.append({"type": "image_url", "image_url": {"url": url}})

        resp = requests.post(
            OPENROUTER_BASE,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/vkpeter/domus-quaesitor",
            },
            json={
                "model": self.MODEL,
                "messages": [{"role": "user", "content": content}],
                "max_tokens": 300,
                "temperature": 0.2,
            },
            timeout=15,
        )

        data = resp.json()
        if "error" in data:
            raise Exception(data["error"].get("message", str(data["error"])))

        result = self._extract_json(data["choices"][0]["message"]["content"])
        if result:
            return max(1.0, min(10.0, float(result.get("photo_score", 5.0))))
        return None
