"""Immoweb.be scraper for houses for-sale."""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Listing
from config import TARGET_POSTAL_CODE, TARGET_CITY, MIN_PRICE, MAX_PRICE, MIN_BEDROOMS

logger = logging.getLogger(__name__)


class ImmowebScraper(BaseScraper):
    """Scraper for Immoweb.be house for-sale listings."""

    PLATFORM_NAME = "immoweb"
    REQUEST_DELAY = 2.0

    # Search URL with filters for houses for-sale
    SEARCH_URL = (
        "https://www.immoweb.be/en/search/house/for-sale"
        "?countries=BE"
        f"&postalCodes=BE-{TARGET_POSTAL_CODE}"
        f"&minPrice={MIN_PRICE}"
        f"&maxPrice={MAX_PRICE}"
        f"&minBedroomCount={MIN_BEDROOMS}"
        "&orderBy=newest"
        "&page={{page}}"
    )

    MAX_PAGES = 3

    # Immoweb search-results API
    API_URL = (
        "https://www.immoweb.be/en/search-results/house/for-sale"
        "?countries=BE"
        f"&postalCodes=BE-{TARGET_POSTAL_CODE}"
        f"&minPrice={MIN_PRICE}"
        f"&maxPrice={MAX_PRICE}"
        f"&minBedroomCount={MIN_BEDROOMS}"
        "&orderBy=newest"
        "&page={{page}}"
    )

    def scrape(self) -> list[Listing]:
        """Scrape Immoweb search results across multiple pages."""
        listings = []

        for page in range(1, self.MAX_PAGES + 1):
            url = self.SEARCH_URL.format(page=page)
            page_listings = self._scrape_via_api(page)
            if not page_listings:
                page_listings = self._scrape_search_page(url)
            if not page_listings:
                logger.info(f"[{self.PLATFORM_NAME}] No more results on page {page}")
                break
            listings.extend(page_listings)
            logger.info(f"[{self.PLATFORM_NAME}] Page {page}: {len(page_listings)} listings")

        return listings

    def _scrape_via_api(self, page: int) -> list[Listing]:
        """Try Immoweb's search API which returns JSON."""
        api_url = self.API_URL.format(page=page)
        try:
            response = self._rate_limited_get(api_url, headers={
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            })
            data = response.json()
            results = data if isinstance(data, list) else data.get("results", [])
            listings = []
            for item in results:
                listing = self._parse_api_result(item)
                if listing:
                    listings.append(listing)
            if listings:
                logger.info(f"[{self.PLATFORM_NAME}] API returned {len(listings)} listings")
            return listings
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] API approach failed: {e}")
            return []

    def _parse_api_result(self, item: dict) -> Listing | None:
        """Parse a listing from Immoweb's API JSON response."""
        try:
            listing_id = str(item.get("id", ""))
            if not listing_id:
                return None

            # Skip if not a house
            prop = item.get("property", {})
            if isinstance(prop, dict):
                prop_type = prop.get("type", "") or item.get("type", "")
                if prop_type and prop_type.upper() != "HOUSE":
                    return None

            # Price
            price_data = item.get("price", {})
            price = 0
            if isinstance(price_data, dict):
                price = int(price_data.get("mainValue") or 0)
            elif isinstance(price_data, (int, float)):
                price = int(price_data)
            if price == 0:
                sale = item.get("transaction", {}).get("sale", {})
                if isinstance(sale, dict):
                    price = int(sale.get("price", 0) or 0)

            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None

            # Location — straat+nummer+postcode+stad
            prop = item.get("property", {})
            loc = prop.get("location", {}) if isinstance(prop, dict) else {}
            if isinstance(loc, dict):
                street = loc.get("street", "")
                number = loc.get("number", "")
                postal = loc.get("postalCode", TARGET_POSTAL_CODE)
                locality = loc.get("locality", TARGET_CITY.capitalize())
                if street and number:
                    address = f"{street} {number} {postal} {locality}"
                else:
                    address = f"{postal} {locality}"
            else:
                address = f"{TARGET_POSTAL_CODE} {TARGET_CITY.capitalize()}"

            # Bedrooms
            bedrooms = 0
            if isinstance(prop, dict):
                bedrooms = int(prop.get("bedroomCount", 0))
            if bedrooms < MIN_BEDROOMS:
                return None

            # Surface
            surface = None
            if isinstance(prop, dict) and prop.get("netHabitableSurface"):
                surface = int(prop["netHabitableSurface"])

            # Lot surface
            lot_surface = None
            if isinstance(prop, dict) and prop.get("land", {}).get("surface"):
                lot_surface = int(prop["land"]["surface"])

            # EPC
            epc = None
            if isinstance(prop, dict) and prop.get("certificates", {}).get("epcScore"):
                epc = prop["certificates"]["epcScore"]

            # Title: straat + nummer + stad (geen postcode)
            if street and number:
                display_title = f"{street} {number} — {locality}"
            elif locality:
                display_title = f"Te koop: {locality}"
            else:
                display_title = f"Te koop in {TARGET_POSTAL_CODE} {TARGET_CITY.capitalize()}"

            # Status detectie uit search API flags
            status = None
            flags = item.get("flags", {}) if isinstance(item.get("flags"), dict) else {}
            flag_main = flags.get("main", "")
            if flag_main == "under_option":
                status = "under_option"
            elif flag_main == "life_annuity":
                status = "life_annuity"

            # URL
            url = f"https://www.immoweb.be/en/classified/house/for-sale/{listing_id}"

            # Images
            images = []
            media = item.get("media", {})
            if isinstance(media, dict):
                pics = media.get("pictures", [])
                if isinstance(pics, list):
                    for pic in pics[:5]:
                        if isinstance(pic, dict):
                            img_url = pic.get("largeUrl") or pic.get("mediumUrl") or pic.get("smallUrl")
                            if img_url:
                                images.append(img_url)

            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                status=status,
                title=display_title,
                price=price,
                bedrooms=bedrooms,
                address=address,
                url=url,
                description="",
                image_urls=images,
                surface_m2=surface,
                lot_surface_m2=lot_surface,
                epc_label=epc,
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to parse API result: {e}")
            return None

    def _scrape_search_page(self, url: str) -> list[Listing]:
        """Parse a single search results page (HTML fallback)."""
        """Parse a single search results page (HTML fallback)."""
        response = self._get_with_fallback(url)
        if not response:
            logger.warning(f"[{self.PLATFORM_NAME}] Failed to fetch search page")
            return []

        soup = BeautifulSoup(response.text, "lxml")
        listings = []

        cards = soup.select("article.card--result")
        if not cards:
            cards = soup.select("article.card")
        if not cards:
            cards = soup.select("[id^='classified_']")

        logger.info(f"[{self.PLATFORM_NAME}] Found {len(cards)} card elements on page")

        for card in cards:
            listing = self._parse_html_card(card)
            if listing:
                listings.append(listing)

        if not listings:
            json_listings = self._extract_json_data(response.text)
            if json_listings:
                for item in json_listings:
                    listing = self._parse_json_listing(item)
                    if listing:
                        listings.append(listing)

        return listings

    def _parse_html_card(self, card: BeautifulSoup) -> Listing | None:
        """Parse a listing from Immoweb HTML card."""
        try:
            link = card.select_one("a.card__title-link")
            if not link:
                link = card.select_one("a[href*='/classified/']")
            if not link:
                return None

            href = link.get("href", "")
            if not href.startswith("http"):
                href = f"https://www.immoweb.be{href}"

            if link.has_attr("@click"):
                click_val = link.get("@click", "")
                idx = click_val.find("{")
                if idx >= 0:
                    try:
                        decoder = json.JSONDecoder()
                        data, _ = decoder.raw_decode(click_val[idx:])
                        return self._parse_click_data(data, href)
                    except json.JSONDecodeError:
                        pass

            article_id = card.get("id", "")
            listing_id = article_id.replace("classified_", "") if article_id.startswith("classified_") else ""

            if not listing_id:
                id_match = re.search(r'/(\d{6,})', href)
                listing_id = id_match.group(1) if id_match else ""
            if not listing_id:
                return None

            title = link.get_text(strip=True) or "House"

            # Als title enkel "House" is, probeer via address data
            # Address wordt later geparsed, dus na de location check
            # (we fixen titel onderaan met address fallback)

            # Price
            price = 0
            price_el = card.select_one(".card--result__price, .price__formatted, [class*='price']")
            if price_el:
                visible_span = price_el.select_one("span[aria-hidden='true']")
                price_text = visible_span.get_text(strip=True) if visible_span else price_el.get_text(strip=True)
                price = self._parse_price(price_text)
            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None

            # Location
            location_el = card.select_one("[class*='information--locality'], [class*='locality'], .card__location")
            address = location_el.get_text(separator=' ', strip=True) if location_el else f"{TARGET_POSTAL_CODE} {TARGET_CITY.capitalize()}"

            # Vervang "House" titel met adres als we niets beters hebben
            if title in ("House", ""):
                title = f"Te koop: {address}"

            # Bedrooms and surface
            info_el = card.select_one(".card__information--property, [class*='information--property']")
            info_text = info_el.get_text() if info_el else card.get_text()
            bedrooms = self._extract_bedrooms(info_text)
            surface = self._extract_surface_m2(info_text)
            if bedrooms < MIN_BEDROOMS:
                bedrooms = self._extract_bedrooms(card.get_text())
            if bedrooms < MIN_BEDROOMS:
                return None

            # Image
            img = card.select_one("img.card__media-picture, img[class*='card__media'], img[src*='immowebstatic']")
            image_url = ""
            if img:
                image_url = img.get("src") or img.get("data-src") or ""

            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=f"{title} — {address}",
                price=price,
                bedrooms=bedrooms,
                address=address,
                url=href,
                description="",
                image_urls=[image_url] if image_url else [],
                surface_m2=surface,
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to parse HTML card: {e}")
            return None

    def _parse_click_data(self, data: dict, href: str) -> Listing | None:
        """Parse listing from @click JSON data."""
        try:
            listing_id = str(data.get("id", ""))
            if not listing_id:
                return None

            price_data = data.get("price", {})
            if isinstance(price_data, dict):
                price = int(price_data.get("mainValue") or 0)
            else:
                price = 0
            if price == 0 and "transaction" in data:
                sale = data.get("transaction", {}).get("sale", {})
                if isinstance(sale, dict):
                    price = int(sale.get("price", 0) or 0)
            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None

            loc_data = data.get("property", {}).get("location", {})
            if isinstance(loc_data, dict):
                street = loc_data.get("street", "")
                number = loc_data.get("number", "")
                postal = loc_data.get("postalCode", TARGET_POSTAL_CODE)
                locality = loc_data.get("locality", TARGET_CITY.capitalize())
                if street and number:
                    address = f"{street} {number} {postal} {locality}"
                else:
                    address = f"{postal} {locality}"
            else:
                address = f"{TARGET_POSTAL_CODE} {TARGET_CITY.capitalize()}"

            prop_data = data.get("property", {})
            bedrooms = int(prop_data.get("bedroomCount", 0)) if isinstance(prop_data, dict) else 0
            if bedrooms < MIN_BEDROOMS:
                return None

            surface = None
            if isinstance(prop_data, dict) and prop_data.get("netHabitableSurface"):
                surface = int(prop_data["netHabitableSurface"])

            lot_surface = None
            if isinstance(prop_data, dict) and prop_data.get("land", {}).get("surface"):
                lot_surface = int(prop_data["land"]["surface"])

            epc = None
            if isinstance(prop_data, dict) and prop_data.get("certificates", {}).get("epcScore"):
                epc = prop_data["certificates"]["epcScore"]

            images = []
            pics = data.get("media", {}).get("pictures", [])
            if isinstance(pics, list):
                for pic in pics[:5]:
                    if isinstance(pic, dict):
                        img_url = pic.get("largeUrl") or pic.get("mediumUrl") or pic.get("smallUrl")
                        if img_url:
                            images.append(img_url)

            # Title: straat+nummer (geen postcode)
            if isinstance(loc_data, dict) and street and number:
                display_title = f"{street} {number} — {locality}"
            else:
                display_title = "House"

            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=display_title,
                price=price,
                bedrooms=bedrooms,
                address=address,
                url=href,
                description="",
                image_urls=images,
                surface_m2=surface,
                lot_surface_m2=lot_surface,
                epc_label=epc,
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to parse click data: {e}")
            return None

    def enrich_listing(self, listing: Listing) -> Listing:
        """Fetch the detail page to get full description and photos."""
        if listing.description and len(listing.image_urls) > 1:
            return listing

        try:
            response = self._rate_limited_get(listing.url)

            classified_match = re.search(
                r'window\.classified\s*=\s*(\{.*?\});\s*\n',
                response.text,
                re.DOTALL,
            )
            if classified_match:
                try:
                    data = json.loads(classified_match.group(1))
                    if not listing.description:
                        desc = data.get("property", {}).get("description", {})
                        if isinstance(desc, dict):
                            listing.description = desc.get("nl", "") or desc.get("fr", "") or ""
                        elif isinstance(desc, str):
                            listing.description = desc
                    if len(listing.image_urls) <= 1:
                        media = data.get("media", {}).get("pictures", [])
                        listing.image_urls = [
                            p.get("url", "") for p in media[:5] if isinstance(p, dict) and p.get("url")
                        ]
                    if not listing.epc_label:
                        listing.epc_label = self._extract_epc_from_json(data)
                    if not listing.surface_m2:
                        listing.surface_m2 = self._extract_surface_from_json(data)
                    if not listing.lot_surface_m2:
                        listing.lot_surface_m2 = self._extract_lot_surface_from_json(data)

                    # Status detectie
                    if not listing.status:
                        flags = data.get("flags", {}) if isinstance(data.get("flags"), dict) else {}
                        flag_main = flags.get("main", "")
                        if flag_main == "under_option":
                            listing.status = "under_option"
                        elif flag_main == "life_annuity":
                            listing.status = "life_annuity"

                        # Lijfrente check in beschrijving
                        desc = listing.description or ""
                        if "lijfrente" in desc.lower() or "levenslang" in desc.lower():
                            listing.status = "life_annuity"

                    # Titel met straat+nummer (detail page heeft betere data)
                    prop = data.get("property", {})
                    if isinstance(prop, dict):
                        loc = prop.get("location", {})
                        if isinstance(loc, dict):
                            street = loc.get("street", "")
                            number = loc.get("number", "")
                            locality = loc.get("locality", "")
                            if street and number:
                                listing.title = f"{street} {number} — {locality}"

                    return listing
                except json.JSONDecodeError:
                    pass

            # Fallback HTML parse
            soup = BeautifulSoup(response.text, "lxml")
            if not listing.description:
                desc_el = soup.select_one("[class*='classified__description'], .description")
                if desc_el:
                    listing.description = desc_el.get_text(strip=True)
            if len(listing.image_urls) <= 1:
                images = soup.select("[class*='classified__gallery'] img, .gallery img")
                listing.image_urls = [
                    img.get("src") or img.get("data-src") or ""
                    for img in images[:5]
                    if img.get("src") or img.get("data-src")
                ]
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to enrich listing {listing.id}: {e}")

        return listing

    def _extract_json_data(self, html: str) -> list[dict] | None:
        """Extract listing data from embedded JSON."""
        soup = BeautifulSoup(html, "lxml")
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") in ["ItemList", "SearchResultsPage"]:
                    return data.get("itemListElement", [])
            except (json.JSONDecodeError, TypeError):
                continue
        return None

    def _parse_json_listing(self, item: dict) -> Listing | None:
        """Parse a listing from JSON-LD."""
        try:
            actual = item.get("item", item) if isinstance(item, dict) else item
            listing_id = str(actual.get("id", ""))
            url = actual.get("url", "")
            if not listing_id and url:
                id_match = re.search(r'/(\d{6,})', url)
                listing_id = id_match.group(1) if id_match else ""
            if not listing_id:
                return None
            offers = actual.get("offers", {})
            price = int(offers.get("price", 0)) if isinstance(offers, dict) else 0
            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None

            # Straat + nummer + stad uit JSON
            addr_obj = actual.get("address", {})
            if isinstance(addr_obj, dict):
                street_addr = addr_obj.get("streetAddress", "")
                postal = addr_obj.get("postalCode", TARGET_POSTAL_CODE)
                locality = addr_obj.get("addressLocality", TARGET_CITY.capitalize())
                if street_addr:
                    address = f"{street_addr} {postal} {locality}"
                else:
                    address = f"{postal} {locality}"
            else:
                address = f"{TARGET_POSTAL_CODE} {TARGET_CITY.capitalize()}"

            # Title: straat+nummer (geen postcode)
            title = actual.get("name", "") or ""
            if isinstance(addr_obj, dict):
                street_addr = addr_obj.get("streetAddress", "")
                locality = addr_obj.get("addressLocality", TARGET_CITY.capitalize())
            if street_addr:
                display_title = f"{street_addr} — {locality}"
            else:
                display_title = title or f"House — €{price}"

            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=display_title,
                price=price,
                bedrooms=MIN_BEDROOMS,
                address=address,
                url=url if url.startswith("http") else f"https://www.immoweb.be{url}",
                description=actual.get("description", ""),
                image_urls=[actual["image"]] if actual.get("image") else [],
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to parse JSON listing: {e}")
            return None

    @staticmethod
    def _parse_price(text: str) -> int:
        """Extract price from text like '€450.000' or '€ 550 000'."""
        clean = text.replace("€", "").replace("\u20ac", "")
        match = re.search(r'([\d\s.,]+)', clean)
        if match:
            num_str = match.group(1).strip()
            num_str = num_str.replace(" ", "").replace(".", "").replace(",", "")
            try:
                price = int(num_str)
                if 50000 <= price <= 2000000:
                    return price
            except ValueError:
                pass
        return 0

    @staticmethod
    def _extract_bedrooms(text: str) -> int:
        """Extract bedroom count."""
        patterns = [r'(\d+)\s*bdr', r'(\d+)\s*bed', r'(\d+)\s*slaapkamer', r'(\d+)\s*chambre']
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return 0

    @staticmethod
    def _extract_surface_m2(text: str) -> int | None:
        """Extract surface area."""
        match = re.search(r'(\d+)\s*m²', text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _extract_epc_from_json(data: dict) -> str | None:
        """Extract EPC label from detail page JSON."""
        try:
            for path in [
                lambda: data.get("transaction", {}).get("certificates", {}).get("epcScore"),
                lambda: data.get("property", {}).get("certificates", {}).get("epcScore"),
                lambda: data.get("epcLabel"),
            ]:
                val = path()
                if val:
                    return str(val)
        except (TypeError, AttributeError):
            pass
        return None

    @staticmethod
    def _extract_surface_from_json(data: dict) -> int | None:
        """Extract surface area from detail page JSON."""
        try:
            for path in [
                lambda: data.get("property", {}).get("netHabitableSurface"),
                lambda: data.get("property", {}).get("land", {}).get("surface"),
                lambda: data.get("netHabitableSurface"),
            ]:
                val = path()
                if val:
                    return int(val)
        except (TypeError, ValueError, AttributeError):
            pass
        return None

    @staticmethod
    def _extract_lot_surface_from_json(data: dict) -> int | None:
        """Extract lot/land surface from detail page JSON."""
        try:
            val = data.get("property", {}).get("land", {}).get("surface")
            if val:
                return int(val)
        except (TypeError, ValueError, AttributeError):
            pass
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    scraper = ImmowebScraper()
    results = scraper.safe_scrape()
    for r in results:
        print(f"  [{r.id}] {r.title} — €{r.price} — {r.bedrooms}bd — {r.surface_m2}m² — EPC {r.epc_label}")
    print(f"\nTotal: {len(results)} listings")
