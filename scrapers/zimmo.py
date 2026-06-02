"""Zimmo.be scraper for houses for-sale."""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Listing
from config import TARGET_POSTAL_CODE, TARGET_CITY, MIN_PRICE, MAX_PRICE, MIN_BEDROOMS

logger = logging.getLogger(__name__)


class ZimmoScraper(BaseScraper):
    """Scraper for Zimmo.be house for-sale listings."""

    PLATFORM_NAME = "zimmo"
    REQUEST_DELAY = 2.0

    SEARCH_URL = (
        f"https://www.zimmo.be/nl/{TARGET_CITY.lower()}-{TARGET_POSTAL_CODE}/"
        f"te-koop/huis/?priceMin={MIN_PRICE}&priceMax={MAX_PRICE}&roomsMin={MIN_BEDROOMS}"
    )

    MAX_PAGES = 3

    def scrape(self) -> list[Listing]:
        """Scrape Zimmo search results."""
        listings = []

        for page in range(1, self.MAX_PAGES + 1):
            page_listings = self._scrape_search_page(self._page_url(page))
            if not page_listings:
                logger.info(f"[{self.PLATFORM_NAME}] No more results on page {page}")
                break
            listings.extend(page_listings)
            logger.info(f"[{self.PLATFORM_NAME}] Page {page}: {len(page_listings)} listings")

        return listings

    def _scrape_search_page(self, url: str) -> list[Listing]:
        """Parse a single Zimmo search results page."""
        response = self._get_with_fallback(url)
        if not response:
            return []

        soup = BeautifulSoup(response.text, "lxml")

        json_listings = self._extract_jsonld(soup)
        if json_listings:
            return json_listings

        json_listings = self._extract_embedded_json(response.text)
        if json_listings:
            return json_listings

        listings = []
        cards = soup.select(
            ".property-item, [class*='property-card'], "
            "[class*='search-result'], article[class*='result'], .card-property"
        )
        for card in cards:
            listing = self._parse_html_card(card, soup)
            if listing:
                listings.append(listing)

        if listings:
            return listings
        return self._parse_anchor_blocks(soup)

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return self.SEARCH_URL
        separator = "&" if "?" in self.SEARCH_URL else "?"
        return f"{self.SEARCH_URL}{separator}page={page}"

    def _parse_anchor_blocks(self, soup: BeautifulSoup) -> list[Listing]:
        listings: list[Listing] = []
        seen_ids: set[str] = set()

        for link in soup.find_all("a", href=True):
            title_text = link.get_text(" ", strip=True)
            if "Huis te koop" not in title_text:
                continue
            # Skip appartments
            if "Appartement" in title_text:
                continue
            href = link.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = f"https://www.zimmo.be{href}"

            listing_id = self._extract_id_from_url(href)
            if not listing_id or listing_id in seen_ids:
                continue

            container = self._find_result_container(link)
            text = container.get_text("\n", strip=True)
            price = self._parse_price(text)
            if not (MIN_PRICE <= price <= MAX_PRICE):
                continue

            address = self._extract_address_from_text(text)
            # Fix huisnummer dat tegen postcode plakt
            address = re.sub(r'(\d+)(\d{4})\s', r'\1 \2', address)
            bedrooms = self._extract_bedrooms(container)
            if bedrooms and bedrooms < MIN_BEDROOMS:
                continue

            image_urls: list[str] = []
            img = container.select_one("img[src], img[data-src]")
            if img:
                image_url = img.get("src") or img.get("data-src") or ""
                if image_url and not image_url.startswith("http"):
                    image_url = f"https://www.zimmo.be{image_url}"
                if image_url:
                    image_urls.append(image_url)

            surface = self._extract_surface(container)

            listings.append(Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=f"Te koop: {address}",
                price=price,
                bedrooms=max(bedrooms, MIN_BEDROOMS),
                address=address,
                url=href,
                description="",
                image_urls=image_urls,
                surface_m2=surface,
            ))
            seen_ids.add(listing_id)

        return listings

    def enrich_listing(self, listing: Listing) -> Listing:
        if listing.description and len(listing.description) >= 80 and len(listing.image_urls) > 1:
            return listing
        try:
            response = self._rate_limited_get(listing.url)
            soup = BeautifulSoup(response.text, "lxml")
            if not listing.description:
                for sel in ['meta[property="og:description"]', 'meta[name="description"]',
                            '[class*="description"]', '[data-testid*="description"]']:
                    node = soup.select_one(sel)
                    if not node:
                        continue
                    text = node.get("content", "").strip() if node.name == "meta" else node.get_text(" ", strip=True)
                    if len(text) >= 40:
                        listing.description = text
                        break
            if len(listing.image_urls) <= 1:
                images = soup.select('img[src*="zimmo"], img[data-src*="zimmo"], img[src]')
                urls = []
                for img in images:
                    u = img.get("src") or img.get("data-src") or ""
                    if not u:
                        continue
                    if u.startswith("//"):
                        u = f"https:{u}"
                    elif not u.startswith("http"):
                        u = f"https://www.zimmo.be{u}"
                    if u not in urls:
                        urls.append(u)
                    if len(urls) >= 5:
                        break
                if urls:
                    listing.image_urls = urls

            # Enrich EPC label, surface, and lot surface from detail page
            if not listing.epc_label:
                epc_el = soup.select_one("[class*='epc'], [class*='energie'], [class*='energielabel'], "
                                         "[class*='peb'], [data-testid*='epc']")
                if epc_el:
                    listing.epc_label = epc_el.get_text(strip=True) or None

            if not listing.surface_m2:
                surface = self._extract_surface(soup)
                if surface:
                    listing.surface_m2 = surface

            if not listing.lot_surface_m2:
                lot = self._extract_lot_surface(soup)
                if lot:
                    listing.lot_surface_m2 = lot

        except Exception as exc:
            logger.debug(f"[{self.PLATFORM_NAME}] enrich failed {listing.id}: {exc}")
        return listing

    def _extract_jsonld(self, soup: BeautifulSoup) -> list[Listing]:
        listings = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "ItemList":
                    for item in data.get("itemListElement", []):
                        listing = self._parse_jsonld_item(item)
                        if listing:
                            listings.append(listing)
            except (json.JSONDecodeError, TypeError):
                continue
        return listings

    def _extract_embedded_json(self, html: str) -> list[Listing]:
        for pattern in [r'var\s+properties\s*=\s*(\[.*?\]);',
                        r'"properties"\s*:\s*(\[.*?\])',
                        r'searchResults\s*[=:]\s*(\[.*?\])']:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    results = [self._parse_json_item(item) for item in data]
                    results = [r for r in results if r]
                    if results:
                        return results
                except (json.JSONDecodeError, TypeError):
                    continue
        return []

    def _parse_jsonld_item(self, item: dict) -> Listing | None:
        try:
            actual = item.get("item", item)

            # Check for type or name indicating apartment
            name = actual.get("name", "")
            description = actual.get("description", "")
            type_val = actual.get("@type", "")
            if "Appartement" in name or "Appartement" in description:
                return None
            if isinstance(type_val, str) and "Appartement" in type_val:
                return None

            url = actual.get("url", "")
            listing_id = self._extract_id_from_url(url)
            if not listing_id:
                return None
            offers = actual.get("offers", {})
            price = int(offers.get("price", 0)) if isinstance(offers, dict) else 0
            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None
            image = actual.get("image", "")
            address = (actual.get("address", {}).get("streetAddress", TARGET_CITY.capitalize())
                         if isinstance(actual.get("address"), dict) else TARGET_CITY.capitalize())
            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=f"Te koop: {address}",
                price=price,
                bedrooms=MIN_BEDROOMS,
                address=(actual.get("address", {}).get("streetAddress", TARGET_CITY.capitalize())
                         if isinstance(actual.get("address"), dict) else TARGET_CITY.capitalize()),
                url=url if url.startswith("http") else f"https://www.zimmo.be{url}",
                description=actual.get("description", ""),
                image_urls=[image] if image else [],
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] parse jsonld failed: {e}")
            return None

    def _parse_json_item(self, item: dict) -> Listing | None:
        try:
            # Skip appartments
            title_val = item.get("title", item.get("name", ""))
            if "Appartement" in title_val:
                return None

            listing_id = str(item.get("id", ""))
            if not listing_id:
                return None
            price = int(item.get("price", item.get("rent", 0)))
            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None
            bedrooms = int(item.get("rooms", item.get("bedrooms", item.get("bedroom_count", 0))))
            if bedrooms < MIN_BEDROOMS:
                bedrooms = MIN_BEDROOMS
            url = item.get("url", item.get("link", ""))
            if url and not url.startswith("http"):
                url = f"https://www.zimmo.be{url}"
            images = []
            if "image" in item:
                img = item["image"]
                images = [img] if isinstance(img, str) else img if isinstance(img, list) else []
            elif "images" in item and isinstance(item["images"], list):
                images = item["images"][:5]
            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=f"Te koop: {item.get('address', item.get('location', TARGET_CITY.capitalize()))}",
                price=price,
                bedrooms=bedrooms,
                address=item.get("address", item.get("location", TARGET_CITY.capitalize())),
                url=url,
                description=item.get("description", ""),
                image_urls=images,
                epc_label=item.get("epc", item.get("epc_label")),
                surface_m2=self._safe_int(item.get("surface", item.get("area"))),
                lot_surface_m2=self._safe_int(item.get("lot_surface", item.get("land_area", item.get("plot_area")))),
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] parse json failed: {e}")
            return None

    def _parse_html_card(self, card: BeautifulSoup, page_soup: BeautifulSoup) -> Listing | None:
        try:
            # Skip appartments
            card_text = card.get_text(" ", strip=True)
            if "Appartement" in card_text:
                return None

            link = card.find("a", href=True)
            if not link:
                return None
            href = link.get("href", "")
            if not href:
                return None
            if not href.startswith("http"):
                href = f"https://www.zimmo.be{href}"
            listing_id = self._extract_id_from_url(href)
            if not listing_id:
                return None

            title_el = card.select_one("h2, h3, [class*='title'], .property-title")
            title = title_el.get_text(separator=' ', strip=True) if title_el else ""
            price_el = card.select_one("[class*='price'], .property-price")
            price = self._parse_price(price_el.get_text(strip=True)) if price_el else 0
            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None
            addr_el = card.select_one("[class*='address'], [class*='location'], .property-location")
            address = addr_el.get_text(separator=' ', strip=True) if addr_el else TARGET_CITY.capitalize()
            # Fix 1: Split huisnummer van postcode (bv. "1582440" → "158 2440")
            address = re.sub(r'(\d+)(\d{4})\s', r'\1 \2', address)
            # Fix 2: Split straat van postcode (bv. "Stationsstraat2440" → "Stationsstraat 2440")
            address = re.sub(r'([^\d\s])(\d{4})\b', r'\1 \2', address)
            # Titels die geen straatnaam/huisnummer bevatten overschrijven met adres
            def _is_generic_title(t: str) -> bool:
                """Check of titel alleen placeholder is zonder straatnaam"""
                if not t:
                    return True
                known_generic = {"huis te koop", "house", "te koop", "house for sale", "koopwoning", ""}
                if t.strip().lower() in known_generic:
                    return True
                # Geen enkel cijfer = geen huisnummer → generic
                return not bool(re.search(r'\d', t))

            if _is_generic_title(title):
                # Format: "Klavet 14 — 2440 GEEL"
                parts = address.split(" ", 1) if " " in address else [address, ""]
                street_part = parts[0]
                rest = parts[1] if len(parts) > 1 else ""
                # Extract postcode/city uit rest
                pc_match = re.search(r'(\d{4})\s+(.+)$', rest)
                if pc_match:
                    title = f"{street_part} — {pc_match.group(2).upper()}"
                else:
                    title = f"Te koop in {address}"
            bedrooms = self._extract_bedrooms(card)
            surface = self._extract_surface(card)
            lot_surface = self._extract_lot_surface(card)
            img = card.select_one("img[src], img[data-src]")
            image_url = ""
            if img:
                image_url = img.get("src") or img.get("data-src") or ""
                if "placeholder" in image_url.lower():
                    image_url = img.get("data-src") or ""
            epc_el = card.select_one("[class*='epc'], [class*='energie'], [class*='energielabel']")
            epc = epc_el.get_text(strip=True) if epc_el else None
            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=title,
                price=price,
                bedrooms=max(bedrooms, MIN_BEDROOMS),
                address=address,
                url=href,
                description="",
                image_urls=[image_url] if image_url else [],
                surface_m2=surface,
                lot_surface_m2=lot_surface,
                epc_label=epc,
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] parse html failed: {e}")
            return None

    @staticmethod
    def _extract_id_from_url(url: str) -> str:
        """Extract Zimmo listing ID uit URL.
        ID zit in path als /huis/LNWKX/ of /huis/LNWKX?params...
        """
        # Strip query params en trailing slash
        clean_url = url.split("?")[0].rstrip("/")
        # Eerst: 5+ uppercase letters (Zimmo formaat: LNWKX, LOQTE)
        match = re.search(r"/([A-Z0-9]{5,})(?:/|$)", clean_url)
        if match:
            return match.group(1)
        # Dan: getallen 4+
        match = re.search(r'/(\d{4,})(?:/|$)', clean_url)
        if match:
            return match.group(1)
        # Laatste path segment
        parts = clean_url.split("/")
        return parts[-1] if parts else ""

    @staticmethod
    def _parse_price(text: str) -> int:
        numbers = re.findall(r'[\d]+', text.replace(".", "").replace(",", ""))
        for num in numbers:
            try:
                p = int(num)
                if 50000 <= p <= 2000000:
                    return p
            except ValueError:
                continue
        return 0

    @staticmethod
    def _extract_bedrooms(card: BeautifulSoup) -> int:
        text = card.get_text()
        for pat in [r'(\d+)\s*(?:slaapkamer|bedroom|chambre)', r'(\d+)\s*(?:slpk|bed)']:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                return int(m.group(1))
        return 0

    @staticmethod
    def _extract_surface(card: BeautifulSoup) -> int | None:
        text = card.get_text()
        m = re.search(r'(\d+)\s*m²', text)
        return int(m.group(1)) if m else None

    @staticmethod
    def _extract_lot_surface(card: BeautifulSoup) -> int | None:
        """Extract lot/surface area (m²) from card text."""
        text = card.get_text()
        # Look for "perceel", "grond", "lot", "terrein" followed by m²
        for pattern in [
            r'(?:perceel|grond|lot|terrein|grondoppervlakte)\s*[:\s]*(\d+)\s*m²',
            r'(\d+)\s*m²\s*(?:perceel|grond|lot|terrein)',
        ]:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return int(m.group(1))
        # Fallback: look for a second surface value if there are multiple m² matches
        surfaces = re.findall(r'(\d+)\s*m²', text)
        if len(surfaces) >= 2:
            return int(surfaces[1])
        return None

    @staticmethod
    def _safe_int(val) -> int | None:
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _find_result_container(link):
        node = link
        for _ in range(6):
            node = node.parent
            if node is None:
                break
            text = node.get_text(" ", strip=True)
            if "€" in text and len(text) < 900:
                return node
        return link.parent or link

    @staticmethod
    def _extract_address_from_text(text: str) -> str:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if re.fullmatch(rf"{TARGET_POSTAL_CODE}\s+{re.escape(TARGET_CITY)}", line, re.IGNORECASE):
                return f"{lines[i-1]}, {line}" if i > 0 else line
        m = re.search(rf"([A-ZÀ-ÿ0-9][^\n]+?)\s*({TARGET_POSTAL_CODE}\s+{re.escape(TARGET_CITY)})", text, re.IGNORECASE)
        if m:
            return f"{m.group(1).strip()}, {m.group(2).strip()}"
        return TARGET_CITY.capitalize()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    scraper = ZimmoScraper()
    results = scraper.safe_scrape()
    for r in results:
        print(f"  [{r.id}] {r.title} — €{r.price} — {r.address}")
    print(f"\nTotal: {len(results)} listings")
