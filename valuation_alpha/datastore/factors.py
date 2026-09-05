"""Factor and benchmark data: Ken French 5-factor and S&P 500."""

import io
import os
import zipfile
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import requests

FF5_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
FF5_URL_MONTHLY = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_CSV.zip"

_COLUMNS = ["date", "Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]

_FRED_SERIES = {
    "Mkt-RF": "Mkt-RF",
    "SMB": "SMB",
    "HML": "HML",
    "RMW": "RMW",
    "CMA": "CMA",
    "RF": "RF",
}

_CACHE_DIR = Path("data/fred_cache")
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_FF5_CACHE = _CACHE_DIR / "ff5_factors_daily.parquet"


def _parse_ken_french_daily(raw: str) -> pd.DataFrame:
    """Parse Ken French daily 5-factor CSV format."""
    lines = raw.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith(",") and "Mkt-RF" in line:
            start = i + 1
            break
    if start is None:
        return pd.DataFrame()

    rows = []
    date_format = None
    for line in lines[start:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7 or not (
            len(parts[0]) in (6, 8) and parts[0].isdigit()
        ):
            continue
        try:
            if date_format is None:
                date_format = "%Y%m%d" if len(parts[0]) == 8 else "%Y%m"
            rows.append([parts[0]] + [float(p) for p in parts[1:7]])
        except ValueError:
            continue

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=_COLUMNS)
    df["date"] = pd.to_datetime(df["date"], format=date_format)
    df = df.set_index("date").sort_index()
    df.index.name = "date"
    for col in _COLUMNS[1:]:
        df[col] = df[col] / 100.0
    return df


def _fetch_ken_french() -> pd.DataFrame:
    """Fetch and parse Ken French 5-factor daily data (authoritative source)."""
    try:
        resp = requests.get(FF5_URL, timeout=60)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            name = [n for n in zf.namelist() if n.lower().endswith(".csv")][0]
            raw = zf.read(name).decode("latin-1")
        return _parse_ken_french_daily(raw)
    except Exception:
        return pd.DataFrame()


def _fetch_fred_ff5() -> pd.DataFrame:
    """Fetch FF5 factors from FRED API with retry/backoff."""
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        return pd.DataFrame()
    
    try:
        from fredapi import Fred
    except ImportError:
        return pd.DataFrame()

    fred = Fred(api_key=api_key)
    max_retries = 3
    base_delay = 2

    for attempt in range(max_retries):
        try:
            series_data = {}
            for col, series_id in _FRED_SERIES.items():
                try:
                    s = fred.get_series(series_id)
                    if s is not None and not s.empty:
                        series_data[col] = s
                except Exception:
                    continue
            
            if len(series_data) < 6:
                return pd.DataFrame()

            df = pd.DataFrame(series_data)
            df.index = pd.to_datetime(df.index)
            df = df.sort_index()
            df.index.name = "date"
            return df[_COLUMNS[1:]]  # Return factor columns only
        except Exception:
            if attempt < max_retries - 1:
                import time
                time.sleep(base_delay * (2 ** attempt))
            continue
    return pd.DataFrame()


def _load_cache(max_age_days: int) -> pd.DataFrame | None:
    """Load cached FF5 data if fresh enough."""
    if not _FF5_CACHE.exists():
        return None
    age = (datetime.now() - datetime.fromtimestamp(_FF5_CACHE.stat().st_mtime)).days
    if age > max_age_days:
        return None
    try:
        df = pd.read_parquet(_FF5_CACHE)
        if df.empty or len(df.columns) < 6:
            return None
        return df
    except Exception:
        return None


def _save_cache(df: pd.DataFrame) -> None:
    """Atomically save FF5 data to parquet cache."""
    tmp = _FF5_CACHE.with_suffix(".tmp")
    df.to_parquet(tmp)
    tmp.replace(_FF5_CACHE)


def fetch_ff5_factors(use_cache: bool = True, max_age_days: int = 1) -> pd.DataFrame:
    """Robust fetch of FF5 daily factors with cache, FRED, and Ken French fallback.

    Priority:
    1. Local parquet cache (if fresh)
    2. Ken French authoritative daily CSV (primary source)
    3. FRED API (fallback, requires FRED_API_KEY)
    
    Returns DataFrame with columns [date, Mkt-RF, SMB, HML, RMW, CMA, RF] as decimals.
    """
    if use_cache:
        cached = _load_cache(max_age_days)
        if cached is not None:
            return cached

    # Primary: Ken French (authoritative, no auth needed)
    df = _fetch_ken_french()
    if not df.empty:
        _save_cache(df)
        return df

    # Fallback: FRED API
    df = _fetch_fred_ff5()
    if not df.empty:
        df = df.reset_index()
        df.columns = _COLUMNS
        _save_cache(df)
        return df

    # Last resort: return empty
    return pd.DataFrame()


def fetch_sp500(start, end) -> pd.Series:
    """Fetch S&P 500 daily close via yfinance ^GSPC. Empty Series on failure."""
    try:
        import yfinance as yf
        data = yf.download("^GSPC", start=start, end=end, progress=False, auto_adjust=True)
    except Exception:
        return pd.Series(dtype=float)
    if data is None or data.empty:
        return pd.Series(dtype=float)
    close = data["Close"]
    if close.ndim > 1:
        close = close.iloc[:, 0]
    return close