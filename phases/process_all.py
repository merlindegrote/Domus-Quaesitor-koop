#!/usr/bin/env python3
"""Merge alle batch outputs in /tmp/domus-batches/, dedup, score, output processed JSON"""
import sys, os, json, glob, time, gc, resource, re
from pathlib import Path
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import Listing, listing_fingerprint, listing_full_fingerprint, load_history, save_history
# Scoring done via subprocess runners (deepseek_runner.py, photo_runner.py)
from email_sender.digest import send_digest
from config import ACCEPT_CITIES, EXCLUDE_CITIES_FINAL, EPC_ALLOWED, MIN_LOT_SURFACE, MIN_LIVING_SURFACE
from dataclasses import fields
from phases.embed_images import embed_images
from scrapers.immoweb import ImmowebScraper
from scrapers.zimmo import ZimmoScraper
from scrapers.immoscoop import ImmoscoopScraper
from scrapers.immovlan import ImmovlanScraper

HISTORY_FILE = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "data" / "listing_history.json"

def _enrich_via_subprocess(listing_dict: dict, timeout: int = 45) -> dict:
    """Verrijk 1 listing via apart subprocess.
    
    Doel: curl_cffi C-level geheugen wordt door OS opgeruimd
    als het proces stopt. Geen accumulatie = geen hang na ~100 requests.
    """
    import subprocess as _sp
    import json as _json
    
    worker = os.path.join(os.path.dirname(__file__), "..", "scrapers", "enrich_worker.py")
    worker = os.path.abspath(worker)
    
    input_json = _json.dumps({"listing": listing_dict})
    
    try:
        proc = _sp.run(
            [sys.executable, worker],
            input=input_json,
            capture_output=True,
            timeout=timeout,
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"}
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Worker exit {proc.returncode}: {proc.stderr[:200]}")
        result = _json.loads(proc.stdout)
        if result.get("ok"):
            return result["listing"]
        else:
            raise RuntimeError(result.get("error", "onbekende fout"))
    except _sp.TimeoutExpired:
        raise TimeoutError(f"Enrich worker timeout na {timeout}s")


def dict_to_listing(d):
    field_names = {f.name for f in fields(Listing)}
    kwargs = {k: v for k, v in d.items() if k in field_names}
    return Listing(**kwargs)

def load_all_batches():
    all_listings = []
    sources = {}
    for fpath in sorted(glob.glob("/tmp/domus-batches/*.json")):
        with open(fpath) as f:
            data = json.load(f)
        plat = data.get("platform", "?")
        cnt = data.get("count", 0)
        sources[plat] = cnt
        for ld in data.get("listings", []):
            if isinstance(ld, dict):
                if "property_type" not in ld or not ld["property_type"]:
                    ld["property_type"] = "house"
                all_listings.append(ld)
        print(f"  {plat}: {cnt} listings")
    return all_listings, sources

def dedup_listings(listings):
    """Dedup + merge:zelfde huis = 1 entry met beste info uit alle bronnen.
    
    Stappen:
    0. Eerst op platform+ID (altijd zelfde huis, ongeacht adres)
    1. Dan op volledig adres (met huisnummer)
    2. Fallback op straatnaam als adres geen huisnummer heeft
    """
    seen = {}
    seen_by_id = {}
    merged_ids = set()
    for ld in listings:
        platform = ld.get("platform", "")
        lid = str(ld.get("id", ""))
        pid_key = f"{platform}|{lid}" if lid else ""
        
        if pid_key and pid_key in seen_by_id:
            existing = seen_by_id[pid_key]
            merged = _merge_listings(existing, ld)
            seen_by_id[pid_key] = merged
            for old_fp, old_ld in list(seen.items()):
                if old_ld is existing:
                    del seen[old_fp]
                    break
            try:
                listing = dict_to_listing(merged)
                fp = listing_full_fingerprint(listing)
                if not any(c.isdigit() for c in fp.split("|")[0]):
                    fp = listing_fingerprint(listing)
            except Exception:
                fp = f"{merged.get('address','')}|{merged.get('price',0)}"
                fp = fp.replace(" ", "").lower()
            seen[fp] = merged
            merged_ids.add(lid)
            continue
        
        try:
            listing = dict_to_listing(ld)
            fp = listing_full_fingerprint(listing)
            if not any(c.isdigit() for c in fp.split("|")[0]):
                fp = listing_fingerprint(listing)
        except Exception:
            fp = f"{ld.get('address','')}|{ld.get('price',0)}"
            fp = fp.replace(" ", "").lower()
        
        if fp in seen:
            existing = seen[fp]
            merged = _merge_listings(existing, ld)
            seen[fp] = merged
            merged_ids.add(lid)
        else:
            seen[fp] = ld
        
        if pid_key:
            seen_by_id[pid_key] = seen[fp]
    
    return list(seen.values())


def _merge_listings(a: dict, b: dict) -> dict:
    """Merge 2 entries van hetzelfde huis — alle info bundelen, score resetten."""
    merged = a.copy()
    
    # Beste titel (met straatnaam > zonder)
    def is_bad_title(t):
        t = t or ""
        return "House" in t or "Huis te koop" in t or t in ("", "House")
    if is_bad_title(merged.get("title", "")) and not is_bad_title(b.get("title", "")):
        merged["title"] = b["title"]
    
    # Langste beschrijving (beste context voor scoring)
    desc_a = len(a.get("description") or "")
    desc_b = len(b.get("description") or "")
    if desc_b > desc_a:
        merged["description"] = b.get("description", "")
    
    # Combineer beschrijvingen als ze vullen aan
    if desc_a > 0 and desc_b > 0:
        merged["description"] = f"{a.get('description','')} / {b.get('description','')}"
    
    # Meeste foto's
    imgs_a = len(a.get("image_urls") or [])
    imgs_b = len(b.get("image_urls") or [])
    if imgs_b > imgs_a:
        merged["image_urls"] = b.get("image_urls", [])
    
    # Duid platform als multi
    plat_a = a.get("platform", "")
    plat_b = b.get("platform", "")
    platform_parts = set()
    for p in [plat_a, plat_b]:
        for part in p.split("+"):
            part = part.strip()
            if part:
                platform_parts.add(part)
    if len(platform_parts) > 1:
        merged["platform"] = "+".join(sorted(platform_parts))
        # Beste URL (langste = meest specifiek)
        if len(b.get("url","")) > len(a.get("url","")):
            merged["url"] = b["url"]
    
    # Adres: meest complete (met huisnummer)
    if len(b.get("address", "")) > len(a.get("address", "")):
        merged["address"] = b["address"]
    
    # Oppervlakte: hoogste is meest accuraat
    if (b.get("surface_m2") or 0) > (a.get("surface_m2") or 0):
        merged["surface_m2"] = b.get("surface_m2")
    if (b.get("lot_surface_m2") or 0) > (a.get("lot_surface_m2") or 0):
        merged["lot_surface_m2"] = b.get("lot_surface_m2")
    
    # EPC: beste label
    epc_order = {"a+": 0, "a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6}
    epc_a = (a.get("epc_label") or "").lower().strip()
    epc_b = (b.get("epc_label") or "").lower().strip()
    if epc_b and (not epc_a or epc_order.get(epc_b, 99) < epc_order.get(epc_a, 99)):
        merged["epc_label"] = b["epc_label"]
    
    # Status: meest significante
    status_order = {"life_annuity": 3, "under_option": 2, "sold": 2, "available": 1}
    st_a = status_order.get(a.get("status"), 0)
    st_b = status_order.get(b.get("status"), 0)
    if st_b > st_a:
        merged["status"] = b.get("status")
    
    # Prijs: gemiddelde als ze te veel verschillen (>5%)
    price_a = a.get("price", 0)
    price_b = b.get("price", 0)
    if price_a > 0 and price_b > 0 and price_a != price_b:
        diff_pct = abs(price_a - price_b) / max(price_a, price_b)
        if diff_pct < 0.05:
            merged["price"] = round((price_a + price_b) / 2)
    
    # SCORE RESET — moet opnieuw gescoord worden met de samengevoegde data
    merged["final_score"] = None
    merged["text_score"] = None
    merged["photo_score"] = None
    merged["score_reasoning"] = None
    
    return merged

def filter_appartementen(listings):
    clean = []
    removed = 0
    for ld in listings:
        pt = ld.get("property_type", "house")
        title = (ld.get("title") or "").lower()
        description = (ld.get("description") or "").lower()
        if pt == "apartment" or "appartement" in title or "appartement" in description:
            removed += 1
            continue
        clean.append(ld)
    return clean, removed

def filter_by_city(listings):
    """Filter listings die niet in ACCEPT_CITIES vallen via adres of URL + EXCLUDE_CITIES"""
    clean = []
    removed = 0
    for ld in listings:
        addr = (ld.get("address") or "").lower()
        url = (ld.get("url") or "").lower()
        # Skip if in EXCLUDE_CITIES
        if any(city.lower() in addr or city.lower() in url for city in EXCLUDE_CITIES_FINAL):
            removed += 1
            continue
        # Accept if in ACCEPT_CITIES
        if any(city.lower() in addr or city.lower() in url for city in ACCEPT_CITIES):
            clean.append(ld)
        else:
            removed += 1
    return clean, removed

def og_image_fallback(listings):
    for ld in listings:
        photos = ld.get("image_urls") or ld.get("images") or []
        if not photos:
            url = ld.get("url") or ""
            # Zet URL als placeholder
            ld["image_urls"] = []
        else:
            ld["image_urls"] = photos
    return listings

def _enrich_from_description(listings):
    """Universele backstop: extraheer missende velden uit beschrijving.
    
    Vangt EPC, oppervlakte, perceelgrootte, slaapkamers die in de
    beschrijving staan maar niet in gestructureerde API/HTML data.
    """
    import re as _re
    
    updated = 0
    for ld in listings:
        desc = ld.get("description") or ""
        
        # 1. EPC label
        if not ld.get("epc_label"):
            m = _re.search(
                r"EPC[\s:-]*\s*(?:label\s*)?(?:waarde[\s:-]*)?([A-F][+-]?)\b"
                r"|energie(?:label|prestatie)[\s:-]*([A-F][+-]?)\b"
                r"|energielabel[\s:-]*([A-F][+-]?)\b"
                r"|EPC[-\s]*(?:score|waarde)[\s:-]*\d+[\s/]*kWh[^.]*?([A-F][+-]?)\b",
                desc, _re.I
            )
            if m:
                label = next(g for g in m.groups() if g)
                ld["epc_label"] = label.upper().replace("+", "+")
                updated += 1
        
        # 2. Bewoonbare oppervlakte (surface_m2)
        if not ld.get("surface_m2"):
            # "180m² woonoppervlakte", "bewoonbare opp. 120 m²", "woonoppervlakte van 95 m2"
            m = _re.search(
                r"(?:woon|bewoonbare|leef)?\s*(?:opp|oppervlakte|oppervlak)\s*(?:van\s*)?(\d+)\s*m[²2]"
                r"|(\d+)\s*m[²2]\s*(?:woon|bewoonbare|leef)?\s*(?:opp|oppervlakte|oppervlak)",
                desc, _re.I
            )
            if m:
                val = next(g for g in m.groups() if g)
                ld["surface_m2"] = int(val)
                updated += 1
        
        # 3. Perceeloppervlakte (lot_surface_m2)
        if not ld.get("lot_surface_m2"):
            m = _re.search(
                r"(?:perceel|grond|kavel)\s*(?:opp|oppervlakte|grootte|oppervlak)?\s*(?:van\s*)?(\d+)\s*m[²2]",
                desc, _re.I
            )
            if m and m.group(1):
                ld["lot_surface_m2"] = int(m.group(1))
                updated += 1
        
        # 4. Slaapkamers (bedrooms)
        if not ld.get("bedrooms"):
            # "3 slaapkamers", "4 SLK", "2 slpk"
            m = _re.search(r"(\d+)\s*slaapkamer(?:s)?\b|(\d+)\s*SLK\b|(\d+)\s*slpk\b", desc, _re.I)
            if m:
                val = next(g for g in m.groups() if g)
                ld["bedrooms"] = int(val)
                updated += 1
    
    if updated:
        print(f"  📋 {updated} velden uit beschrijving geëxtraheerd")
    return listings


def detect_status(listings):
    """Backstop: detecteer 'onder optie' en 'lijfrente' uit beschrijvingen voor alle platforms"""
    for ld in listings:
        if ld.get("status"):
            continue
        desc = (ld.get("description") or "").lower()
        title = (ld.get("title") or "").lower()
        url = (ld.get("url") or "").lower()
        text = f"{desc} {title} {url}"
        if "lijfrente" in text or "levenslang" in text:
            ld["status"] = "life_annuity"
        elif "onder optie" in text or "under option" in text or "in optie" in text:
            ld["status"] = "under_option"
    return listings

def update_history(listings):
    """History bijwerken met nieuwe listings"""
    history = load_history(HISTORY_FILE)
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d")
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S")
    records = history.get("records", {})
    
    for ld in listings:
        try:
            listing = dict_to_listing(ld)
            fp = listing_fingerprint(listing)
        except Exception:
            fp = f"{ld.get('address','')}|{ld.get('price',0)}".replace(" ", "").lower()
        
        plat = ld.get("platform", "?")
        lid = str(ld.get("id", ""))
        
        if fp in records:
            # Update bestaand record
            rec = records[fp]
            rec["last_seen_at"] = now_iso
            rec["last_seen_date"] = now_str
            rec["last_sent_at"] = now_iso
            if now_str not in rec.get("sent_dates", []):
                rec.setdefault("sent_dates", []).append(now_str)
            # Nieuwe unique_key toevoegen
            uk = f"{plat}_{lid}"
            if uk not in rec.get("unique_keys", []):
                rec.setdefault("unique_keys", []).append(uk)
        else:
            # Nieuw record
            records[fp] = {
                "fingerprint": fp,
                "first_seen_at": now_iso,
                "last_seen_at": now_iso,
                "first_seen_date": now_str,
                "last_seen_date": now_str,
                "platform": plat,
                "id": lid,
                "unique_keys": [f"{plat}_{lid}"],
                "title": ld.get("title", ""),
                "status": ld.get("status"),
                "price": ld.get("price", 0),
                "bedrooms": ld.get("bedrooms", 0),
                "address": ld.get("address", ""),
                "url": ld.get("url", ""),
                "description": (ld.get("description") or "")[:500],
                "image_urls": ld.get("image_urls", []),
                "epc_label": ld.get("epc_label"),
                "surface_m2": ld.get("surface_m2"),
                "posted_date": ld.get("posted_date"),
                "text_score": ld.get("text_score"),
                "photo_score": ld.get("photo_score"),
                "final_score": ld.get("final_score", 5.0),
                "score_reasoning": ld.get("score_reasoning"),
                "first_sent_at": now_iso,
                "last_sent_at": now_iso,
                "sent_dates": [now_str]
            }
    
    history["records"] = records
    save_history(HISTORY_FILE, history)
    return len(records)

def main():
    print("=" * 50)
    print("DOMUS-PROCESS — Merge + dedup + score + email")
    print("=" * 50)
    
    # 1. Laad alle batches
    all_listings, sources = load_all_batches()
    total_raw = len(all_listings)
    print(f"\n📥 Totaal raw: {total_raw} (bronnen: {sources})")
    
    if not all_listings:
        print("❌ Geen listings om te verwerken.")
        return
    
    # 2. Filter appartementen
    all_listings, removed_app = filter_appartementen(all_listings)
    print(f"🚫 Appartementen uitgefilterd: {removed_app}")

    # Backstop: filter appartementen
    before = len(all_listings)
    all_listings = [ld for ld in all_listings if ld.get("property_type", "house") == "house"]
    all_listings = [ld for ld in all_listings if "appartement" not in ld.get("title", "").lower()]
    filtered = before - len(all_listings)
    if filtered:
        print(f"  🏢 {filtered} appartementen uitgefilterd (backstop)")

    # 2b. Filter op stad (ALLEEN voor Immovlan — andere scrapers zoeken al lokaal)
    all_listings, removed_city = filter_by_city(all_listings)
    if removed_city:
        print(f"📍 Buiten doelstad uitgefilterd: {removed_city}")
    
    # 3. og:image fallback
    all_listings = og_image_fallback(all_listings)
    
    # 3b. detect status (onder optie / lijfrente)
    all_listings = detect_status(all_listings)
    status_count = sum(1 for l in all_listings if l.get("status"))
    if status_count:
        print(f"📋 Status badges: {status_count} (onder optie / lijfrente)")

    # 3c. Enrich ALL Immoweb listings — parallel
    house_listings = [l for l in all_listings if l.get("platform") == "immoweb"]
    
    # 3d. Enrich Immoscoop listings — detail page heeft echte fotos ipv placeholders
    immoscoop_listings = [l for l in all_listings if l.get("platform") == "immoscoop"]
    
    # 3e. Enrich Zimmo listings — detail page voor straat+nummer
    zimmo_listings = [l for l in all_listings if l.get("platform") == "zimmo"]

    # ─── Immoweb enrich (sequential — _spawn_fetch heeft eigen daemon thread timeout) ───
    if house_listings:
        BATCH_SIZE = 20
        total_iw = len(house_listings)
        print(f"🔍 Immoweb House-titels verrijken ({total_iw} stuks in batches van {BATCH_SIZE})...")
        enriched_count = 0

        for batch_start in range(0, total_iw, BATCH_SIZE):
            immoweb = ImmowebScraper()  # verse scraper per batch — curl_cffi accumuleert interne state
            batch_end = min(batch_start + BATCH_SIZE, total_iw)
            batch = house_listings[batch_start:batch_end]
            batch_num = batch_start // BATCH_SIZE + 1
            total_batches = (total_iw + BATCH_SIZE - 1) // BATCH_SIZE
            print(f"  [{time.strftime('%H:%M:%S')}] Batch {batch_num}/{total_batches} ({batch_start+1}-{batch_end} van {total_iw})")

            # Pre-batch memory check
            usage = resource.getrusage(resource.RUSAGE_SELF)
            mem_mb = usage.ru_maxrss / 1024  # Linux reports in KB
            print(f"    Geheugen: {mem_mb:.0f} MB")

            for idx_offset, ld in enumerate(batch):
                idx = batch_start + idx_offset + 1
                try:
                    listing = dict_to_listing(ld)
                    enriched = immoweb.enrich_listing(listing)
                    ld["title"] = enriched.title or ld["title"]
                    ld["description"] = enriched.description or ld.get("description", "")
                    if enriched.image_urls:
                        ld["image_urls"] = enriched.image_urls
                    if enriched.epc_label:
                        ld["epc_label"] = enriched.epc_label
                    if enriched.surface_m2:
                        ld["surface_m2"] = enriched.surface_m2
                    if enriched.lot_surface_m2:
                        ld["lot_surface_m2"] = enriched.lot_surface_m2
                    if enriched.status:
                        ld["status"] = enriched.status
                    enriched_count += 1
                except Exception as exc:
                    print(f"  ⚠ Fout bij Immoweb enrich {ld.get('id','?')}: {exc}")
                if (idx) % 10 == 0:
                    print(f"  [{time.strftime('%H:%M:%S')}] [{idx}/{total_iw}] enrich bezig...")
                if (idx) % 5 == 0:
                    gc.collect()

            # Post-batch: forced GC + memory reset
            gc.collect()
            gc.collect()  # twee keer voor generaties
            usage = resource.getrusage(resource.RUSAGE_SELF)
            mem_mb = usage.ru_maxrss / 1024
            print(f"    Geheugen na batch: {mem_mb:.0f} MB — {enriched_count}/{idx} enriched")

        print(f"  [{time.strftime('%H:%M:%S')}] ✅ Immoweb enrich: {enriched_count}/{total_iw} gelukt")

    # ─── Immoscoop enrich (sequential) ───
    if immoscoop_listings:
        print(f"🔍 Immoscoop fotos verrijken ({len(immoscoop_listings)} stuks)...")
        immoscoop = ImmoscoopScraper()

        enriched_count = 0
        for idx, ld in enumerate(immoscoop_listings, 1):
            try:
                listing = dict_to_listing(ld)
                enriched = immoscoop.enrich_listing(listing)
                ld["description"] = enriched.description or ld.get("description", "")
                if enriched.image_urls and len(enriched.image_urls) >= len(ld.get("image_urls", [])):
                    ld["image_urls"] = enriched.image_urls
                if enriched.epc_label:
                    ld["epc_label"] = enriched.epc_label
                if enriched.surface_m2:
                    ld["surface_m2"] = enriched.surface_m2
                if enriched.lot_surface_m2:
                    ld["lot_surface_m2"] = enriched.lot_surface_m2
                if enriched.bedrooms:
                    ld["bedrooms"] = enriched.bedrooms
                if enriched.price:
                    ld["price"] = enriched.price
                enriched_count += 1
            except Exception as exc:
                print(f"  ⚠ Fout bij Immoscoop enrich {ld.get('id','?')}: {exc}")
            if idx % 10 == 0:
                print(f"  [{idx}/{len(immoscoop_listings)}] enrich bezig...")
            if idx % 10 == 0:
                gc.collect()
            if idx % 20 == 0:
                print(f"  [{time.strftime('%H:%M:%S')}] [{idx}/{len(immoscoop_listings)}] immoscoop enrich bezig...")
        print(f"  [{time.strftime('%H:%M:%S')}] ✅ Immoscoop enrich: {enriched_count}/{len(immoscoop_listings)} gelukt")

    # ─── Zimmo skippen (data zit al in search page JSON) ───
    if zimmo_listings:
        print(f"  ⏩ Zimmo enrich overgeslagen — data al beschikbaar uit search page ({len(zimmo_listings)} stuks)")
    
    # 3f. Enrich Immovlan listings
    immovlan_listings = [l for l in all_listings if l.get("platform") == "immovlan"]
    if immovlan_listings:
        print(f"🔍 Immovlan verrijken ({len(immovlan_listings)} stuks)...")
        immovlan = ImmovlanScraper()
        for idx, ld in enumerate(immovlan_listings):
            if idx > 0 and idx % 20 == 0:
                print(f"  [{idx}/{len(immovlan_listings)}] immovlan bezig...")
            try:
                listing = dict_to_listing(ld)
                enriched = immovlan.enrich_listing(listing)
                if enriched:
                    ld["address"] = enriched.address or ld.get("address", "")
                    ld["description"] = enriched.description or ld.get("description", "")
                    ld["epc_label"] = enriched.epc_label or ld.get("epc_label")
                    ld["surface_m2"] = enriched.surface_m2 or ld.get("surface_m2")
                    ld["lot_surface_m2"] = enriched.lot_surface_m2 or ld.get("lot_surface_m2")
                    if enriched.image_urls and len(enriched.image_urls) >= len(ld.get("image_urls", [])):
                        ld["image_urls"] = enriched.image_urls
            except Exception as exc:
                print(f"  ⚠ Fout bij immovlan enrich {ld.get('id')}: {exc}")
            time.sleep(0.3)
            if idx % 20 == 0:
                gc.collect()
        print(f"  [{time.strftime('%H:%M:%S')}] ✅ Immovlan enrich gedaan ({len(immovlan_listings)} stuks)")

    # 3g. Universele enrich — extraheer missende velden uit beschrijving
    all_listings = _enrich_from_description(all_listings)

    # 3h. Adres filter — geen straat = eruit
    before_addr = len(all_listings)
    addr_filtered = []
    for ld in all_listings:
        addr = (ld.get("address") or "").strip()
        # Filter: alleen postcode+stad ("2500 Lier") of alleen stad ("Ranst")
        # ✅ Pater Domstraat 30, 2520 Broechem — heeft straat + huisnummer
        # ❌ 2500 Lier — alleen postcode + stad
        # ❌ Ranst — alleen stad
        has_number = bool(re.search(r'\d', addr))
        starts_with_postcode = bool(re.match(r'^\d{4}\s+\w', addr))
        if not has_number or starts_with_postcode:
            continue  # geen straatadres → skippen
        addr_filtered.append(ld)
    removed_addr = before_addr - len(addr_filtered)
    if removed_addr:
        print(f"⛔ Adres filter: {removed_addr} verwijderd (geen straat)")
        for ld in all_listings:
            addr = (ld.get("address") or "").strip()
            has_number = bool(re.search(r'\d', addr))
            starts_with_postcode = bool(re.match(r'^\d{4}\s+\w', addr))
            if not has_number or starts_with_postcode:
                print(f"  ⛔ {ld.get('id','?')}: adres='{addr}'")
    all_listings = addr_filtered

    # 3i. Quality filter — EPC, perceel, woonopp
    before_qf = len(all_listings)
    qf_reasons = []
    filtered_qf = []
    for ld in all_listings:
        epc = (ld.get("epc_label") or "").upper().strip()
        lot = ld.get("lot_surface_m2")
        living = ld.get("surface_m2")

        # EPC filter: skip als EPC bekend is en niet in allowed lijst
        if epc and epc not in EPC_ALLOWED:
            qf_reasons.append(f"  ⛔ {ld.get('id','?')}: EPC {epc} (niet in {EPC_ALLOWED})")
            continue

        # Perceel filter: skip als perceel bekend (non-zero) is en te klein
        if lot and lot >= 1 and lot < MIN_LOT_SURFACE:
            qf_reasons.append(f"  ⛔ {ld.get('id','?')}: perceel {lot}m² < {MIN_LOT_SURFACE}m²")
            continue

        # Woonopp filter: skip als woonopp bekend is en te klein
        if living is not None and living < MIN_LIVING_SURFACE:
            qf_reasons.append(f"  ⛔ {ld.get('id','?')}: woonopp {living}m² < {MIN_LIVING_SURFACE}m²")
            continue

        filtered_qf.append(ld)

    removed_qf = before_qf - len(filtered_qf)
    if removed_qf:
        print(f"⛔ Quality filter: {removed_qf} verwijderd")
        for r in qf_reasons:
            print(r)
    all_listings = filtered_qf

    unique_listings = dedup_listings(all_listings)
    print(f"🔍 Na dedup: {len(unique_listings)} (verwijderd: {len(all_listings) - len(unique_listings)})")
    
    if not unique_listings:
        print("❌ Geen listings na dedup.")
        return
    
    # 5. DeepSeek scoring (subprocess for reliable timeout)
    print(f"\n🧠 DeepSeek text scoring ({len(unique_listings)} listings)...")
    print("  (via subprocess for reliable timeout)")
    scored = []

    # Batch all in one subprocess call
    import subprocess, sys, json as _json
    runner = os.path.join(os.path.dirname(__file__), "..", "scoring", "deepseek_runner.py")
    runner = os.path.abspath(runner)

    # Build minimal input per listing
    batch_input = []
    for ld in unique_listings:
        batch_input.append({
            "id": ld.get("id", ""),
            "platform": ld.get("platform", ""),
            "title": ld.get("title", ""),
            "price": ld.get("price", 0),
            "address": ld.get("address", ""),
            "surface_m2": ld.get("surface_m2", 0),
            "epc_label": ld.get("epc_label", ""),
            "bedrooms": ld.get("bedrooms", 0),
            "description": ld.get("description", ""),
        })

    try:
        proc = subprocess.run(
            [sys.executable, "-u", runner, _json.dumps(batch_input)],
            capture_output=True, text=True, timeout=300,
        )
        out_line = proc.stdout.strip()
        if not out_line:
            out_line = "{}"
        result = _json.loads(out_line)

        if result.get("ok"):
            results_map = {r["id"]: r for r in result["results"]}
            for ld in unique_listings:
                lid = ld.get("id", "")
                r = results_map.get(lid, {})
                score = r.get("score", 5.0)
                reasoning = r.get("reasoning", "AI scoring via subprocess")
                ld["text_score"] = score
                ld["score_reasoning"] = reasoning
                ld["final_score"] = score
                scored.append((score, ld))
            print(f"  ✅ Scored {len(result['results'])} listings via subprocess")
        else:
            err = result.get("err", "Unknown")
            print(f"  ⚠ DeepSeek subprocess failed: {err}, using defaults")
            for ld in unique_listings:
                ld["text_score"] = 5.0
                ld["score_reasoning"] = "DeepSeek subprocess failed"
                ld["final_score"] = 5.0
                scored.append((5.0, ld))
    except subprocess.TimeoutExpired:
        print(f"  ⚠ DeepSeek subprocess timed out after 300s, using defaults")
        for ld in unique_listings:
            ld["text_score"] = 5.0
            ld["score_reasoning"] = "DeepSeek timeout"
            ld["final_score"] = 5.0
            scored.append((5.0, ld))
    except Exception as e:
        print(f"  ⚠ DeepSeek subprocess error: {e}, using defaults")
        for ld in unique_listings:
            ld["text_score"] = 5.0
            ld["score_reasoning"] = f"DeepSeek error: {str(e)[:80]}"
            ld["final_score"] = 5.0
            scored.append((5.0, ld))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    
    # 5b. Photo scoring via subprocess (OpenRouter + Gemini 3.5 Flash)
    print(f"\n📸 Photo scoring ({len(scored)} listings)...")
    print("  (via subprocess for reliable timeout)")
    photo_runner = os.path.join(os.path.dirname(__file__), "..", "scoring", "photo_runner.py")
    photo_runner = os.path.abspath(photo_runner)

    batch_input = []
    for _, ld in scored:
        batch_input.append({
            "id": ld.get("id", ""),
            "title": ld.get("title", ""),
            "price": ld.get("price", 0),
            "surface_m2": ld.get("surface_m2", 0),
            "epc_label": ld.get("epc_label", ""),
            "image_urls": ld.get("image_urls", []),
        })

    photo_scored_count = 0
    try:
        proc = subprocess.run(
            [sys.executable, "-u", photo_runner, _json.dumps(batch_input)],
            capture_output=True, text=True, timeout=300,
        )
        out_line = proc.stdout.strip()
        if not out_line:
            out_line = "{}"
        result = _json.loads(out_line)

        if result.get("ok"):
            results_map = {r["id"]: r for r in result["results"]}
            for _, ld in scored:
                lid = ld.get("id", "")
                r = results_map.get(lid, {})
                pscore = r.get("score", 5.0)
                ld["photo_score"] = pscore
                text = ld.get("text_score") or 5.0
                ld["final_score"] = round(text * 0.6 + pscore * 0.4, 1)
                photo_scored_count += 1
            print(f"  📸 {photo_scored_count}/{len(scored)} listings gescoord op foto's")
        else:
            print(f"  ⬜ Photo scorer failed: {result.get('err', 'unknown')}")
    except subprocess.TimeoutExpired:
        print(f"  ⚠ Photo scoring timed out after 300s, using text scores only")
    except Exception as e:
        print(f"  ⚠ Photo scoring error: {e}")
    
    # Re-sort met photo-inclusive final scores
    scored = [(ld.get("final_score", 5.0), ld) for _, ld in scored]
    scored.sort(key=lambda x: x[0], reverse=True)
    
    # Filter: alleen listings met score > 5.0
    before_filter = len(scored)
    scored = [(s, ld) for s, ld in scored if s > 5.0]
    filtered_low = before_filter - len(scored)
    if filtered_low:
        print(f"⏬ {filtered_low} listings uitgefilterd (score ≤ 5.0)")
    
    print(f"\n[{time.strftime('%H:%M:%S')}] 📊 Scoring voltooid — top 5:")
    for score, ld in scored[:5]:
        title = ld.get("title", "?")[:60]
        price = ld.get("price", 0)
        print(f"  {score:.1f}/10 — €{price:,} — {title}")
    
    # 6. Sla processed output op
    final_listings = [ld for _, ld in scored]
    output = {
        "total_raw": total_raw,
        "after_app_filter": total_raw - removed_app,
        "after_dedup": len(unique_listings),
        "sources": sources,
        "listings": final_listings
    }
    with open("/tmp/domus-processed.json", "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n💾 Opgeslagen: /tmp/domus-processed.json ({len(final_listings)} listings)")
    
    # 7. Update history
    print("\n📝 History bijwerken...")
    total_records = update_history(final_listings)
    print(f"  ✅ History: {total_records} records")
    
    # 8. Embed images as base64 (bypass CDN hotlink blocking)
    print(f"\n📷 Afbeeldingen embedden ({len(final_listings)} listings)...")
    try:
        final_listings = embed_images(final_listings)
    except Exception as e:
        print(f"  ⚠ Embed error: {e}")
    
    # 9. Build + send email
    print(f"\n📧 Email bouwen & versturen...")
    listing_objects = [dict_to_listing(ld) for ld in final_listings]
    try:
        success = send_digest(listing_objects)
        if success:
            print(f"  ✅ Email verzonden!")
        else:
            print(f"  ❌ send_digest returned False")
            with open("/tmp/domus-email-fallback.txt", "w") as f:
                f.write(f"{len(final_listings)} huizen\n")
                for ld in final_listings:
                    f.write(f"{ld.get('title','?')} - €{ld.get('price',0):,}\n")
    except Exception as e:
        print(f"  ❌ Email FAILED: {e}")
        with open("/tmp/domus-email-fallback.txt", "w") as f:
            f.write(f"{len(final_listings)} huizen\n")
            for ld in final_listings:
                f.write(f"{ld.get('title','?')} - €{ld.get('price',0):,}\n")
    
    print(f"\n{'='*50}")
    print(f"✅ KLAAR! {len(final_listings)} huizen verwerkt.")
    print(f"{'='*50}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log_path = "/tmp/domus-process-all-crash.log"
        with open(log_path, "w") as f:
            f.write(f"CRASH at {datetime.now().isoformat()}\n")
            f.write(f"Error: {e}\n")
            f.write(tb)
        print(f"❌ CRASH: {e}", file=sys.stderr)
        print(f"   Details: {log_path}", file=sys.stderr)
        sys.exit(1)
