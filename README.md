# 🏠 Apartment Hunter

Automated apartment hunting for Belgium. Scrapes rental listings from Immoweb, Zimmo, and Immoscoop, filters them according to your custom criteria, AI-scores them for "modern & clean" vibes, avoids re-sending duplicates, emails a ranked daily digest, and sends a Friday top-10 weekly recap.

## How it works

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Immoweb   │     │    Zimmo     │     │  Immoscoop  │
│  Scraper    │     │   Scraper    │     │   Scraper   │
└──────┬──────┘     └──────┬───────┘     └──────┬──────┘
       │                   │                    │
       └───────────────────┼────────────────────┘
                           │
                    ┌──────▼──────┐
                    │ Deduplicate │
                    │ (seen.json) │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              │                         │
       ┌──────▼──────┐          ┌──────▼──────┐
       │  Groq AI    │          │  OpenRouter  │
       │ Text Score  │          │ Photo Score  │
       │ (Llama 3.3) │          │ (free vision)│
       └──────┬──────┘          └──────┬──────┘
              │                         │
              └────────────┬────────────┘
                           │
                    ┌──────▼──────┐
                    │  Rank &     │
                    │  Email      │
                    │  Digest     │
                    └─────────────┘
```

## Configurable Search Criteria

All parameters can be configured easily through an interactive setup wizard or directly via the `.env` file:

- **Location:** Any Belgian city and postal code (e.g., Ghent 9000, Antwerp 2000, etc.)
- **Price Range:** Custom minimum and maximum monthly rent constraints
- **Bedrooms:** Minimum bedrooms count filter
- **Location Proximity (Optional):** Define preferred keywords (e.g. near station) and excluded keywords (e.g. far areas) to filter local listings dynamically.

### AI Scoring Priorities
1. **Modern & clean** (top priority) — renovated, contemporary finishes
2. **EPC label** — A or B preferred
3. **Overall quality** — photos + description analysis

## Hosting Reality

GitHub-hosted Actions runners are a poor fit for these real-estate sites. They run from public cloud IP ranges and fresh machine fingerprints, which makes 403 blocks on Immoweb and Zimmo very likely.

Free options that do make sense:
- Run it locally on your own machine with `launchd` or cron.
- Run it as a GitHub **self-hosted** runner on your own machine. That is still your own PC, just triggered by GitHub.

If you want the most reliable free setup, use the local runner on your own computer.

## Quick Setup

### 1. Fork this repo

Click "Fork" on GitHub to create your own copy.

### 2. Run the Interactive Setup Wizard

Run the interactive setup tool to generate your configuration (`.env` file) automatically:

```bash
python3 setup.py
```

The wizard will guide you through:
- Target search city and postal code
- Budget range (Min/Max price)
- Minimum bedrooms
- Proximity location filter (Yes/No with custom keywords)
- Gmail SMTP sender/recipient configuration
- Groq / OpenRouter AI Keys (Optional)

### 3. Get optional AI API Keys

If you want to use the AI quality ranking features, sign up for free tier keys:

| Service | Free? | Sign up |
|---------|-------|---------|
| **Groq** | ✅ Free tier | [console.groq.com](https://console.groq.com) |
| **OpenRouter** | ✅ Free tier | [openrouter.ai](https://openrouter.ai) |
| **Gmail App Password** | ✅ Free | Google Account → Security → 2FA → App Passwords |

### 4. Set GitHub Secrets (If running via GitHub Action)

If you configure a self-hosted runner, go to your repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret** and add:

- `TARGET_CITY`
- `TARGET_POSTAL_CODE`
- `MIN_PRICE`
- `MAX_PRICE`
- `MIN_BEDROOMS`
- `GROQ_API_KEY`
- `OPENROUTER_API_KEY`
- `GMAIL_APP_PASSWORD`
- `GMAIL_FROM`
- `EMAIL_TO`
- `EMAIL_CC`

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Run the setup wizard to create/modify your settings
python3 setup.py

# Full run
python main.py

# Dry run (scrapes and prints results to console, skips email)
python main.py --dry-run

# Scrape only (skips AI scoring and email)
python main.py --scrape-only

# Weekly summary only (sends top-10 recap)
python main.py --weekly-only
```

### macOS daily run at 8:00

This repo already includes a `launchd` job file:
- [com.apartmenthunter.daily.plist](com.apartmenthunter.daily.plist)

For a one-click setup on Mac, run the helper command:
- [install_mac.command](install_mac.command)

What it does:
- Creates a project-local virtual environment (`venv`)
- Installs dependencies
- Launches `setup.py` to configure your search settings (if `.env` is missing)
- Runs a dry run to verify the config
- Installs the daily `launchd` schedule on success

To install it on a Mac manually:

```bash
mkdir -p ~/Library/LaunchAgents
cp "com.apartmenthunter.daily.plist" ~/Library/LaunchAgents/
launchctl unload ~/Library/LaunchAgents/com.apartmenthunter.daily.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.apartmenthunter.daily.plist
launchctl start com.apartmenthunter.daily
```

### Move to another Mac

1. Copy the whole project folder to the new machine.
2. Install Python 3.11+.
3. Create a virtualenv and install dependencies:

```bash
cd "Appartement Hunter"
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

4. Run `python3 setup.py` to create your `.env` config file.
5. Copy over your state files if you want to keep deduplication history:
   - `data/seen_listings.json`
   - `data/listing_history.json`
6. Test locally:
   ```bash
   ./run_hunter.sh --dry-run
   ./run_hunter.sh
   ```
7. Install the `launchd` plist on the new Mac (make sure to update the absolute path inside the plist file if the repository lives in a different folder).

## State and Deduplication

- `data/seen_listings.json` stores legacy "already emailed" listing IDs.
- `data/listing_history.json` stores the richer history used for deduplication and weekly summaries.
- A listing is only treated as "already seen" once it was actually emailed in a daily digest.
- Friday runs send a separate weekly email with the top 10 listings first discovered in the current ISO week.

## Project Structure

```
apartment-hunter/
├── .github/workflows/
│   └── apartment-hunt.yml    # Daily cron + manual trigger
├── scrapers/
│   ├── base.py               # Listing dataclass + base scraper
│   ├── immoweb.py            # Immoweb.be scraper
│   ├── zimmo.py              # Zimmo.be scraper
│   └── immoscoop.py          # Immoscoop.be scraper
├── scoring/
│   ├── text_scorer.py        # Groq AI (Llama 3.3 70B)
│   └── photo_scorer.py       # OpenRouter free vision
├── email_sender/
│   └── digest.py             # HTML email builder + SMTP
├── config.py                 # Central config loader
├── setup.py                  # Interactive setup wizard
├── main.py                   # Orchestrator
├── requirements.txt
└── README.md
```

## Customization

### Change search parameters

Simply run the setup wizard again to modify any location, price, bedroom constraints, or email targets:
```bash
python3 setup.py
```

### Change schedule

Edit `.github/workflows/apartment-hunt.yml`:
```yaml
schedule:
  - cron: '0 6 * * *'  # 6 AM UTC = 7 AM CET
```

### Change scoring criteria

Edit the system prompts in:
- `scoring/text_scorer.py` → `SYSTEM_PROMPT`
- `scoring/photo_scorer.py` → `SYSTEM_PROMPT`

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Scraper blocked (403) | Common on GitHub-hosted runners; local execution is more reliable |
| Groq rate limited | Skips scoring, sends unranked listings |
| OpenRouter unavailable | Falls back to text-only scoring |
| Email fails | Logs error, still saves seen_listings.json |
| No new listings | Sends brief "nothing new today" email |
| Friday run | Sends the usual daily digest plus a weekly top-10 recap |

## License

MIT — do whatever you want with it. Good luck finding your apartment! 🍀
