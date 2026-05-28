#!/usr/bin/env python3
"""
Apartment Hunter orchestrator.

Daily mode:
- scrape listings
- deduplicate against previously emailed listings
- score and email new listings
- on Fridays, also send a weekly top-10 summary
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
from location_filter import assess_station_proximity, filter_listings_for_station
from scoring.photo_scorer import PhotoScorer, compute_final_scores
from scoring.text_scorer import TextScorer
from scrapers import ImmowebScraper, ImmoscoopScraper, Listing, ZimmoScraper
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
    scrapers = [ImmowebScraper(), ZimmoScraper(), ImmoscoopScraper()]
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
    """
    Keep only listings that have not been emailed before.

    Duplicates are detected in two ways:
    1. Exact platform listing ID seen in a prior email
    2. Cross-platform fuzzy fingerprint seen in a prior email
    """
    new_listings: list[Listing] = []
    run_fingerprints: set[str] = set()

    for listing in listings:
        unique_key = listing.unique_key
        fingerprint = listing_fingerprint(listing)

        if unique_key in sent_unique_keys:
            logger.debug("Skipping previously emailed listing id: %s", unique_key)
            continue

        if fingerprint in sent_fingerprints:
            logger.debug("Skipping previously emailed fingerprint: %s", fingerprint)
            continue

        if fingerprint in run_fingerprints:
            logger.debug("Skipping same-run duplicate fingerprint: %s", fingerprint)
            continue

        run_fingerprints.add(fingerprint)
        new_listings.append(listing)

    logger.info(
        "New listings after deduplication: %s (filtered %s duplicates/previously emailed)",
        len(new_listings),
        len(listings) - len(new_listings),
    )
    return new_listings


def enrich_listings(listings: list[Listing]) -> list[Listing]:
    """Enrich listings with full descriptions from detail pages."""
    scrapers = {
        "immoweb": ImmowebScraper(),
        "zimmo": ZimmoScraper(),
        "immoscoop": ImmoscoopScraper(),
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
        logger.info("\nPhoto scoring disabled by configuration")

    return compute_final_scores(listings)


def log_ranked_results(listings: list[Listing], heading: str) -> None:
    """Print ranked listings to the log."""
    logger.info("\n%s", "=" * 60)
    logger.info("%s", heading)
    logger.info("%s", "=" * 60)

    if not listings:
        logger.info("No listings to show")
        return

    for index, listing in enumerate(listings, start=1):
        score_str = f"{listing.final_score:.1f}" if listing.final_score is not None else "-"
        logger.info(
            "#%s [%s/10] %s - EUR %s/mo - %s (%s)",
            index,
            score_str,
            listing.title,
            listing.price,
            listing.address,
            listing.platform,
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
    """Send the Friday weekly digest once per ISO week."""
    if dry_run or scrape_only:
        return

    if now.weekday() != 4:
        return

    week_key = iso_week_key(now)
    if weekly_report_already_sent(history, week_key):
        logger.info("Weekly digest already sent for %s", week_key)
        return

    weekly_listings = weekly_top_listings(history, week_key, limit=10)
    log_ranked_results(weekly_listings, f"WEEKLY TOP 10 - {week_label(week_key)}")

    logger.info("\nSending weekly digest...")
    if send_weekly_digest(weekly_listings, week_label(week_key)):
        mark_weekly_report_sent(history, week_key)
    else:
        logger.error("Weekly digest failed")


def run_weekly_only(history: dict, now: datetime, dry_run: bool) -> None:
    """Send or preview the weekly digest without scraping."""
    week_key = iso_week_key(now)
    weekly_listings = weekly_top_listings(history, week_key, limit=10)
    log_ranked_results(weekly_listings, f"WEEKLY TOP 10 - {week_label(week_key)}")

    if dry_run:
        logger.info("\nDry run - skipping weekly email")
        return

    if weekly_report_already_sent(history, week_key):
        logger.info("Weekly digest already sent for %s", week_key)
        return

    logger.info("\nSending weekly digest...")
    if send_weekly_digest(weekly_listings, week_label(week_key)):
        mark_weekly_report_sent(history, week_key)
    else:
        logger.error("Weekly digest failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Apartment Hunter - Ghent rental finder")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape and score but do not send email",
    )
    parser.add_argument(
        "--scrape-only",
        action="store_true",
        help="Only scrape listings; skip scoring and email",
    )
    parser.add_argument(
        "--weekly-only",
        action="store_true",
        help="Skip scraping and send only the weekly top-10 summary",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send a minimal test email without scraping",
    )
    args = parser.parse_args()

    start_time = datetime.now()
    logger.info("Apartment Hunter starting...")
    logger.info("Timestamp: %s", start_time.strftime("%Y-%m-%d %H:%M:%S"))
    logger.info(
        "Mode: %s",
        "test-email"
        if args.test_email
        else "weekly-only"
        if args.weekly_only
        else "dry-run"
        if args.dry_run
        else "scrape-only"
        if args.scrape_only
        else "full",
    )

    seen_ids = load_seen_ids(SEEN_FILE)
    history = load_history(HISTORY_FILE)
    sent_unique_keys, sent_fingerprints = build_sent_index(history, seen_ids)
    logger.info("Previously emailed listings: %s ids, %s fingerprints", len(sent_unique_keys), len(sent_fingerprints))

    if args.test_email:
        logger.info("Sending minimal test email...")
        if send_digest([]):
            logger.info("Test email sent successfully")
        else:
            logger.error("Test email failed")
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
    new_listings = deduplicate(all_listings, sent_unique_keys, sent_fingerprints)

    if new_listings:
        logger.info("\nEnriching listings...")
        new_listings = enrich_listings(new_listings)
        before_station_filter = len(new_listings)
        new_listings = filter_listings_for_station(new_listings)
        if len(new_listings) != before_station_filter:
            logger.info(
                "Proximity filter kept %s/%s listings",
                len(new_listings),
                before_station_filter,
            )

        if not args.scrape_only:
            new_listings = score_listings(new_listings)

        from config import ENABLE_STATION_FILTER
        if ENABLE_STATION_FILTER:
            for listing in new_listings:
                station_result = assess_station_proximity(listing)
                if listing.score_reasoning:
                    listing.score_reasoning = f"{listing.score_reasoning} | {station_result.reason}"
                else:
                    listing.score_reasoning = station_result.reason

        log_ranked_results(new_listings, f"DAILY RESULTS - {len(new_listings)} new listings ranked")
    else:
        logger.info("No new listings after deduplication")

    daily_email_sent = False
    if not args.dry_run and not args.scrape_only:
        logger.info("\nSending daily digest...")
        daily_email_sent = send_digest(new_listings)
        if not daily_email_sent:
            logger.error("Daily digest failed")
    elif args.dry_run:
        logger.info("\nDry run - skipping email")
    else:
        logger.info("\nScrape-only mode - skipping scoring and email")

    if not args.dry_run and not args.scrape_only:
        persist_new_listings(history, seen_ids, new_listings, start_time, daily_email_sent)
        save_history(HISTORY_FILE, history)
        save_seen_ids(SEEN_FILE, seen_ids)

    maybe_send_weekly_digest(
        history,
        now=start_time,
        dry_run=args.dry_run,
        scrape_only=args.scrape_only,
    )
    if not args.dry_run and not args.scrape_only:
        save_history(HISTORY_FILE, history)

    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info("\nDone in %.1fs", elapsed)


if __name__ == "__main__":
    main()
