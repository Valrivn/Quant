"""Data layer for the diversification sleeve: price and FRED series fetching."""

import json
import time
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

from Quantitative.shared.fred_scraper import FREDScraper

SLEEVES = {
    "corporate_bonds": ["VCSH", "VCIT"],
    "short_bills": ["BIL", "SHY"],
    "gold": ["GLD", "IAU"],
    "equity_income": ["VTI", "VB", "BND"],
}

CACHE_DIR = Path(__file__).resolve().parents[1] / "data"
NASDAQ_BASE = "https://api.nasdaq.com/api/quote/{sym}/chart"
NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "Chrome/125.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/plain, */*",
}


def _nasdaq_cache_path(sym):
    return CACHE_DIR / "nasdaq" / f"{sym}.csv"


def fetch_nasdaq(symbols: list, start: str, end: str, cache_dir: Path = None) -> pd.DataFrame:
    """Fetch daily Close prices from the Nasdaq public API (non-yfinance source).

    Symbols are exchange tickers (SPY, GLD, ...) fetched as ``assetclass=etf``.
    Results are cached to disk (``cache_dir`` or the repo ``data/`` tree) to
    keep request volume low. Returns a DataFrame indexed by date with one column
    per symbol; per-symbol failures are skipped silently. Price integrity is
    verified via return correlation against the yfinance series in the
    simulation report.
    """
    cache_root = cache_dir or CACHE_DIR
    cache_root.mkdir(parents=True, exist_ok=True)
    (cache_root / "nasdaq").mkdir(exist_ok=True)
    session = requests.Session()
    frames = {}
    for sym in symbols:
        cache = cache_root / "nasdaq" / f"{sym}.csv"
        if cache.exists():
            try:
                s = pd.read_csv(cache, index_col=0, parse_dates=True)["close"]
                s = s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
                if not s.empty:
                    frames[sym] = s
                continue
            except Exception:
                pass
        url = (
            f"{NASDAQ_BASE.format(sym=sym)}?assetclass=etf"
            f"&fromdate={start}&todate={end}&random=1"
        )
        try:
            resp = session.get(url, headers=NASDAQ_HEADERS, timeout=25)
            if resp.status_code != 200:
                continue
            chart = resp.json().get("data", {}).get("chart", [])
            if not chart:
                continue
            dates, closes = [], []
            for entry in chart:
                z = entry.get("z", {})
                dt = z.get("dateTime")
                close = z.get("value")
                if dt is None or close is None:
                    continue
                dates.append(pd.to_datetime(dt))
                closes.append(float(str(close).replace("$", "").replace(",", "")))
            s = pd.Series(closes, index=pd.DatetimeIndex(dates))
            s = s[~s.index.duplicated()].sort_index()
            s = s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
            if not s.empty:
                frames[sym] = s
                pd.DataFrame({"close": s}).to_csv(cache)
        except Exception:
            continue
        finally:
            time.sleep(0.6)
    return pd.DataFrame(frames) if frames else pd.DataFrame()


def fetch_sleeve_prices(tickers: list, start: str, end: str) -> pd.DataFrame:
    """Fetch daily close prices for tickers between start and end.

    Returns a DataFrame indexed by date with one column per ticker; empty on failure.
    """
    if not tickers:
        return pd.DataFrame()
    try:
        data = yf.download(tickers, start=start, end=end, progress=False, auto_adjust=True)
    except Exception:
        return pd.DataFrame()
    if data is None or data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        close = data["Close"] if "Close" in data.columns.get_level_values(0) else data
    else:
        close = data["Close"] if "Close" in data.columns else data
    if close.ndim == 1:
        close = close.to_frame(tickers[0])
    return close


def fetch_dividend_history(tickers: list, start: str, end: str, cache_dir: Path = None) -> dict:
    """Fetch per-share dividend histories (ex-date indexed) from yfinance.

    Best-effort, per-symbol failures skipped; results cached to disk under
    ``<cache_dir>/dividends/<SYM>.csv`` so repeat runs are offline. Used by the
    Phase-2 stable-dividend audit as the primary dividend feed; the SEC XBRL
    cross-check (dividend_audit.xbrl_dividend_crosscheck) is the second source.
    """
    cache_root = cache_dir or CACHE_DIR
    (cache_root / "dividends").mkdir(parents=True, exist_ok=True)
    out = {}
    for sym in tickers:
        cache = cache_root / "dividends" / f"{sym}.csv"
        if cache.exists():
            try:
                s = pd.read_csv(cache, index_col=0, parse_dates=True).iloc[:, 0]
                s = s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
                if not s.empty:
                    out[sym] = s
                continue
            except Exception:
                pass
        try:
            s = yf.Ticker(sym).dividends
            if s is None or s.empty:
                continue
            s.index = pd.to_datetime(s.index)
            if getattr(s.index, "tz", None) is not None:
                s.index = s.index.tz_localize(None)
            s = s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
            if not s.empty:
                out[sym] = s
                pd.DataFrame({sym: s}).to_csv(cache)
            time.sleep(0.6)
        except Exception:
            time.sleep(0.6)
            continue
    return out


def fetch_fred_series(series_id: str, start: str, end: str, api_key: str = None) -> pd.Series:
    """Fetch a FRED series between start and end.

    Uses the FREDScraper pattern. Returns an empty Series on any failure; never raises.
    """
    try:
        scraper = FREDScraper()
        result = scraper.fetch_series(series_id)
        observations = result.observations
        if not observations:
            return pd.Series(dtype=float)
        series = pd.Series(
            [obs.value for obs in observations],
            index=pd.to_datetime([obs.date for obs in observations]),
        )
        series = series.sort_index()
        series = series[(series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))]
        return series
    except Exception:
        return pd.Series(dtype=float)


def assemble_historical(sleeve_prices: pd.DataFrame, fred_series: dict) -> dict:
    """Bundle sleeve prices and FRED series into a single historical context."""
    return {"prices": sleeve_prices, "fred": fred_series}