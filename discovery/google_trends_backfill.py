"""Google Trends real backfill — pytrends + exponential backoff + checkpoint resume.

Enhances the partial ``ingest_google_trends`` in ``sprint1_parallel_ingest.py``
by driving pytrends automatically, saving both interest-over-time and
interest-by-region parquets, and falling back to a "needs_manual" status when
the library is unavailable or Google blocks all requests.

Designed to be imported into SPRINT1_SOURCES as a drop-in replacement.
"""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sprint1_parallel_ingest import CheckpointManager, ShutdownManager

logger = logging.getLogger(__name__)

# ---- constants (must stay in sync with sprint1_parallel_ingest) ----

KEYWORDS: list[str] = ["stock", "earnings", "buy", "sell", "price", "invest"]

TRENDS_DIR = Path("data/source/trends")
SOURCE_NAME = "google_trends"  # key used in checkpoint state

# Retry / back-off tunables
_MAX_RETRIES = 5
_BASE_DELAY = 4.0        # seconds — first retry
_MAX_DELAY = 120.0       # cap exponential back-off
_JITTER_FRAC = 0.25      # ±25 % random jitter on top of back-off


# ---- helpers -----------------------------------------------------------

def _trend_paths(ticker: str, kw: str) -> tuple[Path, Path]:
    """Return (time_path, region_path) for a ticker-keyword pair."""
    time_path = TRENDS_DIR / f"{ticker}_{kw}_time.parquet"
    region_path = TRENDS_DIR / f"{ticker}_{kw}_region.parquet"
    return time_path, region_path


def _init_pytrends():
    """Attempt to import and construct a TrendReq client.

    Returns (client, None) on success or (None, error_string) on failure.
    """
    try:
        from pytrends.request import TrendReq  # type: ignore[import-untyped]

        client = TrendReq(
            hl="en-US",
            tz=360,
            timeout=(10, 25),
            retries=2,
            backoff_factor=0.1,
        )
        return client, None
    except ImportError:
        return None, "pytrends package not installed (pip install pytrends)"
    except Exception as exc:  # pragma: no cover – network edge cases
        return None, f"pytrends init failed: {exc}"


async def _sleep_backoff(attempt: int) -> None:
    """Sleep for an exponentially-growing period with jitter."""
    delay = min(_BASE_DELAY * (2 ** attempt), _MAX_DELAY)
    jitter = delay * _JITTER_FRAC * (random.random() * 2 - 1)
    await asyncio.sleep(delay + jitter)


# ---- core ingest -------------------------------------------------------

async def ingest_google_trends_real(
    checkpoint_mgr: "CheckpointManager",
    shutdown_mgr: "ShutdownManager",
) -> None:
    """Fully-automatic Google Trends backfill with pytrends.

    Behaviour
    ---------
    1. Iterates over ``MASTER_TICKERS × KEYWORDS`` (6 keywords).
    2. For each pair fetches interest_over_time **and** interest_by_region,
       writing parquet to ``data/source/trends/``.
    3. Handles 429 / transient errors via exponential back-off.
    4. Checkpoints after every ticker so a Ctrl-C resumes cleanly.
    5. If pytrends is unavailable or every request fails, sets status to
       ``needs_manual`` and logs CSV-export instructions — it will **not**
       be marked ``done``.
    """
    # Import MASTER_TICKERS from the parent module
    from sprint1_parallel_ingest import MASTER_TICKERS

    state = checkpoint_mgr.get(SOURCE_NAME)
    total = len(MASTER_TICKERS) * len(KEYWORDS)

    # Resume state
    start_ticker_idx = state.checkpoint.get("ticker_idx", 0)
    done_keywords: list[str] = state.checkpoint.get("keywords_done", [])

    # Determine what's left — rebuild full list so checkpoints stay valid
    all_keywords = list(KEYWORDS)

    checkpoint_mgr.mark_started(SOURCE_NAME, total=total)

    # --- pytrends bootstrap -----------------------------------------------
    pytrends, init_err = _init_pytrends()
    if pytrends is None:
        logger.warning("pytrends unavailable — marking needs_manual: %s", init_err)
        _mark_needs_manual(checkpoint_mgr, init_err)
        return

    # Track consecutive failures so we can bail out if pytrends is truly dead
    consecutive_fails = 0
    MAX_CONSECUTIVE_FAILS = 12  # after this many back-to-back errors, assume block

    processed = state.items_processed

    for t_idx in range(start_ticker_idx, len(MASTER_TICKERS)):
        if shutdown_mgr.check_shutdown():
            checkpoint_mgr.mark_stopped(SOURCE_NAME)
            return

        ticker = MASTER_TICKERS[t_idx]

        for kw in all_keywords:
            if shutdown_mgr.check_shutdown():
                checkpoint_mgr.mark_stopped(SOURCE_NAME)
                return

            # Skip if this (ticker, kw) pair was already persisted
            if t_idx < start_ticker_idx or (
                t_idx == start_ticker_idx and kw in done_keywords
            ):
                continue

            time_path, region_path = _trend_paths(ticker, kw)

            success = await _fetch_and_save(
                pytrends, ticker, kw, time_path, region_path,
            )

            if success:
                consecutive_fails = 0
            else:
                consecutive_fails += 1

            # --- back-off if pytrends seems to be throttling ---------------
            if not success:
                if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                    logger.error(
                        "pytrends blocked after %d consecutive failures — "
                        "switching to needs_manual",
                        consecutive_fails,
                    )
                    _mark_needs_manual(
                        checkpoint_mgr,
                        f"pytrends blocked after {consecutive_fails} consecutive failures",
                    )
                    return

                # Exponential back-off before the next attempt
                await _sleep_backoff(min(consecutive_fails, 6))

            processed += 1

            # Mark progress after every keyword so resume is fine-grained
            done_keywords.append(kw)
            checkpoint_mgr.mark_progress(
                SOURCE_NAME,
                processed,
                {
                    "ticker_idx": t_idx,
                    "keywords_done": list(done_keywords),
                },
            )

        # Between tickers: a polite pause + clear done_keywords for next ticker
        done_keywords.clear()
        checkpoint_mgr.mark_progress(
            SOURCE_NAME,
            processed,
            {
                "ticker_idx": t_idx + 1,
                "keywords_done": [],
            },
        )

        # Rate-limiter pause (belt-and-suspenders with the token bucket)
        await asyncio.sleep(2.0)

    checkpoint_mgr.mark_done(SOURCE_NAME)
    logger.info("Google Trends real backfill complete — %d pairs ingested", processed)


# ---- private helpers ---------------------------------------------------

async def _fetch_and_save(
    pytrends,
    ticker: str,
    kw: str,
    time_path: Path,
    region_path: Path,
) -> bool:
    """Fetch one (ticker, keyword) pair; return True on success."""
    query = f"{ticker} {kw}"

    for attempt in range(_MAX_RETRIES):
        try:
            pytrends.build_payload(
                [query],
                timeframe="today 5-y",
                geo="US",
            )

            # interest_over_time
            df_time = pytrends.interest_over_time()
            if df_time is not None and not df_time.empty:
                time_path.parent.mkdir(parents=True, exist_ok=True)
                df_time.to_parquet(time_path)

            # interest_by_region
            df_region = pytrends.interest_by_region(
                resolution="REGION", inc_low_vol=True, inc_geo_code=True,
            )
            if df_region is not None and not df_region.empty:
                region_path.parent.mkdir(parents=True, exist_ok=True)
                df_region.to_parquet(region_path)

            logger.debug("Trends OK: %s %s", ticker, kw)
            return True

        except Exception as exc:
            exc_str = str(exc).lower()
            is_429 = "429" in exc_str or "too many" in exc_str or "rate" in exc_str
            backoff = min(_BASE_DELAY * (2 ** attempt), _MAX_DELAY)
            if is_429:
                backoff *= 2  # extra punishment for rate-limit

            logger.warning(
                "Trends %s/%s attempt %d/%d (%s) — backing off %.1fs",
                ticker, kw, attempt + 1, _MAX_RETRIES, exc, backoff,
            )
            jitter = backoff * _JITTER_FRAC * (random.random() * 2 - 1)
            await asyncio.sleep(backoff + jitter)

    logger.warning("Trends %s/%s: all %d retries exhausted", ticker, kw, _MAX_RETRIES)
    return False


def _mark_needs_manual(checkpoint_mgr: "CheckpointManager", reason: str) -> None:
    """Set source status to needs_manual with instructions."""
    state = checkpoint_mgr.get(SOURCE_NAME)
    state.status = "needs_manual"
    state.error = reason
    from datetime import datetime
    state.completed_at = datetime.utcnow().isoformat()
    state.checkpoint["manual_instructions"] = (
        "pytrends is unavailable or blocked. To export manually:\n"
        "  1. Go to https://trends.google.com\n"
        "  2. Search e.g. 'AAPL stock', set timeframe to past 5 years, US\n"
        "  3. Click download (CSV/TSV) for interest_over_time\n"
        "  4. Save to data/source/trends/{TICKER}_{KW}_time.csv\n"
        "  5. Repeat for interest_by_region -> {TICKER}_{KW}_region.csv\n"
        "After all downloads, run this backfill again to convert CSVs."
    )
    checkpoint_mgr.save(SOURCE_NAME)
    logger.warning(
        "Google Trends marked needs_manual — %s\n"
        "Manual export instructions saved to checkpoint.",
        reason,
    )
