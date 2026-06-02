#!/usr/bin/env python3
"""
Phase 4 — Email: build HTML digest and send via SMTP.

Input:  /tmp/domus-processed.json
Output: log success/failure; send email via ssl0.ovh.net:465
"""

from __future__ import annotations

import json
import logging
import os
import sys

from dotenv import load_dotenv

from scrapers.base import Listing
from email_sender.digest import send_digest, send_weekly_digest

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

INPUT_PATH = "/tmp/domus-processed.json"


def _dict_to_listing(item: dict) -> Listing:
    """Convert a dict back to a Listing object."""
    return Listing(
        id=str(item.get("id", "")),
        platform=item.get("platform", "unknown"),
        title=item.get("title", ""),
        price=int(item.get("price", 0) or 0),
        bedrooms=int(item.get("bedrooms", 0) or 0),
        address=item.get("address", ""),
        url=item.get("url", ""),
        description=item.get("description", ""),
        image_urls=list(item.get("image_urls", [])),
        epc_label=item.get("epc_label"),
        surface_m2=item.get("surface_m2"),
        lot_surface_m2=item.get("lot_surface_m2"),
        posted_date=item.get("posted_date"),
        property_type=item.get("property_type", "house"),
        text_score=item.get("text_score"),
        photo_score=item.get("photo_score"),
        final_score=item.get("final_score"),
        score_reasoning=item.get("score_reasoning"),
    )


def send_email(max_retries: int = 2) -> bool:
    """Load processed listings and send email with retries."""
    # Load processed listings
    if not os.path.exists(INPUT_PATH):
        logger.warning("[email] No processed data at %s — nothing to send", INPUT_PATH)
        return False

    try:
        with open(INPUT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("[email] Failed to read %s: %s", INPUT_PATH, e)
        return False

    listings = [_dict_to_listing(item) for item in data if isinstance(item, dict)]

    logger.info("[email] Loaded %d listings for email", len(listings))

    # Try sending with retries
    for attempt in range(1, max_retries + 1):
        logger.info("[email] Attempt %d/%d — sending digest...", attempt, max_retries)
        try:
            success = send_digest(listings)
            if success:
                logger.info("[email] Email sent successfully")
                return True
            else:
                logger.warning("[email] send_digest returned False (attempt %d)", attempt)
        except Exception as e:
            logger.error("[email] Send failed (attempt %d): %s", attempt, e)

        if attempt < max_retries:
            import time
            time.sleep(5)

    # All retries exhausted — write to fallback log
    fallback_path = "/tmp/domus-email-fallback.json"
    try:
        with open(fallback_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.warning("[email] All SMTP attempts failed. Data saved to %s", fallback_path)
    except Exception:
        logger.error("[email] Failed to write fallback file")

    return False


def main() -> None:
    logger.info("=" * 50)
    logger.info("Phase 4 — Email")
    logger.info("=" * 50)

    send_email()


if __name__ == "__main__":
    main()
