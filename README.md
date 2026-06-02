# 🏠 Domus-Quaesitor

**Automatische huizenjager voor de Belgische koopmarkt.**

Scrapet te-koop-listing van Immoweb, Zimmo, Immoscoop, Immovlan en 2dehands, filtert op jouw criteria (prijs, slaapkamers, oppervlakte, EPC, locatie), scoort ze met AI (DeepSeek + Gemini), en mailt elke dag een gerankeerde HTML-digest.

> **Domus-Quaesitor** = Latijn voor 'huizenzocker'. Koop alleen, geen huur, geen appartementen.

---

## Hoe het werkt

```
┌──────────┐ ┌────────┐ ┌──────────┐ ┌──────────┐ ┌─────────┐
│ Immoweb  │ │ Zimmo  │ │Immoscoop │ │Immovlan  │ │ 2dehands│
│ Next.js  │ │ detail │ │ multi-   │ │ IDs →    │ │ search  │
│ API+JSON │ │ enrich │ │ postal   │ │ detail   │ │ +detail │
└────┬─────┘ └───┬────┘ └────┬─────┘ └────┬─────┘ └────┬────┘
     │           │           │           │            │
     └───────────┼───────────┼───────────┼────────────┘
                 │           │           │
          ┌──────▼───────────▼──────────▼──────┐
          │  Parallelle batches (5 scrapers)    │
          │  Elk in apart subproces (max 30s)   │
          └────────────────┬───────────────────┘
                           │
                    ┌──────▼──────┐
                    │  Merge &    │
                    │  Dedup      │
                    │  (history)  │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │                         │
       ┌──────▼──────┐          ┌──────▼────────┐
       │  DeepSeek   │          │  OpenRouter /  │
       │ Text Score  │          │  Gemini Flash  │
       │ (beschrijv.)│          │  Photo Score   │
       └──────┬──────┘          └──────┬─────────┘
              │                         │
              └────────────┬────────────┘
                           │
                    ┌──────▼──────┐
                    │  Weeg &     │
                    │  Rank       │
                    │ text×0.6 +  │
                    │ photo×0.4   │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  Filter     │
                    │  score>5.0  │
                    └──────┬──────┘
                           │
                    ┌──────▼──────┐
                    │  HTML       │
                    │  Digest     │
                    │  via OVH    │
                    │  SMTP       │
                    └─────────────┘
```

## Doelposten

| Stad | Postcode |
|------|----------|
| Geel | 2440 |
| Lier | 2500 |
| Ranst / Emblem | 2520 |
| Broechem / Vremde | 2531 |
| Wommelgem | 2160 |
| Kessel | 2560 |

## Zoekcriteria (configureerbaar)

| Parameter | Standaard | Via env var |
|-----------|-----------|-------------|
| Min. prijs | €400.000 | `MIN_PRICE` |
| Max. prijs | €700.000 | `MAX_PRICE` |
| Min. slaapkamers | 3 | `MIN_BEDROOMS` |
| Min. woonopp. | 115 m² | `MIN_LIVING_SURFACE` |
| Min. perceel | 450 m² | `MIN_LOT_SURFACE` |
| EPC labels | A, A+, A++, B, C | (hardcoded in `config.py`) |
| Geen renovatieprojecten | Ja | `SKIP_RENOVATION` |
| Doelsteden | Geel, Lier, Ranst, Emblem, Broechem, Vremde, Wommelgem, Kessel | `CITY_ACCEPT_LIST` |
| Uitgesloten steden | Koningshooikt, Oelegem, Nijlen, Bevel | `CITY_EXCLUDE_LIST` |

## De 5 Scrapers

### 1. Immoweb
- Next.js API endpoint + JSON-LD parsing
- HTML fallback via BeautifulSoup
- Itereert over alle doelpostcodes
- Filtert `property_type = "house"`

### 2. Immoscoop
- Multi-postal search via zoek-API
- JSON-LD detail page parsing
- Postcode validatie (alleen `TARGET_POSTALS`)
- Herkent appartementen via titel/description

### 3. Zimmo
- Multi-postal search via zoekpagina
- Detail page enrich met extra velden (EPC, perceelopp.)
- Apartementen eruit via `property_type` + backstop

### 4. Immovlan
- **Two-phase**: eerst search IDs scrapen → daarna detailpages ophalen
- Meta description parsing voor opp/EPC/prijs
- 19 parallelle batches van 5 detailpages

### 5. 2dehands / 2ememain
- Next.js SPA — `__NEXT_DATA__` JSON parsing
- Per-stad search pages
- Detail page enrich voor ontbrekende velden
- Categorie filter: houses (1041)

## AI Scoring

### Text Scorer — DeepSeek
- **Model:** `deepseek-chat` (directe API, niet via router)
- Analyseert de beschrijving op:
  - Modern & afgewerkt interieur
  - Kwaliteit van afwerking
  - EPC-label
  - Prijs-kwaliteitverhouding
- Timeout op 30s per listing — voorkomt hang
- Fallback: score 5.0 als AI niet beschikbaar is

### Photo Scorer — OpenRouter / Gemini
- **Model:** Gemini 2.5 Flash Lite via OpenRouter
- Alle foto's van 1 huis in 1 API call (max 8 foto's)
- Enkel top 25 listings scoren (na text scoring)
- EPC D/E/F/G worden overgeslagen
- Fallback: score 5.0 — geen blokkade

### Final Score
```
final_score = text_score × 0.6 + photo_score × 0.4
```

Alleen listings met **score > 5.0** worden in de digest opgenomen.

## Filters

| Filter | Waar | Wat |
|--------|------|-----|
| Appartementen | `property_type` + backstop op titel/description | Alle appartementen eruit |
| Postcode validatie | Immoscoop | Alleen `TARGET_POSTALS` |
| Stad exclude | `location_filter.py` | `EXCLUDE_CITIES` gecheckt op titel + adres + description |
| Score drempel | `process_all.py` | Alleen `final_score > 5.0` |
| Renovatie | `config.py` | `SKIP_RENOVATION=true` filtert keywords in titel/description |

## Email — OVH SMTP

- **Host:** `ssl0.ovh.net:465` (SMTP-SSL)
- **Format:** Rijk HTML digest met:
  - Score emoji's (🔥 ≥8, ✨ ≥6, 🏠 ≥4, ⚠️ rest)
  - Inline foto's (base64 embedded)
  - EPC badge (kleurgecodeerd)
  - Score breakdown (text / photo / final)
  - Platform badge (Immoweb, Zimmo, etc.)
- **Geen herhaling:** `seen_listings.json` + `listing_history.json` voorkomen dat je 2× dezelfde mail krijgt
- **Weekly top 10:** Vrijdag aparte samenvatting van de beste 10 van de week

## Projectstructuur

```
domus-quaesitor/
├── config.py                  # Centrale config (env vars + defaults)
├── main.py                    # Legacy orchestrator entry point
├── orchestrator.py            # Huidige batch orchestrator
├── storage.py                 # History/seen/dedup management
├── location_filter.py         # Stad accept/exclude filter
├── _check_run.py              # Quick sanity check
├── .env                       # Lokale config (niet gecommit)
├── .env.example               # Voorbeeld env vars
├── requirements.txt           # Python dependencies
├── run_hunter.sh              # Runner script
├── run_local.sh               # Lokale runner
│
├── scrapers/
│   ├── base.py                # Listing dataclass + BaseScraper
│   ├── immoweb.py             # Immoweb scraper
│   ├── immoscoop.py           # Immoscoop scraper
│   ├── zimmo.py               # Zimmo scraper
│   ├── immovlan.py            # Immovlan scraper
│   └── tweedehands.py         # 2dehands/2ememain scraper
│
├── scoring/
│   ├── text_scorer.py         # DeepSeek text scoring
│   └── photo_scorer.py        # OpenRouter/Gemini photo scoring
│
├── email_sender/
│   └── digest.py              # HTML builder + OVH SMTP sender
│
├── phases/
│   ├── collect.py             # Fase 1: collect IDs (alle scrapers)
│   ├── collect_immoweb.py     # Immoweb-specifieke collect
│   ├── extract_immovlan_ids.py # Immovlan search IDs
│   ├── scrape_batch.py        # Batch detailpages
│   ├── run_immoweb.py         # Immoweb batch runner
│   ├── run_zimmo.py           # Zimmo batch runner
│   ├── run_immoscoop.py       # Immoscoop batch runner
│   ├── run_immovlan.py        # Immovlan batch runner
│   ├── run_2dehands.py        # 2dehands batch runner
│   ├── process_all.py         # Fase 3: merge + dedup + score + filter
│   ├── process.py             # Legacy process
│   ├── email_sender.py        # Fase 4: email versturen
│   ├── embed_images.py        # Base64 embed voor HTML
│   └── _schema.py             # Data schema
│
└── data/
    ├── seen_listings.json     # Al gemailde IDs
    └── listing_history.json   # Volledige geschiedenis
```

## Setup

### 1. Clone & install

```bash
git clone <repo> domus-quaesitor
cd domus-quaesitor
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configuratie

Kopieer `.env.example` naar `.env` en vul aan:

```bash
cp .env.example .env
# Bewerk .env met je voorkeuren
```

Belangrijkste env vars:

| Var | Voorbeeld | Verplicht |
|-----|-----------|-----------|
| `SMTP_FROM` | `jij@example.com` | ✅ |
| `SMTP_PASSWORD` | `jouw-ovh-wachtwoord` | ✅ |
| `EMAIL_TO` | `mail@holie.be` | ✅ |
| `DEEPSEEK_API_KEY` | `sk-...` | ⬜ (zonder = unranked) |
| `OPENROUTER_API_KEY` | `sk-or-...` | ⬜ (zonder = geen foto scoring) |
| `GEMINI_API_KEY` | `AIza...` | ⬜ |
| `MIN_PRICE` | `400000` | ⬜ (default: 400k) |
| `MAX_PRICE` | `700000` | ⬜ (default: 700k) |
| `MIN_BEDROOMS` | `3` | ⬜ (default: 3) |
| `CITY_ACCEPT_LIST` | `Geel, Lier, Ranst` | ⬜ (default: alle doelsteden) |
| `CITY_EXCLUDE_LIST` | `Koningshooikt, Oelegem` | ⬜ |

### 3. Draaien

```bash
# Volledige pipeline
python3 orchestrator.py

# Dry run (scrapen + scoren, geen email)
python3 main.py --dry-run

# Alleen scrapen (geen scoring, geen email)
python3 main.py --scrape-only

# Alleen wekelijks overzicht (vrijdag)
python3 main.py --weekly-only

# Test email (zonder te scrapen)
python3 main.py --test-email

# Full dump (ook al geziene listings opnieuw mailen)
python3 main.py --full-dump
```

### 4. Automatiseren (cron)

Deze run draait lokaal via cron — geen GitHub Actions of launchd.

```bash
# Elke ochtend om 7:00
crontab -e
0 7 * * * cd /pad/naar/domus-quaesitor && source venv/bin/activate && python3 orchestrator.py >> /tmp/domus-cron.log 2>&1
```

## State & Deduplication

- `data/seen_listings.json` — lijst van "al gemailde" listing-IDs
- `data/listing_history.json` — rijke geschiedenis (scores, EPC, prijs) voor weekly top 10
- Een listing wordt pas als "gezien" gemarkeerd nadat de digest-effectief is gemaild
- Vrijdag-run stuurt een aparte weekly top-10 email

## Error Handling

| Scenario | Gedrag |
|----------|--------|
| Scraper 403/blocked | Skip die scraper, rest gaat door |
| DeepSeek timeout (30s) | Fallback score 5.0 |
| OpenRouter/Gemini unavailable | Geen photo scoring, alleen text score |
| SMTP fail | Logt error, slaat data lokaal op |
| Geen nieuwe listings | Geen email, logt "nothing new" |
| Één batch faalt | Andere batches gaan gewoon door |

---

**License:** MIT — doe ermee wat je wil. Veel succes met de jacht! 🍀
