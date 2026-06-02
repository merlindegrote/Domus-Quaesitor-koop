"""Immoscoop.be scraper for for-sale houses."""

from __future__ import annotations

import json
import logging
import re

import requests
from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Listing
from config import TARGET_POSTALS, TARGET_CITIES, MIN_PRICE, MAX_PRICE, MIN_BEDROOMS

logger = logging.getLogger(__name__)

_FALLBACK_POSTAL = "2520"
_FALLBACK_CITY = "Ranst"


class ImmoscoopScraper(BaseScraper):
    """Scraper for Immoscoop.be for-sale listings."""

    PLATFORM_NAME = "immoscoop"
    REQUEST_DELAY = 3.0

    MAX_PAGES = 3

    def __init__(self):
        super().__init__()
        self._current_postal = _FALLBACK_POSTAL
        self._current_city = _FALLBACK_CITY

    def _search_url(self) -> str:
        """Build search URL with current postal/city."""
        return (
            f"https://www.immoscoop.be/zoeken/te-koop/{self._current_postal}"
            f"-{self._current_city.lower()}/huis"
            f"?priceMin={MIN_PRICE}&priceMax={MAX_PRICE}"
        )

    def _search_url_alt(self) -> str:
        """Build alternative search URL (city-only) with current city."""
        return (
            f"https://www.immoscoop.be/zoeken/te-koop/{self._current_city.lower()}/huis"
            f"?priceMin={MIN_PRICE}&priceMax={MAX_PRICE}"
        )

    def scrape(self) -> list[Listing]:
        """Scrape Immoscoop search results for all target postals."""
        all_listings = []

        for postal, city in zip(TARGET_POSTALS, TARGET_CITIES):
            self._current_postal = postal
            self._current_city = city
            logger.info(f"[{self.PLATFORM_NAME}] Scraping {city} ({postal})")

            found_any = False
            for page in range(1, self.MAX_PAGES + 1):
                page_listings = self._scrape_search_page(
                    self._page_url(self._search_url(), page)
                )
                if not page_listings and page == 1:
                    logger.info(
                        f"[{self.PLATFORM_NAME}] Trying alternative URL for {city}"
                    )
                    page_listings = self._scrape_search_page(
                        self._page_url(self._search_url_alt(), page)
                    )

                if not page_listings:
                    logger.info(
                        f"[{self.PLATFORM_NAME}] No more results on page {page} "
                        f"for {city}"
                    )
                    break

                all_listings.extend(page_listings)
                found_any = True
                logger.info(
                    f"[{self.PLATFORM_NAME}] {city} page {page}: "
                    f"{len(page_listings)} listings"
                )

            if not found_any:
                logger.info(
                    f"[{self.PLATFORM_NAME}] No results found for {city} ({postal})"
                )

        return all_listings

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
        city = self._current_city

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

            # Image — probeer meerdere selectoren en attributen
            image_urls: list[str] = []
            img_attrs = ("src", "data-src", "data-lazy", "data-original", "data-srcset", "srcset")

            # Eerst door alle img tags in de container
            all_imgs = container.find_all("img")
            for img_tag in all_imgs:
                for attr in img_attrs:
                    val = img_tag.get(attr, "")
                    if val and "placeholder" not in val.lower():
                        if " " in val:
                            val = val.split(" ")[0]
                        if val not in image_urls:
                            image_urls.append(val)
                        break
                if len(image_urls) >= 3:
                    break

            # Relative URL fix
            image_urls = [
                f"https:{u}" if u.startswith("//") else u
                for u in image_urls
            ]

            address = self._extract_address_from_text(text)
            if title_text in ("Huis te koop", "House", "Te koop") or any(
                x in title_text.lower()
                for x in ("huis te koop", "te koop", "house", "immoscoop only")
            ):
                pc_match = re.search(r'^(.+?)\s+(\d{4})\s+(.+)$', address.strip())
                if pc_match:
                    street_part = pc_match.group(1).strip()
                    city_part = pc_match.group(3).upper()
                    title_final = f"{street_part} — {city_part}"
                else:
                    title_final = f"Te koop: {address}"
            else:
                title_final = title_text
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
                    epc_label=self._extract_epc_from_text(
                        container.get_text(" ", strip=True)
                    ),
                )
            )
            seen_ids.add(listing_id)

        return listings

    def enrich_listing(self, listing: Listing) -> Listing:
        """Fetch detail page, extract fotos + EPC uit __NEXT_DATA__ JSON."""
        if listing.description and len(listing.description) >= 80 and listing.epc_label:
            return listing

        try:
            response = requests.get(listing.url, timeout=(5, 10), headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            soup = BeautifulSoup(response.text, "lxml")

            # Parse __NEXT_DATA__ — bevat ALLE data
            script = soup.find("script", id="__NEXT_DATA__")
            if script:
                data = json.loads(script.string)
                prop = data.get("props", {}).get("pageProps", {}).get("property", {})

                # Beschrijving
                if not listing.description or len(listing.description) < 80:
                    desc = prop.get("description", "")
                    if desc:
                        desc_clean = re.sub(r"<[^>]+>", " ", desc).strip()
                        desc_clean = re.sub(r"\s+", " ", desc_clean)
                        if len(desc_clean) >= 40:
                            listing.description = desc_clean

                # Fotos — uit prop.images[]
                images = prop.get("images", [])
                image_urls = []
                for img in images:
                    url = img.get("url", "")
                    if url and url.startswith("http"):
                        image_urls.append(url)
                    if len(image_urls) >= 8:
                        break
                if image_urls:
                    listing.image_urls = image_urls

                # EPC — uit features (id == "EpcClass") of propertyDetailGroups
                if not listing.epc_label:
                    for feature in prop.get("features", []):
                        if isinstance(feature, dict) and feature.get("id") == "EpcClass":
                            val = feature.get("value", "")
                            if val:
                                listing.epc_label = val.upper()
                                break

                if not listing.epc_label:
                    for group in prop.get("propertyDetailGroups", []):
                        if isinstance(group, dict) and "Energie" in group.get("group", ""):
                            for detail in group.get("propertyDetails", []):
                                if "EPC-label" in detail.get("title", ""):
                                    val = detail.get("description", "")
                                    if val:
                                        listing.epc_label = val.upper()
                                        break

                # Surface — uit Groep "Gebouw" > "Bewoonbare oppervlakte"
                if not listing.surface_m2:
                    for group in prop.get("propertyDetailGroups", []):
                        if isinstance(group, dict) and "Gebouw" in group.get("group", ""):
                            for detail in group.get("propertyDetails", []):
                                if "Bewoonbare oppervlakte" in detail.get("title", ""):
                                    val = self._safe_int(re.sub(r"[^0-9]", "", detail.get("description", "")))
                                    if val:
                                        listing.surface_m2 = val
                                        break

                # Lot surface — uit Groep "Terrein" > "Perceeloppervlakte"
                if not listing.lot_surface_m2:
                    for group in prop.get("propertyDetailGroups", []):
                        if isinstance(group, dict) and "Terrein" in group.get("group", ""):
                            for detail in group.get("propertyDetails", []):
                                if "Perceeloppervlakte" in detail.get("title", ""):
                                    val = self._safe_int(re.sub(r"[^0-9]", "", detail.get("description", "")))
                                    if val:
                                        listing.lot_surface_m2 = val
                                        break

                # Bedrooms — uit Groep "Indeling" > "Aantal slaapkamers"
                if not listing.bedrooms:
                    for group in prop.get("propertyDetailGroups", []):
                        if isinstance(group, dict) and "Indeling" in group.get("group", ""):
                            for detail in group.get("propertyDetails", []):
                                if "Aantal slaapkamers" in detail.get("title", ""):
                                    val = self._safe_int(re.sub(r"[^0-9]", "", detail.get("description", "")))
                                    if val:
                                        listing.bedrooms = val
                                        break

                # Prijs — uit prop.price
                if isinstance(prop.get("price"), dict):
                    price_label = prop["price"].get("label", "")
                    price_val = self._safe_int(re.sub(r"[^0-9]", "", price_label))
                    if price_val and MIN_PRICE <= price_val <= MAX_PRICE:
                        listing.price = price_val

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
                title=item.get(
                    "title",
                    item.get(
                        "name",
                        f"House in {self._current_city.capitalize()} — €{price}",
                    ),
                ),
                price=price,
                bedrooms=bedrooms,
                address=item.get(
                    "address",
                    item.get("location", self._current_city.capitalize()),
                ),
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
            address = addr_el.get_text(separator=' ', strip=True) if addr_el else self._current_city.capitalize()
            if not title or any(x in title.lower() for x in ("huis te koop", "te koop", "house", "immoscoop only")):
                pc_match = re.search(r'^(.+?)\s+(\d{4})\s+(.+)$', address.strip())
                if pc_match:
                    street_part = pc_match.group(1).strip()
                    city_part = pc_match.group(3).upper()
                    title = f"{street_part} — {city_part}"
                else:
                    title = f"Te koop: {address}"

            # Bedrooms
            bedrooms = self._extract_bedrooms(card)

            # Surface
            surface = self._extract_surface(card)

            # Image — probeer meerdere selectoren en attributen
            image_url = ""
            img_attrs = ("src", "data-src", "data-lazy", "data-original", "data-srcset", "srcset")

            # Eerst img[src] of img[data-src]
            img = card.select_one("img[src], img[data-src]")
            if img:
                for attr in img_attrs:
                    val = img.get(attr, "")
                    if val:
                        if " " in val:
                            val = val.split(" ")[0]  # srcset = "url 1x, url2 2x"
                        image_url = val
                        break

            if not image_url:
                # Fallback: alle img tags in de card
                all_imgs = card.find_all("img")
                for img_tag in all_imgs:
                    for attr in ("src", "data-src", "data-lazy", "data-original"):
                        val = img_tag.get(attr, "")
                        if val and "placeholder" not in val.lower():
                            image_url = val
                            break
                    if image_url:
                        break

            if not image_url:
                # Fallback: check figure/picture elementen
                fig = card.select_one("figure img, picture img")
                if fig:
                    for attr in img_attrs:
                        val = fig.get(attr, "")
                        if val:
                            if " " in val:
                                val = val.split(" ")[0]
                            image_url = val
                            break

            # EPC label — scan card text voor laatste letter A-F
            epc_label = None
            card_text = card.get_text(" ", strip=True)
            epc_match = re.search(r'\b([A-F])\s*$', card_text)
            if epc_match:
                epc_label = epc_match.group(1).upper()

            # Ook check in specifieke classes
            epc_el = card.select_one("[class*='epc'], [class*='energie'], [class*='label']")
            if epc_el:
                epc_text = epc_el.get_text(strip=True)
                epc_match2 = re.search(r'\b([A-F][+-]?)\b', epc_text)
                if epc_match2:
                    epc_label = epc_match2.group(1).upper()

            # Relative URL fix (bv. "//cdn.immoscoop.be/...")
            if image_url and image_url.startswith("//"):
                image_url = f"https:{image_url}"

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
                epc_label=epc_label,
                surface_m2=surface,
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to parse HTML card: {e}")
            return None

    # --- Helper methods ---

    def _extract_id_from_url(self, url: str) -> str:
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
        # Fallback: laatste numerieke segment, niet de huidige postcode
        for part in reversed(parts):
            if re.match(r'^\d{4,}$', part) and part != self._current_postal:
                return part
        # Laatste segment als fallback
        return parts[-1] if parts else ""

    def _extract_address(self, item: dict) -> str:
        """Extract address from JSON-LD item."""
        addr = item.get("address", {})
        if isinstance(addr, dict):
            parts = [
                addr.get("streetAddress", ""),
                addr.get("postalCode", ""),
                addr.get("addressLocality", self._current_city.capitalize()),
            ]
            return " ".join(p for p in parts if p).strip()
        if isinstance(addr, str):
            return addr
        return self._current_city.capitalize()

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
    def _extract_epc_from_text(text: str) -> str | None:
        """Extract EPC label from text, e.g. 'EPC A', 'energielabel B+'"""
        m = re.search(
            r"EPC[\s:-]*\s*(?:label\s*)?(?:waarde[\s:-]*)?([A-E][+-]?)\b"
            r"|energie(?:label|prestatie)[\s:-]*([A-E][+-]?)\b"
            r"|energielabel[\s:-]*([A-E][+-]?)\b"
            r"|EPC[-\s]*(?:score|waarde)[\s:-]*\d+[\s/]*kWh[^.]*?([A-E][+-]?)\b",
            text, re.I
        )
        if m:
            label = next((g for g in m.groups() if g), None)
            if label:
                return label.upper().replace("+", "+")
        return None

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

    def _find_result_container(self, link):
        """Find a reasonably scoped card container for a listing link."""
        node = link
        for _ in range(6):
            node = node.parent
            if node is None:
                break
            text = node.get_text(" ", strip=True)
            if "€" in text and self._current_city.lower() in text.lower() and len(text) < 900:
                return node
        return link.parent or link

    def _extract_address_from_text(self, text: str) -> str:
        """Extract an address from nearby card text."""
        postal = self._current_postal
        city = self._current_city
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if re.fullmatch(rf"{re.escape(postal)}\s+{re.escape(city)}", line, re.IGNORECASE):
                if index > 0:
                    return f"{lines[index - 1]}, {line}"
                return line

        match = re.search(
            rf"([A-ZÀ-ÿ0-9][^\n]+?)\s*({re.escape(postal)}\s+{re.escape(city)})",
            text,
            re.IGNORECASE,
        )
        if match:
            return f"{match.group(1).strip()}, {match.group(2).strip()}"

        return city.capitalize()


# Allow running standalone for testing
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    scraper = ImmoscoopScraper()
    results = scraper.safe_scrape()
    for r in results:
        print(f"  [{r.id}] {r.title} — €{r.price} — {r.address}")
    print(f"\nTotal: {len(results)} listings")
