#!/usr/bin/env python3
"""
generate_sector_shock_data.py — One-Time Data Generation Script

Pulls historical annual income statements for sector constituents via yfinance,
computes EBIT year-over-year changes, filters out macro crisis years, and derives
the empirical sector-level operational shock probability.

Operational Shock Definition (Lynch/Damodaran framework):
    1_{EBIT_{t} - EBIT_{t-1}} / EBIT_{t-1} <= -0.30  AND  NOT in crisis year

Crisis year filter: BAA10Y spread > 400 bps (Moody's investment-grade spread).

Output:
    data/sector_shock_probs.json — per-sector shock statistics
    data/sector_ebit_history.csv — raw EBIT panel data

Usage:
    python Quantitative/stochastic/generate_sector_shock_data.py
"""

import json
import csv
import os
import sys
import time
import logging
from pathlib import Path

import yfinance as yf
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_JSON = DATA_DIR / "sector_shock_probs.json"
OUTPUT_CSV = DATA_DIR / "sector_ebit_history.csv"

SECTORS = {
    "semiconductor": [
        "NVDA", "AMD", "INTC", "AVGO", "QCOM", "MRVL", "MU", "SWKS", "LSCC", "TSM",
    ],
    "platform_software": [
        "MSFT", "GOOGL", "META", "CRM", "ADBE", "NOW", "SHOP", "WDAY",
    ],
    "hardware_oem": [
        "AAPL", "TSLA", "AMZN", "DELL", "HPQ", "IBM", "HPE", "SMCI", "ANET",
    ],
}

CRISIS_SPREAD_THRESHOLD_BPS = 400.0
EBIT_SHOCK_THRESHOLD = -0.30


def fetch_annual_ebit(ticker: str, max_retries: int = 3) -> list[dict]:
    """
    Fetch historical annual income statement data for a ticker via yfinance.

    Returns a list of dicts: [{year: int, ebit: float, revenue: float, margin: float}, ...]
    """
    for attempt in range(max_retries):
        try:
            stock = yf.Ticker(ticker)
            inc = stock.income_stmt
            if inc is None or inc.empty:
                logger.warning(f"  {ticker}: No income statement data")
                return []

            rows = []
            for col in inc.columns:
                year = col.year if hasattr(col, "year") else int(str(col)[:4])
                ebit = inc.loc["EBIT", col] if "EBIT" in inc.index else None
                if ebit is None and "Operating Income" in inc.index:
                    ebit = inc.loc["Operating Income", col]
                revenue = inc.loc["Total Revenue", col] if "Total Revenue" in inc.index else None
                if ebit is None or revenue is None or revenue == 0:
                    continue
                margin = float(ebit) / float(revenue)
                rows.append({
                    "year": year,
                    "ticker": ticker,
                    "ebit": float(ebit),
                    "revenue": float(revenue),
                    "margin": margin,
                })
            rows.sort(key=lambda r: r["year"])
            return rows
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
            else:
                logger.error(f"  {ticker}: Failed after {max_retries} attempts: {e}")
                return []
    return []


def detect_crisis_years() -> set[int]:
    """
    Detect crisis years from BAA10Y credit spread data.
    Crisis = spread > 400 bps.
    """
    try:
        from Quantitative.shared.fred_scraper import FredScraper
        scraper = FredScraper()
        result = scraper.fetch_series("BAA10Y")
        crisis_years = set()
        if result and result.data_points:
            for dp in result.data_points:
                if dp.value > CRISIS_SPREAD_THRESHOLD_BPS:
                    crisis_years.add(dp.date.year if hasattr(dp.date, "year") else int(str(dp.date)[:4]))
        return crisis_years
    except Exception as e:
        logger.warning(f"Could not fetch BAA10Y data: {e}. Using hardcoded crisis years.")
        return {2001, 2002, 2008, 2009, 2020}


def compute_sector_shock_stats(
    ebit_panel: list[dict],
    crisis_years: set[int],
) -> dict:
    """
    Compute sector-level operational shock statistics from an EBIT panel.

    Returns dict with:
        p_base: float — empirical base arrival rate
        n_firms: int — number of unique tickers
        n_years: int — total firm-years
        n_shocks: int — number of operational shocks
        margin_vol_10y: float — 10-year average margin volatility
        shock_years: list[int] — years when shocks occurred
        firm_shocks: dict — per-firm shock counts
    """
    if not ebit_panel:
        return {
            "p_base": 0.02,
            "n_firms": 0,
            "n_years": 0,
            "n_shocks": 0,
            "margin_vol_10y": 0.05,
            "shock_years": [],
            "firm_shocks": {},
        }

    by_ticker = {}
    for row in ebit_panel:
        by_ticker.setdefault(row["ticker"], []).append(row)

    n_shocks = 0
    n_firm_years = 0
    shock_years = []
    firm_shocks = {}
    margins_by_year = {}

    for ticker, records in by_ticker.items():
        records.sort(key=lambda r: r["year"])
        firm_shocks[ticker] = 0
        for i in range(1, len(records)):
            prev = records[i - 1]
            curr = records[i]
            if prev["year"] in crisis_years or curr["year"] in crisis_years:
                continue
            n_firm_years += 1
            ebit_change = (curr["ebit"] - prev["ebit"]) / abs(prev["ebit"]) if prev["ebit"] != 0 else 0
            margins_by_year.setdefault(curr["year"], []).append(curr["margin"])
            if ebit_change <= EBIT_SHOCK_THRESHOLD:
                n_shocks += 1
                shock_years.append(curr["year"])
                firm_shocks[ticker] = firm_shocks.get(ticker, 0) + 1

    p_base = n_shocks / n_firm_years if n_firm_years > 0 else 0.02

    margin_vols = []
    for year, margins in margins_by_year.items():
        if len(margins) >= 3:
            margin_vols.append(float(np.std(margins)))
    margin_vol_10y = float(np.mean(margin_vols)) if margin_vols else 0.05

    return {
        "p_base": round(p_base, 6),
        "n_firms": len(by_ticker),
        "n_years": n_firm_years,
        "n_shocks": n_shocks,
        "margin_vol_10y": round(margin_vol_10y, 6),
        "shock_years": sorted(set(shock_years)),
        "firm_shocks": firm_shocks,
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    crisis_years = detect_crisis_years()
    logger.info(f"Crisis years: {sorted(crisis_years)}")

    all_panel = []
    sector_stats = {}

    for sector, tickers in SECTORS.items():
        logger.info(f"\n{'='*50}")
        logger.info(f"Sector: {sector} ({len(tickers)} tickers)")
        logger.info(f"{'='*50}")

        sector_panel = []
        for ticker in tickers:
            logger.info(f"  Fetching {ticker}...")
            records = fetch_annual_ebit(ticker)
            if records:
                logger.info(f"    Got {len(records)} years of data")
            sector_panel.extend(records)
            time.sleep(0.5)

        all_panel.extend(sector_panel)
        stats = compute_sector_shock_stats(sector_panel, crisis_years)
        sector_stats[sector] = stats
        logger.info(f"  Sector {sector}: p_base={stats['p_base']:.4f}, "
                     f"n_shocks={stats['n_shocks']}/{stats['n_years']} firm-years, "
                     f"margin_vol_10y={stats['margin_vol_10y']:.4f}")

    with open(OUTPUT_JSON, "w") as f:
        json.dump(sector_stats, f, indent=2, default=str)
    logger.info(f"\nWrote {OUTPUT_JSON}")

    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["year", "ticker", "ebit", "revenue", "margin"])
        writer.writeheader()
        for row in sorted(all_panel, key=lambda r: (r["year"], r["ticker"])):
            writer.writerow(row)
    logger.info(f"Wrote {OUTPUT_CSV}")

    print("\n" + "=" * 60)
    print("SECTOR SHOCK PROBABILITY SUMMARY")
    print("=" * 60)
    for sector, stats in sector_stats.items():
        print(f"\n  {sector}:")
        print(f"    p_base (empirical):  {stats['p_base']:.4f} ({stats['p_base']*100:.2f}%)")
        print(f"    Firms:               {stats['n_firms']}")
        print(f"    Firm-years:          {stats['n_years']}")
        print(f"    Operational shocks:  {stats['n_shocks']}")
        print(f"    Margin vol (10Y):     {stats['margin_vol_10y']:.4f}")
        if stats['shock_years']:
            print(f"    Shock years:         {stats['shock_years']}")
    print()


if __name__ == "__main__":
    main()
