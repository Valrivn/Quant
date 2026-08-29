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
        
    clean_map = {}
    for t in tickers:
        clean = t.replace("IG_LLM_", "")
        clean_map[clean] = t
        
    download_tickers = list(clean_map.keys())
    
    data = None
    for attempt in range(3):
        try:
            data = yf.download(download_tickers, start=start, end=end, progress=False, auto_adjust=True)
            if data is not None and not data.empty:
                break
        except Exception:
            pass
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    if data is None or data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        close = data["Close"] if "Close" in data.columns.get_level_values(0) else data
    else:
        close = data["Close"] if "Close" in data.columns else data
    if close.ndim == 1:
        close = close.to_frame(download_tickers[0])
        
    # Rename columns back to prefixed names
    close = close.rename(columns=clean_map)
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
        clean = sym.replace("IG_LLM_", "")
        cache = cache_root / "dividends" / f"{clean}.csv"
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
            s = None
            for attempt in range(3):
                try:
                    s = yf.Ticker(clean).dividends
                    if s is not None and not s.empty:
                        break
                except Exception:
                    pass
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
            if s is None or s.empty:
                time.sleep(0.6)
                continue
            s.index = pd.to_datetime(s.index)
            if getattr(s.index, "tz", None) is not None:
                s.index = s.index.tz_localize(None)
            s = s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
            if not s.empty:
                out[sym] = s
                pd.DataFrame({clean: s}).to_csv(cache)
            time.sleep(0.6)
        except Exception:
            time.sleep(0.6)
            continue
    return out


def get_fred_api_key() -> str:
    """Load FRED API key from environment or .env file."""
    import os
    key = os.environ.get("FRED_API_KEY")
    if key:
        return key
    try:
        import dotenv
        env_path = Path(__file__).resolve().parents[1] / ".env"
        if env_path.exists():
            dotenv.load_dotenv(dotenv_path=env_path)
            return os.environ.get("FRED_API_KEY")
    except Exception:
        pass
    return None


def _fetch_fred_api(series_id: str, start: str, end: str, api_key: str) -> pd.Series:
    """Fetch FRED observations using the API."""
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "asc",
        "observation_start": start,
        "observation_end": end,
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 400:
            # FRED says the series does not exist (e.g. discontinued gold-fix):
            # skip the slow scrape fallback for a series that is gone upstream.
            s = pd.Series(dtype=float)
            s.attrs["_fred_api_missing"] = True
            return s
        if resp.status_code != 200:
            return pd.Series(dtype=float)
        data = resp.json()
        observations = data.get("observations", [])
        if not observations:
            return pd.Series(dtype=float)
        
        dates = []
        values = []
        for obs in observations:
            d = obs.get("date")
            v = obs.get("value")
            if d is None or v is None:
                continue
            v_str = str(v).strip()
            if v_str == "." or not v_str:
                continue
            try:
                val = float(v_str)
                dates.append(pd.to_datetime(d))
                values.append(val)
            except ValueError:
                continue
        
        if not dates:
            return pd.Series(dtype=float)
            
        series = pd.Series(values, index=dates)
        series = series.sort_index()
        series = series[(series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))]
        series = series[~series.index.duplicated(keep='first')]
        return series
    except Exception:
        return pd.Series(dtype=float)


def fetch_fred_series(series_id: str, start: str, end: str, api_key: str = None, allow_stale: bool = False) -> pd.Series:
    """Fetch a FRED series between start and end.

    Uses the FRED API if a key is available, falling back to FREDScraper.
    Returns an empty Series on any failure; never raises.
    """
    try:
        resolved_key = api_key or get_fred_api_key()
        is_real_scraper = getattr(FREDScraper, "__module__", "").startswith("Quantitative.shared")
        if resolved_key and is_real_scraper:
            series = _fetch_fred_api(series_id, start, end, resolved_key)
            if series.attrs.get("_fred_api_missing"):
                return pd.Series(dtype=float)
            if not series.empty:
                series.attrs["source"] = "FRED_API"
                return series
            
            scraper = FREDScraper()
            result = scraper.fetch_series(series_id, allow_stale=allow_stale)
            observations = result.observations
            if observations:
                series = pd.Series(
                    [obs.value for obs in observations],
                    index=pd.to_datetime([obs.date for obs in observations]),
                )
                series = series.sort_index()
                series = series[(series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))]
                series = series[~series.index.duplicated(keep='first')]
                if not series.empty:
                    series.attrs["source"] = "FRED_SCRAPE"
                    return series
            return pd.Series(dtype=float)
        else:
            scraper = FREDScraper()
            result = scraper.fetch_series(series_id, allow_stale=allow_stale)
            observations = result.observations
            if not observations:
                return pd.Series(dtype=float)
            series = pd.Series(
                [obs.value for obs in observations],
                index=pd.to_datetime([obs.date for obs in observations]),
            )
            series = series.sort_index()
            series = series[(series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))]
            series = series[~series.index.duplicated(keep='first')]
            if not series.empty:
                if allow_stale and result.retrieval_method == "cache":
                    series.attrs["source"] = "FRED_CACHE_STALE"
                else:
                    series.attrs["source"] = "FRED_SCRAPE"
                return series
            return pd.Series(dtype=float)
    except Exception:
        return pd.Series(dtype=float)


def assemble_historical(sleeve_prices: pd.DataFrame, fred_series: dict) -> dict:
    """Bundle sleeve prices and FRED series into a single historical context."""
    return {"prices": sleeve_prices, "fred": fred_series}