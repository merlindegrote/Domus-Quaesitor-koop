"""Zimmo.be scraper for rental apartments in Ghent."""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Listing
from config import TARGET_POSTAL_CODE, TARGET_CITY, MIN_PRICE, MAX_PRICE, MIN_BEDROOMS

logger = logging.getLogger(__name__)


class ZimmoScraper(BaseScraper):
    """Scraper for Zimmo.be rental listings."""

    PLATFORM_NAME = "zimmo"
    REQUEST_DELAY = 2.0

    SEARCH_URL = f"https://www.zimmo.be/nl/{TARGET_CITY.lower()}-{TARGET_POSTAL_CODE}/te-huur/appartement/?priceMin={MIN_PRICE}&priceMax={MAX_PRICE}&roomsMin={MIN_BEDROOMS}"

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
            logger.warning(f"[{self.PLATFORM_NAME}] Failed to fetch: all attempts returned 403")
            return []

        soup = BeautifulSoup(response.text, "lxml")
        listings = []

        # Try extracting from JSON-LD first
        json_listings = self._extract_jsonld(soup)
        if json_listings:
            return json_listings

        # Try embedded JSON data
        json_listings = self._extract_embedded_json(response.text)
        if json_listings:
            return json_listings

        # Fallback: parse HTML property cards
        cards = soup.select(
            ".property-item, "
            "[class*='property-card'], "
            "[class*='search-result'], "
            "article[class*='result'], "
            ".card-property"
        )

        for card in cards:
            listing = self._parse_html_card(card, soup)
            if listing:
                listings.append(listing)

        if listings:
            return listings

        return self._parse_anchor_blocks(soup)

    def _page_url(self, page: int) -> str:
        """Build a paginated Zimmo URL."""
        if page <= 1:
            return self.SEARCH_URL
        separator = "&" if "?" in self.SEARCH_URL else "?"
        return f"{self.SEARCH_URL}{separator}page={page}"

    def _parse_anchor_blocks(self, soup: BeautifulSoup) -> list[Listing]:
        """Fallback parser for the current Zimmo results layout."""
        listings: list[Listing] = []
        seen_ids: set[str] = set()

        for link in soup.find_all("a", href=True):
            title_text = link.get_text(" ", strip=True)
            if "Appartement te huur" not in title_text:
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

            listings.append(
                Listing(
                    id=listing_id,
                    platform=self.PLATFORM_NAME,
                    title=title_text,
                    price=price,
                    bedrooms=max(bedrooms, MIN_BEDROOMS),
                    address=address,
                    url=href,
                    description="",
                    image_urls=image_urls,
                    surface_m2=self._extract_surface(container),
                )
            )
            seen_ids.add(listing_id)

        return listings

    def enrich_listing(self, listing: Listing) -> Listing:
        """Fetch detail page data for description and extra photos."""
        if listing.description and len(listing.description) >= 80 and len(listing.image_urls) > 1:
            return listing

        try:
            response = self._rate_limited_get(listing.url)
            soup = BeautifulSoup(response.text, "lxml")

            if not listing.description:
                for selector in [
                    'meta[property="og:description"]',
                    'meta[name="description"]',
                    '[class*="description"]',
                    '[data-testid*="description"]',
                ]:
                    node = soup.select_one(selector)
                    if not node:
                        continue
                    if node.name == "meta":
                        text = node.get("content", "").strip()
                    else:
                        text = node.get_text(" ", strip=True)
                    if len(text) >= 40:
                        listing.description = text
                        break

            if len(listing.image_urls) <= 1:
                images = soup.select('img[src*="zimmo"], img[data-src*="zimmo"], img[src]')
                image_urls: list[str] = []
                for img in images:
                    image_url = img.get("src") or img.get("data-src") or ""
                    if not image_url:
                        continue
                    if image_url.startswith("//"):
                        image_url = f"https:{image_url}"
                    elif not image_url.startswith("http"):
                        image_url = f"https://www.zimmo.be{image_url}"
                    if image_url not in image_urls:
                        image_urls.append(image_url)
                    if len(image_urls) >= 5:
                        break
                if image_urls:
                    listing.image_urls = image_urls
        except Exception as exc:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to enrich listing {listing.id}: {exc}")

        return listing

    def _extract_jsonld(self, soup: BeautifulSoup) -> list[Listing]:
        """Extract listings from JSON-LD structured data."""
        listings = []
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
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
        """Try to find listing data in embedded scripts."""
        listings = []
        patterns = [
            r'var\s+properties\s*=\s*(\[.*?\]);',
            r'"properties"\s*:\s*(\[.*?\])',
            r'searchResults\s*[=:]\s*(\[.*?\])',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    for item in data:
                        listing = self._parse_json_item(item)
                        if listing:
                            listings.append(listing)
                    if listings:
                        return listings
                except (json.JSONDecodeError, TypeError):
                    continue

        return listings

    def _parse_jsonld_item(self, item: dict) -> Listing | None:
        """Parse a listing from JSON-LD format."""
        try:
            listed_item = item.get("item", item)
            if not listed_item:
                return None

            url = listed_item.get("url", "")
            name = listed_item.get("name", "")

            # Extract ID from URL
            listing_id = self._extract_id_from_url(url)
            if not listing_id:
                return None

            # Price
            offers = listed_item.get("offers", {})
            price = int(offers.get("price", 0)) if isinstance(offers, dict) else 0
            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None

            # Image
            image = listed_item.get("image", "")
            images = [image] if image else []

            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=name or f"Apartment in {TARGET_CITY.capitalize()} — €{price}/mo",
                price=price,
                bedrooms=MIN_BEDROOMS,  # Default, will be enriched
                address=listed_item.get("address", {}).get("streetAddress", TARGET_CITY.capitalize())
                if isinstance(listed_item.get("address"), dict)
                else TARGET_CITY.capitalize(),
                url=url if url.startswith("http") else f"https://www.zimmo.be{url}",
                description=listed_item.get("description", ""),
                image_urls=images,
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to parse JSON-LD item: {e}")
            return None

    def _parse_json_item(self, item: dict) -> Listing | None:
        """Parse a listing from embedded JSON."""
        try:
            listing_id = str(item.get("id", ""))
            if not listing_id:
                return None

            price = int(item.get("price", item.get("rent", 0)))
            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None

            bedrooms = int(item.get("rooms", item.get("bedrooms", item.get("bedroom_count", 0))))
            if bedrooms < MIN_BEDROOMS:
                bedrooms = MIN_BEDROOMS  # Default

            url = item.get("url", item.get("link", ""))
            if not url.startswith("http"):
                url = f"https://www.zimmo.be{url}"

            images = []
            if "image" in item:
                img = item["image"]
                images = [img] if isinstance(img, str) else img if isinstance(img, list) else []
            elif "images" in item:
                images = item["images"][:5] if isinstance(item["images"], list) else []

            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=item.get("title", item.get("name", f"Apartment in {TARGET_CITY.capitalize()} — €{price}/mo")),
                price=price,
                bedrooms=bedrooms,
                address=item.get("address", item.get("location", TARGET_CITY.capitalize())),
                url=url,
                description=item.get("description", ""),
                image_urls=images,
                epc_label=item.get("epc", item.get("epc_label")),
                surface_m2=self._safe_int(item.get("surface", item.get("area"))),
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to parse JSON item: {e}")
            return None

    def _parse_html_card(self, card: BeautifulSoup, page_soup: BeautifulSoup) -> Listing | None:
        """Parse a listing from an HTML card element."""
        try:
            # Find link
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

            # Title
            title_el = card.select_one("h2, h3, [class*='title'], .property-title")
            title = title_el.get_text(strip=True) if title_el else f"Apartment in {TARGET_CITY.capitalize()}"

            # Price
            price_el = card.select_one("[class*='price'], .property-price")
            price = self._parse_price(price_el.get_text(strip=True)) if price_el else 0
            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None

            # Address
            addr_el = card.select_one("[class*='address'], [class*='location'], .property-location")
            address = addr_el.get_text(strip=True) if addr_el else TARGET_CITY.capitalize()

            # Bedrooms
            bedrooms = self._extract_bedrooms(card)

            # Surface
            surface = self._extract_surface(card)

            # Image
            img = card.select_one("img[src], img[data-src]")
            image_url = ""
            if img:
                image_url = img.get("src") or img.get("data-src") or ""
                # Skip placeholder images
                if "placeholder" in image_url.lower() or "lazy" in image_url.lower():
                    image_url = img.get("data-src") or ""

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
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to parse HTML card: {e}")
            return None

    # --- Helper methods ---

    @staticmethod
    def _extract_id_from_url(url: str) -> str:
        """Extract a unique ID from a Zimmo URL."""
        match = re.search(r"/([A-Z0-9]{5,})/?$", url)
        if match:
            return match.group(1)
        match = re.search(r'/(\d{4,})', url)
        if match:
            return match.group(1)
        # Fallback: use last path segment
        parts = url.rstrip("/").split("/")
        return parts[-1] if parts else ""

    @staticmethod
    def _parse_price(text: str) -> int:
        """Parse price from text."""
        numbers = re.findall(r'[\d]+', text.replace(".", "").replace(",", ""))
        for num in numbers:
            try:
                price = int(num)
                if 100 <= price <= 10000:
                    return price
            except ValueError:
                continue
        return 0

    @staticmethod
    def _extract_bedrooms(card: BeautifulSoup) -> int:
        """Extract bedroom count from card."""
        text = card.get_text()
        patterns = [
            r'(\d+)\s*(?:slaapkamer|bedroom|chambre)',
            r'(\d+)\s*(?:slpk|bed)',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return 0

    @staticmethod
    def _extract_surface(card: BeautifulSoup) -> int | None:
        """Extract surface area."""
        text = card.get_text()
        match = re.search(r'(\d+)\s*m²', text)
        if match:
            return int(match.group(1))
        return None

    @staticmethod
    def _safe_int(val) -> int | None:
        """Safely convert to int."""
        if val is None:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _find_result_container(link):
        """Find a reasonably scoped card container for a listing link."""
        node = link
        for _ in range(6):
            node = node.parent
            if node is None:
                break
            text = node.get_text(" ", strip=True)
            if "€" in text and TARGET_CITY.lower() in text.lower() and len(text) < 900:
                return node
        return link.parent or link

    @staticmethod
    def _extract_address_from_text(text: str) -> str:
        """Extract an address from nearby card text."""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if re.fullmatch(rf"{TARGET_POSTAL_CODE}\s+{re.escape(TARGET_CITY)}", line, re.IGNORECASE):
                if index > 0:
                    return f"{lines[index - 1]}, {line}"
                return line

        match = re.search(rf"([A-ZÀ-ÿ0-9][^\n]+?)\s*({TARGET_POSTAL_CODE}\s+{re.escape(TARGET_CITY)})", text, re.IGNORECASE)
        if match:
            return f"{match.group(1).strip()}, {match.group(2).strip()}"

        return TARGET_CITY.capitalize()


# Allow running standalone for testing
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    scraper = ZimmoScraper()
    results = scraper.safe_scrape()
    for r in results:
        print(f"  [{r.id}] {r.title} — €{r.price} — {r.address}")
    print(f"\nTotal: {len(results)} listings")
