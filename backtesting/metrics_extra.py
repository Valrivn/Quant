"""Extra risk metrics merged from the backtesting-frameworks skill (v2).

Sortino, Calmar, and win-rate round out the house metric bundle (which
already computes Sharpe, maxDD, fees, IR). Pure functions on return/level
series so they can be unit-tested in isolation.
"""

import numpy as np
import pandas as pd

ANNUALIZE = 252.0


def sortino(returns, annualize=ANNUALIZE, target=0.0):
    """Annualized Sortino ratio: excess mean return over downside deviation.

    Downside deviation penalizes only periods below ``target``. Returns NaN
    when no downside is observed (unbounded ratio) or too few samples.
    """
    r = pd.Series(returns).dropna()
    if len(r) < 2:
        return np.nan
    mean = float(r.mean())
    downside = r[r < target] - target
    dd = float(np.sqrt(np.mean(np.square(downside)))) if len(downside) else 0.0
    if dd <= 0:
        return np.nan
    return (mean - target) / dd * np.sqrt(annualize)


def calmar(ann_return, maxdd):
    """Calmar ratio: annualized return over absolute max drawdown.

    ``maxdd`` must be <= 0 (a drawdown). Returns NaN for a flat path.
    """
    if maxdd is None or not (maxdd < 0):
        return np.nan
    return float(ann_return) / abs(float(maxdd))


def win_rate(returns, bench=None):
    """Fraction of periods with positive return (or above benchmark).

    ``bench`` is optional; when given, a win is return > benchmark return in
    the same period (aligned on the union index).
    """
    r = pd.Series(returns).dropna()
    if len(r) == 0:
        return np.nan
    if bench is not None:
        b = pd.Series(bench).dropna()
        df = pd.concat([r.rename("r"), b.rename("b")], axis=1).dropna()
        if len(df) == 0:
            return np.nan
        return float((df["r"] > df["b"]).mean())
    return float((r > 0).mean())


def period_return(series, start, end):
    """Cumulative return of a level/value series over [start, end]."""
    seg = pd.Series(series)
    seg = seg[(seg.index >= pd.Timestamp(start)) & (seg.index <= pd.Timestamp(end))]
    if len(seg) < 2:
        return np.nan
    return float(seg.iloc[-1] / seg.iloc[0] - 1.0)


def monthly_returns(level_series):
    """Resample a level series to month-end and return month-over-month returns."""
    s = pd.Series(level_series).dropna()
    if len(s) < 2:
        return pd.Series(dtype=float)
    end = s.groupby(s.index.to_period("M")).last()
    return end.pct_change(fill_method=None).dropna()
