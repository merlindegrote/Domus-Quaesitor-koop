"""Zimmo.be scraper for houses for-sale.

Iterates over ALL postcodes/cities from TARGET_POSTALS/TARGET_CITIES.
"""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Listing
from config import TARGET_POSTALS, TARGET_CITIES, MIN_PRICE, MAX_PRICE, MIN_BEDROOMS

logger = logging.getLogger(__name__)

# Fallback when instance vars aren't set (shouldn't happen in normal flow)
_FALLBACK_POSTAL = "2520"
_FALLBACK_CITY = "Ranst"


class ZimmoScraper(BaseScraper):
    """Scraper for Zimmo.be house for-sale listings."""

    PLATFORM_NAME = "zimmo"
    REQUEST_DELAY = 2.0

    MAX_PAGES = 1

    def __init__(self):
        super().__init__()
        self._current_postal = _FALLBACK_POSTAL
        self._current_city = _FALLBACK_CITY

    def _search_url(self, page: int) -> str:
        """Build search URL for current city/postal."""
        base = (
            f"https://www.zimmo.be/nl/{self._current_city.lower()}-{self._current_postal}/"
            f"te-koop/huis/"
            f"?priceMin={MIN_PRICE}&priceMax={MAX_PRICE}&roomsMin={MIN_BEDROOMS}"
        )
        if page <= 1:
            return base
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}page={page}"

    def scrape(self) -> list[Listing]:
        """Scrape Zimmo search results across all postcodes and pages."""
        all_listings = []
        for postal_code, city in zip(TARGET_POSTALS, TARGET_CITIES):
            self._current_postal = postal_code
            self._current_city = city
            city_listings = self._scrape_city()
            logger.info(f"[{self.PLATFORM_NAME}] {city} ({postal_code}): {len(city_listings)} listings")
            all_listings.extend(city_listings)
        return all_listings

    def _scrape_city(self) -> list[Listing]:
        """Scrape one city across multiple pages."""
        listings = []
        for page in range(1, self.MAX_PAGES + 1):
            url = self._search_url(page)
            page_listings = self._scrape_search_page(url)
            if not page_listings:
                logger.info(f"[{self.PLATFORM_NAME}] No more results on page {page}")
                break
            listings.extend(page_listings)
            logger.info(f"[{self.PLATFORM_NAME}] Page {page}: {len(page_listings)} listings")
        return listings

    def _scrape_search_page(self, url: str) -> list[Listing]:
        """Parse a single Zimmo search results page.
        Priority: 1) embedded JSON from app.start block  2) JSON-LD  3) HTML parsing
        """
        response = self._get_with_fallback(url)
        if not response:
            return []

        # Embedded JSON is fastest — try first
        json_listings = self._extract_embedded_json(response.text)
        if json_listings:
            return json_listings

        soup = BeautifulSoup(response.text, "lxml")

        json_listings = self._extract_jsonld(soup)
        if json_listings:
            return json_listings

        # HTML fallback — only for pages with fewer than 300KB
        if len(response.text) < 300_000:
            cards = soup.select(
                ".property-item, [class*='property-card'], "
                "[class*='search-result'], article[class*='result'], .card-property"
            )
            if cards:
                listings = []
                for card in cards:
                    listing = self._parse_html_card(card, soup)
                    if listing:
                        listings.append(listing)
                if listings:
                    return listings
            return self._parse_anchor_blocks(soup)
        
        return []

    def _page_url(self, page: int) -> str:
        """Legacy wrapper — delegates to _search_url."""
        return self._search_url(page)

    def _parse_anchor_blocks(self, soup: BeautifulSoup) -> list[Listing]:
        listings: list[Listing] = []
        seen_ids: set[str] = set()
        postal = self._current_postal
        city = self._current_city

        for link in soup.find_all("a", href=True):
            title_text = link.get_text(" ", strip=True)
            if "Huis te koop" not in title_text:
                continue
            # Skip appartments
            if "appartement" in title_text.lower():
                continue
            href = link.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = f"https://www.zimmo.be{href}"
            # Strip query params — only keep clean listing detail URL
            clean_url = href.split("?")[0].rstrip("/") + "/"

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
                url=clean_url,
                description="",
                image_urls=image_urls,
                surface_m2=surface,
            ))
            seen_ids.add(listing_id)

        return listings

    def enrich_listing(self, listing: Listing) -> Listing:
        """Enrich listing with detail-page data: description, EPC, surface, lot, bedrooms, images."""
        # Early return if already fully enriched
        if (
            listing.description
            and len(listing.description) >= 80
            and len(listing.image_urls) > 1
            and listing.epc_label
            and listing.surface_m2
            and listing.bedrooms
        ):
            return listing
        try:
            response = self._rate_limited_get(listing.url, timeout=15)
            soup = BeautifulSoup(response.text, "lxml")

            # --- 1. Beschrijving ---
            if not listing.description or len(listing.description) < 80:
                # Prefer long description blocks over tab labels
                for sel in ['.section-description', '.description-block',
                            '[class*="description"]:not(.tabmenu-description)']:
                    desc_el = soup.select_one(sel)
                    if desc_el:
                        text = desc_el.get_text(strip=True)
                        if len(text) >= 40:
                            listing.description = text
                            break

            # --- 2. Woonoppervlakte ---
            if not listing.surface_m2:
                for text_node in soup.find_all(string=re.compile(r'Woonopp\.?\s*')):
                    parent = text_node.parent
                    row = parent.parent if parent else None
                    if row:
                        m = re.search(r'(\d+)\s*m', row.get_text(strip=True))
                        if m:
                            listing.surface_m2 = int(m.group(1))
                            break

            # --- 3. Perceeloppervlakte ---
            if not listing.lot_surface_m2:
                for text_node in soup.find_all(string=re.compile(r'Grondopp\.?\s*')):
                    parent = text_node.parent
                    row = parent.parent if parent else None
                    if row:
                        m = re.search(r'(\d+)\s*m', row.get_text(strip=True))
                        if m:
                            listing.lot_surface_m2 = int(m.group(1))
                            break

            # --- 4. EPC-waarde ---
            if not listing.epc_label:
                # EPC letter zit in image filename: /public/images/energielabels/epc_a.png
                epc_img = soup.select_one(".energie-label img, img.energie-label, [class*='energie-label'] img")
                if not epc_img:
                    epc_img = soup.select_one("img[src*='energielabels']")
                if epc_img:
                    src = epc_img.get("src") or ""
                    epc_match = re.search(r'epc_([a-f][+-]?)\.', src, re.IGNORECASE)
                    if epc_match:
                        listing.epc_label = epc_match.group(1).upper()
                if not listing.epc_label:
                    # Fallback: text-based search
                    for text_node in soup.find_all(string=re.compile(r'EPC-waarde')):
                        parent = text_node.parent
                        row = parent.parent if parent else None
                        if row:
                            txt = row.get_text(strip=True)
                            epc_match = re.search(r'([A-F][+-]?)', txt)
                            if epc_match:
                                listing.epc_label = epc_match.group(1).upper()
                                break

            # --- 5. Slaapkamers ---
            if not listing.bedrooms:
                for text_node in soup.find_all(string=re.compile(r'Slaapkamers?\s*$')):
                    parent = text_node.parent
                    row = parent.parent if parent else None
                    if row:
                        txt = row.get_text(strip=True)
                        m = re.search(r'(\d+)', txt)
                        if m:
                            listing.bedrooms = int(m.group(1))
                            break
                if not listing.bedrooms:
                    for el in soup.find_all(string=re.compile(r'Aantal slaapkamers')):
                        p = el.parent.parent if el.parent else None
                        if p:
                            m = re.search(r'(\d+)', p.get_text(strip=True))
                            if m:
                                listing.bedrooms = int(m.group(1))
                                break

            # --- 6. Extra afbeeldingen ---
            if len(listing.image_urls) <= 1:
                gallery_imgs = soup.select(
                    "[class*='gallery'] img, [class*='photo'] img, "
                    "[class*='slider'] img[src*='files.zimmo'], "
                    "[class*='slider'] img[data-src*='files.zimmo']"
                )
                new_urls = []
                seen = set(listing.image_urls)
                for img in gallery_imgs:
                    src = img.get("src") or img.get("data-src") or ""
                    if src and "files.zimmo" in src and src not in seen:
                        new_urls.append(src)
                        seen.add(src)
                if new_urls:
                    listing.image_urls = (listing.image_urls or []) + new_urls[:5]

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
        """Extract properties array from app.start({properties: [...]}) JS block."""
        # Find app.start block via brace counting
        start = html.find('app.start({')
        if start < 0:
            return []
            
        depth = 0
        in_str = False
        escaped = False
        for i in range(start, len(html)):
            c = html[i]
            if escaped:
                escaped = False
                continue
            if c == '\\' and in_str:
                escaped = True
                continue
            if c == '"' and not escaped:
                in_str = not in_str
                continue
            if not in_str:
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        block = html[start:i+1]
                        break
        else:
            return []
        
        # Find properties: [...] in the block - match the array by tracking brackets
        props_start = block.find('properties:')
        if props_start < 0:
            return []
            
        arr_start = block.find('[', props_start)
        if arr_start < 0:
            return []
            
        # Count brackets to find matching close
        arr_depth = 0
        in_str = False
        escaped = False
        for i in range(arr_start, len(block)):
            c = block[i]
            if escaped:
                escaped = False
                continue
            if c == '\\' and in_str:
                escaped = True
                continue
            if c == '"' and not escaped:
                in_str = not in_str
                continue
            if not in_str:
                if c == '[':
                    arr_depth += 1
                elif c == ']':
                    arr_depth -= 1
                    if arr_depth == 0:
                        raw_array = block[arr_start:i+1]
                        break
        else:
            return []
        
        # Clean JS -> JSON: fix backslash escaping
        clean = raw_array.replace('\\\\/', '/')
        clean = clean.replace('\\\\"', '"')
        clean = clean.replace("\\'", "'")
        
        # Remove trailing commas
        clean = re.sub(r',\s*]', ']', clean)
        
        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            # Try more aggressive: convert unquoted JS keys
            # Replace unquoted word keys with quoted ones
            try:
                clean2 = re.sub(r'(?<=[{,])\s*(\w+):', r'"\1":', clean)
                data = json.loads(clean2)
            except json.JSONDecodeError:
                return []
        
        results = [self._parse_json_item(item) for item in data]
        return [r for r in results if r]

    def _parse_jsonld_item(self, item: dict) -> Listing | None:
        city = self._current_city
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
            addr_obj = actual.get("address", {})
            if isinstance(addr_obj, dict):
                address = addr_obj.get("streetAddress", city.capitalize())
            else:
                address = city.capitalize()
            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=f"Te koop: {address}",
                price=price,
                bedrooms=MIN_BEDROOMS,
                address=address,
                url=url if url.startswith("http") else f"https://www.zimmo.be{url}",
                description=actual.get("description", ""),
                image_urls=[image] if image else [],
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] parse jsonld failed: {e}")
            return None

    def _parse_json_item(self, item: dict) -> Listing | None:
        """Parse flat property object from Zimmo's app.start properties array.
        Field names: code, type, prijs, address, gemeente, postcode, slaapkamers,
        b_woonopp, hoofdFoto, nieuwbouw, status
        """
        try:
            # Skip non-house types
            if item.get("type", "").lower() not in ("huis", "house", ""):
                return None
            
            # Skip if status indicates rental
            status = item.get("status", "")
            if "huur" in status.lower() or "rent" in status.lower():
                return None

            listing_id = str(item.get("code", item.get("id", "")))
            if not listing_id:
                return None

            # Price: string, number, or nested {vraagPrijs: X}
            rp = item.get("prijs", 0); raw_price = rp.get("vraagPrijs", 0) if isinstance(rp, dict) else rp
            if isinstance(raw_price, str):
                raw_price = raw_price.replace(".", "").replace(",", "")
            try:
                price = int(float(raw_price))
            except (ValueError, TypeError):
                price = 0
            if price == 0 or price < MIN_PRICE or price > MAX_PRICE:
                return None

            # Bedrooms
            raw_beds = item.get("slaapkamers", MIN_BEDROOMS)
            if isinstance(raw_beds, str):
                try:
                    bedrooms = int(raw_beds)
                except ValueError:
                    bedrooms = MIN_BEDROOMS
            else:
                bedrooms = int(raw_beds) if raw_beds else MIN_BEDROOMS
            if bedrooms < MIN_BEDROOMS:
                bedrooms = MIN_BEDROOMS

            # Address - handle nested adres object
            ao = item.get("adres") or {}
            street = item.get("address", "") or item.get("straat", "") or ao.get("straat", "")
            number = item.get("huisnummer", "") or ao.get("huisnummer", "") or ""
            city = item.get("gemeente", "") or ao.get("gemeente", "") or self._current_city
            postal = item.get("postcode", "") or ao.get("postcode", "") or self._current_postal
            
            if street and number:
                full_address = f"{street} {number}, {postal} {city}"
            elif street:
                full_address = f"{street}, {postal} {city}"
            else:
                full_address = f"{postal} {city}"

            # URL
            url = f"https://www.zimmo.be/nl/{city.lower()}-{postal}/huis/{listing_id}"

            # Images — firstImages has 5 photos, hoofdFoto has 1
            images = item.get("firstImages", []) or []
            if not images:
                m = item.get("hoofdFoto", "") or item.get("image", "")
                main_photo = m.get("url", "") if isinstance(m, dict) else m
                if main_photo:
                    images.append(main_photo)

            # Surface
            raw_surface = item.get("b_woonopp", item.get("woonopp", ""))
            surface = None
            if raw_surface:
                try:
                    surface = int(float(raw_surface))
                except (ValueError, TypeError):
                    pass

            # EPC label — zit in search JSON, niet enkel op detailpagina
            raw_epc = item.get("energyLabel", "") or item.get("epc", "")
            epc_label = raw_epc.upper() if raw_epc else None

            return Listing(
                id=listing_id,
                platform=self.PLATFORM_NAME,
                title=f"Te koop: {street} {number} — {city}".strip(),
                price=price,
                bedrooms=bedrooms,
                epc_label=epc_label,
                address=full_address,
                url=url,
                description="",
                image_urls=images,
                surface_m2=surface,
            )
        except Exception as e:
            logger.debug(f"[{self.PLATFORM_NAME}] parse json failed: {e}")
            return None

    def _parse_html_card(self, card: BeautifulSoup, page_soup: BeautifulSoup) -> Listing | None:
        city = self._current_city
        try:
            # Skip appartments
            card_text = card.get_text(" ", strip=True)
            if "appartement" in card_text.lower():
                return None

            title_el = card.select_one("h2, h3, [class*='title'], .property-title")
            title = title_el.get_text(separator=' ', strip=True) if title_el else ""

            # Fallback: check title + description for appartement
            description_el = card.select_one("[class*='description'], [class*='desc'], [class*='type'], p")
            description_text = description_el.get_text(separator=' ', strip=True) if description_el else ""
            if "appartement" in (title + " " + description_text).lower():
                return None

            link = card.find("a", href=True)
            if not link:
                return None
            href = link.get("href", "")
            if not href:
                return None
            if not href.startswith("http"):
                href = f"https://www.zimmo.be{href}"
            # Strip query params — only keep clean listing detail URL
            clean_url = href.split("?")[0].rstrip("/") + "/"
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
            address = addr_el.get_text(separator=' ', strip=True) if addr_el else city.capitalize()
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
                # Format: "Klavet 14 — GEEL" — pak alles vóór postcode
                pc_match = re.search(r'^(.+?)\s+(\d{4})\s+(.+)$', address.strip())
                if pc_match:
                    street_full = pc_match.group(1).strip()
                    c = pc_match.group(3).upper()
                    title = f"{street_full} — {c}"
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
                url=clean_url,
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

    def _extract_address_from_text(self, text: str) -> str:
        postal = self._current_postal
        city = self._current_city
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if re.fullmatch(rf"{postal}\s+{re.escape(city)}", line, re.IGNORECASE):
                return f"{lines[i-1]}, {line}" if i > 0 else line
        m = re.search(rf"([A-ZÀ-ÿ0-9][^\n]+?)\s*({postal}\s+{re.escape(city)})", text, re.IGNORECASE)
        if m:
            return f"{m.group(1).strip()}, {m.group(2).strip()}"
        return city.capitalize()


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    scraper = ZimmoScraper()
    results = scraper.safe_scrape()
    for r in results:
        print(f"  [{r.id}] {r.title} — €{r.price} — {r.address}")
    print(f"\nTotal: {len(results)} listings")
