"""AI photo scoring using Google Gemini (vision model) for house quality."""

from __future__ import annotations

import json
import logging
import os
import re
import time

from scrapers.base import Listing

logger = logging.getLogger(__name__)

DEFAULT_SCORE = 5.0


class PhotoScorer:
    """Score listing photos for house quality using Gemini vision."""

    MODEL = "gemini-2.0-flash"
    MAX_PHOTOS = 3
    MAX_RETRIES = 2
    RETRY_DELAY = 3

    SYSTEM_PROMPT = """You are a real estate quality analyst. You rate house photos for quality, modernity and curb appeal.

Scoring criteria (1-10):
- 9-10: Excellent condition, modern finishes, spacious, well-maintained
- 7-8: Good condition, updated, clean, well-cared for
- 5-6: Average, some modern but also dated elements, acceptable
- 3-4: Dated interior/exterior, needs work, basic quality
- 1-2: Very old, poor condition, needs full renovation

You MUST respond with valid JSON only."""

    USER_PROMPT = """Rate the quality and condition of this house on a scale of 1-10.

Respond with this exact JSON format:
{"photo_score": <number 1-10>, "reasoning": "<brief observation>"}"""

    def __init__(self):
        self.api_key = os.environ.get("GEMINI_API_KEY", "")
        self.client = None

        if self.api_key:
            try:
                from google import genai
                self.client = genai.Client(api_key=self.api_key)
                logger.info("✅ Gemini photo scorer initialized")
            except ImportError:
                logger.warning("⚠️ google-genai package not installed, photo scoring disabled")
            except Exception as e:
                logger.warning(f"⚠️ Failed to initialize Gemini client: {e}")
        else:
            logger.warning("⚠️ GEMINI_API_KEY not set, photo scoring disabled")

    @property
    def is_available(self) -> bool:
        return self.client is not None

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
                from google.genai import types

                parts = [types.Part.from_text(text=self.SYSTEM_PROMPT),
                         types.Part.from_text(text=self.USER_PROMPT)]

                for url in image_urls:
                    img_data = self._fetch_image(url)
                    parts.append(
                        types.Part.from_bytes(data=img_data, mime_type="image/jpeg")
                    )

                response = self.client.models.generate_content(
                    model=self.MODEL,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        temperature=0.3,
                        max_output_tokens=200,
                    )
                )

                result = self._extract_json(response.text)
                if result:
                    score = float(result.get("photo_score", DEFAULT_SCORE))
                    score = max(1.0, min(10.0, score))
                    logger.debug(f"[photo_scorer] {listing.platform}:{listing.id} -> {score:.1f}/10")
                    return score

                logger.warning(f"[photo_scorer] Could not parse: {response.text[:200]}")
                return DEFAULT_SCORE

            except Exception as e:
                error_str = str(e).lower()
                if any(x in error_str for x in ["rate_limit", "429", "quota", "safety", "blocked"]):
                    logger.warning(f"[photo_scorer] Provider issue (attempt {attempt + 1}/{self.MAX_RETRIES + 1}): {e}")
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
    def _fetch_image(url: str) -> bytes:
        """Fetch image bytes from URL."""
        import requests
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        resp.raise_for_status()
        return resp.content

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        match = re.search(r'\{[^{}]*"photo_score"[^{}]*\}', text)
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
