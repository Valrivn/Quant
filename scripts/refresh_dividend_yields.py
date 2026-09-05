#!/usr/bin/env python
"""Refresh dividend yields for all universe tickers (ETFs + dividend candidates).

Fetches trailing 12-month dividend yield from yfinance (sum of dividends in
last 365 days / current price). Outputs to ``config/dividend_yields.yaml``
with version/timestamp/checksum for auditability.

Usage:
  python scripts/refresh_dividend_yields.py [--dry-run] [--out PATH]
"""

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "config" / "dividend_yields.yaml"

# Universe: all sleeve ETFs + dividend candidates + P3 tickers
UNIVERSE = [
    # ETFs from sleeves.py SLEEVES
    "SPY", "VCSH", "VCIT", "BIL", "SHY", "SGOV", "GLD", "IAU", "VTI", "VB", "BND",
    # P3 small/mid proxies
    "MDY", "IWM",
    # Dividend candidates
    "JNJ", "PG", "KO", "PEP", "MCD", "CL", "KMB", "TGT", "HD", "GIS", "MO", "O", "VZ",
]


def fetch_yield(symbol: str, max_retries: int = 3) -> float | None:
    """Fetch trailing 12M dividend yield for a single symbol."""
    for attempt in range(max_retries):
        try:
            t = yf.Ticker(symbol)
            divs = t.dividends
            if len(divs) == 0:
                return 0.0
            # yfinance returns tz-aware index (America/New_York)
            cutoff = pd.Timestamp.now(tz="America/New_York") - timedelta(days=365)
            recent = divs[divs.index >= cutoff]
            div_sum = float(recent.sum())
            hist = t.history(period="1d")
            if hist.empty:
                return None
            price = float(hist["Close"].iloc[-1])
            if price <= 0:
                return None
            return div_sum / price
        except Exception:
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
            continue
    return None


def compute_checksum(yields: dict) -> str:
    """SHA-256 of sorted yield values for change detection."""
    payload = json.dumps({k: yields[k] for k in sorted(yields)}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser(description="Refresh dividend yields cache")
    ap.add_argument("--dry-run", action="store_true", help="Print yields without writing")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output YAML path")
    ap.add_argument("--max-retries", type=int, default=3, help="Retries per ticker")
    args = ap.parse_args()

    print(f"Fetching yields for {len(UNIVERSE)} tickers...")
    yields = {}
    errors = []
    for sym in UNIVERSE:
        y = fetch_yield(sym, max_retries=args.max_retries)
        if y is not None:
            yields[sym] = round(y, 6)
            print(f"  {sym}: {y*100:.2f}%")
        else:
            errors.append(sym)
            print(f"  {sym}: FAILED")

    if errors:
        print(f"\nWarning: {len(errors)} tickers failed: {errors}", file=sys.stderr)

    if args.dry_run:
        print(f"\nWould write {len(yields)} yields to {args.out}")
        return 0

    # Build YAML payload
    payload = {
        "yields": {k: yields[k] for k in sorted(yields)},
        "meta": {
            "description": "Trailing 12-month dividend yields (sum of last 365 days of dividends / current price) from yfinance",
            "source": "yfinance Ticker.dividends + current price",
            "universe": "SLEEVES ETFs + P3 proxies + DIVIDEND_CANDIDATES",
            "method": "TTM yield = sum(dividends in last 365 days) / last close",
            "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "tickers": len(yields),
            "checksum": compute_checksum(yields),
            "version": datetime.utcnow().strftime("%Y.%m.%d"),
        },
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as fh:
        yaml.dump(payload, fh, sort_keys=False, default_flow_style=False)
    print(f"\nWrote {args.out} ({len(yields)} yields, checksum {payload['meta']['checksum'][:16]}...)")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())