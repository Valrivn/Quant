#!/usr/bin/env python
"""
Batch download price & dividend history for master universe tickers.
Uses yfinance with parallel workers, rate limiting, resume capability.
Outputs: data/master_prices/<TICKER>.parquet, data/master_dividends/<TICKER>.parquet
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf
import yaml

ROOT = Path(__file__).resolve().parents[1]
PRICES_DIR = ROOT / "data" / "master_prices"
DIVIDENDS_DIR = ROOT / "data" / "master_dividends"
META_PATH = ROOT / "data" / "master_download_meta.json"

PRICES_DIR.mkdir(parents=True, exist_ok=True)
DIVIDENDS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_UNIVERSE = ROOT / "config" / "master_universe_liquid.yaml"
DEFAULT_WORKERS = 2
REQUEST_DELAY = 5.0  # seconds between requests per worker
MAX_RETRIES = 3
CHUNK_SIZE = 50  # tickers per yfinance batch call


def load_universe(path: Path) -> List[Dict]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["tickers"]


def load_meta() -> Dict:
    if META_PATH.exists():
        return json.loads(META_PATH.read_text())
    return {"downloads": {}, "last_full_run": None}


def save_meta(meta: Dict):
    META_PATH.write_text(json.dumps(meta, indent=2, default=str))


def download_batch(tickers: List[str], period: str = "max") -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
    """Download price + dividend history for a batch of tickers."""
    prices = {}
    dividends = {}
    
    try:
        batch = yf.Tickers(" ".join(tickers))
        for sym in tickers:
            try:
                t = batch.tickers[sym]
                # Price history
                hist = t.history(period=period, auto_adjust=True)
                if not hist.empty:
                    prices[sym] = hist[["Open", "High", "Low", "Close", "Volume"]].copy()
                    prices[sym].index.name = "Date"
                
                # Dividend history
                divs = t.dividends
                if not divs.empty:
                    divs_df = divs.to_frame(name="Dividends")
                    divs_df.index.name = "Date"
                    dividends[sym] = divs_df
            except Exception as e:
                print(f"    {sym}: error - {e}")
    except Exception as e:
        print(f"  Batch error: {e}")
    
    return prices, dividends


def save_ticker_data(sym: str, prices: pd.DataFrame, dividends: pd.DataFrame):
    """Save price and dividend data to parquet."""
    if not prices.empty:
        prices.to_parquet(PRICES_DIR / f"{sym}.parquet")
    if not dividends.empty:
        dividends.to_parquet(DIVIDENDS_DIR / f"{sym}.parquet")


def download_worker(ticker_chunk: List[str], worker_id: int, progress: Dict) -> Tuple[int, int]:
    """Worker function for parallel download."""
    success = 0
    failed = 0
    
    for i, sym in enumerate(ticker_chunk):
        price_path = PRICES_DIR / f"{sym}.parquet"
        div_path = DIVIDENDS_DIR / f"{sym}.parquet"
        
        # Skip if already downloaded
        if price_path.exists() and div_path.exists():
            progress["skipped"] += 1
            continue
        
        # Rate limiting
        time.sleep(REQUEST_DELAY)
        
        for attempt in range(MAX_RETRIES):
            try:
                prices, dividends = download_batch([sym])
                if prices.get(sym) is not None or dividends.get(sym) is not None:
                    save_ticker_data(sym, prices.get(sym, pd.DataFrame()), dividends.get(sym, pd.DataFrame()))
                    success += 1
                    progress["completed"] += 1
                    break
            except Exception as e:
                if attempt == MAX_RETRIES - 1:
                    failed += 1
                    progress["failed"] += 1
                    print(f"  Worker {worker_id}: {sym} failed after {MAX_RETRIES} attempts - {e}")
                else:
                    time.sleep(2 ** attempt)
    
    return success, failed


def main():
    ap = argparse.ArgumentParser(description="Download master universe price/dividend data")
    ap.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE, help="Universe YAML path")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Parallel workers")
    ap.add_argument("--resume", action="store_true", help="Skip already downloaded")
    ap.add_argument("--tickers", nargs="+", help="Specific tickers to download")
    args = ap.parse_args()
    
    # Load universe
    if args.tickers:
        universe = [{"ticker": t} for t in args.tickers]
    else:
        universe = load_universe(args.universe)
    
    tickers = [u["ticker"] for u in universe]
    print(f"Universe: {len(tickers)} tickers")
    
    # Load meta
    meta = load_meta()
    
    # Filter already downloaded if resume
    if args.resume:
        existing = set()
        for p in PRICES_DIR.glob("*.parquet"):
            existing.add(p.stem)
        tickers = [t for t in tickers if t not in existing]
        print(f"Resuming: {len(tickers)} remaining (skipped {len(existing)} existing)")
    
    if not tickers:
        print("All tickers already downloaded!")
        return
    
    # Chunk tickers for workers
    chunks = [tickers[i::args.workers] for i in range(args.workers)]
    chunks = [c for c in chunks if c]
    
    print(f"Starting download with {args.workers} workers, {len(chunks)} chunks...")
    print(f"Rate limit: {REQUEST_DELAY}s between requests per worker")
    
    progress = {"completed": 0, "skipped": 0, "failed": 0}
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(download_worker, chunk, i, progress) for i, chunk in enumerate(chunks)]
        
        for future in as_completed(futures):
            s, f = future.result()
    
    elapsed = time.time() - start_time
    
    # Update meta
    meta["last_full_run"] = datetime.now(timezone.utc).isoformat()
    meta["downloads"]["last_run"] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tickers_requested": len(tickers),
        "completed": progress["completed"],
        "skipped": progress["skipped"],
        "failed": progress["failed"],
        "elapsed_seconds": elapsed,
    }
    save_meta(meta)
    
    # Summary
    print(f"\n=== Download Complete ===")
    print(f"Completed: {progress['completed']}")
    print(f"Skipped (existing): {progress['skipped']}")
    print(f"Failed: {progress['failed']}")
    print(f"Time: {elapsed:.1f}s")
    print(f"Prices dir: {PRICES_DIR} ({len(list(PRICES_DIR.glob('*.parquet')))} files)")
    print(f"Dividends dir: {DIVIDENDS_DIR} ({len(list(DIVIDENDS_DIR.glob('*.parquet')))} files)")


if __name__ == "__main__":
    main()