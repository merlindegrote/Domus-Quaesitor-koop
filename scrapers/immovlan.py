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
    r"Huis\s+te\s+koop"
    r"\s*\|\s*(?P<address>[^|]+?)\s*"
    r"\|\s*(?P<price>[\d.,\u202f\s]+)\s*\u20ac"
    r"\s*\|\s*Bewoonbare opp\.?\s*(?P<surface>[\d.]+)\s*m[²2\u00b2]?\s*"
    r"(?:\|\s*EPC\s*(?P<epc>[A-F][+-]?))?"
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
        """Scrape Immovlan: search pages → detail URLs → detail pages → city-filtered listings."""
        all_detail_urls: list[str] = []
        seen_ids: set[str] = set()

        for page in range(1, self.MAX_PAGES + 1):
            page_urls = self._fetch_search_page(page)
            if not page_urls:
                break

            # Filter: type "huis" only (not villa/appartement/handelspand)
            house_urls = [u for u in page_urls if "/detail/huis/" in u]

            # Filter by accepted cities BEFORE fetching detail pages (city is in URL)
            house_urls = [u for u in house_urls if self._is_in_accepted_cities(u)]

            new_urls = []
            for u in house_urls:
                uid = u.rstrip("/").split("/")[-1]
                if uid not in seen_ids:
                    seen_ids.add(uid)
                    new_urls.append(u)

            if not house_urls:
                # No house listings on this page — might be last page
                if len(page_urls) < 2:
                    break
                continue

            if not new_urls:
                break

            all_detail_urls.extend(new_urls)

            if len(page_urls) < 2:
                break

            logger.info(
                f"[{self.PLATFORM_NAME}] Page {page}: {len(new_urls)} new listings"
            )

        # Fetch detail pages
        logger.info(
            f"[{self.PLATFORM_NAME}] Fetching {len(all_detail_urls)} detail pages..."
        )
        listings: list[Listing] = []
        for detail_url in all_detail_urls:
            listing = self._fetch_detail(detail_url)
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

    def _is_in_accepted_cities(self, url: str) -> bool:
        """Check if URL's postcode is in our target postals WITHOUT fetching detail page.
        URL format: https://immovlan.be/nl/detail/huis/te-koop/{postcode}/{city}/{id}"""
        from config import TARGET_POSTALS, EXCLUDE_CITIES_FINAL
        # Postcode is at position -3 in the URL path
        parts = url.rstrip("/").split("/")
        try:
            postal = parts[-3]
        except IndexError:
            return False
        if postal in TARGET_POSTALS:
            # Check if the city (parts[-2]) is in the exclude list
            city_part = parts[-2].lower() if len(parts) >= 2 else ""
            for ex_city in EXCLUDE_CITIES_FINAL:
                if ex_city.lower() == city_part:
                    return False
            return True
        return False

    def _fetch_search_page(self, page: int) -> list[str]:
        """Fetch search result page and extract full detail URLs from anchor links."""
        url = (
            f"https://www.immovlan.be/nl/vastgoed"
            f"?type=huis&transactiontypes=te-koop&places=all"
            f"&pricemin={MIN_PRICE}&pricemax={MAX_PRICE}"
            f"&page={page}"
        )
        response = self._get_with_fallback(url)
        if not response:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        seen: set[str] = set()
        urls = []
        for a_tag in soup.find_all("a", href=re.compile(r"/detail/")):
            href = a_tag.get("href", "").strip()
            if not href:
                continue
            # Normalise absolute URL
            if href.startswith("/"):
                href = "https://www.immovlan.be" + href
            if href not in seen:
                seen.add(href)
                urls.append(href)

        return urls

    def _fetch_detail(self, detail_url: str) -> Listing | None:
        """Fetch and parse a single property detail page using the full URL."""
        prop_id = detail_url.rstrip("/").split("/")[-1]

        response = self._get_with_fallback(detail_url, timeout=20)
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

        # Skip appartments
        if "Appartement" in meta_content:
            return None

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
            url=f"https://www.immovlan.be/nl/detail/huis/te-koop/{prop_id}",
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
        # Skip if meta description says Appartement
        raw_meta = _RAW_META_RE.search(html)
        if raw_meta:
            meta_content = _html_unescape(raw_meta.group(1))
            if "Appartement" in meta_content:
                return None

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

        # Skip appartments
        if "Appartement" in desc or "Appartement" in house_data.get("name", ""):
            return None

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
            url=f"https://www.immovlan.be/nl/detail/huis/te-koop/{prop_id}",
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

        # Skip appartments
        if "Appartement" in text:
            return None

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
            url=f"https://www.immovlan.be/nl/detail/huis/te-koop/{prop_id}",
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
        epc = re.search(r"epc-water-mark\s*Flanders([A-F][+-]?)", text)
        if epc:
            return epc.group(1)
        epc = re.search(r"EPC[\s-]*(?:label\s*)?([A-F][+-]?)", text, re.I)
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
        """Fetch detail page and extract images via og:image and gallery images."""
        if listing.image_urls:
            return listing
        try:
            resp = self._get_with_fallback(listing.url, timeout=15)
            if not resp:
                return listing
            soup = BeautifulSoup(resp.text, "html.parser")

            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                listing.image_urls = [og["content"]]
                return listing

            imgs = soup.find_all("img", class_=re.compile("photo|image|gallery|slide"))
            seen = set()
            for img in imgs[:8]:
                src = img.get("src") or img.get("data-src") or img.get("data-lazy")
                if src and src.startswith("http") and src not in seen:
                    listing.image_urls.append(src)
                    seen.add(src)
        except Exception:
            pass
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
