"""2dehands.be / 2ememain.be scraper for for-sale houses.

2dehands is a Next.js SPA. All listing data is embedded in:
  <script id="__NEXT_DATA__" type="application/json"> ... </script>

We fetch per-city search pages and parse the JSON for houses (categoryId 1041)
in target cities, filtering by price, bedrooms, and other attributes.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from scrapers.base import BaseScraper, Listing
from config import MIN_PRICE, MAX_PRICE, MIN_BEDROOMS

logger = logging.getLogger(__name__)

# 2dehands search URL for houses in a city
SEARCH_URL = "https://www.2dehands.be/l/immo/q/{city}/"

# Category 1041 = houses_and_rooms (immobilien/huizen)
HOUSES_CATEGORY = 2142

# Title patterns to filter out (gezocht, makelaar services, Costa Blanca, etc.)
EXCLUDE_TITLE_PATTERNS = [
    r"gezocht",
    r"zonder makelaar",
    r"verkopen",
    r"direct verkopen",
    r"op zoek",
    r"costa blanca",
    r"alicante",
    r"spanje",
    r"discreet",
    r"erfenis",
    r"scheiding",
    r"opbrengsteigendom",
    r"handelspand",
    r"schilders?",
    r"kapsalon",
    r"postduif",
    r"betaalbare",
]


class TweeDeHandsScraper(BaseScraper):
    """Scraper for 2dehands.be for-sale house listings."""

    PLATFORM_NAME = "2dehands"
    REQUEST_DELAY = 1.5

    def scrape(self) -> list[Listing]:
        """Scrape 2dehands for each target city."""
        from config import ACCEPT_CITIES
        all_listings: list[Listing] = []
        for city in ACCEPT_CITIES:
            listings = self._scrape_city(city)
            all_listings.extend(listings)
        return all_listings

    def _scrape_city(self, city: str) -> list[Listing]:
        """Scrape houses for one city."""
        url = SEARCH_URL.format(city=city.lower())

        response = self._get_with_fallback(url)
        if not response:
            return []

        html = response.text
        data = self._extract_next_data(html)
        if not data:
            return []

        listings = self._parse_listings(data, city)
        logger.info(
            f"[{self.PLATFORM_NAME}] {city}: {len(listings)} houses found"
        )
        return listings

    @staticmethod
    def _extract_next_data(html: str) -> dict[str, Any] | None:
        """Extract and parse __NEXT_DATA__ JSON from the page."""
        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None

    def _parse_listings(
        self, data: dict[str, Any], city: str
    ) -> list[Listing]:
        """Parse listings from NEXT_DATA structure."""
        page_props = data.get("props", {}).get("pageProps", {})
        sr = page_props.get("searchRequestAndResponse", {})
        raw_listings = sr.get("listings", [])

        results: list[Listing] = []
        for raw in raw_listings:
            listing = self._parse_one(raw)
            if listing:
                results.append(listing)

        return results

    def _parse_one(self, raw: dict[str, Any]) -> Listing | None:
        """Parse a single raw listing dict into a Listing object."""

        category_id = raw.get("categoryId")
        if category_id != HOUSES_CATEGORY:
            return None

        # Location
        location = raw.get("location", {})
        city_name = location.get("cityName", "")

        # Skip "buitenland" (abroad) listings — search is fuzzy and returns non-local results
        vip_url = raw.get("vipUrl", "")
        if "/buitenland/" in vip_url or "buitenland" in city_name.lower():
            return None

        from config import ACCEPT_CITIES

        if not any(c.lower() == city_name.lower() for c in ACCEPT_CITIES):
            return None

        # Title filter — remove non-relevant listings (gezocht, makelaar diensten, etc.)
        title = raw.get("title", "")
        title_lower = title.lower()
        import re as re_mod
        for pat in EXCLUDE_TITLE_PATTERNS:
            if re_mod.search(pat, title_lower):
                return None

        # Price
        price_info = raw.get("priceInfo", {})
        price_cents = price_info.get("priceCents", 0)
        price_type = price_info.get("priceType", "")
        if price_type in ("NOTK", "SEE_DESCRIPTION"):
            return None
        price = price_cents // 100
        if price < MIN_PRICE or price > MAX_PRICE:
            return None

        # Extract attributes
        attrs: dict[str, str] = {}
        for attr in raw.get("attributes", []):
            key = attr.get("key", "")
            value = attr.get("value", "")
            attrs[key] = value

        # Bedrooms
        bedrooms = MIN_BEDROOMS
        bed_raw = attrs.get("numberOfBedrooms", "")
        bed_match = re.search(r"(\d+)", bed_raw)
        if bed_match:
            bedrooms = int(bed_match.group(1))
        if bedrooms < MIN_BEDROOMS:
            return None

        # EPC — not consistently available on 2dehands
        epc_label = attrs.get("epc", None)

        # Surface
        surface = None
        surface_raw = attrs.get("surface", "")
        surf_match = re.search(r"(\d+)", surface_raw)
        if surf_match:
            surface = int(surf_match.group(1))

        # Lot surface — not available on 2dehands
        lot_surface = None

        # Property type
        title = raw.get("title", f"House in {city_name}")
        # Detect property type from title
        title_lower = title.lower()
        if "appartement" in title_lower or "apartment" in title_lower:
            property_type = "apartment"
        else:
            property_type = "house"

        # Description
        description = raw.get("description", "")
        cat_desc = raw.get("categorySpecificDescription", "")
        full_description = description or cat_desc

        # Images
        image_urls: list[str] = []
        pictures = raw.get("pictures", [])
        for pic in pictures:
            # Use the largest available size (XXXL = _86 or extraExtraLargeUrl)
            img_url = pic.get("extraExtraLargeUrl", "")
            if img_url:
                if img_url.startswith("//"):
                    img_url = "https:" + img_url
                image_urls.append(img_url)
            if len(image_urls) >= 5:
                break

        # Item ID as unique reference
        item_id = raw.get("itemId", "")
        vip_url = raw.get("vipUrl", "")
        detail_url = f"https://www.2dehands.be{vip_url}" if vip_url else ""

        return Listing(
            id=item_id,
            platform=self.PLATFORM_NAME,
            title=title,
            price=price,
            bedrooms=bedrooms,
            address=city_name,
            url=detail_url or f"https://www.2dehands.be/l/immo/q/{city_name.lower()}/",
            description=full_description,
            image_urls=image_urls,
            surface_m2=surface,
            epc_label=epc_label,
            lot_surface_m2=lot_surface,
            property_type=property_type,
        )

    def enrich_listing(self, listing: Listing) -> Listing:
        """Fetch detail page and try to extract EPC, surface, lot surface.

        2dehands provides minimal structured data on detail pages.
        EPC is rarely available — usually null in JSON-LD.
        Surface/lot may appear in free-text description only.
        """
        if not listing.url:
            return listing

        response = self._get_with_fallback(listing.url)
        if not response:
            return listing

        html = response.text

        # --- Try JSON-LD for EPC ---
        for m in re.finditer(
            r'<script type="application/ld\+json">(.*?)</script>',
            html, re.DOTALL
        ):
            try:
                ld = json.loads(m.group(1))
                if isinstance(ld, dict):
                    epc = ld.get("hasEnergyEfficiencyCategory")
                    if epc and isinstance(epc, str) and epc.strip():
                        listing.epc_label = epc.strip()
                        break
            except json.JSONDecodeError:
                continue

        # --- Try structured data from __CONFIG__ for attributes ---
        idx = html.find("window.__CONFIG__ = ")
        if idx >= 0:
            try:
                start = html.index("{", idx)
                depth = 1
                end = start + 1
                while depth > 0 and end < len(html):
                    ch = html[end]
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                    end += 1
                config = json.loads(html[start:end])
                listing_data = config.get("listing", {})
                # __CONFIG__ usually has no surface/epc in listing data
                # but check customDimensions for anything useful
            except (json.JSONDecodeError, ValueError):
                pass

        # --- Try from description text for surface / lot surface ---
        # Description is already on the listing from search page
        desc = listing.description or ""
        if not listing.surface_m2:
            surface = self._extract_surface_from_text(desc)
            if surface:
                listing.surface_m2 = surface

        if not listing.lot_surface_m2:
            lot = self._extract_lot_surface_from_text(desc)
            if lot:
                listing.lot_surface_m2 = lot

        return listing

    @staticmethod
    def _extract_surface_from_text(text: str) -> int | None:
        """Try to extract living surface m² from Dutch description text.

        Patterns:
          - "woonoppervlakte X m²"
          - "oppervlakte X m²"
          - "bewoonbare oppervlakte X m²"
          - "X m² woonoppervlak"
          - "ongeveer X m²"
          - "X m²" (near words like kamer, appartement, woning)
        """
        patterns = [
            r"woonoppervlak(?:te)?\s*(?:is|van|:)?\s*(\d{2,4})\s*m[²2]",
            r"bewoonbare\s+oppervlakte\s*(?:is|van|:)?\s*(\d{2,4})\s*m[²2]",
            r"oppervlakte\s*(?:is|van|:)?\s*(\d{2,4})\s*m[²2]",
            r"(\d{2,4})\s*m[²2]\s*(?:woonoppervlak|woonoppervlakte)",
            r"ongeveer\s*(\d{2,4})\s*m[²2]",
            r"totale\s+oppervlakte\s*(?:is|van|:)?\s*(\d{2,4})\s*m[²2]",
        ]
        text_lower = text.lower()
        for pat in patterns:
            m = re.search(pat, text_lower)
            if m:
                val = int(m.group(1))
                # Sanity: surface between 10 and 2000 m²
                if 10 <= val <= 2000:
                    return val
        return None

    @staticmethod
    def _extract_lot_surface_from_text(text: str) -> int | None:
        """Try to extract lot/plot surface m² from Dutch description text.

        Patterns:
          - "perceel X m²"
          - "grond X m²"
          - "kavel X m²"
          - "X m² grond"
          - "X m² perceel"
          - "tuin X m²"
        """
        patterns = [
            r"perceel(?:soppervlakte)?\s*(?:is|van|:)?\s*(\d{2,5})\s*m[²2]",
            r"grond(?:oppervlakte)?\s*(?:is|van|:)?\s*(\d{2,5})\s*m[²2]",
            r"kavel(?:oppervlakte)?\s*(?:is|van|:)?\s*(\d{2,5})\s*m[²2]",
            r"(\d{2,5})\s*m[²2]\s*(?:grond|perceel|kavel|terrein)",
            r"tuin\s*(?:is|van|:)?\s*(\d{2,5})\s*m[²2]",
        ]
        text_lower = text.lower()
        for pat in patterns:
            m = re.search(pat, text_lower)
            if m:
                val = int(m.group(1))
                # Sanity: lot surface between 10 and 50000 m²
                if 10 <= val <= 50000:
                    return val
        return None


# Standalone test
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = TweeDeHandsScraper()
    results = scraper.safe_scrape()
    for r in results:
        print(
            f"  [{r.id}] {r.price:>7}\u20ac  {r.bedrooms}sl  "
            f"{r.surface_m2 or '?'}m\u00b2  {r.address}"
        )
    print(f"\nTotal: {len(results)} listings")
