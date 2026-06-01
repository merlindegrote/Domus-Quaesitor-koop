"""Immovlan.be scraper for for-sale houses.

Immovlan is a JS SPA. We use a two-phase approach:
1. Search page → extract property IDs from embedded JS
2. Detail page → parse meta description + JSON-LD + URL for city/price/EPC/surface

CRITICAL: Search results are independent of city filter — properties from ALL
Belgian cities are returned. We filter by accepted cities after fetching each
detail page using the city in the redirect URL path.
"""

from __future__ import annotations

import html as html_mod
import json
import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Listing
from config import MIN_PRICE, MAX_PRICE, MIN_BEDROOMS

logger = logging.getLogger(__name__)

# Meta description format (AFTER HTML unescaping):
# "Huis te koop | [address] | [price] € | Bewoonbare opp. [surface]m² | EPC [label] | [bedrooms] Slaapkamers"
_META_RE = re.compile(
    r"(?:Huis|Appartement)\s+te\s+koop"
    r"\s*\|\s*(?P<address>[^|]+)"
    r"\s*\|\s*(?P<price>[\d.,\u202f\s]+)\s*\u20ac"
    r"\s*\|\s*Bewoonbare opp\.?\s*(?P<surface>[\d.]+)\s*m[²2\u00b2]?\s*"
    r"(?:\|\s*EPC\s*(?P<epc>[A-E][+-]?))?"
    r"(?:\s*\|\s*(?P<bedrooms>\d+)\s*Slaapkamers)?",
    re.I,
)

# Pattern in raw HTML before unescaping <meta name="description" content="...">
_RAW_META_RE = re.compile(
    r'<meta name="description"\s+content="([^"]+)"',
)


def _html_unescape(text: str) -> str:
    """Unescape HTML entities for regex matching."""
    return html_mod.unescape(text)


class ImmovlanScraper(BaseScraper):
    """Scraper for Immovlan.be for-sale listings."""

    PLATFORM_NAME = "immovlan"
    REQUEST_DELAY = 2.0
    MAX_PAGES = 5

    def scrape(self) -> list[Listing]:
        """Scrape Immovlan: search pages → IDs → detail pages → city-filtered listings."""
        all_ids: list[str] = []
        seen_ids: set[str] = set()

        for page in range(1, self.MAX_PAGES + 1):
            page_ids = self._fetch_search_page(page)
            if not page_ids:
                break

            # Short IDs (start with letter) = individual properties
            short_ids = [pid for pid in page_ids if re.match(r"^[A-Z]", pid)]
            new_ids = [pid for pid in short_ids if pid not in seen_ids]
            if not new_ids:
                break

            all_ids.extend(new_ids)
            seen_ids.update(new_ids)

            if len(page_ids) < 20:
                break

            logger.info(
                f"[{self.PLATFORM_NAME}] Page {page}: {len(new_ids)} new IDs"
            )

        # Fetch detail pages
        logger.info(
            f"[{self.PLATFORM_NAME}] Fetching {len(all_ids)} detail pages..."
        )
        listings: list[Listing] = []
        for prop_id in all_ids:
            listing = self._fetch_detail(prop_id)
            if listing:
                listings.append(listing)

        # Filter by accepted cities from config
        from config import ACCEPT_CITIES
        before = len(listings)
        listings = [l for l in listings if any(
            city.lower() in l.address.lower() or city.lower() in l.url.lower()
            for city in ACCEPT_CITIES
        )]
        logger.info(
            f"[{self.PLATFORM_NAME}] City filter: {len(listings)}/{before} kept"
        )

        return listings

    def _fetch_search_page(self, page: int) -> list[str]:
        """Fetch search result page and extract property IDs."""
        url = (
            f"https://www.immovlan.be/nl/vastgoed"
            f"?type=huis&transactiontypes=te-koop&places=all"
            f"&pricemin={MIN_PRICE}&pricemax={MAX_PRICE}"
            f"&page={page}"
        )

        response = self._get_with_fallback(url)
        if not response:
            return []

        match = re.search(
            r"STORAGE_KEY_SEARCH_RESULTS.*?JSON\.stringify\((\[.*?\])\)",
            response.text,
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
            logger.debug(f"[{self.PLATFORM_NAME}] Failed {prop_id}: {exc}")
            return None

        if not response or not response.text:
            return None

        return self._parse_detail(response.text, prop_id, response.url)

    def _parse_detail(
        self, html: str, prop_id: str, final_url: str
    ) -> Listing | None:
        """Parse detail page. Relies primarily on meta description tag + JSON-LD."""
        # --- Method 1: Parse the meta description tag (most structured) ---
        raw_meta = _RAW_META_RE.search(html)
        if raw_meta:
            listing = self._parse_meta_description(raw_meta.group(1), prop_id, final_url)
            if listing:
                return listing

        # --- Method 2: JSON-LD ---
        listing = self._parse_jsonld(html, prop_id, final_url)
        if listing:
            return listing

        # --- Method 3: HTML fallback ---
        return self._parse_html(html, prop_id, final_url)

    def _parse_meta_description(
        self, meta_content: str, prop_id: str, final_url: str
    ) -> Listing | None:
        """Parse the meta description tag for structured property data."""
        # HTML entities (&#x27;, &#x202F;, etc.) break regex matching
        meta_content = _html_unescape(meta_content)
        m = _META_RE.search(meta_content)
        if not m:
            return None

        # Price
        price = self._parse_price(m.group("price"))
        if not price or price < MIN_PRICE or price > MAX_PRICE:
            return None

        # Bedrooms
        bedrooms = MIN_BEDROOMS
        if m.group("bedrooms"):
            bedrooms = int(m.group("bedrooms"))
        if bedrooms < MIN_BEDROOMS:
            return None

        # EPC
        epc_label = m.group("epc") or self._extract_epc(meta_content)

        # Surface
        surface = None
        if m.group("surface"):
            try:
                surface = int(float(m.group("surface")))
            except ValueError:
                pass

        # Address
        address = m.group("address").strip()
        if address.startswith("|"):
            address = address[1:].strip()

        # Extract city from URL for lot surface (detail page not fetched separately yet)
        city_from_url = self._city_from_url(final_url)

        # Images — we'd need another round-trip for images, skip for now
        # (Immoweb/Zimmo already cover most, Immovlan is supplementary)

        return Listing(
            id=prop_id,
            platform=self.PLATFORM_NAME,
            title=f"Huis te koop in {city_from_url or address}",
            price=price,
            bedrooms=bedrooms,
            address=address,
            url=f"https://www.immovlan.be/nl/detail/{prop_id}",
            description=meta_content,
            image_urls=[],
            surface_m2=surface,
            epc_label=epc_label,
            lot_surface_m2=self._extract_lot_surface(meta_content),
        )

    def _parse_jsonld(
        self, html: str, prop_id: str, final_url: str
    ) -> Listing | None:
        """Parse JSON-LD blocks for House + SellAction data."""
        # Unescape HTML entities for JSON parsing
        clean = (
            html.replace("&#x202F;", " ")
            .replace("&#x20AC;", "\u20ac")
            .replace("&#xB2;", "\u00b2")
            .replace("&#x27;", "'")
            .replace("&#x2B;", "+")
            .replace("&#x2019;", "'")
            .replace("&amp;", "&")
            .replace("&euro;", "\u20ac")
        )

        scripts = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', clean, re.DOTALL
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
            if tp == "House":
                house_data = data
                break

        if not house_data:
            return None

        desc = house_data.get("description", "")

        # Bedrooms from description
        bedrooms = MIN_BEDROOMS
        bed_match = re.search(r"(\d+)\s*(?:slaapkamers?)", desc, re.I)
        if bed_match:
            bedrooms = int(bed_match.group(1))
        if bedrooms < MIN_BEDROOMS:
            return None

        # Price from meta description (more reliable than JSON-LD)
        # Re-check meta description for price
        raw_meta = _RAW_META_RE.search(html)
        price = 0
        if raw_meta:
            mm = _META_RE.search(raw_meta.group(1))
            if mm:
                price = self._parse_price(mm.group("price"))

        if not price:
            return None

        if price < MIN_PRICE or price > MAX_PRICE:
            return None

        # EPC
        epc_label = self._extract_epc(desc + " " + html)

        # Surfaces
        living_surface = self._extract_living_surface(desc + " " + html)
        lot_surface = self._extract_lot_surface(desc + " " + html)

        # Address
        address = self._extract_address(html)

        return Listing(
            id=prop_id,
            platform=self.PLATFORM_NAME,
            title=f"Huis te koop — {address}",
            price=price,
            bedrooms=bedrooms,
            address=address,
            url=f"https://www.immovlan.be/nl/detail/{prop_id}",
            description=desc.strip(),
            image_urls=[],
            surface_m2=living_surface,
            epc_label=epc_label,
            lot_surface_m2=lot_surface,
        )

    def _parse_html(
        self, html: str, prop_id: str, final_url: str
    ) -> Listing | None:
        """Minimal HTML fallback — should rarely trigger."""
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)

        meta_desc = soup.select_one('meta[name="description"]')
        description = meta_desc.get("content", "") if meta_desc else ""

        # Price
        price_match = re.search(r"([\d\u202f.]+)\s*\u20ac", description)
        price = self._parse_price(price_match.group(1)) if price_match else 0
        if not price or price < MIN_PRICE or price > MAX_PRICE:
            return None

        # Bedrooms
        bedrooms = MIN_BEDROOMS
        bed_match = re.search(r"(\d+)\s*(?:slaapkamers?)", text, re.I)
        if bed_match:
            bedrooms = int(bed_match.group(1))
        if bedrooms < MIN_BEDROOMS:
            return None

        epc_label = self._extract_epc(text)
        surface = self._extract_living_surface(text)
        lot_surface = self._extract_lot_surface(text)

        return Listing(
            id=prop_id,
            platform=self.PLATFORM_NAME,
            title=f"Huis te koop",
            price=price,
            bedrooms=bedrooms,
            address=self._extract_address(html),
            url=f"https://www.immovlan.be/nl/detail/{prop_id}",
            description=description,
            image_urls=[],
            surface_m2=surface,
            epc_label=epc_label,
            lot_surface_m2=lot_surface,
        )

    # ─── Helpers ────────────────────────────────────────────────

    @staticmethod
    def _city_from_url(url: str) -> str | None:
        """Extract city from the redirect URL path."""
        # Pattern: /nl/detail/huis/te-koop/{postcode}/{city}/{ref}
        m = re.search(r"/nl/detail/[^/]+/[^/]+/\d+/([^/]+)/", url)
        if m:
            return m.group(1)
        return None

    @staticmethod
    def _extract_epc(text: str) -> str | None:
        """Extract EPC label."""
        epc = re.search(r"epc-water-mark\s*Flanders([A-E][+-]?)", text)
        if epc:
            return epc.group(1)
        epc = re.search(r"EPC[\s-]*(?:label\s*)?([A-E][+-]?)", text, re.I)
        if epc:
            return epc.group(1).upper()
        return None

    @staticmethod
    def _extract_living_surface(text: str) -> int | None:
        """Extract living surface in m²."""
        for pat in [
            r"Bewoonbare\s+opp\.?\s*([\d.]+)\s*m",
            r"([\d.]+)\s*m[²2]\s*(?:woonruimte|woonopp|living)",
        ]:
            m = re.search(pat, text, re.I)
            if m:
                try:
                    return int(float(m.group(1).replace(",", ".")))
                except ValueError:
                    continue
        return None

    @staticmethod
    def _extract_lot_surface(text: str) -> int | None:
        """Extract lot/perceel surface in m²."""
        are = re.search(r"perceel\s+van\s+([\d.,]+)\s+are", text, re.I)
        if are:
            try:
                return int(float(are.group(1).replace(",", ".")) * 100)
            except ValueError:
                pass
        m = re.search(r"perceel\s+van\s+([\d.]+)\s*m[²2]", text, re.I)
        if m:
            try:
                return int(m.group(1).replace(".", "").replace(",", ""))
            except ValueError:
                pass
        return None

    @staticmethod
    def _extract_address(html: str) -> str:
        """Extract address from raw HTML."""
        # Meta description
        m = re.search(r'Huis te koop\s*\|\s*([^|]+?)\s*\|\s*[\d\u202f.]+\s*\u20ac', html)
        if m:
            addr = m.group(1).strip()
            # Remove trailing "|"
            if addr.endswith("|"):
                addr = addr[:-1].strip()
            return addr

        # OG title
        m = re.search(r'Huis te koop\s+in\s+(.+?)\s*\(', html)
        if m:
            return m.group(1).strip()
        return ""

    @staticmethod
    def _parse_price(text: str) -> int:
        """Parse price string with various separators."""
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

    def enrich_listing(self, listing: Listing) -> Listing:
        """Detail data already obtained."""
        return listing


# Standalone test
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
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
