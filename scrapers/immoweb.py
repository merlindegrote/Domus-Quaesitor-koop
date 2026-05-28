"""Immoweb.be scraper for rental apartments in Ghent.

Immoweb uses server-side rendering. Listing cards are in the HTML as:
  - <li class="search-results__item"> containing <article class="card card--result">
  - Title/URL: <a class="card__title-link"> with href to /en/classified/apartment/for-rent/gent/9000/ID
  - Price: <span aria-hidden="true" class="resizable-text ..."> inside .card--result__price
  - Bedrooms: <p class="card__information--property"> text like "1 bdr. · 60 m²"
  - Location: <p class="card--results__information--locality"> text like "9000 Gent"
  - Image: <img class="card__media-picture"> with src URL
  - Listing ID: <article id="classified_XXXXXXXX">
"""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Listing
from config import TARGET_POSTAL_CODE, TARGET_CITY, MIN_PRICE, MAX_PRICE, MIN_BEDROOMS

logger = logging.getLogger(__name__)


class ImmowebScraper(BaseScraper):
    """Scraper for Immoweb.be rental listings."""

    PLATFORM_NAME = "immoweb"
    REQUEST_DELAY = 2.0

    # Search URL with filters for rentals
    SEARCH_URL = (
        "https://www.immoweb.be/en/search/apartment/for-rent"
        "?countries=BE"
        f"&postalCodes=BE-{TARGET_POSTAL_CODE}"
        f"&minPrice={MIN_PRICE}"
        f"&maxPrice={MAX_PRICE}"
        f"&minBedroomCount={MIN_BEDROOMS}"
        "&orderBy=newest"
        "&page={{page}}"
    )

    MAX_PAGES = 3

    # Immoweb search-results API — the frontend fetches JSON from this endpoint
    API_URL = (
        "https://www.immoweb.be/en/search-results/apartment/for-rent"
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

            # Try JSON API first (XHR-style request), then fall back to HTML
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
        """Try Immoweb's search API which returns JSON (less likely to be blocked)."""
        api_url = self.API_URL.format(page=page)
        try:
            # Mimic an XHR request from the Immoweb frontend
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

            # Price
            price_data = item.get("price", {})
            price = 0
            if isinstance(price_data, dict):
                price = int(price_data.get("mainValue") or price_data.get("monthlyRentalPrice") or 0)
            elif isinstance(price_data, (int, float)):
                price = int(price_data)

            if price == 0:
                transaction = item.get("transaction", {})
                if isinstance(transaction, dict):
                    rental = transaction.get("rental", {})
                    if isinstance(rental, dict):
                        price = int(rental.get("monthlyRentalPrice", 0))

            if not (MIN_PRICE <= price <= MAX_PRICE):
                return None

            # Location
            prop = item.get("property", {})
            loc = prop.get("location", {}) if isinstance(prop, dict) else {}
            address = f"{TARGET_POSTAL_CODE} {TARGET_CITY.capitalize()}"
            if isinstance(loc, dict):
                postal = loc.get("postalCode", TARGET_POSTAL_CODE)
                locality = loc.get("locality", TARGET_CITY.capitalize())
                address = f"{postal} {locality}"

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

            # Title
            title = item.get("title", "") or f"Apartment in {address}"

            # URL
            url = f"https://www.immoweb.be/en/classified/apartment/for-rent/{listing_id}"
            if item.get("property", {}).get("location", {}).get("locality"):
                loc_name = loc["locality"].lower().replace(" ", "-")
                postal = loc.get("postalCode", TARGET_POSTAL_CODE)
                url = f"https://www.immoweb.be/en/classified/apartment/for-rent/{loc_name}/{postal}/{listing_id}"

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
                title=f"{title} — {address}",
                price=price,
                bedrooms=bedrooms,
                address=address,
                url=url,
                description="",
                image_urls=images,
                surface_m2=surface,
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to parse API result: {e}")
            return None

    def _scrape_search_page(self, url: str) -> list[Listing]:
        """Parse a single search results page (HTML fallback)."""
        response = self._get_with_fallback(url)
        if not response:
            logger.warning(f"[{self.PLATFORM_NAME}] Failed to fetch search page")
            return []

        soup = BeautifulSoup(response.text, "lxml")
        listings = []

        # Primary: parse SSR HTML cards — this is the confirmed working structure
        # Immoweb renders article.card.card--result inside li.search-results__item
        cards = soup.select("article.card--result")
        if not cards:
            # Fallback selectors
            cards = soup.select("article.card")
        if not cards:
            cards = soup.select("[id^='classified_']")

        logger.info(f"[{self.PLATFORM_NAME}] Found {len(cards)} card elements on page")

        for card in cards:
            listing = self._parse_html_card(card)
            if listing:
                listings.append(listing)

        # If no HTML cards found, try JSON extraction as last resort
        if not listings:
            json_listings = self._extract_json_data(response.text)
            if json_listings:
                for item in json_listings:
                    listing = self._parse_json_listing(item)
                    if listing:
                        listings.append(listing)

        return listings

    def _parse_html_card(self, card: BeautifulSoup) -> Listing | None:
        """Parse a listing from a confirmed Immoweb HTML card structure."""
        try:
            # Check for JSON data in @click first! This is the most reliable method
            # for SSR Immoweb pages.
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
                        
                        listing_id = str(data.get("id", ""))
                        
                        # Price
                        price_data = data.get("price", {})
                        if isinstance(price_data, dict):
                            price = int(price_data.get("mainValue") or price_data.get("monthlyRentalPrice") or 0)
                        else:
                            price = 0
                            
                        if price == 0 and "transaction" in data:
                            rental = data.get("transaction", {}).get("rental", {})
                            if isinstance(rental, dict):
                                price = int(rental.get("monthlyRentalPrice", 0))
                        
                        if not (MIN_PRICE <= price <= MAX_PRICE):
                            logger.debug(f"[{self.PLATFORM_NAME}] Skipping {listing_id}: price €{price} out of range")
                            return None
                            
                        # Location
                        loc_data = data.get("property", {}).get("location", {})
                        if isinstance(loc_data, dict):
                            address = f"{loc_data.get('postalCode', TARGET_POSTAL_CODE)} {loc_data.get('locality', TARGET_CITY.capitalize())}"
                        else:
                            address = f"{TARGET_POSTAL_CODE} {TARGET_CITY.capitalize()}"
                        
                        # Bedrooms
                        bedrooms = 0
                        prop_data = data.get("property", {})
                        if isinstance(prop_data, dict):
                            bedrooms = int(prop_data.get("bedroomCount", 0))
                        if bedrooms < MIN_BEDROOMS:
                            logger.debug(f"[{self.PLATFORM_NAME}] Skipping {listing_id}: no bedrooms found")
                            return None
                            
                        # Surface
                        surface = None
                        if isinstance(prop_data, dict):
                            surface = prop_data.get("netHabitableSurface")
                            if surface is not None:
                                surface = int(surface)
                        
                        # Images
                        images = []
                        pics = data.get("media", {}).get("pictures", [])
                        if isinstance(pics, list):
                            for pic in pics[:5]:
                                if isinstance(pic, dict):
                                    img_url = pic.get("largeUrl") or pic.get("mediumUrl") or pic.get("smallUrl")
                                    if img_url:
                                        images.append(img_url)
                                        
                        title = link.get_text(strip=True) or "Apartment"
                        
                        return Listing(
                            id=listing_id,
                            platform=self.PLATFORM_NAME,
                            title=f"{title} — {address}",
                            price=price,
                            bedrooms=bedrooms,
                            address=address,
                            url=href,
                            description="",
                            image_urls=images,
                            surface_m2=surface,
                        )
                    except json.JSONDecodeError:
                        pass
            
            # Extract listing ID from article id attribute: "classified_21470905"
            article_id = card.get("id", "")
            listing_id = ""
            if article_id.startswith("classified_"):
                listing_id = article_id.replace("classified_", "")

            # Extract ID from URL if not found in article id
            if not listing_id:
                id_match = re.search(r'/(\d{6,})', href)
                listing_id = id_match.group(1) if id_match else ""
            if not listing_id:
                return None

            # Title
            title = link.get_text(strip=True) or "Apartment"

            # Price — look for the aria-hidden span inside price element
            price = 0
            price_el = card.select_one(".card--result__price, .price__formatted, [class*='price']")
            if price_el:
                # Get the visible text (aria-hidden="true" span)
                visible_span = price_el.select_one("span[aria-hidden='true']")
                if visible_span:
                    price_text = visible_span.get_text(strip=True)
                else:
                    price_text = price_el.get_text(strip=True)
                price = self._parse_price(price_text)

            if not (MIN_PRICE <= price <= MAX_PRICE):
                logger.debug(f"[{self.PLATFORM_NAME}] Skipping {listing_id}: price €{price} out of range")
                return None

            # Location
            location_el = card.select_one(
                "[class*='information--locality'], "
                "[class*='locality'], "
                ".card__location"
            )
            address = location_el.get_text(strip=True) if location_el else f"{TARGET_POSTAL_CODE} {TARGET_CITY.capitalize()}"

            # Bedrooms and surface from the information element
            # Text looks like: "1 bdr. · 60 m²" or "2 bdr. · 95 m²"
            info_el = card.select_one(
                ".card__information--property, "
                "[class*='information--property']"
            )
            info_text = info_el.get_text() if info_el else card.get_text()

            bedrooms = self._extract_bedrooms(info_text)
            surface = self._extract_surface_m2(info_text)

            if bedrooms < MIN_BEDROOMS:
                # Try from full card text as fallback
                bedrooms = self._extract_bedrooms(card.get_text())
            if bedrooms < MIN_BEDROOMS:
                logger.debug(f"[{self.PLATFORM_NAME}] Skipping {listing_id}: no bedrooms found")
                return None

            # Image
            img = card.select_one("img.card__media-picture, img[class*='card__media']")
            if not img:
                img = card.select_one("img[src*='immowebstatic'], img[src]")
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
                description="",  # Will be enriched from detail page
                image_urls=[image_url] if image_url else [],
                surface_m2=surface,
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to parse HTML card: {e}")
            return None

    def enrich_listing(self, listing: Listing) -> Listing:
        """Fetch the detail page to get full description and photos."""
        if listing.description and len(listing.image_urls) > 1:
            return listing

        try:
            response = self._rate_limited_get(listing.url)

            # Try to extract from window.classified JSON (detail pages have this)
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
                    return listing
                except json.JSONDecodeError:
                    pass

            # Fallback: parse HTML of detail page
            soup = BeautifulSoup(response.text, "lxml")

            if not listing.description:
                desc_el = soup.select_one(
                    "[class*='classified__description'], "
                    ".description, "
                    "#classified-description-content"
                )
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

    # --- JSON extraction (fallback) ---

    def _extract_json_data(self, html: str) -> list[dict] | None:
        """Try to extract listing data from embedded JSON."""
        # JSON-LD
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
        """Parse a listing from JSON-LD or embedded data."""
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

            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=actual.get("name", f"Apartment — €{price}/mo"),
                price=price,
                bedrooms=MIN_BEDROOMS,
                address=f"{TARGET_POSTAL_CODE} {TARGET_CITY.capitalize()}",
                url=url if url.startswith("http") else f"https://www.immoweb.be{url}",
                description=actual.get("description", ""),
                image_urls=[actual["image"]] if actual.get("image") else [],
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] Failed to parse JSON listing: {e}")
            return None

    # --- Helper methods ---

    @staticmethod
    def _parse_price(text: str) -> int:
        """Extract the main rent price from text like '€850 (+ €40)' or '€980/month'.

        We want the FIRST number which is the base rent, ignoring charges.
        """
        # Remove euro sign and common separators
        clean = text.replace("€", "").replace("\u20ac", "")
        # Find the first number (the base rent)
        match = re.search(r'(\d[\d\s.,]*\d|\d+)', clean)
        if match:
            num_str = match.group(1).replace(" ", "").replace(".", "").replace(",", "")
            try:
                price = int(num_str)
                if 100 <= price <= 10000:
                    return price
            except ValueError:
                pass
        return 0

    @staticmethod
    def _extract_bedrooms(text: str) -> int:
        """Extract bedroom count from text like '1 bdr.' or '2 bedrooms'."""
        patterns = [
            r'(\d+)\s*bdr',
            r'(\d+)\s*bed',
            r'(\d+)\s*slaapkamer',
            r'(\d+)\s*chambre',
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return int(match.group(1))
        return 0

    @staticmethod
    def _extract_surface_m2(text: str) -> int | None:
        """Extract surface area from text like '60 m²'."""
        match = re.search(r'(\d+)\s*m²', text)
        if match:
            return int(match.group(1))
        return None

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


# Allow running standalone for testing
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    scraper = ImmowebScraper()
    results = scraper.safe_scrape()
    for r in results:
        print(f"  [{r.id}] {r.title} — €{r.price} — {r.bedrooms}bd — {r.surface_m2}m² — {r.address}")
        if r.image_urls:
            print(f"    📷 {r.image_urls[0][:80]}...")
    print(f"\nTotal: {len(results)} listings")
