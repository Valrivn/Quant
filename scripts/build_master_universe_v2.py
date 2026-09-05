#!/usr/bin/env python
"""
Build master universe from local ticker_master.csv + yfinance enrichment.
Filters for liquid US stocks (major exchanges, >$300M market cap).
Outputs config/master_universe.yaml
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import pandas as pd
import yaml
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]

# Major US exchanges for liquid stocks
MAJOR_EXCHANGES = {
    "NYSE", "NASDAQ Global Select", "NASDAQ Global", "NYSE Arca", "BATS"
}

# Market cap tiers (in USD)
TIER_THRESHOLDS = {
    "mega": 200e9,      # >$200B
    "large": 10e9,      # $10B - $200B
    "mid": 2e9,         # $2B - $10B
    "small": 300e6,     # $300M - $2B
    "micro": 0,         # <$300M
}

# Known index memberships (from our PIT data)
with open(ROOT / "data" / "pit_sp500_constituents.json") as f:
    pit_data = json.load(f)
SP500_TICKERS = set()
for k, v in pit_data.items():
    if k != "_meta":
        SP500_TICKERS.update(v)

# Known dividend aristocrats (static list)
DIVIDEND_ARISTOCRATS = {
    "AAPL", "MSFT", "JNJ", "PG", "KO", "PEP", "MCD", "CL", "KMB", "TGT",
    "HD", "GIS", "MO", "VZ", "XOM", "CVX", "CAT", "MMM", "EMR", "ITW",
    "DOV", "PH", "ROP", "ROK", "SWK", "SNA", "CTAS", "CHD", "CINF", "EXPD",
}

# Known KBW Bank Index components
KBW_BANKS = {
    "JPM", "BAC", "WFC", "C", "GS", "MS", "USB", "PNC", "TFC", "COF",
    "BK", "STT", "NTRS", "RF", "KEY", "HBAN", "FITB", "ZION", "CFG",
    "MTB", "CMA", "PBCT", "CBSH", "UMBF", "WBS", "FNB", "COLB", "FFIN",
}

# Known key ETFs (manually added since ticker_master is incomplete)
KEY_ETFS = {
    "SPY": {"cik": 1090872, "company_name": "SPDR S&P 500 ETF Trust", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Equity"},
    "VCSH": {"cik": 1215834, "company_name": "Vanguard Short-Term Corporate Bond ETF", "exchange": "NASDAQ", "sector": "ETF", "industry": "ETF - Bond"},
    "VCIT": {"cik": 1215834, "company_name": "Vanguard Intermediate-Term Corporate Bond ETF", "exchange": "NASDAQ", "sector": "ETF", "industry": "ETF - Bond"},
    "BIL": {"cik": 1215834, "company_name": "SPDR Bloomberg 1-3 Month T-Bill ETF", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Bond"},
    "SHY": {"cik": 1215834, "company_name": "iShares 1-3 Year Treasury Bond ETF", "exchange": "NASDAQ", "sector": "ETF", "industry": "ETF - Bond"},
    "SGOV": {"cik": 1215834, "company_name": "iShares 0-3 Month Treasury Bond ETF", "exchange": "NASDAQ", "sector": "ETF", "industry": "ETF - Bond"},
    "GLD": {"cik": 1371085, "company_name": "SPDR Gold Shares", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Commodity"},
    "IAU": {"cik": 1371085, "company_name": "iShares Gold Trust", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Commodity"},
    "VTI": {"cik": 1215834, "company_name": "Vanguard Total Stock Market ETF", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Equity"},
    "VB": {"cik": 1215834, "company_name": "Vanguard Small-Cap ETF", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Equity"},
    "BND": {"cik": 1215834, "company_name": "Vanguard Total Bond Market ETF", "exchange": "NASDAQ", "sector": "ETF", "industry": "ETF - Bond"},
    "MDY": {"cik": 1215834, "company_name": "SPDR S&P MidCap 400 ETF Trust", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Equity"},
    "IWM": {"cik": 1215834, "company_name": "iShares Russell 2000 ETF", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Equity"},
    "LQD": {"cik": 1215834, "company_name": "iShares iBoxx Investment Grade Corporate Bond ETF", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Bond"},
    "HYG": {"cik": 1215834, "company_name": "iShares iBoxx High Yield Corporate Bond ETF", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Bond"},
    "TLT": {"cik": 1215834, "company_name": "iShares 20+ Year Treasury Bond ETF", "exchange": "NASDAQ", "sector": "ETF", "industry": "ETF - Bond"},
    "IEF": {"cik": 1215834, "company_name": "iShares 7-10 Year Treasury Bond ETF", "exchange": "NASDAQ", "sector": "ETF", "industry": "ETF - Bond"},
    "SHV": {"cik": 1215834, "company_name": "iShares Short Treasury Bond ETF", "exchange": "NASDAQ", "sector": "ETF", "industry": "ETF - Bond"},
    "VEA": {"cik": 1215834, "company_name": "Vanguard FTSE Developed Markets ETF", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Equity"},
    "VWO": {"cik": 1215834, "company_name": "Vanguard FTSE Emerging Markets ETF", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Equity"},
    "VNQ": {"cik": 1215834, "company_name": "Vanguard Real Estate ETF", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Real Estate"},
    "SGOV": {"cik": 1215834, "company_name": "iShares 0-3 Month Treasury Bond ETF", "exchange": "NASDAQ", "sector": "ETF", "industry": "ETF - Bond"},
    "SHV": {"cik": 1215834, "company_name": "iShares Short Treasury Bond ETF", "exchange": "NASDAQ", "sector": "ETF", "industry": "ETF - Bond"},
    "SHY": {"cik": 1215834, "company_name": "iShares 1-3 Year Treasury Bond ETF", "exchange": "NASDAQ", "sector": "ETF", "industry": "ETF - Bond"},
    "BIL": {"cik": 1215834, "company_name": "SPDR Bloomberg 1-3 Month T-Bill ETF", "exchange": "NYSE Arca", "sector": "ETF", "industry": "ETF - Bond"},
}

def get_market_cap(ticker: str) -> float:
    """Get market cap from yfinance."""
    try:
        t = yf.Ticker(ticker)
        info = t.info
        return info.get("marketCap", 0) or 0
    except Exception:
        return 0

def get_sector(ticker: str) -> str:
    """Get sector from yfinance."""
    try:
        t = yf.Ticker(ticker)
        info = t.info
        return info.get("sector", "Unknown") or "Unknown"
    except Exception:
        return "Unknown"

def assign_tier(market_cap: float, ticker: str) -> str:
    """Assign tier based on market cap and index membership."""
    if ticker in SP500_TICKERS:
        return "core"  # S&P 500 members
    
    for tier, threshold in TIER_THRESHOLDS.items():
        if market_cap >= threshold:
            return tier
    return "micro"

def assign_tags(ticker: str, tier: str) -> List[str]:
    """Assign descriptive tags."""
    tags = [tier]
    if ticker in SP500_TICKERS:
        tags.append("sp500")
    if ticker in DIVIDEND_ARISTOCRATS:
        tags.append("dividend_aristocrat")
    if ticker in KBW_BANKS:
        tags.append("bank")
    # Tech sector heuristic
    tech_tickers = {"AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "META", "AVGO", "ADBE",
                    "CRM", "ORCL", "INTC", "AMD", "QCOM", "TXN", "AMAT", "MU", "NOW",
                    "INTU", "AMZN", "NFLX", "UBER", "PYPL", "SHOP", "SQ", "SNOW",
                    "PLTR", "DDOG", "ZS", "CRWD", "OKTA", "TEAM", "ATLASSIAN", "TWLO"}
    if ticker in tech_tickers:
        tags.append("tech")
    return tags

def main():
    print("Loading ticker master...")
    master_df = pd.read_csv(ROOT / "data" / "ticker_master.csv")
    
    # Filter: major exchanges, include ETFs, active status
    filtered = master_df[
        (master_df["exchange"].isin(MAJOR_EXCHANGES)) &
        (master_df["status_flag"].isna() | (master_df["status_flag"] == ""))
    ].copy()
    
    # Add key ETFs not in ticker_master
    etf_rows = []
    for sym, info in KEY_ETFS.items():
        if sym not in filtered["ticker"].values:
            etf_rows.append({
                "ticker": sym,
                "cik": info["cik"],
                "company_name": info["company_name"],
                "exchange": info["exchange"],
                "is_etf": 1,
                "is_test_issue": 0,
                "status_flag": None,
            })
    if etf_rows:
        filtered = pd.concat([filtered, pd.DataFrame(etf_rows)], ignore_index=True)
    
    print(f"Filtered to {len(filtered)} tickers on major exchanges")
    
    # Get market caps and sectors via yfinance (batched)
    tickers = filtered["ticker"].tolist()
    print(f"Fetching market caps/sectors for {len(tickers)} tickers...")
    
    results = []
    batch_size = 50
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i+batch_size]
        print(f"  Batch {i//batch_size + 1}/{(len(tickers)-1)//batch_size + 1}: {batch[:3]}...")
        
        try:
            batch_data = yf.Tickers(" ".join(batch))
            for sym in batch:
                try:
                    info = batch_data.tickers[sym].info
                    mc = info.get("marketCap", 0) or 0
                    sector = info.get("sector", "Unknown") or "Unknown"
                    industry = info.get("industry", "Unknown") or "Unknown"
                except Exception:
                    mc = 0
                    sector = "Unknown"
                    industry = "Unknown"
                
                # Get row from filtered or KEY_ETFS
                if sym in filtered["ticker"].values:
                    row = filtered[filtered["ticker"] == sym].iloc[0]
                else:
                    row = KEY_ETFS.get(sym, {})
                
                tier = assign_tier(mc, sym)
                tags = assign_tags(sym, tier)
                
                results.append({
                    "ticker": sym,
                    "cik": int(row.get("cik", 0)),
                    "company_name": row.get("company_name", sym),
                    "exchange": row.get("exchange", "Unknown"),
                    "market_cap": mc,
                    "sector": sector,
                    "industry": industry,
                    "tier": tier,
                    "tags": tags,
                    "pit_eligible": sym in SP500_TICKERS,
                })
        except Exception as e:
            print(f"  Batch error: {e}")
            # Fallback: try individual
            for sym in batch:
                mc = get_market_cap(sym)
                sector = get_sector(sym)
                row = filtered[filtered["ticker"] == sym].iloc[0]
                tier = assign_tier(mc, sym)
                tags = assign_tags(sym, tier)
                results.append({
                    "ticker": sym,
                    "cik": int(row["cik"]),
                    "company_name": row["company_name"],
                    "exchange": row["exchange"],
                    "market_cap": mc,
                    "sector": sector,
                    "industry": "Unknown",
                    "tier": tier,
                    "tags": tags,
                    "pit_eligible": sym in SP500_TICKERS,
                })
        
        time.sleep(1)  # Rate limit
    
    # Sort by tier priority then market cap
    tier_order = {"core": 0, "mega": 1, "large": 2, "mid": 3, "small": 4, "micro": 5}
    results.sort(key=lambda x: (tier_order.get(x["tier"], 99), -x["market_cap"]))
    
    # Build output
    output = {
        "meta": {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "total_tickers": len(results),
            "source": "ticker_master.csv + yfinance enrichment",
            "tier_counts": {t: sum(1 for r in results if r["tier"] == t) for t in tier_order},
            "sector_counts": {s: sum(1 for r in results if r["sector"] == s) for s in set(r["sector"] for r in results)},
            "tags_summary": {tag: sum(1 for r in results if tag in r["tags"]) for tag in 
                            ["core", "mega", "large", "mid", "small", "micro", "sp500", "dividend_aristocrat", "bank", "tech"]},
        },
        "tickers": results,
    }
    
    out_path = ROOT / "config" / "master_universe.yaml"
    with open(out_path, "w") as f:
        yaml.dump(output, f, sort_keys=False, default_flow_style=False)
    
    print(f"\nDone! Wrote {len(results)} tickers to {out_path}")
    print(f"Tier breakdown: {output['meta']['tier_counts']}")
    print(f"Sector breakdown: {output['meta']['sector_counts']}")
    print(f"Tags: {output['meta']['tags_summary']}")


if __name__ == "__main__":
    main()