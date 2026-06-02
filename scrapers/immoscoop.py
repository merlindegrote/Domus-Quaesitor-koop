"""Immoscoop.be scraper for for-sale houses in Ghent."""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Listing
from config import TARGET_POSTAL_CODE, TARGET_CITY, MIN_PRICE, MAX_PRICE, MIN_BEDROOMS

logger = logging.getLogger(__name__)


class ImmoscoopScraper(BaseScraper):
    """Scraper for Immoscoop.be for-sale listings."""

    PLATFORM_NAME = "immoscoop"
    REQUEST_DELAY = 3.0

    SEARCH_URL = f"https://www.immoscoop.be/zoeken/te-koop/{TARGET_POSTAL_CODE}-{TARGET_CITY.lower()}/huis?priceMin={MIN_PRICE}&priceMax={MAX_PRICE}"
    SEARCH_URL_ALT = f"https://www.immoscoop.be/zoeken/te-koop/{TARGET_CITY.lower()}/huis?priceMin={MIN_PRICE}&priceMax={MAX_PRICE}"

    MAX_PAGES = 3

    def scrape(self) -> list[Listing]:
        """Scrape Immoscoop search results."""
        listings = []

        for page in range(1, self.MAX_PAGES + 1):
            page_listings = self._scrape_search_page(self._page_url(self.SEARCH_URL, page))
            if not page_listings and page == 1:
                logger.info(f"[{self.PLATFORM_NAME}] Trying alternative URL")
                page_listings = self._scrape_search_page(self._page_url(self.SEARCH_URL_ALT, page))

            if not page_listings:
                logger.info(f"[{self.PLATFORM_NAME}] No more results on page {page}")
                break

            listings.extend(page_listings)
            logger.info(f"[{self.PLATFORM_NAME}] Page {page}: {len(page_listings)} listings")

        return listings

    def _scrape_search_page(self, url: str) -> list[Listing]:
        """Parse a single search results page."""
        response = self._get_with_fallback(url)
        if not response:
            logger.warning(f"[{self.PLATFORM_NAME}] Failed to fetch: all attempts failed")
            return []

        soup = BeautifulSoup(response.text, "lxml")
        listings = []

        # Try JSON-LD first
        json_listings = self._extract_jsonld(soup)
        if json_listings:
            return json_listings

        # Try embedded JSON
        json_listings = self._extract_embedded_json(response.text)
        if json_listings:
            return json_listings

        # Fallback: parse HTML cards
        cards = soup.select(
            ".property-card, "
            "[class*='search-result'], "
            "article[class*='listing'], "
            ".result-item, "
            "[class*='property-list'] > div, "
            "[class*='property-list'] > article"
        )

        for card in cards:
            listing = self._parse_html_card(card)
            if listing:
                listings.append(listing)

        if listings:
            return listings

        return self._parse_anchor_blocks(soup)

    def _page_url(self, base_url: str, page: int) -> str:
        """Build a paginated Immoscoop URL."""
        if page <= 1:
            return base_url
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}page={page}"

    def _parse_anchor_blocks(self, soup: BeautifulSoup) -> list[Listing]:
        """Fallback parser for the current Immoscoop results layout."""
        listings: list[Listing] = []
        seen_ids: set[str] = set()

        for link in soup.find_all("a", href=True):
            title_text = link.get_text(" ", strip=True)
            if "Huis te koop" not in title_text:
                continue

            href = link.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = f"https://www.immoscoop.be{href}"

            listing_id = self._extract_id_from_url(href)
            if not listing_id or listing_id in seen_ids:
                continue

            container = self._find_result_container(link)
            text = container.get_text("\n", strip=True)
            price = self._parse_price(text)
            if not (MIN_PRICE <= price <= MAX_PRICE):
                continue

            bedrooms = self._extract_bedrooms(container)
            if bedrooms and bedrooms < MIN_BEDROOMS:
                continue

            image_urls: list[str] = []
            img = container.select_one("img[src], img[data-src]")
            if img:
                image_url = img.get("src") or img.get("data-src") or ""
                if image_url and not image_url.startswith("http"):
                    image_url = f"https://www.immoscoop.be{image_url}"
                if image_url:
                    image_urls.append(image_url)

            address = self._extract_address_from_text(text)
            if title_text in ("Huis te koop", "House", "Te koop"):
                title_final = f"Te koop: {address}"
            else:
                # Haal een proper adres uit de ruime anchor text i.p.v. de hele blok
                title_final = f"Te koop: {address}"
            listings.append(
                Listing(
                    id=listing_id,
                    platform=self.PLATFORM_NAME,
                    title=title_final,
                    price=price,
                    bedrooms=max(bedrooms, MIN_BEDROOMS),
                    address=self._extract_address_from_text(text),
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
                    '[class*="beschrij"]',
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
                images = soup.select('img[src*="immoscoop"], img[data-src*="immoscoop"], img[src]')
                image_urls: list[str] = []
                for img in images:
                    image_url = img.get("src") or img.get("data-src") or ""
                    if not image_url:
                        continue
                    if image_url.startswith("//"):
                        image_url = f"https:{image_url}"
                    elif not image_url.startswith("http"):
                        image_url = f"https://www.immoscoop.be{image_url}"
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
                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    if data.get("@type") == "ItemList":
                        items = data.get("itemListElement", [])
                    elif data.get("@type") in ["Apartment", "Residence", "Product"]:
                        items = [data]

                for item in items:
                    actual = item.get("item", item) if isinstance(item, dict) else item
                    listing = self._parse_jsonld_item(actual)
                    if listing:
                        listings.append(listing)
            except (json.JSONDecodeError, TypeError):
                continue
        return listings

    def _extract_embedded_json(self, html: str) -> list[Listing]:
        """Extract listings from embedded JavaScript data."""
        listings = []
        patterns = [
            r'var\s+(?:properties|listings|results)\s*=\s*(\[.*?\]);',
            r'"(?:properties|listings|results)"\s*:\s*(\[.*?\])',
            r'__INITIAL_STATE__\s*=\s*(\{.*?\});',
            r'__NEXT_DATA__\s*=\s*(\{.*?\});',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    items = data if isinstance(data, list) else self._find_listings_in_dict(data)
                    for item in items:
                        listing = self._parse_json_item(item)
                        if listing:
                            listings.append(listing)
                    if listings:
                        return listings
                except (json.JSONDecodeError, TypeError):
                    continue

        return listings

    def _find_listings_in_dict(self, data: dict) -> list:
        """Recursively find listing arrays in nested dict."""
        for key in ["results", "properties", "listings", "items", "data"]:
            if key in data:
                val = data[key]
                if isinstance(val, list):
                    return val
                if isinstance(val, dict):
                    return self._find_listings_in_dict(val)
        return []

    def _parse_jsonld_item(self, item: dict) -> Listing | None:
        """Parse a listing from JSON-LD item."""
        try:
            url = item.get("url", "")
            listing_id = self._extract_id_from_url(url)
            if not listing_id:
                return None

            offers = item.get("offers", {})
            price = int(offers.get("price", 0)) if isinstance(offers, dict) else 0
            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None

            address = self._extract_address(item)
            title_name = item.get("name", "")
            if not title_name or title_name in ("Huis te koop", "House", "Te koop"):
                title_name = f"Te koop: {address}"
            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=title_name,
                price=price,
                bedrooms=MIN_BEDROOMS,
                address=address,
                url=url if url.startswith("http") else f"https://www.immoscoop.be{url}",
                description=item.get("description", ""),
                image_urls=[item["image"]] if item.get("image") else [],
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to parse JSON-LD: {e}")
            return None

    def _parse_json_item(self, item: dict) -> Listing | None:
        """Parse a listing from embedded JSON."""
        try:
            listing_id = str(item.get("id", ""))
            if not listing_id:
                return None

            price = int(item.get("price", item.get("rent", item.get("huurprijs", 0))))
            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None

            bedrooms = int(item.get("bedrooms", item.get("slaapkamers", item.get("rooms", MIN_BEDROOMS))))
            if bedrooms < MIN_BEDROOMS:
                return None

            url = item.get("url", item.get("link", ""))
            if url and not url.startswith("http"):
                url = f"https://www.immoscoop.be{url}"

            images = []
            for key in ["image", "images", "photo", "photos", "thumbnail"]:
                if key in item:
                    val = item[key]
                    if isinstance(val, str):
                        images = [val]
                    elif isinstance(val, list):
                        images = [v for v in val[:5] if isinstance(v, str)]
                    break

            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=item.get("title", item.get("name", f"House in {TARGET_CITY.capitalize()} — €{price}")),
                price=price,
                bedrooms=bedrooms,
                address=item.get("address", item.get("location", TARGET_CITY.capitalize())),
                url=url,
                description=item.get("description", item.get("beschrijving", "")),
                image_urls=images,
                epc_label=item.get("epc", item.get("epc_label")),
                surface_m2=self._safe_int(item.get("surface", item.get("oppervlakte"))),
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to parse JSON item: {e}")
            return None

    def _parse_html_card(self, card: BeautifulSoup) -> Listing | None:
        """Parse a listing from HTML card."""
        try:
            # Find link
            link = card.find("a", href=True)
            if not link:
                return None

            href = link.get("href", "")
            if not href:
                return None
            if not href.startswith("http"):
                href = f"https://www.immoscoop.be{href}"

            listing_id = self._extract_id_from_url(href)
            if not listing_id:
                return None

            # Title
            title_el = card.select_one("h2, h3, [class*='title']")
            title = title_el.get_text(separator=' ', strip=True) if title_el else ""

            # Price
            price_el = card.select_one("[class*='price'], [class*='prijs']")
            price = self._parse_price(price_el.get_text(strip=True)) if price_el else 0
            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None

            # Address
            addr_el = card.select_one("[class*='address'], [class*='location'], [class*='adres']")
            address = addr_el.get_text(separator=' ', strip=True) if addr_el else TARGET_CITY.capitalize()
            if not title or title in ("Huis te koop", "House", "Te koop"):
                title = f"Te koop: {address}"

            # Bedrooms
            bedrooms = self._extract_bedrooms(card)

            # Surface
            surface = self._extract_surface(card)

            # Image — probeer meerdere attributen en fallback op figure/picture
            img = card.select_one("img")
            image_url = ""
            if img:
                for attr in ["src", "data-src", "data-srcset", "srcset"]:
                    val = img.get(attr, "")
                    if val:
                        if " " in val:
                            val = val.split(" ")[0]  # srcset = "url 1x, url2 2x"
                        image_url = val
                        break
            if not image_url:
                # Fallback: check figure/picture elementen
                fig = card.select_one("figure img, picture img")
                if fig:
                    for attr in ["src", "data-src", "data-srcset", "srcset"]:
                        val = fig.get(attr, "")
                        if val:
                            if " " in val:
                                val = val.split(" ")[0]
                            image_url = val
                            break

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
        """Extract unique ID from URL.
        Immoscoop IDs zijn de laatste numerieke segmenten:
        /te-koop/2440-geel/1137750  ->  "1137750"
        """
        # Strip query params
        clean_url = url.split("?")[0].rstrip("/")
        parts = clean_url.split("/")
        # Zoek van achteren naar een numeriek segment (minstens 4 cijfers, geen postcode)
        for part in reversed(parts):
            if re.match(r'^\d{6,}$', part):
                return part
        # Fallback: laatste numerieke segment
        for part in reversed(parts):
            if re.match(r'^\d{4,}$', part) and part != TARGET_POSTAL_CODE:
                return part
        # Laatste segment als fallback
        return parts[-1] if parts else ""

    @staticmethod
    def _extract_address(item: dict) -> str:
        """Extract address from JSON-LD item."""
        addr = item.get("address", {})
        if isinstance(addr, dict):
            parts = [
                addr.get("streetAddress", ""),
                addr.get("postalCode", ""),
                addr.get("addressLocality", TARGET_CITY.capitalize()),
            ]
            return " ".join(p for p in parts if p).strip()
        if isinstance(addr, str):
            return addr
        return TARGET_CITY.capitalize()

    @staticmethod
    def _parse_price(text: str) -> int:
        """Parse price from text."""
        numbers = re.findall(r'[\d]+', text.replace(".", "").replace(",", ""))
        for num in numbers:
            try:
                price = int(num)
                if 50000 <= price <= 2000000:
                    return price
            except ValueError:
                continue
        return 0

    @staticmethod
    def _extract_bedrooms(card: BeautifulSoup) -> int:
        """Extract bedroom count."""
        text = card.get_text()
        for pattern in [
            r'(\d+)\s*(?:slaapkamer|bedroom|chambre)',
            r'(\d+)\s*(?:slpk|bed)',
        ]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return 0

    @staticmethod
    def _extract_surface(card: BeautifulSoup) -> int | None:
        """Extract surface area."""
        text = card.get_text()
        match = re.search(r'(\d+)\s*m²', text)
        return int(match.group(1)) if match else None

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
    scraper = ImmoscoopScraper()
    results = scraper.safe_scrape()
    for r in results:
        print(f"  [{r.id}] {r.title} — €{r.price} — {r.address}")
    print(f"\nTotal: {len(results)} listings")
