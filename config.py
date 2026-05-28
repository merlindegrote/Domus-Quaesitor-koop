"""Central configuration module for Apartment Hunter."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Search settings
TARGET_CITY = os.environ.get("TARGET_CITY", "gent").strip()
TARGET_POSTAL_CODE = os.environ.get("TARGET_POSTAL_CODE", "9000").strip()

try:
    MIN_PRICE = int(os.environ.get("MIN_PRICE", "800"))
except ValueError:
    MIN_PRICE = 800

try:
    MAX_PRICE = int(os.environ.get("MAX_PRICE", "1000"))
except ValueError:
    MAX_PRICE = 1000

try:
    MIN_BEDROOMS = int(os.environ.get("MIN_BEDROOMS", "1"))
except ValueError:
    MIN_BEDROOMS = 1

# Proximity/Station Filter settings
ENABLE_STATION_FILTER = os.environ.get("ENABLE_STATION_FILTER", "false").lower() in {"1", "true", "yes", "on"}

# Parse station/proximity keywords
DEFAULT_NEAR = [
    "gent-sint-pieters", "sint-pieters", "stationsbuurt", "st pieters",
    "koningin elisabethlaan", "prinses clementinalaan", "clementinalaan",
    "kortrijksesteenweg", "smidsestraat", "aannemersstraat", "vasco da gamastraat",
    "patijntjesstraat", "zwijnaardsesteenweg", "voskenslaan", "sint-denijslaan"
]

DEFAULT_FAR = [
    "dampoort", "muide", "wondelgem", "oostakker", "mariakerke", "sint-amandsberg",
    "gentbrugge", "moscou", "rabat", "rabot", "bloemekenswijk", "dok noord",
    "blaisantvest", "oslostraat", "blankenbergestraat", "hofstraat", "komijnstraat",
    "begijnhoflaan", "steenakker", "kasteellaan", "francisco ferrerlaan"
]

near_raw = os.environ.get("STATION_NEAR_KEYWORDS", "")
STATION_NEAR_KEYWORDS = [
    k.strip().lower() for k in near_raw.split(",") if k.strip()
] if near_raw else DEFAULT_NEAR

far_raw = os.environ.get("STATION_FAR_KEYWORDS", "")
STATION_FAR_KEYWORDS = [
    k.strip().lower() for k in far_raw.split(",") if k.strip()
] if far_raw else DEFAULT_FAR
