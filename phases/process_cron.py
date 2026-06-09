#!/usr/bin/env python3
"""Domus cron — snelle variant, enkel cached data, geen AI scoring.
Draait in < 30s. Geen model calls. Stuurt mail via SMTP."""
import sys, os, json, glob, re
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from storage import Listing, listing_fingerprint, listing_full_fingerprint, load_history, save_history
from dataclasses import fields
from config import ACCEPT_CITIES, MIN_LOT_SURFACE, MIN_LIVING_SURFACE, EPC_ALLOWED, EXCLUDE_CITIES_FINAL
from email_sender.digest import send_digest

HISTORY_FILE = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / "data" / "listing_history.json"

# --- Helpers voor veld mapping (batch data != Listing dataclass) ---

def ld_get(ld, *keys):
    """Try multiple keys, return first non-None value."""
    for k in keys:
        v = ld.get(k)
        if v is not None:
            return v
    return None

def ld_price(ld):
    return ld_get(ld, "price") or 0

def ld_rooms(ld):
    """bedrooms. immoweb has 'bedrooms', 2dehands 'rooms'."""
    return ld_get(ld, "bedrooms", "rooms", "slaapkamers") or 0

def ld_living(ld):
    return ld_get(ld, "living_surface", "surface_m2", "surface", "woonoppervlakte") or 0

def ld_lot(ld):
    return ld_get(ld, "lot_surface", "lot_surface_m2", "plot_surface", "perceeloppervlakte") or 0

def ld_epc(ld):
    epc = ld_get(ld, "epc", "epc_label", "energielabel")
    return (str(epc or "")).upper().strip("+")

def ld_city(ld):
    """Extract city from address (last word, likely Belgian postal code context)."""
    c = ld_get(ld, "city", "gemeente")
    if c:
        return str(c).strip()
    # Parse from address: "Boomlaarstraat 75D 2500 Lier" → last word
    addr = ld_get(ld, "address", "title") or ""
    parts = addr.strip().split()
    if len(parts) >= 2:
        # Last word after a 4-digit postal code
        for i, p in enumerate(parts):
            if p.isdigit() and len(p) == 4 and i + 1 < len(parts):
                return parts[i + 1]
        # Fallback: just last word
        return parts[-1]
    return ""

def ld_address(ld):
    a = ld_get(ld, "address", "title") or ""
    return str(a)

def ld_url(ld):
    return ld_get(ld, "url") or ""


# --- Filters ---

def filter_city(ld):
    addr = ld_address(ld).lower()
    city = ld_city(ld).lower()
    if not ACCEPT_CITIES:
        return True
    addr_str = addr + " " + city
    for ac in ACCEPT_CITIES:
        if ac.lower() in addr_str:
            return True
    return False

def filter_exclude(ld):
    city = ld_city(ld).lower()
    addr = ld_address(ld).lower()
    for ex in EXCLUDE_CITIES_FINAL:
        if ex.lower() in city or ex.lower() in addr:
            return False
    return True

def score_deterministic(ld):
    """Snelle deterministische score — geen AI, geen subprocess."""
    price = ld_price(ld)
    living = ld_living(ld)
    lot = ld_lot(ld)
    rooms = ld_rooms(ld)
    epc = ld_epc(ld)

    score = 5.0

    # Prijs (hoe lager hoe beter, basisrange 200k-800k)
    if 150000 < price < 300000:
        score += 1.5
    elif price <= 150000:
        score += 2.0
    elif price < 500000:
        score += 0.8
    elif price > 800000:
        score -= 1.0

    # Woonoppervlakte
    if 80 < living < 150:
        score += 0.8
    elif living >= 150:
        score += 1.0
    elif 0 < living <= 40:
        score -= 1.5

    # Perceel
    if lot >= 500:
        score += 0.5
    if lot >= 1000:
        score += 0.5

    # Kamers
    if rooms >= 3:
        score += 0.5
    if rooms >= 4:
        score += 0.3

    # EPC
    if epc in ("A", "A+", "A++"):
        score += 1.0
    elif epc == "B":
        score += 0.5
    elif epc == "C":
        pass  # neutraal
    elif epc in ("D", "E", "F"):
        score -= 1.0

    return round(min(10, max(0, score)), 1)


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
                all_listings.append(ld)
    return all_listings, sources


def ld_to_digest_dict(ld):
    """Map batch dict naar wat de digest email verwacht."""
    d = {
        "title": ld_get(ld, "title", "address") or "Onbekend",
        "address": ld_address(ld),
        "city": ld_city(ld),
        "price": ld_price(ld),
        "bedrooms": ld_rooms(ld),
        "surface": ld_living(ld),
        "lot_surface": ld_lot(ld),
        "epc": ld_epc(ld),
        "url": ld_url(ld),
        "platform": ld_get(ld, "platform", "web"),
        "score": ld.get("score", 0),
        "accept_city": "",
    }
    # Match accept_city
    city = d["city"].lower()
    for ac in ACCEPT_CITIES:
        if ac.lower() == city:
            d["accept_city"] = ac
            break
    return d


if __name__ == "__main__":
    print(f"Domus cron — cached only")

    # Laad batches
    all_listings, sources = load_all_batches()
    total_raw = len(all_listings)
    print(f"  Geladen: {total_raw} listings uit {len(sources)} bronnen")
    for k, v in sources.items():
        print(f"    {k}: {v}")

    if total_raw == 0:
        print("❌ Geen data — scrapers nog niet gedraaid.")
        sys.exit(1)

    # Filter op stad
    before = len(all_listings)
    all_listings = [ld for ld in all_listings if filter_city(ld)]
    print(f"  Na stadfilter: {len(all_listings)} (was {before})")

    # Exclude steden
    before2 = len(all_listings)
    all_listings = [ld for ld in all_listings if filter_exclude(ld)]
    print(f"  Na excludefilter: {len(all_listings)} (was {before2})")

    # EPC filter — alleen als data aanwezig
    if EPC_ALLOWED:
        has_epc = [ld for ld in all_listings if ld_epc(ld)]
        if has_epc:
            before3 = len(all_listings)
            all_listings = [ld for ld in all_listings if ld_epc(ld) in EPC_ALLOWED]
            print(f"  Na EPC filter: {len(all_listings)} (was {before3})")
        else:
            print(f"  ⚠ EPC overgeslagen — geen data in batches")

    if len(all_listings) == 0:
        print("❌ Geen listings na filters")
        sys.exit(1)

    # Score
    for ld in all_listings:
        ld["score"] = score_deterministic(ld)

    # Sorteer
    all_listings.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Top 5 print
    print(f"\n📊 Top {min(5, len(all_listings))}:")
    for ld in all_listings[:5]:
        p = ld_price(ld)
        s = ld["score"]
        addr = ld_address(ld)[:50]
        city = ld_city(ld)
        epc = ld_epc(ld)
        print(f"   {s}/10 — €{p:,} — {addr} — {city} (EPC: {epc})")

    # History laden
    history = load_history(HISTORY_FILE)

    # Filter nieuw op fingerprint
    new_listings = []
    for ld in all_listings:
        # Build listing for fingerprint
        addr = ld_address(ld)
        city = ld_city(ld)
        full_addr = f"{addr}, {city}" if city and city not in addr else addr
        
        l = Listing(
            id="cron-" + ld_url(ld)[-8:],
            url=ld_url(ld),
            title=ld_get(ld, "title", "address") or addr[:80] or "Onbekend",
            price=float(ld_price(ld)),
            bedrooms=int(ld_rooms(ld)),
            surface_m2=float(ld_living(ld)),
            lot_surface_m2=float(ld_lot(ld)),
            epc_label=ld_epc(ld),
            address=full_addr,
            platform=ld_get(ld, "platform", "web"),
            property_type=ld_get(ld, "property_type", "house") or "house",
            description="",
            image_urls=ld_get(ld, "image_urls") or [],
            posted_date=ld_get(ld, "posted_date", "datum") or "",
            status=ld_get(ld, "status", "description") or "",
            text_score=float(ld.get("text_score", 0) or 0),
            photo_score=float(ld.get("photo_score", 0) or 0),
            final_score=float(ld["score"]),
            score_reasoning=f"Deterministisch: {ld['score']}/10",
        )
        fp = listing_full_fingerprint(l)
        if fp not in history:
            history[fp] = None
            new_listings.append(l)

    print(f"\n  Nieuw: {len(new_listings)}")

    if len(new_listings) == 0:
        print("Geen nieuwe — skip email")
        save_history(HISTORY_FILE, history)
        sys.exit(0)

    # Email
    print(f"\n📧 Email versturen naar mail@holie.be...")
    try:
        send_digest(new_listings)
        print(f"  ✅ Email verzonden! ({len(new_listings)} huizen)")
    except Exception as e:
        print(f"  ❌ Email fout: {e}")
        import traceback
        traceback.print_exc()

    # History saven
    save_history(HISTORY_FILE, history)
    print(f"\n✅ KLAAR!")
