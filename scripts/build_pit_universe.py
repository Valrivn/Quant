#!/usr/bin/env python
"""Build real S&P 500 quarterly PIT universe from fja05680/sp500 (GitHub).

Primary source: https://github.com/fja05680/sp500 — daily S&P 500 constituent
list reconstructed from Wikipedia / S&P DJI reconstitution history, free and
public. Covers 1996-01-02 .. 2026-06-30 (documented reconstruction).

The output quarterly snapshot for each quarter-end uses the constituent list
as of the last available daily observation <= quarter-end, giving a genuine
point-in-time universe with zero survivorship bias (delisted/merged names are
present up to their removal date, e.g. AAMRQ, ENRNQ, TWTR, etc.).

Ticker format: the source uses dot-form share classes (BRK.B, BF.B, RDS.A);
these are converted to Yahoo dash form (BRK-B) to match the rest of the
codebase (`diversification.datastore` / yfinance downloads).
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime

import pandas as pd
import requests

SOURCE_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/"
    "S%26P%20500%20Historical%20Components%20%26%20Changes%20(Updated).csv"
)
SOURCE_REPO = "github.com/fja05680/sp500"
CACHE_FILE = Path(tempfile.gettempdir()) / "opencode" / "sp500_hist.csv"

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "data" / "pit_sp500_constituents.json"


def download_csv() -> pd.DataFrame:
    """Download (or load cached) daily constituent history as a DataFrame."""
    df = None
    if CACHE_FILE.exists():
        try:
            df = pd.read_csv(CACHE_FILE)
            if not df.empty and {"date", "tickers"}.issubset(df.columns):
                print(f"Using cached source: {CACHE_FILE}")
        except Exception:
            df = None
    if df is None:
        print(f"Downloading {SOURCE_URL}")
        resp = requests.get(SOURCE_URL, timeout=120)
        resp.raise_for_status()
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_bytes(resp.content)
        df = pd.read_csv(CACHE_FILE)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")
    return df


def yahoo_ticker(t: str) -> str:
    """Convert dot-form share classes to Yahoo dash form (BRK.B -> BRK-B)."""
    return t.replace(".", "-")


def build_snapshots(hist: pd.DataFrame) -> dict:
    """Build quarter-end snapshots using the last observation <= quarter-end."""
    quarters = pd.date_range("2000-03-31", "2025-06-30", freq="QE")

    snapshots = {}
    all_dates = hist.index
    for q_end in quarters:
        # Latest daily observation at or before quarter-end
        obs = all_dates[all_dates <= q_end]
        if len(obs) == 0:
            continue
        latest = obs[-1]
        raw = hist.loc[latest, "tickers"]
        tickers = sorted(
            {yahoo_ticker(t.strip()) for t in raw.split(",") if t.strip()}
        )
        q_str = q_end.strftime("%Y-%m-%d")
        snapshots[q_str] = tickers
    return snapshots


def validate(snapshots: dict) -> None:
    """Run quality gates: counts, turnover, known-ticker timing."""
    keys = sorted(snapshots)
    assert len(keys) == 102, f"Expected 102 quarters, got {len(keys)}"
    print(f"\nValidation ({len(keys)} quarters):")

    for i, q in enumerate(keys):
        n = len(snapshots[q])
        assert 490 <= n <= 510, f"{q}: {n} tickers out of range 490-510"
        assert len(snapshots[q]) == len(set(snapshots[q])), f"{q}: duplicates!"
        if i > 0:
            prev = set(snapshots[keys[i - 1]])
            cur = set(snapshots[q])
            turnover = len(prev ^ cur)
            if turnover > 25:
                print(f"  WARNING {q}: turnover {turnover} unusually high")

    known = {
        "AAPL": ("2000-03-31", None),
        "MSFT": ("2000-03-31", None),
        "GOOGL": ("2006-06-30", None),   # no Google until Aug 2004
        "TSLA": ("2020-12-31", None),    # added Dec 2020
        "MRNA": ("2021-03-31", None),    # added Dec 2020, still a member
        "TWX": ("2000-03-31", "2018-06-30"),   # Time Warner merged into AT&T 2018
        "TWTR": ("2018-06-30", "2022-12-31"),  # Twitter delisted after Musk buyout 2022
        "ENRNQ": ("2000-03-31", "2002-06-30"), # Enron, delisted/zeroed post-bankruptcy
    }
    for ticker, (first_q, last_q) in known.items():
        present_quarters = [q for q in keys if ticker in snapshots[q]]
        assert present_quarters, f"{ticker} never present!"
        assert present_quarters[0] >= first_q, (
            f"{ticker} present too early: first at {present_quarters[0]} (expected from {first_q})"
        )
        if last_q is not None:
            # Delisted names must eventually disappear (no presence after last_q).
            assert not any(q > last_q for q in keys if ticker in snapshots.get(q, [])), (
                f"{ticker} should be absent by {last_q}"
            )
            absent_from = next((q for q in keys if q >= last_q and ticker not in snapshots[q]), None)
            assert absent_from is not None, f"{ticker} never absent after {last_q}"
            print(f"  OK {ticker}: {present_quarters[0]} .. {absent_from}")
        else:
            print(f"  OK {ticker}: {present_quarters[0]} .. {present_quarters[-1]}")

    total_added = sum(
        len(set(snapshots[keys[i]]) - set(snapshots[keys[i - 1]])) for i in range(1, len(keys))
    )
    print(f"\n  Mean quarterly turnover: {total_added / (len(keys)-1):.1f} tickers")
    print("  Validation PASSED")


def main():
    hist = download_csv()
    snapshots = build_snapshots(hist)
    validate(snapshots)

    payload = {
        "_meta": {
            "description": (
                "Real S&P 500 quarterly constituents, point-in-time (survivorship "
                "bias eliminated). Snapshot per quarter-end uses the last daily "
                "observation <= quarter-end from the fja05680/sp500 reconstruction."
            ),
            "source": SOURCE_REPO,
            "source_url": SOURCE_URL,
            "ticker_format": "Yahoo dash form (BRK.B -> BRK-B)",
            "quarter_end": "Quarter-end dates (2000-03-31 onwards, quarterly)",
            "tickers": "Actual S&P 500 constituents as of each quarter-end",
            "generated": datetime.now().isoformat(timespec="seconds"),
            "quarters": len(snapshots),
        }
    }
    payload.update(snapshots)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nWrote {OUTPUT_PATH} ({len(snapshots)} quarters)")


if __name__ == "__main__":
    sys.exit(main())