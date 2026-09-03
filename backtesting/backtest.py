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


def _compute_var_cvar(returns: pd.Series, percentile: float) -> tuple:
    """Compute Value at Risk and Conditional VaR from a returns Series.

    VaR = value at the given percentile (e.g. 5th for VaR-95).
    CVaR = mean of returns at or below VaR (expected shortfall).
    """
    var = float(np.percentile(returns.values, percentile))
    tail = returns.values[returns.values <= var]
    cvar = float(tail.mean()) if len(tail) > 0 else var
    return var, cvar


def _compute_ulcer_maxdd(returns: pd.Series) -> tuple:
    """Compute Maximum Drawdown and Ulcer Index from daily returns.

    Drawdown is derived from cumulative returns: cum = (1+r).cumprod().
    Ulcer Index = sqrt(mean(drawdown^2)).
    Max Drawdown = min drawdown (most negative).
    """
    if returns.empty or returns.std() < 1e-15:
        return 0.0, 0.0
    cum = (1 + returns).cumprod()
    running_peak = cum.cummax()
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdowns = np.where(running_peak > 0, (cum - running_peak) / running_peak, 0.0)
    drawdowns = np.nan_to_num(drawdowns, nan=0.0)
    ulcer = float(np.sqrt(np.mean(drawdowns ** 2)))
    max_dd = float(drawdowns.min())
    return ulcer, max_dd


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
        return {"ic": 0.0, "sharpe": 0.0, "hit_rate": 0.0, "max_drawdown": 0.0, "ulcer_index": 0.0, "var_95": 0.0, "cvar_95": 0.0, "returns": []}

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
        return {"ic": 0.0, "sharpe": 0.0, "hit_rate": 0.0, "max_drawdown": 0.0, "ulcer_index": 0.0, "var_95": 0.0, "cvar_95": 0.0, "returns": []}

    active = active.copy()
    active["predicted"] = active["weighted_sentiment"] * active["combo_w"]
    grouped = active.groupby(["date", "ticker"], as_index=False)[["predicted", "combo_w"]].sum()
    grouped["predicted_sentiment"] = grouped["predicted"] / grouped["combo_w"]

    pred_pivot = grouped.pivot_table(
        index="date", columns="ticker", values="predicted_sentiment", aggfunc="mean"
    ).fillna(0.0)
    # Ensure date index is DatetimeIndex (SQLite returns strings; yfinance returns datetime)
    pred_pivot.index = pd.to_datetime(pred_pivot.index)

    tickers = list(pred_pivot.columns)
    start = pred_pivot.index.min().strftime("%Y-%m-%d")
    end = (pred_pivot.index.max() + timedelta(days=1)).strftime("%Y-%m-%d")

    returns = fetch_historical_returns(tickers, start, end)
    if returns is None or returns.empty:
        return {"ic": 0.0, "sharpe": 0.0, "hit_rate": 0.0, "max_drawdown": 0.0, "ulcer_index": 0.0, "var_95": 0.0, "cvar_95": 0.0, "returns": []}

    returns.index = pd.to_datetime(returns.index)
    common = pred_pivot.index[pred_pivot.index.isin(returns.index)]
    if common.empty:
        return {"ic": 0.0, "sharpe": 0.0, "hit_rate": 0.0, "max_drawdown": 0.0, "ulcer_index": 0.0, "var_95": 0.0, "cvar_95": 0.0, "returns": []}

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

    # t+1 execution: signal at t trades at t+1's return (shift by 1 day)
    pos_df = pd.DataFrame(np.sign(P), index=common, columns=tickers)
    shifted_pos = pos_df.shift(1).fillna(0.0)
    daily_ret = (shifted_pos.values * R).mean(axis=1)
    # Align with shifted dates (first date drops due to shift)
    shifted_dates = common[1:]
    daily_ret = pd.Series(daily_ret[1:], index=shifted_dates).fillna(0.0)

    if daily_ret.std() > 1e-12:
        sharpe = float(daily_ret.mean() / daily_ret.std() * np.sqrt(252))
    else:
        sharpe = 0.0

    nonzero_actual = R[np.abs(R) > 1e-12]
    if nonzero_actual.size > 0:
        # Use shifted positions for hit-rate (same lag as execution)
        shifted_pos_arr = shifted_pos.values
        pred_at = shifted_pos_arr[np.abs(R) > 1e-12]
        hit_rate = float((pred_at == np.sign(nonzero_actual)).mean())
    else:
        hit_rate = 0.0

    # Risk metrics from daily returns
    var_95, cvar_95 = _compute_var_cvar(daily_ret, 5.0)
    ulcer_idx, max_dd = _compute_ulcer_maxdd(daily_ret)

    return {
        "ic": ic,
        "sharpe": sharpe,
        "hit_rate": hit_rate,
        "max_drawdown": max_dd,
        "ulcer_index": ulcer_idx,
        "var_95": var_95,
        "cvar_95": cvar_95,
        "returns": [float(x) for x in daily_ret.tolist()],
    }
