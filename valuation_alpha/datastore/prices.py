"""Price fetching via yfinance."""

import pandas as pd
import yfinance as yf


def fetch_prices(tickers: list, start: str, end: str) -> pd.DataFrame:
    """Fetch daily close prices for tickers between indexed.

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