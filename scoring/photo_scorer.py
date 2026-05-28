"""AI photo scoring using OpenRouter free vision model for interior modernity."""

from __future__ import annotations

import json
import logging
import os
import time

from scrapers.base import Listing

logger = logging.getLogger(__name__)

DEFAULT_SCORE = 5.0


class PhotoScorer:
    """Score listing photos for interior modernity using OpenRouter vision models."""

    # Use the free router — it auto-selects an available free vision model
    MODEL = "openrouter/free"
    MAX_PHOTOS = 3
    MAX_RETRIES = 2
    RETRY_DELAY = 3

    SYSTEM_PROMPT = """You are an interior design analyst. You rate apartment photos for modernity and cleanliness.

Scoring criteria (1-10):
- 9-10: Clearly modern, renovated, contemporary design, sleek finishes
- 7-8: Mostly modern, updated appliances and fixtures, clean lines
- 5-6: Average, mix of modern and dated elements
- 3-4: Dated interior, older fixtures, basic quality
- 1-2: Very old-fashioned, poor condition, needs full renovation

You MUST respond with valid JSON only."""

    USER_PROMPT = """Rate the modernity and cleanliness of this apartment interior on a scale of 1-10.

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
        """Check if the scorer is ready to use."""
        return self.client is not None

    def score_listing(self, listing: Listing) -> float | None:
        """
        Score a listing's photos for interior modernity.

        Returns:
            Score (1-10) or None if scoring failed/unavailable
        """
        if not self.is_available:
            return None

        # Filter for valid image URLs
        image_urls = [
            url for url in listing.image_urls[:self.MAX_PHOTOS]
            if url and url.startswith("http") and not self._is_placeholder(url)
        ]

        if not image_urls:
            logger.debug(f"[photo_scorer] No valid images for {listing.platform}:{listing.id}")
            return None

        # Build the multi-image message
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
                        "HTTP-Referer": "https://github.com/apartment-hunter",
                        "X-Title": "Apartment Hunter",
                    },
                )

                choice = response.choices[0].message
                result_text = choice.content if choice else None
                if not result_text:
                    logger.warning("[photo_scorer] Empty model response for %s:%s", listing.platform, listing.id)
                    return None

                # Try to parse JSON from response
                result = self._extract_json(result_text)
                if result:
                    score = float(result.get("photo_score", DEFAULT_SCORE))
                    score = max(1.0, min(10.0, score))
                    logger.debug(
                        f"[photo_scorer] {listing.platform}:{listing.id} → {score:.1f}/10"
                    )
                    return score

                logger.warning(f"[photo_scorer] Could not parse response: {result_text[:200]}")
                return None

            except Exception as e:
                error_str = str(e).lower()
                if "rate_limit" in error_str or "429" in error_str or "quota" in error_str:
                    logger.warning(
                        f"[photo_scorer] Rate limited (attempt {attempt + 1}/"
                        f"{self.MAX_RETRIES + 1})"
                    )
                    if attempt < self.MAX_RETRIES:
                        time.sleep(self.RETRY_DELAY * (attempt + 1))
                        continue
                    return None
                if "developer instruction is not enabled" in error_str:
                    logger.warning("[photo_scorer] Provider rejected system prompt, skipping")
                    return None
                if "no endpoints found that support image input" in error_str:
                    logger.warning("[photo_scorer] No vision-capable provider available, skipping")
                    return None
                if "404" in error_str:
                    logger.warning("[photo_scorer] Provider-side image fetch failed, skipping")
                    return None

                logger.warning(f"[photo_scorer] Failed: {e}")
                return None

        return None

    def score_listings(self, listings: list[Listing]) -> list[Listing]:
        """Score photos for multiple listings."""
        if not self.is_available:
            logger.warning("[photo_scorer] Scorer not available, skipping photo scoring")
            return listings

        logger.info(f"[photo_scorer] Scoring photos for {len(listings)} listings...")
        scored = 0

        for i, listing in enumerate(listings):
            score = self.score_listing(listing)
            if score is not None:
                listing.photo_score = score
                scored += 1
            else:
                listing.photo_score = None

            # Rate limit: OpenRouter free tier is 20 req/min
            if i < len(listings) - 1:
                time.sleep(3)

            logger.info(
                f"[photo_scorer] ({i + 1}/{len(listings)}) "
                f"{listing.platform}:{listing.id} → "
                f"{'%.1f/10' % score if score else 'skipped'}"
            )

        logger.info(f"[photo_scorer] Successfully scored {scored}/{len(listings)} listings")
        return listings

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """Extract JSON from a potentially messy response."""
        # Try direct parse first
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try to find JSON in the text
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
        """Check if URL is likely a placeholder image."""
        placeholder_indicators = [
            "placeholder", "no-image", "noimage", "default",
            "missing", "blank", "empty", "1x1", "pixel",
        ]
        url_lower = url.lower()
        return any(ind in url_lower for ind in placeholder_indicators)


def compute_final_scores(listings: list[Listing]) -> list[Listing]:
    """
    Compute final combined scores for all listings.

    Formula: final_score = text_score * 0.6 + photo_score * 0.4
    If no photo score: final_score = text_score
    """
    for listing in listings:
        text = listing.text_score or DEFAULT_SCORE
        photo = listing.photo_score

        if photo is not None:
            listing.final_score = text * 0.6 + photo * 0.4
        else:
            listing.final_score = text

    # Sort by final score descending
    listings.sort(key=lambda l: l.final_score or 0, reverse=True)
    return listings
