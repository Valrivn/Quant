#!/usr/bin/env python
"""Run PIT fundamentals collection — orchestration script.

Resolves CIKs for the target roster, then runs the async scraper.
Checkpoints allow resume after interruption.

Usage:
    python scripts/run_pit_collection.py                     # all 27 tickers
    python scripts/run_pit_collection.py --test-aapl         # AAPL only (validation)
    python scripts/run_pit_collection.py --tickers AAPL,MSFT # custom set
    python scripts/run_pit_collection.py --resume             # skip fully-done tickers
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

# Ensure project root on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from valuation_alpha.universe.cik_resolver import resolve_ciks
from discovery.sec_pit_scraper import (
    SECEdgarScraper,
    DEFAULT_ROSTER,
    DATA_DIR,
    CHECKPOINT_DIR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_pit_collection")


def resolve_tickers(tickers: list[str]) -> dict[str, str]:
    """Resolve tickers to CIKs using the existing cik_resolver."""
    logger.info("Resolving CIKs for %d tickers...", len(tickers))
    result = resolve_ciks(tickers)
    resolved = {t: c for t, c in result.items() if c}
    failed = [t for t, c in result.items() if not c]
    if failed:
        logger.warning("CIK resolution failed for: %s", failed)
    logger.info("Resolved %d/%d tickers", len(resolved), len(tickers))
    return resolved


def filter_already_done(tickers: list[str]) -> list[str]:
    """Filter out tickers that have full checkpoints (all filings parsed)."""
    remaining = []
    for ticker in tickers:
        cp_path = CHECKPOINT_DIR / f"{ticker}_checkpoint.json"
        parsed_path = DATA_DIR / ticker / "parsed_filings.json"
        if cp_path.exists() and parsed_path.exists():
            try:
                cp = json.loads(cp_path.read_text(encoding="utf-8"))
                if cp.get("filings_parsed", 0) > 0:
                    logger.info("  %s: already done (%d filings), skipping",
                               ticker, cp["filings_parsed"])
                    continue
            except Exception:
                pass
        remaining.append(ticker)
    return remaining


def main():
    parser = argparse.ArgumentParser(
        description="SEC EDGAR PIT Fundamentals Collector")
    parser.add_argument("--tickers", type=str, default=None,
                        help="Comma-separated tickers (default: 27-ticker roster)")
    parser.add_argument("--test-aapl", action="store_true",
                        help="Quick validation on AAPL only")
    parser.add_argument("--resume", action="store_true",
                        help="Skip tickers that are already fully scraped")
    parser.add_argument("--rate-limit", type=float, default=9.0,
                        help="Max requests per second (default: 9)")
    args = parser.parse_args()

    # Determine target tickers
    if args.test_aapl:
        tickers = ["AAPL"]
    elif args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",")]
    else:
        tickers = DEFAULT_ROSTER

    logger.info("Target tickers: %s", tickers)

    # Resolve CIKs
    tickers_ciks = resolve_tickers(tickers)
    if not tickers_ciks:
        logger.error("No CIKs resolved — cannot proceed")
        sys.exit(1)

    # Filter already-done tickers
    if args.resume:
        tickers_to_do = filter_already_done(list(tickers_ciks.keys()))
        tickers_ciks = {t: tickers_ciks[t] for t in tickers_to_do}
        if not tickers_ciks:
            logger.info("All tickers already scraped. Nothing to do.")
            return

    # Run scraper
    logger.info("Starting async scraper for %d tickers (rate limit: %.0f rps)",
               len(tickers_ciks), args.rate_limit)
    t0 = time.time()
    
    scraper = SECEdgarScraper(tickers_ciks, rate_limit=args.rate_limit)
    
    import asyncio
    results = asyncio.run(scraper.run_all())
    
    elapsed = time.time() - t0
    logger.info("Scraping completed in %.1f seconds", elapsed)

    # Save Parquet
    parquet_path = scraper.save_pit_parquet(results)
    logger.info("Parquet saved to %s", parquet_path)

    # Print summary
    scraper.print_summary(results)


if __name__ == "__main__":
    main()
