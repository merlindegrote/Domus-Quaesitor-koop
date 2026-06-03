# 🏠 Domus-Quaesitor (Koop)

**Automatische huizenjager voor koophuizen in Vlaanderen.**

Scrapet te-koop-listing van 5 vastgoedsites (Immoweb, Zimmo, Immoscoop, 2dehands, Immovlan), filtert op prijs/slaapkamers/oppervlakte/EPC/locatie, verrijkt met detailpagina-gegevens, scoort met AI (DeepSeek voor tekst + Gemini 2.5 Flash Lite voor foto's), en mailt elke ochtend een gerankeerde HTML-digest van max 50 woningen.

> **Domus-Quaesitor** = Latijn voor "huizenzocker". Alleen koop, geen huur, geen appartementen.

---

## 📋 Inhoud

- [Pipeline overzicht](#-pipeline-overzicht)
- [Doelposten & Zoekcriteria](#-doelposten--zoekcriteria)
- [De 5 Scrapers](#-de-5-scrapers)
- [Filters](#-filters)
- [AI Scoring](#-ai-scoring)
- [Email Digest](#-email-digest)
- [Projectstructuur](#-projectstructuur)
- [Setup & Installatie](#️-setup--installatie)
- [Gebruik](#-gebruik)
- [Fixes & Evolutie](#-fixes--evolutie)
- [State & Deduplicatie](#-state--deduplicatie)
- [Error Handling](#-error-handling)

---

## 🔁 Pipeline Overzicht

```
fase 1: COLLECT
  ┌───────────┐ ┌────────┐ ┌───────────┐ ┌──────────┐ ┌───────────┐
  │  Immoweb  │ │ Zimmo  │ │ Immoscoop │ │ Immovlan │ │ 2dehands  │
  │  search   │ │ search │ │ search    │ │ search   │ │ search    │
  │  per stad │ │ multi  │ │ multi     │ │ per stad │ │ per stad  │
  └─────┬─────┘ └───┬────┘ └─────┬─────┘ └────┬─────┘ └─────┬─────┘
        │           │            │            │             │
        └───────────┼────────────┼────────────┼─────────────┘
                    │            │            │
                    ▼            ▼            ▼
              /tmp/domus-collected-ids.json (enkel ID's)

fase 2: BATCH SCRAPE (parallel, batches van 10)
          detailpagina's → /tmp/domus-batches/*.json
      ┌─────────┴──────────┐
      │  enrich per bron:  │
      │  • EPC, perceel,   │
      │    woonoppervlakte │
      │  • beschrijvingen  │
      │  • echte foto's    │
      │  • status badges   │
      └────────────────────┘

fase 3: PROCESS (merge → filter → dedup → score → quality)
      ┌─────────────┐
      │  appartement │──✗──
      │  filter      │
      └──────┬──────┘
      ┌──────▼──────┐
      │  stad       │──✗── excluded cities
      │  filter     │
      └──────┬──────┘
      ┌──────▼──────┐
      │  status     │── detect under_option / life_annuity
      │  detectie   │
      └──────┬──────┘
      ┌──────▼───────┐
      │  enrich ALL  │── detail pages voor EPC/perceel/foto's
      │  platforms   │
      └──────┬───────┘
      ┌──────▼────────────┐
      │  Quality filter   │── EPC A-C / perceel ≥450 / woonopp ≥115
      └──────┬────────────┘
      ┌──────▼──────┐
      │  Dedup      │── merge zelfde huis uit meerdere bronnen
      └──────┬──────┘
      ┌──────▼──────────┐
      │  DeepSeek text  │── score 1-10 op moderniteit
      │  scoring        │
      └──────┬──────────┘
      ┌──────▼──────────────┐
      │  Gemini 2.5 Flash   │── foto scoring (top 25)
      │  Lite photo scoring │
      └──────┬──────────────┘
      ┌──────▼──────┐
      │  Weging:    │
      │  text×0.6 + │
      │  photo×0.4  │
      └──────┬──────┘
      ┌──────▼────────┐
      │  Filter       │── alleen score > 5.0
      │  score > 5.0  │
      └──────┬────────┘

fase 4: EMAIL
      ┌──────▼────────┐
      │  Base64 embed │── afbeeldingen inline
      │  van fotos    │
      └──────┬────────┘
      ┌──────▼────────┐
      │  HTML digest  │── max 50 listings, scores, badges
      │  via SMTP SSL │
      └───────────────┘
```

---

## 🎯 Doelposten & Zoekcriteria

### Doelsteden (8 steden, correcte postcodes)

| Stad | Postcode | Opmerking |
|------|----------|-----------|
| **Lier** | 2500 | Hoofdstad regio |
| **Ranst** | 2520 | Deel Ranst |
| **Broechem** | 2520 | Deel Ranst |
| **Emblem** | 2520 | Deel Ranst |
| **Vremde** | 2531 | Deel Ranst |
| **Wommelgem** | 2160 | Antwerpse rand |
| **Kessel** | 2560 | Deel Nijlen |
| **Geel** | 2440 | Kempen |

### Uitgesloten steden
Koningshooikt, Oelegem, Nijlen, Bevel — komen voor in dezelfde postcodes maar liggen buiten het interessegebied.

### Configureerbare zoekcriteria (`config.py` / `.env`)

| Parameter | Standaard | Via env var | Omschrijving |
|-----------|-----------|-------------|--------------|
| Min. prijs | €400.000 | `MIN_PRICE` | Laagste vraagprijs |
| Max. prijs | €700.000 | `MAX_PRICE` | Hoogste vraagprijs |
| Min. slaapkamers | 3 | `MIN_BEDROOMS` | Minimum aantal slaapkamers |
| Min. woonoppervlakte | 115 m² | `MIN_LIVING_SURFACE` | Bewoonbare oppervlakte |
| Min. perceeloppervlakte | 450 m² | `MIN_LOT_SURFACE` | Grondoppervlakte |
| EPC labels | A, A+, A++, B, C | (hardcoded `EPC_ALLOWED`) | Energieprestatie |

---

## 🕷️ De 5 Scrapers

### 1. Immoweb

**Methode:** Next.js API endpoint (`https://www.immoweb.be/nl/zoekertje/...`) + JSON-LD structured data parsing. HTML fallback via BeautifulSoup als de JSON-LD ontbreekt.

**Aanpak:**
- Itereert over alle doelpostcodes (`TARGET_POSTALS`)
- Gebruikt `curl_cffi` via subprocess (beveiligd tegen SSL-hangs)
- Extraheert ID's uit zoekresultaten
- **Enrich:** detailpagina voor EPC-label, perceeloppervlakte, woonoppervlakte, beschrijving, status badges, echte foto's
- **Fix:** `raw_decode` voor ongeldige JSON in Next.js data; directe verrijking i.p.v. conditionele

**Fijngevoeligheid:** Buitenlandse adressen (bijv. Wallonië, Brussel) komen soms in de zoekresultaten — filtert op doelsteden.

### 2. Zimmo

**Methode:** HTML parsing van zoekpagina's + detailpagina's. Gebruikt BeautifulSoup.

**Aanpak:**
- Multi-postcode search (meerdere postcodes tegelijk via de Zimmo zoek-URL)
- Extraheert listing-URL's uit de zoekresultaten
- **Enrich:** detailpagina voor straat+huisnummer, EPC-label, beschrijving, foto's
- Filtert appartementen via `property_type` + backstop op titel/beschrijving
- Buitenlandse resultaten worden uitgefilterd

**Fijngevoeligheid:** Zimmo toont soms eigendommen in Wallonië onder postcode 2520 — de stad-filter vangt dit op.

### 3. Immoscoop

**Methode:** Search API + JSON-LD detailpagina parsing.

**Aanpak:**
- Multi-postal search via de Immoscoop zoek-API
- Extraheert gestructureerde data uit detailpagina's
- **Enrich:** detailpagina voor echte foto's (i.p.v. placeholders), EPC, oppervlakte, slaapkamers, prijs
- **Fix:** detailpagina foto's ingebouwd (placeholder vervangen door echte afbeeldingen)

**Fijngevoeligheid:** Postcode validatie op alle resultaten (alleen `TARGET_POSTALS`); appartementen via titel/beschrijving detectie.

### 4. Immovlan

**Methode:** Twee-fase aanpak: eerst search ID's scrapen via search API → daarna detailpagina's ophalen in parallelle batches.

**Aanpak:**
- **Fase 1:** Search per stad via de Immovlan search endpoint → verzamelt ID's van zoekertjes
- **Fase 2:** 19 parallelle batches van 5 detailpagina's (max 60s per batch)
- Meta description parsing voor oppervlakte, EPC, prijs
- **Enrich:** detailpagina voor volledig adres, beschrijving, EPC, oppervlakte, perceelgrootte, foto's
- City-only filter: adressen buiten ACCEPT_CITIES worden eruit gefilterd

### 5. 2dehands / 2ememain

**Methode:** Next.js SPA — `__NEXT_DATA__` JSON parsing uit de pagina.

**Aanpak:**
- Per-stad search pages
- Extraheert listing data uit de Next.js build-time JSON (ingebed in de HTML)
- Categorie filter: enkel `categoryId: 1041` (huizen te koop)
- **Enrich:** detailpagina voor ontbrekende velden (EPC, beschrijving, foto's)
- **Fix:** ä-land filter — uitzonderingen voor speciale tekens in adressen; buitenland filter
- Apartementen detectie via property_type + titel keywords

---

## 🔍 Filters

| Filter | Waar | Hoe | Detail |
|--------|------|-----|--------|
| **Appartementen** | `process_all.py` | `property_type == "apartment"` + backstop op titel/description | Eerst via property_type, dan nog eens met keyword check op titel |
| **Stad (accept)** | `process_all.py` | `filter_by_city()` — adres/URL matcht met `ACCEPT_CITIES` | Configuratie: `CITY_ACCEPT_LIST` of default `TARGET_CITIES` |
| **Stad (exclude)** | `process_all.py` | `EXCLUDE_CITIES_FINAL` gecheckt op adres + URL | Vangt Koningshooikt, Oelegem, Nijlen, Bevel |
| **Status (onder optie / lijfrente)** | `process_all.py` | `detect_status()` — detectie uit beschrijving + titel + URL | Badges worden in de email getoond maar niet uitgefilterd |
| **EPC** | `process_all.py` | Quality filter: alleen als EPC bekend is én niet in `EPC_ALLOWED` | Alleen **bekende** EPC-waarden filteren — onbekende EPC krijgt benefit of the doubt |
| **Perceel** | `process_all.py` | Quality filter: `lot_surface_m2 < MIN_LOT_SURFACE` | Alleen als perceel bekend is — onbekend = niet filteren |
| **Woonoppervlakte** | `process_all.py` | Quality filter: `surface_m2 < MIN_LIVING_SURFACE` | Alleen als oppervlakte bekend is |
| **Score drempel** | `process_all.py` | `final_score > 5.0` | Alles onder 5.0 wordt niet gemaild |
| **Universele enrich** | `process_all.py` | `_enrich_from_description()` | Regex-based: extraheert EPC, perceel, woonopp, slaapkamers uit beschrijvingstekst — vangt wat gestructureerde data mist |

---

## 🤖 AI Scoring

### Text Scoring — DeepSeek Chat (directe API)

- **Model:** `deepseek-chat` via `api.deepseek.com`
- **Prompt:** Beoordeelt woning op **moderniteit** en **afwerkingsgraad** (1-10)
- **Positieve indicatoren:** gerenoveerd, nieuwbouw, moderne keuken, goede EPC, strakke afwerking, design
- **Negatieve indicatoren:** te renoveren, originele staat, oudere keuken, slechte EPC (D/E/F), drukke weg
- **Reasoning:** altijd in het Nederlands (met geforceerde vertaling van Engelse output)
- **Timeouts:** 30s per listing, max 2 retries bij rate limiting
- **Parallel:** 10 parallelle threads

### Photo Scoring — Gemini 2.5 Flash Lite (via OpenRouter)

- **Model:** `google/gemini-2.5-flash-lite` via OpenRouter API
- **Methode:** Alle foto's van 1 huis in 1 API call (max 8 foto's — zelfde prijs als 1 foto)
- **Toepassing:** Enkel de **top 25** listings (na text scoring) worden visueel gescoord
- **EPC D/E/F/G overslaan:** geen credits verspillen aan slechte energielabels — die hebben sowieso slechte fotos
- **Placeholder detectie:** URLs met "placeholder", "no-image", "default" worden overgeslagen
- **Max retries:** 1 (niet blijven proberen bij falen)
- **Geen rate limit delay** — OpenRouter heeft geen limietproblemen

### Final Score

```
final_score = text_score × 0.6 + photo_score × 0.4
```

- Alleen listings met **final_score > 5.0** worden in de digest opgenomen
- Photo score kan `None` zijn (geen foto's, slechte EPC, API fout) — dan telt enkel text score
- Score wordt afgerond op 1 decimaal

### Score drempel per ranking

| Score | Emoji | Betekenis |
|-------|-------|-----------|
| ≥ 8.0 | 🔥 | Top — moderne, afgewerkte woning |
| ≥ 6.0 | ✨ | Goed — recent vernieuwd of goede staat |
| ≥ 4.0 | 🏠 | Gemiddeld — bewoonbaar maar verouderd |
| < 4.0 | ⚠️ | Slecht — renovatie nodig |

---

## 📧 Email Digest

### Techniek
- **SMTP:** eigen SMTP server (bv. Gmail, SendGrid)
- **Format:** Rijke HTML met inline CSS (geen externe dependencies)
- **Encoding:** SMTPUTF8 — ondersteunt alle speciale tekens
- **Valt terug** naar plain-text als HTML niet werkt

### Digest inhoud
- **Header:** datum, aantal nieuwe huizen
- **Statistieken:** aantal, prijsrange, topscore
- **Listing cards** (max 50):
  - Score emoji + cijfer (bijv. 🔥 8.3/10)
  - Platform badge (Immoweb / Zimmo / Immoscoop / Immovlan / 2dehands)
  - Status badge (⚖️ Onder optie / 🔄 Lijfrente)
  - Thumbnail (180px breed, base64 embedded — omzeilt CDN hotlink blocking)
  - Prijs (€450.000), adres, details (slaapkamers, oppervlakte, perceel, EPC)
  - Korte AI reasoning (max 100 chars)
  - "Bekijk ↗" knop naar detailpagina

### Weekly digest (vrijdag)
- Aparte email met top 10 van de week
- Enkel als er minstens 1 nieuwe listing is geweest
- Markeert week als "verzonden" in history om dubbele mails te voorkomen

---

## 📁 Projectstructuur

```
domus-quaesitor/
│
├── config.py                    # Centrale config (env vars + defaults)
├── main.py                      # Pipeline entry point (huidige orchestrator)
├── orchestrator.py              # Legacy orchestrator (niet meer in gebruik)
├── storage.py                   # History/seen/dedup management
├── location_filter.py           # Stad accept/exclude filter
├── _check_run.py                # Quick sanity check bij opstart
├── .env                         # Lokale config (niet gecommit)
├── .env.example                 # Voorbeeld env vars
├── requirements.txt             # Python dependencies
├── run_hunter.sh                # Productie runner (cron)
├── run_local.sh                 # Lokale test runner
│
├── scrapers/
│   ├── base.py                  # Listing dataclass + BaseScraper (curl_cffi subprocess)
│   ├── curl_runner.py           # Subprocess wrapper voor curl_cffi (anti-hang)
│   ├── immoweb.py               # Immoweb scraper (JSON-LD + HTML fallback)
│   ├── immoscoop.py             # Immoscoop scraper (search API + JSON-LD)
│   ├── zimmo.py                 # Zimmo scraper (HTML parsing)
│   ├── immovlan.py              # Immovlan scraper (search → detail, 2-phase)
│   └── tweedehands.py           # 2dehands/2ememain scraper (Next.js __NEXT_DATA__)
│
├── scoring/
│   ├── text_scorer.py           # DeepSeek text scoring (directe API)
│   └── photo_scorer.py          # OpenRouter/Gemini 2.5 Flash Lite photo scoring
│
├── email_sender/
│   ├── __init__.py
│   └── digest.py                # HTML builder + SMTP sender
│
├── phases/
│   ├── collect.py               # Fase 1: alle 5 scrapers parallel — enkel ID's
│   ├── collect_immoweb.py       # Immoweb-specifieke collect (legacy)
│   ├── extract_immovlan_ids.py  # Immovlan search ID extractie (legacy)
│   ├── scrape_batch.py          # Fase 2: detailpagina's in batches van 10
│   ├── process.py               # Fase 3: merge + dedup + score + filter (legacy)
│   ├── process_all.py           # Huidige Fase 3: alles-in-1 pipeline
│   ├── email_sender.py          # Fase 4: email versturen
│   ├── embed_images.py          # Base64 embed voor CDN-bypass
│   ├── run_immoweb.py           # Immoweb batch runner (legacy)
│   ├── run_zimmo.py             # Zimmo batch runner (legacy)
│   ├── run_immoscoop.py         # Immoscoop batch runner (legacy)
│   ├── run_immovlan.py          # Immovlan batch runner (legacy)
│   ├── run_2dehands.py          # 2dehands batch runner (legacy)
│   └── _schema.py               # Data schema definitie
│
└── data/
    ├── seen_listings.json       # Al gemailde listing-IDs
    └── listing_history.json     # Volledige geschiedenis (scores, prijs, EPC, datums)
```

---

## 🛠️ Setup & Installatie

### 1. Vereisten

- Python 3.10+
- pip
- (Optioneel) cron voor automatische runs

### 2. Clone & virtual environment

```bash
git clone <repo-url> domus-quaesitor
cd domus-quaesitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configuratie

```bash
cp .env.example .env
```

Vul `.env` in met je eigen waarden:

```bash
# --- Email (verplicht) ---
SMTP_FROM=jouw-email@example.com
SMTP_PASSWORD=jouw-smtp-wachtwoord
EMAIL_TO=ontvanger@voorbeeld.be
EMAIL_CC=optioneel@example.com

# --- AI Scoring (optioneel — zonder = unranked) ---
DEEPSEEK_API_KEY=sk-your-deepseek-key
OPENROUTER_API_KEY=sk-or-v1-your-key-here

# --- Zoekcriteria (optioneel — defaults in config.py) ---
MIN_PRICE=400000
MAX_PRICE=700000
MIN_BEDROOMS=3
MIN_LIVING_SURFACE=115
MIN_LOT_SURFACE=450

# --- Steden (optioneel) ---
CITY_ACCEPT_LIST=Geel, Lier, Ranst, Broechem, Emblem, Vremde, Wommelgem, Kessel
CITY_EXCLUDE_LIST=Koningshooikt, Oelegem, Nijlen, Bevel
```

### 4. Belangrijke omgevingsvariabelen

| Variable | Voorbeeld | Verplicht | Omschrijving |
|----------|-----------|-----------|-------------|
| `SMTP_FROM` (`GMAIL_FROM`) | `jij@example.com` | ✅ | Afzender email |
| `SMTP_PASSWORD` (`GMAIL_APP_PASSWORD`) | `***` | ✅ | SMTP wachtwoord |
| `EMAIL_TO` | `ontvanger@voorbeeld.be` | ✅ | Ontvanger |
| `EMAIL_CC` | `partner@example.com` | ⬜ | CC ontvanger |
| `SMTP_HOST` | `smtp.voorbeeld.com` | ⬜ | SMTP server |
| `SMTP_PORT` | `465` | ⬜ | SMTP poort (default: 465) |
| `SMTP_TLS` | `0` | ⬜ | STARTTLS i.p.v. SSL direct |
| `DEEPSEEK_API_KEY` | `sk-...` | ⬜ | Voor AI text scoring |
| `OPENROUTER_API_KEY` | `sk-or-...` | ⬜ | Voor AI photo scoring |

---

## 🚀 Gebruik

### Volledige pipeline

```bash
cd domus-quaesitor
source venv/bin/activate
python3 main.py
```

Dit doorloopt:
1. **Fase 1 — Collect:** 5 scrapers parallel → ID's naar `/tmp/domus-collected-ids.json`
2. **Fase 2 — Batch scrape:** detailpagina's in batches van 10 → `/tmp/domus-batches/*.json`
3. **Fase 3 — Process:** merge, filter, dedup, DeepSeek scoring, Gemini photo scoring, quality filter
4. **Fase 4 — Email:** Base64 embed + HTML digest + SMTP versturen

### Flags

```bash
# Alles scrapen/scoren maar GEEN email
python3 main.py --dry-run

# Alleen scrapen (geen scoring, geen email)
python3 main.py --scrape-only

# Skip scrapen, stuur wekelijks overzicht (vrijdag)
python3 main.py --weekly-only

# Test email sturen zonder te scrapen
python3 main.py --test-email

# Full dump — negeert geziene geschiedenis, mailt ALLE matches
python3 main.py --full-dump
```

### Automatiseren met cron

```bash
# Elke ochtend om 7:00
crontab -e

0 7 * * * cd /pad/naar/domus-quaesitor && ./run_hunter.sh >> /tmp/domus-cron.log 2>&1
```

`run_hunter.sh` laadt automatisch `.env`, activeert de virtualenv, logt naar `logs/hunt_YYYY-MM-DD_HHMMSS.log`, en ruimt logs ouder dan 30 dagen op.

---

## 🔧 Fixes & Evolutie

### Opgeloste problemen (chronologisch)

| Fix | Omschrijving | Betreft |
|-----|-------------|---------|
| **Directe verrijking** | Enrich ALL listings i.p.v. conditioneel — detailpagina's worden voor elk platform apart opgehaald | `process_all.py` |
| **Immoweb JSON raw_decode** | Immoweb Next.js data bevat soms ongeldige JSON — `raw_decode` vangt de eerste geldige structuur | `immoweb.py` |
| **Zimmo HTML parsers** | Zimmo gebruikt geen API — volledige HTML parsing met BeautifulSoup | `zimmo.py` |
| **ä-land filter** | Adressen met speciale tekens (ä, é, è) vielen er soms uit — gefixt met correcte Unicode handling | `tweedehands.py` |
| **EPC regex A-F → A+** | Regex matchte enkel A-F, miste A+/A++ — uitgebreid naar alle EPC varianten | `process_all.py`, `immoweb.py` |
| **Immoscoop detail foto's** | Search resultaten geven placeholders — detailpagina haalt echte foto's op | `immoscoop.py` |
| **2dehands buitenland filter** | Soms resultaten uit Wallonië/Brussel onder postcode-zoekopdracht — gefilterd op stad | `tweedehands.py` |
| **curl_cffi subprocess** | curl_cffi kan hangen in SSL code — via apart subprocess met SIGKILL mogelijk | `base.py`, `curl_runner.py` |
| **Photo scorer EPC skip** | EPC D/E/F/G overslaan voor photo scoring — geen credits verspillen | `photo_scorer.py` |
| **Score > 5.0 filter** | Lage scores eruit filteren voor propere digest | `process_all.py` |
| **Universele enrich uit beschrijving** | Regex backstop voor EPC/perceel/woonopp/slaapkamers die in beschrijving staan maar niet in gestructureerde data | `process_all.py` |
| **Merge + dedup** | Zelfde huis uit meerdere bronnen (bv. Immoweb + 2dehands) = 1 entry met beste data | `process_all.py` |

---

## 💾 State & Deduplicatie

### `data/listing_history.json`
- Rijke geschiedenis per listing: fingerprins, scores, prijs, EPC, platform, URL
- Wordt bij elke run bijgewerkt: `first_seen_at`, `last_seen_at`, `sent_dates`
- Vingerafdruk op basis van **adres + prijs + slaapkamers + oppervlakte** (of straatnaam als er geen huisnummer is)

### `data/seen_listings.json`
- Al gemailde listing-IDs per platform
- Voorkomt dat dezelfde listing in meerdere dagelijkse digests verschijnt

### Weekly digest
- Aparte history tracking (ISO week key)
- Enkel op vrijdag
- Top 10 van die week (best scorend, ongeacht of al gemaild)
- Markeert week als "verzonden" om dubbele wekelijkse mails te voorkomen

---

## ⚠️ Error Handling

| Scenario | Gedrag |
|----------|--------|
| **Scraper 403 / blocked / timeout** | Skip die scraper — rest gaat door |
| **DeepSeek timeout (30s)** | Fallback score 5.0 — "unranked" |
| **OpenRouter / Gemini error** | Geen photo scoring — alleen text score in eindcijfer |
| **SMTP fail** | Logt error, slaat data lokaal op in `/tmp/domus-email-fallback.txt` |
| **Geen nieuwe listings** | Geen email — logt "nothing new" |
| **Één batch scrape faalt** | Andere batches gaan gewoon door |
| **Geen foto's** | Geen photo scoring voor die listing — text score is eindscore |
| **Geen DEEPSEEK_API_KEY** | Alle listings score 5.0 — geen AI ranking |

---

## 📜 License

MIT — doe ermee wat je wil. Veel succes met de jacht! 🍀

---

*Laatste update: juni 2026 — v4.1 (foto scoring, quality filter, 5 scrapers, parallelle batches)*
