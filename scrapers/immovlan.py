"""Immovlan.be scraper for for-sale houses.

Immovlan is a JS SPA (ASP.NET MVC + jQuery). Search results are rendered
client-side via Web Workers, so we use a two-phase approach:
1. Fetch the search-page HTML and extract property IDs from embedded JS
   (STORAGE_KEY_SEARCH_RESULTS in localStore.setItem).
2. Visit each individual detail page to parse price, bedrooms, EPC, surfaces, etc.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Listing
from config import TARGET_CITY, MIN_PRICE, MAX_PRICE, MIN_BEDROOMS

logger = logging.getLogger(__name__)


class ImmovlanScraper(BaseScraper):
    """Scraper for Immovlan.be for-sale listings."""

    PLATFORM_NAME = "immovlan"
    REQUEST_DELAY = 2.0

    MAX_PAGES = 5

    def scrape(self) -> list[Listing]:
        """Scrape Immovlan search results across all pages for TARGET_CITY."""
        all_ids: list[str] = []
        seen_ids: set[str] = set()

        for page in range(1, self.MAX_PAGES + 1):
            page_ids = self._fetch_search_page(page)
            if not page_ids:
                break

            # Separate short IDs (individual properties, start with a letter)
            # from long IDs (project/development units, start with a digit)
            short_ids = [pid for pid in page_ids if re.match(r"^[A-Z]", pid)]
            new_ids = [pid for pid in short_ids if pid not in seen_ids]
            if not new_ids:
                logger.info(
                    f"[{self.PLATFORM_NAME}] No new IDs on page {page}"
                )
                break

            for pid in new_ids:
                all_ids.append(pid)
                seen_ids.add(pid)

            if len(page_ids) < 20:
                logger.info(
                    f"[{self.PLATFORM_NAME}] Partial page ({len(page_ids)} items) on "
                    f"page {page} — last page"
                )
                break

            logger.info(
                f"[{self.PLATFORM_NAME}] Page {page}: {len(page_ids)} items, "
                f"{len(new_ids)} new short IDs"
            )

        # Fetch detail pages
        listings: list[Listing] = []
        for prop_id in all_ids:
            listing = self._fetch_detail(prop_id)
            if listing:
                listings.append(listing)

        return listings

    def _fetch_search_page(self, page: int) -> list[str]:
        """Fetch search result page and extract property IDs."""
        url = (
            f"https://www.immovlan.be/nl/vastgoed"
            f"?type=huis&transactiontypes=te-koop&places={TARGET_CITY.lower()}"
            f"&pricemin={MIN_PRICE}&pricemax={MAX_PRICE}"
            f"&page={page}"
        )

        response = self._get_with_fallback(url)
        if not response:
            logger.warning(
                f"[{self.PLATFORM_NAME}] Failed to fetch search page {page}"
            )
            return []

        html = response.text

        # Extract property IDs from embedded JS
        match = re.search(
            r"STORAGE_KEY_SEARCH_RESULTS.*?JSON\.stringify\((\[.*?\])\)",
            html,
            re.DOTALL,
        )
        if match:
            try:
                return json.loads(match.group(1))
            except (json.JSONDecodeError, TypeError):
                return []

        return []

    def _fetch_detail(self, prop_id: str) -> Listing | None:
        """Fetch and parse a single property detail page."""
        url = f"https://www.immovlan.be/nl/detail/{prop_id}"

        try:
            response = self._rate_limited_get(url, allow_redirects=True, timeout=20)
        except Exception as exc:
            logger.debug(
                f"[{self.PLATFORM_NAME}] Failed to fetch {prop_id}: {exc}"
            )
            return None

        if not response or not response.text:
            return None

        return self._parse_detail(response.text, prop_id)

    def _parse_detail(self, html: str, prop_id: str) -> Listing | None:
        """Parse a property detail page into a Listing."""
        soup = BeautifulSoup(html, "lxml")

        # --- JSON-LD: best source for structured data ---
        listing = self._parse_jsonld(html, prop_id)
        if listing:
            return listing

        # --- Fallback: parse HTML ---
        return self._parse_html(soup, prop_id)

    def _parse_jsonld(self, html: str, prop_id: str) -> Listing | None:
        """Parse JSON-LD blocks for property data."""
        clean_html = (
            html.replace("&#x202F;", " ")
            .replace("&#x20AC;", "\u20ac")
            .replace("&#xB2;", "\u00b2")
            .replace("&#x2019;", "'")
            .replace("&#x27;", "'")
            .replace("&#x2B;", "+")
            .replace("&euro;", "\u20ac")
            .replace("&amp;", "&")
        )

        scripts = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            clean_html,
            re.DOTALL,
        )

        house_data: dict[str, Any] = {}

        for s in scripts:
            try:
                data = json.loads(s)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            tp = data.get("@type", "")
            if tp == "House" or tp == "Apartment":
                house_data = data
                break

        if not house_data:
            return None

        # --- Price ---
        price = 0
        price_match = re.search(
            r"<strong>Prijs</strong>:\s*([\d\u202f.]+)\s*\u20ac", html
        )
        if price_match:
            price = self._parse_price(price_match.group(1))
        if not price:
            # Try meta description
            desc_match = re.search(
                r'<meta name="description"\s+content="[^|]+\|\s*([\d\u202f.]+)\s*\u20ac',
                html,
            )
            if desc_match:
                price = self._parse_price(desc_match.group(1))

        # --- Bedrooms ---
        bedrooms = MIN_BEDROOMS
        desc = house_data.get("description", "")
        bed_match = re.search(r"(\d+)\s*(?:slaapkamer|slaapkamers)", desc, re.I)
        if bed_match:
            bedrooms = int(bed_match.group(1))
        else:
            og_title = re.search(
                r'<meta property="og:title"\s+content="([^"]+)"', html
            )
            if og_title:
                bed_match = re.search(
                    r"(\d+)\s*(?:slaapkamer|slaapkamers)", og_title.group(1), re.I
                )
                if bed_match:
                    bedrooms = int(bed_match.group(1))

        if bedrooms < MIN_BEDROOMS:
            return None

        # --- EPC ---
        epc_label = self._extract_epc(html)

        # --- Surfaces ---
        living_surface = self._extract_living_surface(desc + " " + html)
        lot_surface = self._extract_lot_surface(desc + " " + html)

        # --- Images ---
        image_urls: list[str] = []
        img_matches = re.findall(
            r"https://api-image\.immovlan\.be[^\"'\\]+(?:jpg|jpeg|png|webp)",
            html,
        )
        for img in img_matches:
            if img not in image_urls and "gallery-like-image" in img:
                image_urls.append(img)
            if len(image_urls) >= 5:
                break

        # --- Address ---
        address = self._extract_address(html)

        # --- Description ---
        description = desc.strip()
        if len(description) < 40:
            desc_meta = re.search(
                r'<meta name="description"\s+content="([^"]+)"',
                html,
            )
            if desc_meta:
                description = desc_meta.group(1)

        # --- Title ---
        title = f"Huis te koop in {TARGET_CITY} — \u20ac{price}"
        title_match = re.search(
            r'<meta property="og:title"\s+content="([^"]+)"', html
        )
        if title_match:
            title = title_match.group(1)

        return Listing(
            id=prop_id,
            platform=self.PLATFORM_NAME,
            title=title,
            price=price,
            bedrooms=bedrooms,
            address=address,
            url=f"https://www.immovlan.be/nl/detail/{prop_id}",
            description=description,
            image_urls=image_urls,
            surface_m2=living_surface,
            epc_label=epc_label,
            lot_surface_m2=lot_surface,
        )

    def _parse_html(
        self, soup: BeautifulSoup, prop_id: str
    ) -> Listing | None:
        """Parse HTML fallback when JSON-LD is insufficient."""
        text = soup.get_text(" ", strip=True)

        price = self._parse_price_from_html(text)
        if not price:
            return None

        bedrooms = MIN_BEDROOMS
        bed_match = re.search(r"(\d+)\s*(?:slaapkamer|slaapkamers)", text, re.I)
        if bed_match:
            bedrooms = int(bed_match.group(1))
        if bedrooms < MIN_BEDROOMS:
            return None

        epc_label = self._extract_epc(text)
        surface = self._extract_living_surface(text)
        lot_surface = self._extract_lot_surface(text)

        image_urls: list[str] = []
        for img in soup.select("img[src*='api-image.immovlan']"):
            src = img.get("src", "")
            if "gallery-like-image" in src and src not in image_urls:
                image_urls.append(src)
            if len(image_urls) >= 5:
                break

        desc_el = soup.select_one('meta[name="description"]')
        description = desc_el.get("content", "") if desc_el else ""

        return Listing(
            id=prop_id,
            platform=self.PLATFORM_NAME,
            title=f"Huis te koop in {TARGET_CITY}",
            price=price,
            bedrooms=bedrooms,
            address=self._extract_address_from_text(text),
            url=f"https://www.immovlan.be/nl/detail/{prop_id}",
            description=description,
            image_urls=image_urls,
            surface_m2=surface,
            epc_label=epc_label,
            lot_surface_m2=lot_surface,
        )

    # --- Helper methods ---

    @staticmethod
    def _extract_epc(text: str) -> str | None:
        """Extract EPC label from text (e.g. 'EPC C', 'FlandersC')."""
        epc_match = re.search(r"epc-water-mark\s*Flanders([A-E][+-]?)", text)
        if epc_match:
            return epc_match.group(1)

        epc_match = re.search(
            r"(?:EPC[ -\s]*(?:label\s*)?)([A-E][+-]?)", text, re.I
        )
        if epc_match:
            return epc_match.group(1).upper()

        return None

    @staticmethod
    def _extract_living_surface(text: str) -> int | None:
        """Extract living surface in m²."""
        for pat in [
            r"Bewoonbare\s+opp\.?\s*(\d[\d.]*)\s*m",
            r"Oppervlakte.*?(\d[\d.]*)\s*m[²2].*?(?:woon|living)",
            r"totaal\s*(\d+)\s*m2\s+(?:woonruimte|woonopp)",
            r"(\d[\d.]*)\s*m[²2]\s*(?:woonruimte|woonopp)",
        ]:
            match = re.search(pat, text, re.I)
            if match:
                num = match.group(1).replace(".", "").replace(",", "")
                try:
                    return int(num)
                except ValueError:
                    continue
        return None

    @staticmethod
    def _extract_lot_surface(text: str) -> int | None:
        """Extract total lot/perceel surface in m²."""
        are_match = re.search(r"perceel\s+van\s+(\d[\d.,]*)\s+are", text, re.I)
        if are_match:
            try:
                return int(float(are_match.group(1).replace(",", ".")) * 100)
            except ValueError:
                pass

        m2_match = re.search(r"perceel\s+van\s+(\d[\d.]*)\s*m[²2]", text, re.I)
        if m2_match:
            try:
                return int(m2_match.group(1).replace(".", "").replace(",", ""))
            except ValueError:
                pass

        return None

    @staticmethod
    def _extract_address(html: str) -> str:
        """Extract address from HTML."""
        addr_match = re.search(
            r'<meta name="description"\s+content="[^|]+\|'
            r"\s*(\d{4})\s+([^|]+)\s*\|",
            html,
        )
        if addr_match:
            return f"{addr_match.group(1)} {addr_match.group(2).strip()}"

        addr_match = re.search(
            r"<h4>Adres</h4>\s*<p>([^<]+)</p>", html
        )
        if addr_match:
            return addr_match.group(1).strip()

        return TARGET_CITY.capitalize()

    @staticmethod
    def _extract_address_from_text(text: str) -> str:
        """Extract address from plain text."""
        addr_match = re.search(
            r"\|?\s*(\d{4})\s+(?:" + re.escape(TARGET_CITY) + r"|[A-Z][a-z]+)\s*\|",
            text,
        )
        if addr_match:
            return addr_match.group(0).strip().strip("|").strip()
        return TARGET_CITY.capitalize()

    @staticmethod
    def _parse_price(text: str) -> int:
        """Parse price from a price string."""
        clean = (
            text.replace("\u202f", "")
            .replace("\xa0", "")
            .replace(" ", "")
            .replace(".", "")
            .replace(",", "")
        )
        numbers = re.findall(r"\d+", clean)
        for num in numbers:
            try:
                price = int(num)
                if 50000 <= price <= 2000000:
                    return price
            except ValueError:
                continue
        return 0

    @staticmethod
    def _parse_price_from_html(text: str) -> int:
        """Parse price from HTML text."""
        match = re.search(r"Prijs[^€]*?([\d\u202f.]+)\s*\u20ac", text)
        if match:
            return ImmovlanScraper._parse_price(match.group(1))

        match = re.search(r"\u20ac\s*([\d\u202f.]+)\s*$", text, re.M)
        if match:
            return ImmovlanScraper._parse_price(match.group(1))

        return 0

    def enrich_listing(self, listing: Listing) -> Listing:
        """Immovlan detail pages already have full data."""
        return listing


# Allow running standalone for testing
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    scraper = ImmovlanScraper()
    results = scraper.safe_scrape()
    for r in results:
        print(
            f"  [{r.id}] {r.price:>7}\u20ac  {r.bedrooms}sl  "
            f"EPC {r.epc_label or '?'}  "
            f"{r.surface_m2 or '?'}m\u00b2  "
            f"{r.address}"
        )
    print(f"\nTotal: {len(results)} listings")
