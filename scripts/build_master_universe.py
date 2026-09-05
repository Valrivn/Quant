#!/usr/bin/env python
"""
Build master universe ticker list from multiple free sources.
Sources: S&P 500/400/600 (PIT), NASDAQ 100, Russell 3000, Dividend Aristocrats, KBW Banks.
Respects rate limits, uses caching, outputs config/master_universe.yaml
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "data" / "universe_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Rate limiting
REQUEST_DELAY = 2.0  # seconds between requests
MAX_RETRIES = 3

# Source configurations
SOURCES = {
    "sp500_pit": {
        "file": ROOT / "data" / "pit_sp500_constituents.json",
        "type": "pit_json",
        "tier": "core",
    },
    "sp400_wiki": {
        "url": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
        "type": "wiki_table",
        "table_idx": 0,
        "ticker_col": "Symbol",
        "tier": "mid",
    },
    "sp600_wiki": {
        "url": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
        "type": "wiki_table",
        "table_idx": 0,
        "ticker_col": "Symbol",
        "tier": "small",
    },
    "nasdaq100_wiki": {
        "url": "https://en.wikipedia.org/wiki/Nasdaq-100",
        "type": "wiki_table",
        "table_idx": 4,  # May vary
        "ticker_col": "Ticker",
        "tier": "tech",
    },
    "dividend_aristocrats_wiki": {
        "url": "https://en.wikipedia.org/wiki/Dividend_aristocrats",
        "type": "wiki_table",
        "table_idx": 0,
        "ticker_col": "Symbol",
        "tier": "dividend",
    },
    "kbw_banks_wiki": {
        "url": "https://en.wikipedia.org/wiki/KBW_Bank_Index",
        "type": "wiki_table",
        "table_idx": 0,
        "ticker_col": "Symbol",
        "tier": "banks",
    },
}

# Sector mapping for known tickers (fallback when yfinance not available)
SECTOR_OVERRIDES = {
    # Tech
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology", "GOOGL": "Technology",
    "GOOG": "Technology", "META": "Technology", "AVGO": "Technology", "ADBE": "Technology",
    "CRM": "Technology", "ORCL": "Technology", "INTC": "Technology", "AMD": "Technology",
    "QCOM": "Technology", "TXN": "Technology", "AMAT": "Technology", "MU": "Technology",
    # Banks/Financials
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials", "C": "Financials",
    "GS": "Financials", "MS": "Financials", "USB": "Financials", "PNC": "Financials",
    "TFC": "Financials", "COF": "Financials", "SCHW": "Financials", "BK": "Financials",
    # Healthcare
    "JNJ": "Healthcare", "PFE": "Healthcare", "MRK": "Healthcare", "ABBV": "Healthcare",
    "UNH": "Healthcare", "LLY": "Healthcare", "TMO": "Healthcare", "DHR": "Healthcare",
    # Consumer
    "PG": "Consumer Staples", "KO": "Consumer Staples", "PEP": "Consumer Staples",
    "COST": "Consumer Staples", "WMT": "Consumer Staples", "CL": "Consumer Staples",
    "KMB": "Consumer Staples", "GIS": "Consumer Staples", "K": "Consumer Staples",
    # Industrial
    "CAT": "Industrials", "HON": "Industrials", "UPS": "Industrials", "BA": "Industrials",
    "GE": "Industrials", "MMM": "Industrials", "RTX": "Industrials", "LMT": "Industrials",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "EOG": "Energy", "SLB": "Energy",
    # REITs
    "O": "Real Estate", "AMT": "Real Estate", "PLD": "Real Estate", "CCI": "Real Estate",
    "EQIX": "Real Estate", "SPG": "Real Estate", "VICI": "Real Estate",
}


def fetch_with_cache(url: str, cache_name: str) -> str:
    """Fetch URL with local caching and rate limiting."""
    cache_path = CACHE_DIR / f"{cache_name}.html"
    
    if cache_path.exists():
        age = time.time() - cache_path.stat().st_mtime
        if age < 86400:  # 24 hours
            return cache_path.read_text()
    
    time.sleep(REQUEST_DELAY)
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, timeout=30, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            resp.raise_for_status()
            cache_path.write_text(resp.text)
            return resp.text
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)
    return ""


def parse_wiki_table(html: str, table_idx: int, ticker_col: str) -> List[Dict]:
    """Parse Wikipedia table for tickers."""
    tables = pd.read_html(html)
    if table_idx >= len(tables):
        # Try all tables
        for i, t in enumerate(tables):
            if ticker_col in t.columns:
                table_idx = i
                break
    df = tables[table_idx]
    if ticker_col not in df.columns:
        # Try common alternatives
        for col in ["Symbol", "Ticker", "Ticker symbol"]:
            if col in df.columns:
                ticker_col = col
                break
    tickers = df[ticker_col].astype(str).str.strip().str.upper().tolist()
    # Clean: remove footnotes, special chars
    tickers = [re.sub(r'\[.*?\]', '', t).strip() for t in tickers]
    tickers = [t for t in tickers if t and len(t) <= 6 and t.isalnum()]
    return [{"ticker": t, "source": "wiki"} for t in tickers]


def load_sp500_pit(filepath: Path) -> List[Dict]:
    """Load all unique tickers from PIT S&P 500 data."""
    with open(filepath) as f:
        data = json.load(f)
    tickers = set()
    for k, v in data.items():
        if k != "_meta":
            tickers.update(v)
    return [{"ticker": t, "source": "sp500_pit", "pit": True} for t in sorted(tickers)]


def load_russell3000_csv() -> List[Dict]:
    """Load Russell 3000 from local CSV if available."""
    # Check for cached Russell data
    russell_cache = CACHE_DIR / "russell3000.csv"
    if russell_cache.exists():
        df = pd.read_csv(russell_cache)
        # Assume column 'Ticker' or 'Symbol'
        for col in ["Ticker", "Symbol", "ticker"]:
            if col in df.columns:
                tickers = df[col].astype(str).str.strip().str.upper().tolist()
                return [{"ticker": t, "source": "russell3000"} for t in tickers if t.isalnum()]
    return []


def enrich_with_master(tickers: List[Dict], master_df: pd.DataFrame) -> List[Dict]:
    """Enrich tickers with CIK, company name, exchange from ticker_master.csv."""
    enriched = []
    master_map = master_df.set_index("ticker").to_dict("index")
    for t in tickers:
        sym = t["ticker"]
        info = master_map.get(sym, {})
        enriched.append({
            "ticker": sym,
            "sources": t.get("source", "unknown"),
            "tier": t.get("tier", "extended"),
            "pit_eligible": t.get("pit", False),
            "cik": info.get("cik"),
            "company_name": info.get("company_name"),
            "exchange": info.get("exchange"),
            "is_etf": info.get("is_etf", 0),
        })
    return enriched


def assign_sector(ticker: str, info: Dict) -> str:
    """Assign sector from overrides or master data."""
    if ticker in SECTOR_OVERRIDES:
        return SECTOR_OVERRIDES[ticker]
    # Could add yfinance lookup here if needed
    return "Unknown"


def main():
    print("Building master universe...")
    all_tickers: Dict[str, Dict] = {}
    
    # 1. Load S&P 500 PIT (core)
    print("Loading S&P 500 PIT...")
    sp500 = load_sp500_pit(ROOT / "data" / "pit_sp500_constituents.json")
    for t in sp500:
        all_tickers[t["ticker"]] = {"source": "sp500_pit", "tier": "core", "pit": True}
    
    # 2. Scrape Wikipedia sources in parallel
    wiki_sources = {k: v for k, v in SOURCES.items() if v["type"] == "wiki_table"}
    
    def scrape_source(name: str, cfg: Dict) -> Tuple[str, List[Dict]]:
        print(f"Scraping {name}...")
        html = fetch_with_cache(cfg["url"], name)
        tickers = parse_wiki_table(html, cfg["table_idx"], cfg["ticker_col"])
        for t in tickers:
            t["source"] = name
            t["tier"] = cfg["tier"]
        return name, tickers
    
    # Run sequentially to respect rate limits
    for name, cfg in wiki_sources.items():
        try:
            _, tickers = scrape_source(name, cfg)
            for t in tickers:
                sym = t["ticker"]
                if sym not in all_tickers:
                    all_tickers[sym] = {"source": name, "tier": cfg["tier"]}
                else:
                    # Merge tiers
                    existing = all_tickers[sym]
                    existing["source"] = f"{existing['source']},{name}"
                    # Upgrade tier priority
                    tier_priority = {"core": 5, "tech": 4, "mid": 3, "small": 2, "dividend": 3, "banks": 3, "extended": 1}
                    if tier_priority.get(cfg["tier"], 0) > tier_priority.get(existing.get("tier", "extended"), 0):
                        existing["tier"] = cfg["tier"]
        except Exception as e:
            print(f"  Error scraping {name}: {e}")
    
    # 3. Load Russell 3000 if available
    russell = load_russell3000_csv()
    for t in russell:
        sym = t["ticker"]
        if sym not in all_tickers:
            all_tickers[sym] = {"source": "russell3000", "tier": "extended"}
    
    # 4. Load ticker_master.csv for enrichment
    master_df = pd.read_csv(ROOT / "data" / "ticker_master.csv")
    
    # 5. Enrich all tickers
    ticker_list = [{"ticker": k, **v} for k, v in all_tickers.items()]
    enriched = enrich_with_master(ticker_list, master_df)
    
    # 6. Assign sectors
    for e in enriched:
        e["sector"] = assign_sector(e["ticker"], e)
    
    # 7. Sort and output
    enriched.sort(key=lambda x: (x["tier"], x["ticker"]))
    
    # Build output structure
    output = {
        "meta": {
            "generated_utc": datetime.utcnow().isoformat() + "Z",
            "total_tickers": len(enriched),
            "sources": list(set(s for e in enriched for s in e["sources"].split(","))),
            "tiers": {t: sum(1 for e in enriched if e["tier"] == t) for t in 
                      ["core", "tech", "mid", "small", "dividend", "banks", "extended"]},
        },
        "tickers": enriched,
    }
    
    out_path = ROOT / "config" / "master_universe.yaml"
    with open(out_path, "w") as f:
        yaml.dump(output, f, sort_keys=False, default_flow_style=False)
    
    print(f"\nDone! Wrote {len(enriched)} tickers to {out_path}")
    print(f"Tier breakdown: {output['meta']['tiers']}")


if __name__ == "__main__":
    main()