"""AI text scoring using DeepSeek Chat (direct API) for 'modern & clean' house vibes."""

from __future__ import annotations

import json
import logging
import os
import time

from scrapers.base import Listing

logger = logging.getLogger(__name__)

# Default score when AI scoring is unavailable
DEFAULT_SCORE = 5.0
DEFAULT_REASONING = "AI scoring unavailable — unranked"


class TextScorer:
    """Score listing descriptions for modern/clean vibes using DeepSeek directly."""

    MODEL = "deepseek-chat"
    BASE_URL = "https://api.deepseek.com/v1"
    MAX_RETRIES = 2
    RETRY_DELAY = 5  # seconds

    SYSTEM_PROMPT = """Je bent een vastgoedanalist die Belgische te-koop-woningen beoordeelt.
Je evalueert hoe MODERN en AFGEWERKT een woning is op basis van de beschrijving.

Beoordelingscriteria (1-10):
- 9-10: Duidelijk gerenoveerd/nieuwbouw, moderne afwerking, hedendaags design
- 7-8: Recent vernieuwd, grotendeels modern, goed onderhouden
- 5-6: Gemiddeld, enkele moderne elementen maar ook verouderde aspecten
- 3-4: Oudere stijl, nood aan vernieuwing, basisafwerking
- 1-2: Zeer verouderd, slechte staat, ouderwets

Positieve indicatoren:
- "gerenoveerd", "nieuwbouw", "modern afgewerkt", "hedendaags", "recent gerenoveerd"
- "nieuwe keuken", "inbouwtoestellen", "strakke afwerking", "design"
- "recentelijk vernieuwd", "eigentijds", "kwalitatief", "luxueus"
- Goede EPC labels (A, B), nieuwe apparatuur, kwaliteitsmaterialen
- Tuin/terras, parkeerplaats/garage, goede buurt

Negatieve indicatoren:
- "op te frissen", "te renoveren", "originele staat", "klassiek"
- "oudere keuken", "verouderd", "basisafwerking"
- Geen parkeerplaats, geen buitenruimte, slechte EPC (D/E/F)
- Ligging aan drukke weg

Antwoord ALTIJD in correct Nederlands. Enkel geldige JSON, geen extra tekst."""

    USER_PROMPT_TEMPLATE = """Beoordeel deze woning op moderniteit en afwerkingsgraad.

Titel: {title}
Prijs: €{price}
Adres: {address}
Oppervlakte: {surface}m²
EPC: {epc}
Slaapkamers: {bedrooms}

Beschrijving:
{description}

Antwoord in dit exacte JSON formaat:
{{"modern_score": <cijfer 1-10>, "reasoning": "<korte 1-2 zinnen uitleg in het Nederlands>"}}"""

    def __init__(self):
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        self.client = None

        if self.api_key:
            try:
                from openai import OpenAI
                self.client = OpenAI(
                    base_url=self.BASE_URL,
                    api_key=self.api_key,
                )
                logger.info("✅ DeepSeek text scorer initialized")
            except ImportError:
                logger.warning("⚠️ openai package not installed, text scoring disabled")
            except Exception as e:
                logger.warning(f"⚠️ Failed to initialize DeepSeek client: {e}")
        else:
            logger.warning("⚠️ DEEPSEEK_API_KEY not set, text scoring disabled")

    @property
    def is_available(self) -> bool:
        """Check if the scorer is ready to use."""
        return self.client is not None

    def score_listing(self, listing: Listing) -> tuple[float, str]:
        """
        Score a single listing's description for modern/clean vibes.

        Returns:
            Tuple of (score: float 1-10, reasoning: str)
        """
        if not self.is_available:
            return DEFAULT_SCORE, DEFAULT_REASONING

        description = (listing.description or "").strip()
        if len(description) < 20:
            description = "Limited description available. Infer only from title, EPC, surface, bedrooms, and any location clues."

        prompt = self.USER_PROMPT_TEMPLATE.format(
            title=listing.title,
            price=listing.price,
            address=listing.address,
            surface=listing.surface_m2 or "unknown",
            epc=listing.epc_label or "unknown",
            bedrooms=listing.bedrooms,
            description=description[:2000],
        )

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.MODEL,
                    messages=[
                        {"role": "system", "content": self.SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=200,
                )

                content = response.choices[0].message.content
                result = json.loads(content)

                score = float(result.get("modern_score", DEFAULT_SCORE))
                score = max(1.0, min(10.0, score))  # Clamp to 1-10
                reasoning = result.get("reasoning", "No reasoning provided")

                logger.debug(
                    f"[text_scorer] {listing.platform}:{listing.id} → "
                    f"score={score}, reason={reasoning[:80]}"
                )
                return score, reasoning

            except json.JSONDecodeError as e:
                logger.warning(f"[text_scorer] Invalid JSON response: {e}")
                if attempt < self.MAX_RETRIES:
                    time.sleep(self.RETRY_DELAY)
                    continue
                return DEFAULT_SCORE, "Failed to parse AI response"

            except Exception as e:
                error_str = str(e).lower()
                if "rate_limit" in error_str or "429" in error_str:
                    logger.warning(
                        f"[text_scorer] Rate limited (attempt {attempt + 1}/"
                        f"{self.MAX_RETRIES + 1})"
                    )
                    if attempt < self.MAX_RETRIES:
                        time.sleep(self.RETRY_DELAY * (attempt + 1))
                        continue
                    return DEFAULT_SCORE, "Rate limited — unranked"
                else:
                    logger.error(f"[text_scorer] Scoring failed: {e}")
                    return DEFAULT_SCORE, f"Scoring error: {str(e)[:100]}"

        return DEFAULT_SCORE, DEFAULT_REASONING

    def score_listings(self, listings: list[Listing]) -> list[Listing]:
        """Score multiple listings, updating them in place."""
        if not self.is_available:
            logger.warning("[text_scorer] Scorer not available, skipping all text scoring")
            for listing in listings:
                listing.text_score = DEFAULT_SCORE
                listing.score_reasoning = DEFAULT_REASONING
            return listings

        logger.info(f"[text_scorer] Scoring {len(listings)} listings...")

        for i, listing in enumerate(listings):
            score, reasoning = self.score_listing(listing)
            listing.text_score = score
            listing.score_reasoning = reasoning

            # Small delay between API calls to avoid rate limiting
            if i < len(listings) - 1:
                time.sleep(1)

            logger.info(
                f"[text_scorer] ({i + 1}/{len(listings)}) "
                f"{listing.platform}:{listing.id} → {score:.1f}/10"
            )

        return listings
