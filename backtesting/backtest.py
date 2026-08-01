"""Walk-forward backtesting for the sentiment pipeline.

Computes Information Coefficient (IC), Sharpe ratio, hit rate and a simple
long/short daily return series from the daily_aggregations table vs. realized
asset returns.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from config import CATEGORY_WEIGHTS, SUBREDDIT_TAXONOMY

logger = logging.getLogger(__name__)


def fetch_historical_returns(tickers: List[str], start: str, end: str) -> pd.DataFrame:
    """Fetch daily returns for tickers between dates (inclusive). Returns DataFrame indexed by date."""
    if not tickers:
        return pd.DataFrame()
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed; cannot fetch historical returns")
        return pd.DataFrame()
    try:
        data = yf.download(tickers, start=start, end=end, progress=False, auto_adjust=True)
    except Exception as exc:
        logger.warning(f"Failed to fetch historical returns for {tickers}: {exc}")
        return pd.DataFrame()
    if data is None or data.empty:
        return pd.DataFrame()
    if isinstance(data.columns, pd.MultiIndex):
        close = data["Close"] if "Close" in data.columns.get_level_values(0) else data
    else:
        close = data["Close"] if "Close" in data.columns else data
    if close.ndim == 1:
        close = close.to_frame(tickers[0])
    returns = close.pct_change(fill_method=None)
    return returns


def _load_aggregations(lookback_days: int) -> pd.DataFrame:
    """Load weighted sentiment rows from the last N days."""
    from db.connection import get_connection

    conn = get_connection()
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    query = """
        SELECT ticker, date, category, subreddit,
               CASE WHEN total_weight > 0 THEN weighted_sum / total_weight ELSE 0 END AS weighted_sentiment,
               total_weight
        FROM daily_aggregations
        WHERE date >= ?
    """
    return pd.read_sql_query(query, conn, params=[cutoff])


def run_walk_forward_backtest(
    category_weights: Optional[Dict[str, float]] = None,
    subreddit_weights: Optional[Dict[str, Dict[str, float]]] = None,
    lookback_days: int = 30,
) -> Dict[str, Any]:
    """Run a walk-forward backtest over the stored daily aggregations.

    Returns a dict with keys: ic, sharpe, hit_rate, returns.
    """
    if category_weights is None:
        category_weights = dict(CATEGORY_WEIGHTS)
    if subreddit_weights is None:
        subreddit_weights = {c: dict(s) for c, s in SUBREDDIT_TAXONOMY.items()}

    df = _load_aggregations(lookback_days)
    if df is None or df.empty:
        return {"ic": 0.0, "sharpe": 0.0, "hit_rate": 0.0, "returns": []}

    df = df.copy()
    if "weighted_sentiment" not in df.columns:
        if "weighted_sum" in df.columns and "total_weight" in df.columns:
            denom = df["total_weight"].replace(0, np.nan)
            df["weighted_sentiment"] = (df["weighted_sum"] / denom).fillna(0.0)
        else:
            df["weighted_sentiment"] = 0.0
    if "total_weight" not in df.columns:
        df["total_weight"] = 1.0

    df["cat_w"] = df["category"].map(lambda c: category_weights.get(c, 0.0))
    df["sub_w"] = df.apply(
        lambda r: subreddit_weights.get(r["category"], {}).get(r["subreddit"], 0.0), axis=1
    )
    df["combo_w"] = df["cat_w"] * df["sub_w"]

    active = df[df["combo_w"] > 0]
    if active.empty:
        return {"ic": 0.0, "sharpe": 0.0, "hit_rate": 0.0, "returns": []}

    active = active.copy()
    active["predicted"] = active["weighted_sentiment"] * active["combo_w"]
    grouped = active.groupby(["date", "ticker"], as_index=False)[["predicted", "combo_w"]].sum()
    grouped["predicted_sentiment"] = grouped["predicted"] / grouped["combo_w"]

    pred_pivot = grouped.pivot_table(
        index="date", columns="ticker", values="predicted_sentiment", aggfunc="mean"
    ).fillna(0.0)

    tickers = list(pred_pivot.columns)
    start = (datetime.strptime(str(pred_pivot.index.min()), "%Y-%m-%d")).strftime("%Y-%m-%d")
    end = (datetime.strptime(str(pred_pivot.index.max()), "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    returns = fetch_historical_returns(tickers, start, end)
    if returns is None or returns.empty:
        return {"ic": 0.0, "sharpe": 0.0, "hit_rate": 0.0, "returns": []}

    returns.index = pd.to_datetime(returns.index)
    pred_dates = pd.to_datetime(pred_pivot.index)
    common = pred_dates[pred_dates.isin(returns.index)]
    if common.empty:
        return {"ic": 0.0, "sharpe": 0.0, "hit_rate": 0.0, "returns": []}

    P = pred_pivot.loc[common].to_numpy(dtype=float)
    R = returns.loc[common, tickers].to_numpy(dtype=float)
    R = np.nan_to_num(R, nan=0.0, posinf=0.0, neginf=0.0)

    finite = np.isfinite(P)
    p_flat = P[finite]
    r_flat = R[finite]
    if len(p_flat) >= 2 and p_flat.std() > 1e-12 and r_flat.std() > 1e-12:
        ic = float(np.corrcoef(p_flat, r_flat)[0, 1])
    else:
        ic = 0.0
    if not np.isfinite(ic):
        ic = 0.0

    positions = np.sign(P)
    daily_ret = (positions * R).mean(axis=1)
    daily_ret = pd.Series(daily_ret, index=common).fillna(0.0)

    if daily_ret.std() > 1e-12:
        sharpe = float(daily_ret.mean() / daily_ret.std() * np.sqrt(252))
    else:
        sharpe = 0.0

    nonzero_actual = R[np.abs(R) > 1e-12]
    if nonzero_actual.size > 0:
        pred_at = np.sign(P)[np.abs(R) > 1e-12]
        hit_rate = float((pred_at == np.sign(nonzero_actual)).mean())
    else:
        hit_rate = 0.0

    return {
        "ic": ic,
        "sharpe": sharpe,
        "hit_rate": hit_rate,
        "returns": [float(x) for x in daily_ret.tolist()],
    }
