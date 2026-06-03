"""Central configuration module for Domus-Quaesitor (koop)."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Transaction type
TRANSACTION_TYPE = "for-sale"
PROPERTY_TYPE = "house"

# Search settings — accept only these cities
TARGET_CITIES = ["Lier", "Ranst", "Broechem", "Emblem", "Vremde", "Wommelgem", "Kessel", "Geel"]
TARGET_POSTALS = ["2500", "2520", "2520", "2520", "2531", "2160", "2560", "2440"]

# Cities/postals to exclude (even if they match postals)
EXCLUDE_CITIES = ["Koningshooikt", "Oelegem", "Nijlen", "Bevel"]

# Fallback for single-city operations
TARGET_CITY = os.environ.get("TARGET_CITY", "Ranst").strip()
TARGET_POSTAL_CODE = os.environ.get("TARGET_POSTAL_CODE", "2520").strip()

try:
    MIN_PRICE = int(os.environ.get("MIN_PRICE", "400000"))
except ValueError:
    MIN_PRICE = 400000

try:
    MAX_PRICE = int(os.environ.get("MAX_PRICE", "700000"))
except ValueError:
    MAX_PRICE = 700000

try:
    MIN_BEDROOMS = int(os.environ.get("MIN_BEDROOMS", "3"))
except ValueError:
    MIN_BEDROOMS = 3

# EPC labels to accept (energy performance)
EPC_ALLOWED = ["A", "A+", "A++", "B", "C"]

# Minimum surface areas (m²)
try:
    MIN_LIVING_SURFACE = int(os.environ.get("MIN_LIVING_SURFACE", "115"))
except ValueError:
    MIN_LIVING_SURFACE = 115

try:
    MIN_LOT_SURFACE = int(os.environ.get("MIN_LOT_SURFACE", "450"))
except ValueError:
    MIN_LOT_SURFACE = 450

# Skip listings that mention renovation — DISABLED (EPC filter is sufficient)
SKIP_RENOVATION = os.environ.get("SKIP_RENOVATION", "false").lower() in {"1", "true", "yes", "on"}

# City acceptance/exclusion filter
CITY_ACCEPT_LIST = os.environ.get("CITY_ACCEPT_LIST", "")
CITY_EXCLUDE_LIST = os.environ.get("CITY_EXCLUDE_LIST", "")

# Parse from env or use defaults
if CITY_ACCEPT_LIST:
    ACCEPT_CITIES = [c.strip() for c in CITY_ACCEPT_LIST.split(",") if c.strip()]
else:
    ACCEPT_CITIES = TARGET_CITIES[:]

if CITY_EXCLUDE_LIST:
    EXCLUDE_CITIES_FINAL = [c.strip() for c in CITY_EXCLUDE_LIST.split(",") if c.strip()]
else:
    EXCLUDE_CITIES_FINAL = EXCLUDE_CITIES[:]
