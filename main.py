#!/usr/bin/env python3
"""
Huizenjacht (House Hunter) orchestrator.

Daily mode:
- scrape listings from Immoweb, Zimmo, Immoscoop
- deduplicate against previously emailed listings
- score (text + photos) and email new listings
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from email_sender.digest import send_digest, send_weekly_digest
from location_filter import needs_renovation, filter_listings_by_location
from scoring.photo_scorer import PhotoScorer, compute_final_scores
from scoring.text_scorer import TextScorer
from scrapers import ImmowebScraper, ImmovlanScraper, ImmoscoopScraper, Listing, TweeDeHandsScraper, ZimmoScraper
from storage import (
    build_sent_index,
    iso_week_key,
    listing_fingerprint,
    load_history,
    load_seen_ids,
    mark_weekly_report_sent,
    save_history,
    save_seen_ids,
    upsert_listing_record,
    week_label,
    weekly_report_already_sent,
    weekly_top_listings,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
SEEN_FILE = DATA_DIR / "seen_listings.json"
HISTORY_FILE = DATA_DIR / "listing_history.json"


def scrape_all() -> list[Listing]:
    """Run all scrapers and collect listings."""
    scrapers = [ImmowebScraper(), ZimmoScraper(), ImmoscoopScraper(), ImmovlanScraper(), TweeDeHandsScraper()]
    all_listings: list[Listing] = []

    for scraper in scrapers:
        logger.info("\n%s", "=" * 50)
        logger.info("Scraping %s...", scraper.PLATFORM_NAME)
        logger.info("%s", "=" * 50)
        all_listings.extend(scraper.safe_scrape())

    logger.info("\nTotal listings scraped: %s", len(all_listings))
    return all_listings


def deduplicate(
    listings: list[Listing],
    sent_unique_keys: set[str],
    sent_fingerprints: set[str],
) -> list[Listing]:
    """Keep only listings not emailed before."""
    new_listings: list[Listing] = []
    run_fingerprints: set[str] = set()

    for listing in listings:
        unique_key = listing.unique_key
        fingerprint = listing_fingerprint(listing)

        if unique_key in sent_unique_keys:
            logger.debug("Skipping previously emailed id: %s", unique_key)
            continue
        if fingerprint in sent_fingerprints:
            logger.debug("Skipping previously emailed fingerprint: %s", fingerprint)
            continue
        if fingerprint in run_fingerprints:
            logger.debug("Skipping same-run duplicate: %s", fingerprint)
            continue

        run_fingerprints.add(fingerprint)
        new_listings.append(listing)

    logger.info(
        "New after dedup: %s (filtered %s dupes)",
        len(new_listings),
        len(listings) - len(new_listings),
    )
    return new_listings


def enrich_listings(listings: list[Listing]) -> list[Listing]:
    """Enrich listings with full descriptions from detail pages."""
    from config import ACCEPT_CITIES

    scrapers = {
        "immoweb": ImmowebScraper(),
        "zimmo": ZimmoScraper(),
        "immoscoop": ImmoscoopScraper(),
        "immovlan": ImmovlanScraper(),
        "tweedehands": TweeDeHandsScraper(),
    }

    for listing in listings:
        if not listing.description or len(listing.description) < 80:
            scraper = scrapers.get(listing.platform)
            if scraper and hasattr(scraper, "enrich_listing"):
                logger.info("Enriching %s:%s...", listing.platform, listing.id)
                scraper.enrich_listing(listing)

    return listings


def score_listings(listings: list[Listing]) -> list[Listing]:
    """Run text and photo scoring, then sort by final score."""
    logger.info("\nAI text scoring...")
    text_scorer = TextScorer()
    listings = text_scorer.score_listings(listings)

    if os.environ.get("ENABLE_PHOTO_SCORING", "true").lower() in {"1", "true", "yes", "on"}:
        logger.info("\nAI photo scoring...")
        photo_scorer = PhotoScorer()
        listings = photo_scorer.score_listings(listings)
    else:
        logger.info("\nPhoto scoring disabled")

    return compute_final_scores(listings)


def log_ranked_results(listings: list[Listing], heading: str) -> None:
    """Print ranked listings to the log."""
    logger.info("\n%s", "=" * 60)
    logger.info("%s", heading)
    logger.info("%s", "=" * 60)

    if not listings:
        logger.info("No listings to show")
        return

    for i, listing in enumerate(listings, 1):
        score_str = f"{listing.final_score:.1f}" if listing.final_score is not None else "-"
        logger.info(
            "#%s [%s/10] %s - EUR %s - %s (%s)",
            i, score_str, listing.title, listing.price,
            listing.address, listing.platform,
        )
        if listing.score_reasoning:
            logger.info("    %s", listing.score_reasoning)


def persist_new_listings(
    history: dict,
    seen_ids: set[str],
    listings: list[Listing],
    discovered_at: datetime,
    email_sent: bool,
) -> None:
    """Persist newly discovered listings."""
    for listing in listings:
        upsert_listing_record(history, listing, discovered_at, was_emailed=email_sent)
        if email_sent:
            seen_ids.add(listing.unique_key)


def maybe_send_weekly_digest(
    history: dict,
    *,
    now: datetime,
    dry_run: bool,
    scrape_only: bool,
) -> None:
    """Send the weekly digest once per ISO week."""
    if dry_run or scrape_only:
        return
    if now.weekday() != 4:
        return

    week_key = iso_week_key(now)
    if weekly_report_already_sent(history, week_key):
        logger.info("Weekly digest already sent for %s", week_key)
        return

    weekly = weekly_top_listings(history, week_key, limit=10)
    log_ranked_results(weekly, f"WEEKLY TOP 10 - {week_label(week_key)}")

    logger.info("\nSending weekly digest...")
    if send_weekly_digest(weekly, week_label(week_key)):
        mark_weekly_report_sent(history, week_key)
    else:
        logger.error("Weekly digest failed")


def run_weekly_only(history: dict, now: datetime, dry_run: bool) -> None:
    """Send or preview the weekly digest without scraping."""
    week_key = iso_week_key(now)
    weekly = weekly_top_listings(history, week_key, limit=10)
    log_ranked_results(weekly, f"WEEKLY TOP 10 - {week_label(week_key)}")
    if dry_run:
        logger.info("\nDry run - skipping weekly email")
        return
    if weekly_report_already_sent(history, week_key):
        logger.info("Weekly digest already sent for %s", week_key)
        return
    logger.info("\nSending weekly digest...")
    if send_weekly_digest(weekly, week_label(week_key)):
        mark_weekly_report_sent(history, week_key)
    else:
        logger.error("Weekly digest failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Huizenjacht - house finder")
    parser.add_argument("--dry-run", action="store_true", help="Scrape/score but no email")
    parser.add_argument("--scrape-only", action="store_true", help="Only scrape, skip scoring/email")
    parser.add_argument("--weekly-only", action="store_true", help="Skip scraping, send weekly summary")
    parser.add_argument("--test-email", action="store_true", help="Send a test email without scraping")
    args = parser.parse_args()

    start_time = datetime.now()
    logger.info("Huizenjacht starting...")
    logger.info("Timestamp: %s", start_time.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("Mode: %s", "test-email" if args.test_email else "weekly-only" if args.weekly_only else "dry-run" if args.dry_run else "scrape-only" if args.scrape_only else "full")

    seen_ids = load_seen_ids(SEEN_FILE)
    history = load_history(HISTORY_FILE)
    sent_keys, sent_fps = build_sent_index(history, seen_ids)
    logger.info("Previously emailed: %s ids, %s fingerprints", len(sent_keys), len(sent_fps))

    if args.test_email:
        logger.info("Sending test email...")
        send_digest([])
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info("\nDone in %.1fs", elapsed)
        return

    if args.weekly_only:
        run_weekly_only(history, start_time, args.dry_run)
        save_history(HISTORY_FILE, history)
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info("\nDone in %.1fs", elapsed)
        return

    all_listings = scrape_all()

    # Filter by location + renovation check
    all_listings = [l for l in all_listings if not needs_renovation(l)]

    # Backstop: filter out anything that's not a house
    before = len(all_listings)
    all_listings = [l for l in all_listings if l.property_type == "house"]
    logger.info("Property type filter: kept %s/%s listings (houses only)", len(all_listings), before)

    new_listings = deduplicate(all_listings, sent_keys, sent_fps)

    if new_listings:
        logger.info("\nEnriching listings...")
        new_listings = enrich_listings(new_listings)

        # Apply location filter (city accept/exclude)
        before_filter = len(new_listings)
        new_listings = filter_listings_by_location(new_listings)
        logger.info("Location filter: kept %s/%s", len(new_listings), before_filter)

        if not args.scrape_only:
            new_listings = score_listings(new_listings)

        log_ranked_results(new_listings, f"DAILY RESULTS - {len(new_listings)} new listings ranked")
    else:
        logger.info("No new listings after dedup")

    daily_email_sent = False
    if not args.dry_run and not args.scrape_only:
        logger.info("\nSending daily digest...")
        daily_email_sent = send_digest(new_listings)
    elif args.dry_run:
        logger.info("\nDry run - skipping email")
    else:
        logger.info("\nScrape-only - skipping email")

    if not args.dry_run and not args.scrape_only:
        persist_new_listings(history, seen_ids, new_listings, start_time, daily_email_sent)
        save_history(HISTORY_FILE, history)
        save_seen_ids(SEEN_FILE, seen_ids)

    maybe_send_weekly_digest(history, now=start_time, dry_run=args.dry_run, scrape_only=args.scrape_only)
    if not args.dry_run and not args.scrape_only:
        save_history(HISTORY_FILE, history)

    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info("\nDone in %.1fs", elapsed)


if __name__ == "__main__":
    main()
