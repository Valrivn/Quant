"""
Price Cache Module (RS-04 CORE)
Fetches historical prices from Yahoo Finance (yfinance) / Alpha Vantage / master_prices with local caching,
computes 2-day trading max forward fill, and sets price_stale_flag and illiquid_flag.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

CACHE_DIR = Path("data/prices")
CACHE_FILE = CACHE_DIR / "price_cache.parquet"
MASTER_PRICES_DIR = Path("data/master_prices")
MANIFEST_FILE = Path("data/provenance/manifest.jsonl")


def record_provenance(file_path: Path, source: str, row_count: int, metadata: Optional[dict] = None) -> str:
    """Computes SHA256 and writes provenance entry to manifest.jsonl."""
    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    sha256 = hasher.hexdigest()

    entry = {
        "file_path": str(file_path),
        "source": source,
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "source_version": "1.0",
        "sha256": sha256,
        "row_count": row_count,
        "metadata": metadata or {}
    }

    with open(MANIFEST_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    return sha256


class PriceCache:
    """Manages price fetching, caching, forward-filling, and quality flags."""

    def __init__(self, cache_file: Path = CACHE_FILE):
        self.cache_file = cache_file
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)

    def load_cache(self) -> pd.DataFrame:
        if self.cache_file.exists():
            try:
                return pd.read_parquet(self.cache_file)
            except Exception as e:
                print(f"Warning: Failed to read existing price cache: {e}")
        return pd.DataFrame(columns=[
            "date", "ticker", "close", "volume", "source",
            "retrieval_timestamp", "price_stale_flag", "illiquid_flag"
        ])

    def fetch_prices(
        self,
        tickers: List[str],
        start_date: str = "2018-01-01",
        end_date: str = "2024-12-31",
        max_forward_fill: int = 2
    ) -> pd.DataFrame:
        """Fetches / loads prices and applies forward fill limit and quality flags."""
        tickers = sorted(list(set(tickers)))
        df_cached = self.load_cache()

        existing_tickers = set(df_cached["ticker"].unique()) if not df_cached.empty else set()
        missing_tickers = [t for t in tickers if t not in existing_tickers]

        new_rows = []
        if missing_tickers:
            print(f"Fetching prices for {len(missing_tickers)} tickers...")
            fetched_df = self._fetch_missing_tickers(missing_tickers, start_date, end_date)
            if not fetched_df.empty:
                new_rows.append(fetched_df)

        if new_rows:
            df_new = pd.concat(new_rows, ignore_index=True)
            if not df_cached.empty:
                df_combined = pd.concat([df_cached, df_new], ignore_index=True).drop_duplicates(subset=["date", "ticker"])
            else:
                df_combined = df_new
        else:
            df_combined = df_cached

        if df_combined.empty:
            print("No cached or fetched prices found. Generating fallback synthetic price data...")
            df_combined = self._generate_synthetic_prices(tickers, start_date, end_date)

        # Filter df_combined for requested tickers
        df_combined = df_combined[df_combined["ticker"].isin(tickers)].copy()

        # Apply forward fill with max threshold per ticker
        processed_dfs = []
        df_combined["date"] = pd.to_datetime(df_combined["date"])

        for ticker, group in df_combined.groupby("ticker"):
            group = group.sort_values("date").drop_duplicates(subset=["date"]).set_index("date")
            full_idx = pd.date_range(start=start_date, end=end_date, freq="B")
            reindexed = group.reindex(full_idx)
            reindexed["ticker"] = ticker

            # Price gap / forward fill tracking
            close_ffill = reindexed["close"].ffill(limit=max_forward_fill)

            # quote_age tracks consecutive NA days filled
            valid_mask = reindexed["close"].notna()
            quote_age = np.zeros(len(reindexed), dtype=int)
            current_age = 0
            for i, valid in enumerate(valid_mask):
                if valid:
                    current_age = 0
                else:
                    current_age += 1
                quote_age[i] = current_age

            reindexed["close"] = close_ffill
            reindexed["price_stale_flag"] = quote_age > max_forward_fill

            # Volume 20d average dollar volume
            dollar_vol = reindexed["close"] * reindexed["volume"].fillna(0)
            avg_dollar_vol_20d = dollar_vol.rolling(window=20, min_periods=1).mean()
            reindexed["illiquid_flag"] = avg_dollar_vol_20d < 100_000

            reindexed["source"] = reindexed["source"].fillna("yahoo")
            reindexed["retrieval_timestamp"] = reindexed["retrieval_timestamp"].fillna(datetime.now(timezone.utc).isoformat())

            reindexed = reindexed.reset_index().rename(columns={"index": "date"})
            reindexed["date"] = reindexed["date"].dt.strftime("%Y-%m-%d")
            
            # Keep rows with prices (including forward filled)
            reindexed = reindexed[reindexed["close"].notna()]
            processed_dfs.append(reindexed)

        final_df = pd.concat(processed_dfs, ignore_index=True) if processed_dfs else pd.DataFrame()

        # Save cache and record provenance
        final_df.to_parquet(self.cache_file, index=False)
        record_provenance(self.cache_file, "PriceCache/MultiSource", len(final_df))
        return final_df

    def _fetch_missing_tickers(self, missing_tickers: List[str], start_date: str, end_date: str) -> pd.DataFrame:
        fetched_dfs = []
        still_missing = []

        # 1. Try local master prices first
        print(f"Checking local master_prices repository for {len(missing_tickers)} tickers...")
        for ticker in missing_tickers:
            df_master = self._read_master_price(ticker, start_date, end_date)
            if not df_master.empty:
                fetched_dfs.append(df_master)
            else:
                still_missing.append(ticker)

        print(f"Master prices satisfied {len(missing_tickers) - len(still_missing)} tickers. {len(still_missing)} remaining.")

        # 2. Try Yahoo Finance batching for remaining tickers
        if still_missing:
            print(f"Fetching {len(still_missing)} tickers from Yahoo Finance in batches of 50 with 2s delay...")
            yf_dfs, yf_missing = self._fetch_yahoo_batch(still_missing, start_date, end_date)
            if not yf_dfs.empty:
                fetched_dfs.append(yf_dfs)
            still_missing = yf_missing
            print(f"Yahoo Finance returned data. {len(still_missing)} tickers still missing.")

        # 3. Try AlphaVantage fallback for remaining missing tickers
        if still_missing:
            av_api_key = os.getenv("ALPHAVANTAGE_API_KEY", "")
            if av_api_key:
                print(f"Attempting AlphaVantage fallback for {len(still_missing)} tickers...")
                av_dfs, av_missing = self._fetch_alphavantage_fallback(still_missing, start_date, end_date, av_api_key)
                if not av_dfs.empty:
                    fetched_dfs.append(av_dfs)
                still_missing = av_missing

        # 4. Generate synthetic fallback for any remaining missing tickers (e.g. synthetic test tickers)
        if still_missing:
            print(f"Generating synthetic fallback prices for {len(still_missing)} unresolvable tickers...")
            synth_df = self._generate_synthetic_prices(still_missing, start_date, end_date)
            fetched_dfs.append(synth_df)

        return pd.concat(fetched_dfs, ignore_index=True) if fetched_dfs else pd.DataFrame()

    def _read_master_price(self, ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
        master_file = MASTER_PRICES_DIR / f"{ticker}.parquet"
        if master_file.exists():
            try:
                df = pd.read_parquet(master_file)
                if df.empty:
                    return pd.DataFrame()
                df = df.reset_index()
                date_col = [c for c in df.columns if c.lower() in ("date", "index")][0]
                close_col = [c for c in df.columns if c.lower() in ("close", "adj close")][0]
                vol_col = [c for c in df.columns if c.lower() in ("volume", "vol")][0]

                df["date"] = pd.to_datetime(df[date_col]).dt.tz_localize(None).dt.strftime("%Y-%m-%d")
                df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
                if df.empty:
                    return pd.DataFrame()

                df["ticker"] = ticker
                df["close"] = df[close_col].astype(float)
                df["volume"] = df[vol_col].astype(float)
                df["source"] = "master_prices"
                df["retrieval_timestamp"] = datetime.now(timezone.utc).isoformat()
                return df[["date", "ticker", "close", "volume", "source", "retrieval_timestamp"]]
            except Exception:
                pass
        return pd.DataFrame()

    def _fetch_yahoo_batch(
        self,
        tickers: List[str],
        start_date: str,
        end_date: str,
        batch_size: int = 50,
        delay_seconds: float = 2.0
    ) -> tuple[pd.DataFrame, List[str]]:
        try:
            import yfinance as yf
        except ImportError:
            print("yfinance not installed. Skipping Yahoo Finance batch download.")
            return pd.DataFrame(), tickers

        all_dfs = []
        missing = []
        chunks = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]

        for idx, chunk in enumerate(chunks):
            if idx > 0:
                time.sleep(delay_seconds)
            
            ticker_str = " ".join(chunk)
            try:
                df = yf.download(ticker_str, start=start_date, end=end_date, progress=False, group_by="ticker")
                if not df.empty:
                    for t in chunk:
                        try:
                            if len(chunk) == 1:
                                t_df = df.copy()
                            else:
                                t_df = df[t].copy() if t in df.columns.levels[0] else pd.DataFrame()
                            
                            if not t_df.empty and "Close" in t_df.columns:
                                t_df = t_df.dropna(subset=["Close"]).reset_index()
                                date_col = [c for c in t_df.columns if c.lower() in ("date", "index")][0]
                                t_df["date"] = pd.to_datetime(t_df[date_col]).dt.tz_localize(None).dt.strftime("%Y-%m-%d")
                                t_df["ticker"] = t
                                t_df["close"] = t_df["Close"].astype(float)
                                t_df["volume"] = t_df["Volume"].astype(float) if "Volume" in t_df.columns else 0.0
                                t_df["source"] = "yahoo"
                                t_df["retrieval_timestamp"] = datetime.now(timezone.utc).isoformat()
                                all_dfs.append(t_df[["date", "ticker", "close", "volume", "source", "retrieval_timestamp"]])
                            else:
                                missing.append(t)
                        except Exception:
                            missing.append(t)
                else:
                    missing.extend(chunk)
            except Exception as e:
                missing.extend(chunk)

        combined = pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()
        return combined, list(set(missing))

    def _fetch_alphavantage_fallback(
        self,
        tickers: List[str],
        start_date: str,
        end_date: str,
        api_key: str
    ) -> tuple[pd.DataFrame, List[str]]:
        import urllib.request
        all_dfs = []
        missing = []

        for t in tickers:
            try:
                url = f"https://www.alphavantage.co/query?function=TIME_SERIES_DAILY_ADJUSTED&symbol={t}&outputsize=full&apikey={api_key}"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                
                ts_data = data.get("Time Series (Daily)", {})
                if ts_data:
                    rows = []
                    for d_str, val in ts_data.items():
                        if start_date <= d_str <= end_date:
                            rows.append({
                                "date": d_str,
                                "ticker": t,
                                "close": float(val.get("4. close", val.get("5. adjusted close", 0.0))),
                                "volume": float(val.get("6. volume", 0.0)),
                                "source": "alphavantage",
                                "retrieval_timestamp": datetime.now(timezone.utc).isoformat()
                            })
                    if rows:
                        all_dfs.append(pd.DataFrame(rows))
                    else:
                        missing.append(t)
                else:
                    missing.append(t)
            except Exception:
                missing.append(t)
            
            # Respect free tier rate limit: max 5 calls per minute (12s delay)
            time.sleep(12.0)

        combined = pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()
        return combined, missing

    def _generate_synthetic_prices(self, tickers: List[str], start_date: str, end_date: str) -> pd.DataFrame:
        """Generates synthetic pricing data for testing / offline operation."""
        dates = pd.date_range(start=start_date, end=end_date, freq="B")
        rows = []
        for ticker in tickers:
            np.random.seed(abs(hash(ticker)) % 10000)
            base_price = 50.0 + (abs(hash(ticker)) % 100)
            returns = np.random.normal(0.0005, 0.015, size=len(dates))
            prices = base_price * np.exp(np.cumsum(returns))
            volumes = np.random.randint(10000, 1000000, size=len(dates))

            for d, p, v in zip(dates, prices, volumes):
                rows.append({
                    "date": d.strftime("%Y-%m-%d"),
                    "ticker": ticker,
                    "close": float(p),
                    "volume": float(v),
                    "source": "synthetic_fallback",
                    "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
                    "price_stale_flag": False,
                    "illiquid_flag": False
                })
        return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="Price Cache Engine")
    parser.add_argument("--build", action="store_true", help="Build price cache for specified universe file")
    parser.add_argument("--universe-file", type=Path, default=Path("data/universe/monthly_reconstitution.parquet"), help="Universe parquet file")
    parser.add_argument("--start", type=str, default="2018-01-01", help="Start date")
    parser.add_argument("--end", type=str, default="2024-12-31", help="End date")
    parser.add_argument("--test", action="store_true", help="Run price cache test")
    parser.add_argument("--max-forward-fill", type=int, default=2, help="Max forward fill trading days")
    args = parser.parse_args()

    cache = PriceCache()

    if args.build:
        universe_path = args.universe_file if args.universe_file.exists() else Path("data/universe/monthly_reconstitution.parquet")
        if universe_path.exists():
            if str(universe_path).endswith((".yaml", ".yml")):
                import yaml
                with open(universe_path, "r", encoding="utf-8") as f:
                    u_data = yaml.safe_load(f)
                    tickers = [x["ticker"] for x in u_data.get("tickers", [])]
            else:
                df_u = pd.read_parquet(universe_path)
                tickers = df_u["ticker"].unique().tolist()
            print(f"Building PriceCache for {len(tickers)} universe tickers from {args.start} to {args.end}...")
            df_res = cache.fetch_prices(tickers, start_date=args.start, end_date=args.end, max_forward_fill=args.max_forward_fill)
            print(f"Price Cache Built Successfully!")
            print(f"  Total rows: {len(df_res)}")
            print(f"  Tickers: {df_res['ticker'].nunique()}")
            print(f"  Sources: {df_res['source'].value_counts().to_dict()}")
            print(f"  Date range: {df_res['date'].min()} to {df_res['date'].max()}")
            print(f"  Stale rows: {df_res['price_stale_flag'].sum()}, Illiquid rows: {df_res['illiquid_flag'].sum()}")
        else:
            print(f"Error: Universe file {universe_path} not found.")

    elif args.test:
        print(f"Testing PriceCache with max_forward_fill={args.max_forward_fill}...")
        sample_tickers = ["AAPL", "MSFT", "GOOGL", "DELISTED_TEST"]
        df = cache.fetch_prices(sample_tickers, start_date="2024-01-01", end_date="2024-01-31", max_forward_fill=args.max_forward_fill)
        print(f"Price cache test successful. Output rows: {len(df)}")
        print(df.head())


if __name__ == "__main__":
    main()
