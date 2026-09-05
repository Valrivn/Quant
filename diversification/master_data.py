#!/usr/bin/env python
"""
Unified access layer for master universe price/dividend data.
Provides PIT-aware loading for backtest engines.
"""

import pandas as pd
from pathlib import Path
from typing import Dict, List, Optional, Union
from functools import lru_cache

ROOT = Path(__file__).resolve().parents[1]
PRICES_DIR = ROOT / "data" / "master_prices"
DIVIDENDS_DIR = ROOT / "data" / "master_dividends"
UNIVERSE_PATH = ROOT / "config" / "master_universe_liquid.yaml"

import yaml

# Cache for universe metadata
@lru_cache(maxsize=1)
def _load_universe() -> Dict:
    with open(UNIVERSE_PATH) as f:
        return yaml.safe_load(f)

@lru_cache(maxsize=1)
def _get_ticker_info() -> Dict[str, Dict]:
    universe = _load_universe()
    return {t["ticker"]: t for t in universe["tickers"]}


def get_universe_tickers(tier: Optional[str] = None, tags: Optional[List[str]] = None) -> List[str]:
    """Get tickers filtered by tier and/or tags."""
    info = _get_ticker_info()
    tickers = list(info.keys())
    
    if tier:
        tickers = [t for t in tickers if info[t].get("tier") == tier]
    if tags:
        tickers = [t for t in tickers if any(tag in info[t].get("tags", []) for tag in tags)]
    
    return sorted(tickers)


def get_sector_map() -> Dict[str, str]:
    """Return ticker -> sector mapping."""
    info = _get_ticker_info()
    return {t: info[t].get("sector", "Unknown") for t in info}


def get_tier_map() -> Dict[str, str]:
    """Return ticker -> tier mapping."""
    info = _get_ticker_info()
    return {t: info[t].get("tier", "unknown") for t in info}


def _normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    """Convert tz-aware index to tz-naive UTC."""
    if hasattr(df.index, 'tz') and df.index.tz is not None:
        df = df.copy()
        df.index = df.index.tz_convert('UTC').tz_localize(None)
    return df


def load_price(ticker: str, start: Optional[str] = None, end: Optional[str] = None,
               columns: Optional[List[str]] = None) -> Optional[pd.DataFrame]:
    """Load price history for a single ticker."""
    path = PRICES_DIR / f"{ticker}.parquet"
    if not path.exists():
        return None
    
    df = pd.read_parquet(path)
    df = _normalize_index(df)
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index <= pd.Timestamp(end)]
    if columns:
        df = df[columns]
    return df


def load_dividends(ticker: str, start: Optional[str] = None, end: Optional[str] = None) -> Optional[pd.Series]:
    """Load dividend history for a single ticker."""
    path = DIVIDENDS_DIR / f"{ticker}.parquet"
    if not path.exists():
        return None
    
    df = pd.read_parquet(path)
    df = _normalize_index(df)
    s = df["Dividends"] if "Dividends" in df.columns else df.iloc[:, 0]
    if start:
        s = s[s.index >= pd.Timestamp(start)]
    if end:
        s = s[s.index <= pd.Timestamp(end)]
    return s


def load_universe_prices(tickers: List[str], start: str, end: str,
                         columns: Optional[List[str]] = None) -> pd.DataFrame:
    """Load price panel for multiple tickers (wide format)."""
    dfs = {}
    for t in tickers:
        df = load_price(t, start, end, columns)
        if df is not None and not df.empty:
            col = columns[0] if columns else "Close"
            dfs[t] = df[col]
    
    if not dfs:
        return pd.DataFrame()
    
    panel = pd.DataFrame(dfs)
    panel.index.name = "Date"
    return panel.sort_index()


def load_universe_dividends(tickers: List[str], start: str, end: str) -> Dict[str, pd.Series]:
    """Load dividend histories for multiple tickers."""
    result = {}
    for t in tickers:
        s = load_dividends(t, start, end)
        if s is not None and not s.empty:
            result[t] = s
    return result


def get_available_tickers(as_of: Optional[str] = None) -> List[str]:
    """Get tickers with data available at or before as_of date."""
    info = _get_ticker_info()
    if as_of is None:
        return list(info.keys())
    
    as_of_ts = pd.Timestamp(as_of)
    available = []
    for ticker in info:
        path = PRICES_DIR / f"{ticker}.parquet"
        if path.exists():
            try:
                df = pd.read_parquet(path)
                if df.index[0] <= as_of_ts:
                    available.append(ticker)
            except Exception:
                pass
    return available


def get_pit_universe(tickers: List[str], as_of: str) -> List[str]:
    """Filter tickers to those in the universe as of a date (PIT)."""
    # For now, all liquid universe tickers are available
    # In future, could integrate with S&P 500 PIT for core tier
    return [t for t in tickers if t in get_available_tickers(as_of)]


# Compatibility functions for existing engines
def fetch_sleeve_prices(tickers: List[str], start: str, end: str) -> pd.DataFrame:
    """Drop-in replacement for datastore.fetch_sleeve_prices."""
    return load_universe_prices(tickers, start, end, columns=["Close"])


def fetch_dividend_history(tickers: List[str], start: str, end: str) -> Dict[str, pd.Series]:
    """Drop-in replacement for datastore.fetch_dividend_history."""
    return load_universe_dividends(tickers, start, end)


def get_discovery_universe(tier: Optional[str] = None, tags: Optional[List[str]] = None, limit: int = 200) -> List[str]:
    """Get tickers from master universe for discovery screening.
    
    Replaces the SQLite-based current_scraper_cohort with the full
    liquid master universe. Filters by tier/tags and limits results.
    """
    tickers = get_universe_tickers(tier=tier, tags=tags)
    return tickers[:limit]


if __name__ == "__main__":
    # Quick test
    print("Testing master_data access layer...")
    tickers = get_universe_tickers(tier="core")
    print(f"Core tickers: {len(tickers)}")
    
    prices = load_universe_prices(tickers[:5], "2020-01-01", "2020-12-31")
    print(f"Price panel shape: {prices.shape}")
    print(f"Tickers: {list(prices.columns)}")
    
    sectors = get_sector_map()
    print(f"Sectors: {set(sectors.values())}")
    
    disc = get_discovery_universe(tier="core", limit=50)
    print(f"Discovery universe (core, 50): {len(disc)} tickers")
    print(f"First 10: {disc[:10]}")