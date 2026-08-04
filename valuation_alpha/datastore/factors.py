"""Factor and benchmark data: Ken French 5-factor and S&P 500."""

import io
import zipfile

import pandas as pd
import requests

FF5_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
FF5_URL_MONTHLY = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_CSV.zip"

_COLUMNS = ["date", "Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]


def fetch_ff5_factors(url=FF5_URL) -> pd.DataFrame:
    """Download the Ken French 5-factor 2x3 daily CSV zip and parse it.

    Returns a DataFrame with columns [date, Mkt-RF, SMB, HML, RMW, CMA, RF] as
    decimals (file values are percentages, divided by 100). Both the 8-digit
    daily and 6-digit monthly date formats are accepted; annual-block summary
    rows (bare 4-digit years) are skipped. Empty on failure.
    """
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            name = [n for n in zf.namelist() if n.lower().endswith(".csv")][0]
            raw = zf.read(name).decode("latin-1")
    except Exception:
        return pd.DataFrame()

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