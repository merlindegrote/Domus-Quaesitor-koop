"""AI photo scoring using OpenRouter (Gemini 3.5 Flash vision) for house quality."""

from __future__ import annotations

import json
import logging
import os
import re
import time

from scrapers.base import Listing

logger = logging.getLogger(__name__)

DEFAULT_SCORE = 5.0

OPENROUTER_BASE = "https://openrouter.ai/api/v1/chat/completions"


class PhotoScorer:
    """Score listing photos for house quality via OpenRouter + Gemini 3.5 Flash."""

    MODEL = "google/gemini-3.5-flash"
    MAX_PHOTOS = 3
    MAX_RETRIES = 2
    RETRY_DELAY = 3

    PROMPT = """Rate the quality and condition of this house on a scale of 1-10.

Scoring criteria (1-10):
- 9-10: Excellent condition, modern finishes, spacious, well-maintained
- 7-8: Good condition, updated, clean, well-cared for
- 5-6: Average, some modern but also dated elements, acceptable
- 3-4: Dated interior/exterior, needs work, basic quality
- 1-2: Very old, poor condition, needs full renovation

Respond with ONLY valid JSON, no markdown:
{"photo_score": <number 1-10>, "reasoning": "<brief observation in Dutch>"}"""

    def __init__(self):
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.client_available = False

        if self.api_key:
            self.client_available = True
            logger.info("✅ OpenRouter photo scorer initialized (Gemini 3.5 Flash)")
        else:
            logger.warning("⚠️ OPENROUTER_API_KEY not set, photo scoring disabled")

    @property
    def is_available(self) -> bool:
        return self.client_available

    def score_listing(self, listing: Listing) -> float | None:
        if not self.is_available:
            return None

        image_urls = [
            url for url in listing.image_urls[:self.MAX_PHOTOS]
            if url and url.startswith("http") and not self._is_placeholder(url)
        ]

        if not image_urls:
            logger.debug(f"[photo_scorer] No valid images for {listing.platform}:{listing.id}")
            return None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                score = self._call_openrouter(image_urls)
                if score is not None:
                    logger.debug(f"[photo_scorer] {listing.platform}:{listing.id} -> {score:.1f}/10")
                    return score

                logger.warning(f"[photo_scorer] Could not parse response, using default")
                return DEFAULT_SCORE

            except Exception as e:
                error_str = str(e).lower()
                if any(x in error_str for x in ["rate_limit", "429", "quota", "insufficient_quota"]):
                    logger.warning(f"[photo_scorer] Rate limit (attempt {attempt + 1}/{self.MAX_RETRIES + 1}): {e}")
                    if attempt < self.MAX_RETRIES:
                        time.sleep(self.RETRY_DELAY * (attempt + 1))
                        continue
                    return None
                logger.warning(f"[photo_scorer] Failed: {e}")
                if attempt < self.MAX_RETRIES:
                    time.sleep(self.RETRY_DELAY)
                    continue
                return None

        return None

    def _call_openrouter(self, image_urls: list[str]) -> float | None:
        """Call OpenRouter API with image URLs, return photo_score."""
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
                "max_tokens": 800,
                "temperature": 0.3,
            },
            timeout=30,
        )

        data = resp.json()

        if "error" in data:
            err = data["error"]
            raise Exception(f"OpenRouter API error: {err.get('message', err)}")

        text = data["choices"][0]["message"]["content"]
        result = self._extract_json(text)
        if result:
            score = float(result.get("photo_score", DEFAULT_SCORE))
            return max(1.0, min(10.0, score))

        return None

    def score_listings(self, listings: list[Listing]) -> list[Listing]:
        if not self.is_available:
            logger.warning("[photo_scorer] Not available, skipping")
            return listings

        logger.info(f"[photo_scorer] Scoring {len(listings)} listings...")
        scored = 0

        for i, listing in enumerate(listings):
            score = self.score_listing(listing)
            if score is not None:
                listing.photo_score = score
                scored += 1
            else:
                listing.photo_score = None

            if i < len(listings) - 1:
                time.sleep(3)

            logger.info(
                f"[photo_scorer] ({i + 1}/{len(listings)}) {listing.platform}:{listing.id} -> "
                f"{'%.1f/10' % score if score else 'skipped'}"
            )

        logger.info(f"[photo_scorer] Scored {scored}/{len(listings)}")
        return listings

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """Extract JSON from model response, stripping markdown fences."""
        # Strip markdown code blocks first
        cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.MULTILINE).strip()
        # Try direct parse
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        # Try finding first JSON object with photo_score
        match = re.search(r'\{[^{}]*"photo_score"[^{}]*\}', cleaned)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        return None

    @staticmethod
    def _is_placeholder(url: str) -> bool:
        indicators = ["placeholder", "no-image", "noimage", "default", "missing", "blank", "empty", "1x1", "pixel"]
        url_lower = url.lower()
        return any(ind in url_lower for ind in indicators)


def compute_final_scores(listings: list[Listing]) -> list[Listing]:
    """
    final_score = text_score * 0.6 + photo_score * 0.4
    If no photo score: final_score = text_score
    """
    for listing in listings:
        text = listing.text_score or DEFAULT_SCORE
        photo = listing.photo_score
        if photo is not None:
            listing.final_score = text * 0.6 + photo * 0.4
        else:
            listing.final_score = text

    listings.sort(key=lambda l: l.final_score or 0, reverse=True)
    return listings
