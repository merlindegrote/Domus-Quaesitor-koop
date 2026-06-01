"""AI photo scoring using OpenRouter free vision model for house quality."""

from __future__ import annotations

import json
import logging
import os
import time

from scrapers.base import Listing

logger = logging.getLogger(__name__)

DEFAULT_SCORE = 5.0


class PhotoScorer:
    """Score listing photos for house quality using OpenRouter vision models."""

    MODEL = "openrouter/free"
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
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.client = None

        if self.api_key:
            try:
                from openai import OpenAI
                self.client = OpenAI(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=self.api_key,
                )
                logger.info("✅ OpenRouter client initialized")
            except ImportError:
                logger.warning("⚠️ openai package not installed, photo scoring disabled")
            except Exception as e:
                logger.warning(f"⚠️ Failed to initialize OpenRouter client: {e}")
        else:
            logger.warning("⚠️ OPENROUTER_API_KEY not set, photo scoring disabled")

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

        content = [{"type": "text", "text": self.USER_PROMPT}]
        for url in image_urls:
            content.append({
                "type": "image_url",
                "image_url": {"url": url}
            })

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.MODEL,
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": content},
                    ],
                    max_tokens=200,
                    temperature=0.3,
                    extra_headers={
                        "HTTP-Referer": "https://github.com/merlindegrote/Domus-Quaesitor-koop",
                        "X-Title": "House Hunter",
                    },
                )

                choice = response.choices[0].message
                result_text = choice.content if choice else None
                if not result_text:
                    logger.warning("[photo_scorer] Empty response for %s:%s", listing.platform, listing.id)
                    return None

                result = self._extract_json(result_text)
                if result:
                    score = float(result.get("photo_score", DEFAULT_SCORE))
                    score = max(1.0, min(10.0, score))
                    logger.debug(f"[photo_scorer] {listing.platform}:{listing.id} -> {score:.1f}/10")
                    return score

                logger.warning(f"[photo_scorer] Could not parse: {result_text[:200]}")
                return None

            except Exception as e:
                error_str = str(e).lower()
                if "rate_limit" in error_str or "429" in error_str or "quota" in error_str:
                    logger.warning(f"[photo_scorer] Rate limited (attempt {attempt + 1}/{self.MAX_RETRIES + 1})")
                    if attempt < self.MAX_RETRIES:
                        time.sleep(self.RETRY_DELAY * (attempt + 1))
                        continue
                    return None
                if "developer instruction is not enabled" in error_str:
                    logger.warning("[photo_scorer] Provider rejected system prompt")
                    return None
                if "no endpoints found that support image input" in error_str:
                    logger.warning("[photo_scorer] No vision-capable provider")
                    return None
                logger.warning(f"[photo_scorer] Failed: {e}")
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

            logger.info(f"[photo_scorer] ({i + 1}/{len(listings)}) {listing.platform}:{listing.id} -> {'%.1f/10' % score if score else 'skipped'}")

        logger.info(f"[photo_scorer] Scored {scored}/{len(listings)}")
        return listings

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        import re
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
