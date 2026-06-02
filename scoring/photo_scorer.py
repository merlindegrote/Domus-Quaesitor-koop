"""AI photo scoring using OpenRouter (Gemini 3.5 Flash vision) for house quality.

Cost optimization:
  - Enkel top 25 listings scoren (na text scoring, wat in email komt)
  - Alle fotos van 1 huis in 1 API call (max 8 fotos, zelfde prijs als 1)
  - Geen delay tussen calls (OpenRouter heeft geen rate limit probleem)
  - Max 1 retry (niet blijven proberen als het faalt)
"""

from __future__ import annotations

import json
import logging
import os
import re

from scrapers.base import Listing

logger = logging.getLogger(__name__)

DEFAULT_SCORE = 5.0
OPENROUTER_BASE = "https://openrouter.ai/api/v1/chat/completions"


class PhotoScorer:
    """Score listing photos via OpenRouter + Gemini 3.5 Flash."""

    MODEL = "google/gemini-3.5-flash"
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
        if self.client_available:
            logger.info(f"📸 OpenRouter scorer: top {self.SCORE_TOP_N}, max {self.MAX_PHOTOS} fotos/call")

    @property
    def is_available(self) -> bool:
        return self.client_available

    def score_listing(self, listing: Listing) -> float | None:
        """Score 1 listing. Returns None if no valid images."""
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
        """Score top N listings. Rest skipped (kostenbesparing)."""
        if not self.is_available:
            logger.warning("Photo scorer niet beschikbaar")
            return listings

        to_score = listings[:self.SCORE_TOP_N]
        skipped = max(0, len(listings) - self.SCORE_TOP_N)

        for i, listing in enumerate(to_score):
            score = self.score_listing(listing)
            listing.photo_score = score
            listing.final_score = self._compute_final(listing)

        scored = sum(1 for l in to_score if l.photo_score is not None)
        logger.info(f"📸 {scored}/{len(to_score)} gescoord" +
                    (f", {skipped} overgeslagen" if skipped else ""))
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
            timeout=30,
        )

        data = resp.json()
        if "error" in data:
            raise Exception(data["error"].get("message", str(data["error"])))

        result = self._extract_json(data["choices"][0]["message"]["content"])
        if result:
            return max(1.0, min(10.0, float(result.get("photo_score", 5.0))))
        return None

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
