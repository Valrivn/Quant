"""Alpha estimation, slippage, benchmark excess, and portfolio aggregation."""

import numpy as np
import pandas as pd
from scipy import stats

_FACTOR_COLS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA"]
_TRADING_DAYS = 252


def align_factors(returns: pd.Series, factors: pd.DataFrame) -> pd.DataFrame:
    """Reindex factor data onto the returns index.

    Ken French factors are monthly while returns may be daily; forward-fill the
    factors to the returns frequency so inner joins keep full coverage. When the
    factor frequency is coarser than the returns frequency, each factor value is
    first rescaled to the per-observation (daily) equivalent by dividing by the
    number of returns rows it covers, so the OLS sees consistent units.
    """
    if factors is None or factors.empty or len(returns) == 0:
        return factors
    if factors.index.equals(returns.index):
        return factors
    if not isinstance(factors.index, pd.DatetimeIndex) or not isinstance(
        returns.index, pd.DatetimeIndex
    ):
        idx = returns.index.union(factors.index)
        return factors.reindex(idx).ffill().reindex(returns.index)
    f_dates = pd.DatetimeIndex(factors.index)
    r_dates = pd.DatetimeIndex(returns.index)
    gaps = f_dates.to_series().diff().dropna()
    coarse = bool(gaps.empty) or float(gaps.median().days) > 10
    if not coarse:
        idx = r_dates.union(f_dates)
        return factors.reindex(idx).ffill().reindex(r_dates)
    f_period = f_dates.to_period("M")
    r_period = r_dates.to_period("M")
    counts = (
        pd.Series(r_dates, index=r_dates).groupby(r_period).size().astype(float)
    )
    per_period = factors.groupby(f_period).last()
    scaled = per_period.divide(
        counts.reindex(per_period.index).clip(lower=1.0), axis=0
    )
    out = scaled.reindex(sorted(r_period.unique())).reindex(r_period).reset_index(drop=True)
    out.index = r_dates
    return out


def ff5_residual_alpha(
    returns: pd.Series,
    factors: pd.DataFrame,
    horizon_days: int = 252,
    annualize: bool = True,
    slippage: float = 0.005,
) -> dict:
    """Regress excess returns on the FF5 factors and return residual alpha.

    Uses numpy lstsq over the last horizon_days of aligned rows. Returns a dict
    with alpha_daily, alpha_annualized (x252), t_stat, p_value, ci_lower,
    ci_upper (95% CI on annualized alpha), n_obs, r2, residual_std. Returns
    None when fewer than 60 usable observations.
    """
    if returns is None or len(returns) == 0 or factors is None or factors.empty:
        return None
    if not all(c in factors.columns for c in _FACTOR_COLS) or "RF" not in factors.columns:
        return None
    aligned = align_factors(returns, factors)
    df = pd.concat([returns.rename("ret"), aligned[_FACTOR_COLS + ["RF"]]], axis=1)
    df = df.dropna()
    if len(df) == 0:
        return None
    df = df.iloc[-horizon_days:]
    y = (df["ret"] - df["RF"]).values
    X = df[_FACTOR_COLS].values
    n_obs = len(y)
    if n_obs < 60:
        return None
    Xd = np.column_stack([np.ones(n_obs), X])
    beta, _, _, _ = np.linalg.lstsq(Xd, y, rcond=None)
    alpha_daily = float(beta[0])
    resid = y - Xd @ beta
    p = Xd.shape[1]
    dof = n_obs - p
    residual_std = float(np.sqrt(np.sum(resid ** 2) / dof)) if dof > 0 else 0.0
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    cov = np.linalg.pinv(Xd.T @ Xd)
    se_alpha = residual_std * np.sqrt(cov[0, 0])
    t_stat = alpha_daily / se_alpha if se_alpha > 0 else 0.0
    p_value = 2.0 * (1.0 - stats.t.cdf(abs(t_stat), dof)) if dof > 0 else 1.0
    alpha_annualized = alpha_daily * _TRADING_DAYS
    se_ann = se_alpha * _TRADING_DAYS
    crit = stats.t.ppf(0.975, dof) if dof > 0 else 1.96
    ci_lower = alpha_annualized - crit * se_ann
    ci_upper = alpha_annualized + crit * se_ann
    return {
        "alpha_daily": alpha_daily,
        "alpha_annualized": alpha_annualized,
        "t_stat": t_stat,
        "p_value": p_value,
        "ci_lower": ci_lower,
        "ci_upper": ci_upper,
        "n_obs": n_obs,
        "r2": r2,
        "residual_std": residual_std,
    }


def apply_slippage(returns: pd.Series, slippage: float = 0.005) -> pd.Series:
    """Subtract the round-trip cost fraction on every position.

    Approximation: the cost is charged on the first and last day of each
    consecutive holding streak (a single-day streak pays it twice, i.e. a full
    round trip).
    """
    out = returns.copy()
    mask = returns.notna()
    in_streak = False
    start = None
    for i in range(len(returns)):
        if mask.iloc[i]:
            if not in_streak:
                in_streak = True
                start = i
        else:
            if in_streak:
                out.iloc[start] = out.iloc[start] - slippage
                out.iloc[i - 1] = out.iloc[i - 1] - slippage
                in_streak = False
    if in_streak:
        out.iloc[start] = out.iloc[start] - slippage
        out.iloc[len(returns) - 1] = out.iloc[len(returns) - 1] - slippage
    return out


def excess_vs_sp500(returns: pd.Series, sp500: pd.Series) -> dict:
    """Annualized excess return and information ratio vs the S&P 500.

    Accepts the benchmark as either daily returns or a price/level series
    (converted to returns via pct_change when magnitudes exceed return range).
    Returns a dict with excess_annualized, tracking_error, information_ratio,
    and n_obs over the aligned window, or None on insufficient data.
    """
    if returns is None or len(returns) == 0 or sp500 is None or len(sp500) == 0:
        return None
    bench = sp500.dropna()
    if abs(float(bench.median())) > 1.5:
        bench = bench.pct_change().dropna()
    df = pd.concat([returns.rename("ret"), bench.rename("bench")], axis=1).dropna()
    if len(df) < 2:
        return None
    excess = df["ret"] - df["bench"]
    ann_excess = float(excess.mean() * _TRADING_DAYS)
    te = float(excess.std(ddof=1) * np.sqrt(_TRADING_DAYS))
    ir = ann_excess / te if te > 0 else None
    return {
        "excess_annualized": ann_excess,
        "tracking_error": te,
        "information_ratio": ir,
        "n_obs": len(df),
    }


def portfolio_returns(returns_df: pd.DataFrame, weights: dict) -> pd.Series:
    """Weighted daily portfolio return series.

    Reindexed to the union of all columns, forward-filled (limited), then
    dropna.
    """
    if returns_df is None or returns_df.empty or not weights:
        return pd.Series(dtype=float)
    cols = [c for c in weights if c in returns_df.columns]
    if not cols:
        return pd.Series(dtype=float)
    sub = returns_df[cols]
    weighted = sum(sub[c].fillna(0.0) * weights[c] for c in cols)
    return weighted.reindex(returns_df.index).ffill(limit=1).dropna()