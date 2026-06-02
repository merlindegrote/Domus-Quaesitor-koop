#!/usr/bin/env python3
"""
Huizenjacht (House Hunter) orchestrator — parallel batch architecture.

Runs 4 phases as subprocesses:
  1. collect.py   — run all scrapers in parallel, collect IDs only
  2. scrape_batch — split IDs into batches of 10, fetch detail pages in parallel
  3. process.py   — merge, dedup, score, save history
  4. email.py     — build HTML digest and send via SMTP
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from storage import (
    build_sent_index,
    iso_week_key,
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
from email_sender.digest import send_weekly_digest

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
BATCHES_DIR = "/tmp/domus-batches"
COLLECTED_PATH = "/tmp/domus-collected-ids.json"
PROCESSED_PATH = "/tmp/domus-processed.json"
BATCH_SIZE = 10
PHASE_TIMEOUT = 120  # seconds per subprocess


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def run_phase(script: str, timeout: int = PHASE_TIMEOUT) -> bool:
    """Run a phase script as a subprocess. Returns True on success."""
    script_path = os.path.join(BASE_DIR, "phases", script)
    logger.info("\n%s", "=" * 60)
    logger.info("Running phase: %s", script)
    logger.info("%s", "=" * 60)

    try:
        env = os.environ.copy()
        result = subprocess.run(
            ["python3", script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        # Log output
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                if line.strip():
                    logger.info("[%s] %s", script, line)
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                if line.strip():
                    logger.warning("[%s:stderr] %s", script, line)
        if result.returncode == 0:
            logger.info("[%s] ✅ Completed successfully", script)
            return True
        else:
            logger.error("[%s] ❌ Failed with exit code %d", script, result.returncode)
            return False
    except subprocess.TimeoutExpired:
        logger.error("[%s] ❌ Timed out after %ds", script, timeout)
        return False
    except Exception as e:
        logger.error("[%s] ❌ Error: %s", script, e)
        return False


def run_batch_scrapes() -> bool:
    """Read collected IDs, split into batches, run scrape_batch.py for each."""
    if not os.path.exists(COLLECTED_PATH):
        logger.error("No collected IDs at %s. Skipping Phase 2.", COLLECTED_PATH)
        return False

    try:
        with open(COLLECTED_PATH, "r", encoding="utf-8") as f:
            collected = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.error("Failed to read collected IDs: %s", e)
        return False

    # Build batches: {scraper: [batch_1_ids, batch_2_ids, ...]}
    all_batches: list[tuple[str, list[str], str]] = []
    batch_index = 0

    for scraper_name, ids in collected.items():
        if not ids:
            logger.info("[batches] %s: no IDs to scrape", scraper_name)
            continue

        # Split into batches of BATCH_SIZE
        for start in range(0, len(ids), BATCH_SIZE):
            batch_ids = ids[start:start + BATCH_SIZE]
            output_path = os.path.join(BATCHES_DIR, f"batch-{batch_index}.json")
            all_batches.append((scraper_name, batch_ids, output_path))
            logger.info("[batches] %s batch %d: %d IDs → %s",
                        scraper_name, batch_index, len(batch_ids), output_path)
            batch_index += 1

    if not all_batches:
        logger.warning("[batches] No batches to scrape")
        return False

    logger.info("[batches] Total batches: %d", len(all_batches))

    # Run all batches in parallel using ThreadPoolExecutor
    success_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = []
        for scraper_name, batch_ids, output_path in all_batches:
            ids_str = ",".join(batch_ids)
            cmd = [
                "python3",
                str(BASE_DIR / "phases" / "scrape_batch.py"),
                "--scraper", scraper_name,
                "--ids", ids_str,
                "--output", output_path,
            ]
            futures.append(executor.submit(_run_batch, cmd, scraper_name, len(batch_ids)))

        for future in concurrent.futures.as_completed(futures):
            if future.result():
                success_count += 1

    total = len(all_batches)
    logger.info("[batches] ✅ %d/%d batches completed successfully", success_count, total)
    return success_count > 0


def _run_batch(cmd: list[str], scraper: str, count: int) -> bool:
    """Run a single scrape_batch.py subprocess."""
    try:
        env = os.environ.copy()
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PHASE_TIMEOUT,
            env=env,
        )
        if result.returncode == 0:
            logger.debug("[batch][%s] %d IDs OK", scraper, count)
            return True
        else:
            logger.warning("[batch][%s] Failed (exit %d): %s",
                           scraper, result.returncode,
                           result.stderr.strip()[:200])
            return False
    except subprocess.TimeoutExpired:
        logger.warning("[batch][%s] Timed out after %ds", scraper, PHASE_TIMEOUT)
        return False
    except Exception as e:
        logger.warning("[batch][%s] Error: %s", scraper, e)
        return False


def run_weekly_digest(history: dict, now: datetime, dry_run: bool) -> None:
    """Send the weekly digest once per ISO week."""
    if dry_run:
        return
    if now.weekday() != 4:  # Friday
        return

    week_key = iso_week_key(now)
    if weekly_report_already_sent(history, week_key):
        logger.info("Weekly digest already sent for %s", week_key)
        return

    weekly = weekly_top_listings(history, week_key, limit=10)

    # Log top 10
    logger.info("\nWeekly Top 10 — %s", week_label(week_key))
    for i, l in enumerate(weekly, 1):
        score = f"{l.final_score:.1f}" if l.final_score is not None else "-"
        logger.info(
            "#%s [%s/10] %s — EUR %s — %s (%s)",
            i, score, l.title, l.price, l.address, l.platform,
        )

    logger.info("Sending weekly digest...")
    if send_weekly_digest(weekly, week_label(week_key)):
        mark_weekly_report_sent(history, week_key)
        save_history(Path(BASE_DIR / "data" / "listing_history.json"), history)
        logger.info("Weekly digest sent and marked")
    else:
        logger.error("Weekly digest failed")


def enforce_photo_scoring_env() -> None:
    """Ensure ENABLE_PHOTO_SCORING is disabled (Gemini out)."""
    os.environ["ENABLE_PHOTO_SCORING"] = "false"


def main() -> None:
    parser = argparse.ArgumentParser(description="Huizenjacht - house finder")
    parser.add_argument("--dry-run", action="store_true", help="Scrape/score but no email")
    parser.add_argument("--scrape-only", action="store_true", help="Only scrape, skip scoring/email")
    parser.add_argument("--weekly-only", action="store_true", help="Skip scraping, send weekly summary")
    parser.add_argument("--test-email", action="store_true", help="Send a test email without scraping")
    parser.add_argument("--full-dump", action="store_true", help="Send ALL matching houses, ignoring seen history")
    args = parser.parse_args()

    # Force photo scoring off
    enforce_photo_scoring_env()

    start_time = datetime.now()
    logger.info("Huizenjacht starting...")
    logger.info("Timestamp: %s", start_time.strftime("%Y-%m-%d %H:%M:%S"))
    mode = "full-dump" if args.full_dump else "test-email" if args.test_email else "weekly-only" if args.weekly_only else "dry-run" if args.dry_run else "scrape-only" if args.scrape_only else "full"
    logger.info("Mode: %s", mode)

    # ─── Test email mode ────────────────────────────────────────
    if args.test_email:
        logger.info("Sending test email...")
        run_phase("email.py")
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info("\nDone in %.1fs", elapsed)
        return

    # ─── Weekly-only mode ──────────────────────────────────────
    if args.weekly_only:
        history = load_history(BASE_DIR / "data" / "listing_history.json")
        run_weekly_digest(history, start_time, dry_run=False)
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info("\nDone in %.1fs", elapsed)
        return

    # ─── Full pipeline ──────────────────────────────────────────
    ensure_dir(BATCHES_DIR)

    # Phase 1: Collect IDs (all scrapers in parallel)
    phase1_ok = run_phase("collect.py")

    # Phase 2: Batch scrape (only if Phase 1 produced IDs)
    phase2_ok = False
    if phase1_ok and os.path.exists(COLLECTED_PATH):
        try:
            with open(COLLECTED_PATH) as f:
                collected = json.load(f)
            total_ids = sum(len(v) for v in collected.values())
            if total_ids > 0:
                logger.info("Total IDs collected: %d — starting batch scrapes", total_ids)
                phase2_ok = run_batch_scrapes()
            else:
                logger.warning("No IDs collected — skipping Phase 2")
        except Exception as e:
            logger.warning("Failed to inspect collected IDs: %s", e)
    else:
        logger.warning("Phase 1 did not produce IDs — skipping Phase 2")

    # Phase 3: Process, dedup, score
    if phase2_ok or args.full_dump:
        # For --full-dump, we skip dedup against history, so pass the flag through env
        if args.full_dump:
            os.environ["DOMUS_FULL_DUMP"] = "true"
        else:
            os.environ.pop("DOMUS_FULL_DUMP", None)
        run_phase("process.py", timeout=PHASE_TIMEOUT)
    else:
        logger.warning("No batches to process — skipping Phase 3")

    # Phase 4: Email
    if not args.dry_run and not args.scrape_only:
        if os.path.exists(PROCESSED_PATH):
            logger.info("Sending daily digest...")
            run_phase("email.py", timeout=PHASE_TIMEOUT)
        else:
            logger.warning("No processed data — skipping Phase 4")
    elif args.dry_run:
        logger.info("Dry run — skipping email")
    else:
        logger.info("Scrape-only — skipping email")

    # ─── Weekly digest (Fridays) ──────────────────────────────
    if not args.dry_run and not args.scrape_only:
        history = load_history(BASE_DIR / "data" / "listing_history.json")
        run_weekly_digest(history, start_time, dry_run=False)

    elapsed = (datetime.now() - start_time).total_seconds()
    logger.info("\nDone in %.1fs", elapsed)


if __name__ == "__main__":
    main()
