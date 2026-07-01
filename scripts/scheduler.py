#!/usr/bin/env python3
"""
Scheduler for automated pipeline execution.

This module provides APScheduler-based scheduling for the quant pipeline:
- Daily: VADER scraping + sentiment aggregation (runs at 6 AM UTC)
- Weekly: Bayesian weight optimization (runs Monday 2 AM UTC)
- Bi-weekly: IC drift detection (runs Tuesday/Friday 3 AM UTC)

Usage:
    python scripts/scheduler.py           # Start scheduler daemon
    python scripts/scheduler.py --once daily   # Run daily job once
    python scripts/scheduler.py --once weekly  # Run weekly job once
    python scripts/scheduler.py --once drift   # Run drift check once
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False

from scraper.run_scraper import cmd_scrape, cmd_aggregate, cmd_backtest, cmd_optimize, cmd_drift
from types import SimpleNamespace

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def run_daily_job():
    """Daily job: scrape + aggregate."""
    logger.info("=== Starting daily scrape job ===")
    try:
        args = SimpleNamespace(subreddit=None)
        cmd_scrape(args)
        cmd_aggregate(SimpleNamespace(date=None))
        logger.info("=== Daily job completed ===")
    except Exception as e:
        logger.error(f"Daily job failed: {e}", exc_info=True)


def run_weekly_job():
    """Weekly job: Bayesian weight optimization."""
    logger.info("=== Starting weekly optimization job ===")
    try:
        cmd_optimize(SimpleNamespace(trials=50, metric="sharpe"))
        logger.info("=== Weekly job completed ===")
    except Exception as e:
        logger.error(f"Weekly job failed: {e}", exc_info=True)


def run_drift_job():
    """Bi-weekly job: IC drift detection."""
    logger.info("=== Starting drift detection job ===")
    try:
        cmd_drift(SimpleNamespace(threshold=0.20, window=60))
        logger.info("=== Drift job completed ===")
    except Exception as e:
        logger.error(f"Drift job failed: {e}", exc_info=True)


def start_scheduler():
    """Start the APScheduler daemon."""
    if not APSCHEDULER_AVAILABLE:
        logger.error("APScheduler not installed. Run: pip install apscheduler")
        return 1

    scheduler = BlockingScheduler(timezone="UTC")

    # Daily at 6:00 AM UTC
    scheduler.add_job(
        run_daily_job,
        CronTrigger(hour=6, minute=0),
        id="daily_scrape",
        name="Daily Scrape + Aggregate",
        max_instances=1,
        coalesce=True,
    )

    # Weekly on Monday at 2:00 AM UTC
    scheduler.add_job(
        run_weekly_job,
        CronTrigger(day_of_week="mon", hour=2, minute=0),
        id="weekly_optimize",
        name="Weekly Bayesian Optimization",
        max_instances=1,
        coalesce=True,
    )

    # Bi-weekly on Tuesday and Friday at 3:00 AM UTC
    scheduler.add_job(
        run_drift_job,
        CronTrigger(day_of_week="tue,fri", hour=3, minute=0),
        id="biweekly_drift",
        name="Bi-weekly IC Drift Detection",
        max_instances=1,
        coalesce=True,
    )

    logger.info("Scheduler started. Jobs:")
    for job in scheduler.get_jobs():
        logger.info(f"  {job.name}: {job.trigger}")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user")
    return 0


def run_once(job_type: str):
    """Run a specific job once."""
    jobs = {
        "daily": run_daily_job,
        "weekly": run_weekly_job,
        "drift": run_drift_job,
    }
    if job_type not in jobs:
        logger.error(f"Unknown job type: {job_type}. Available: {list(jobs.keys())}")
        return 1
    jobs[job_type]()
    return 0


def main():
    parser = argparse.ArgumentParser(description="Quant Pipeline Scheduler")
    parser.add_argument("--once", choices=["daily", "weekly", "drift"],
                        help="Run a specific job once and exit")
    args = parser.parse_args()

    if args.once:
        return run_once(args.once)

    if not APSCHEDULER_AVAILABLE:
        logger.error("APScheduler not installed. Run: pip install apscheduler")
        return 1

    return start_scheduler()


if __name__ == "__main__":
    sys.exit(main())