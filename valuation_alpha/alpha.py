"""Alpha estimation, slippage, benchmark excess, and portfolio aggregation."""

import logging
import warnings

import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf
from scipy import stats

logger = logging.getLogger(__name__)

_FACTOR_COLS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA"]
_TRADING_DAYS = 252
_COND_THRESHOLD = 1e12
_RIDGE_EPS = 1e-6


def _conditioned_inverse(M):
    """Invert a covariance-like matrix with conditioning guard.

    If cond(M) > _COND_THRESHOLD, applies Ledoit-Wolf shrinkage (preferred)
    or ridge regularisation as fallback, logging a warning. Returns the
    regularised inverse.
    """
    cond = np.linalg.cond(M)
    if cond <= _COND_THRESHOLD:
        return np.linalg.pinv(M)
    logger.warning(
        "Covariance condition number %.2e exceeds threshold %.0e — "
        "applying shrinkage.",
        cond,
        _COND_THRESHOLD,
    )
    try:
        lw = LedoitWolf().fit(M)
        regularised = lw.covariance_
    except Exception:
        regularised = M + _RIDGE_EPS * np.eye(M.shape[0])
    return np.linalg.pinv(regularised)


def align_factors(returns: pd.Series, factors: pd.DataFrame) -> pd.DataFrame:
    """Reindex factor data onto the returns index.

    Ken French factors are monthly while returns may be daily.  When the factor
    frequency is coarser than the returns frequency, factors are kept at their
    native monthly frequency (one row per month-end) rather than being rescaled
    to a daily equivalent.  Callers that need a common frequency should
    resample the coarser side (typically the returns) before regressing.
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
    # Monthly factors: keep at monthly frequency, one row per month-end.
    f_period = f_dates.to_period("M")
    r_period = r_dates.to_period("M")
    per_period = factors.groupby(f_period).last()
    monthly_idx = sorted(r_period.unique())
    monthly_dates = pd.PeriodIndex(monthly_idx).to_timestamp("M")
    out = per_period.reindex(monthly_idx)
    out.index = monthly_dates
    return out


def _is_coarse_index(idx: pd.DatetimeIndex) -> bool:
    """Return True if the datetime index has median gap > 10 days (monthly)."""
    gaps = idx.to_series().diff().dropna()
    return bool(gaps.empty) or float(gaps.median().days) > 10


def ff5_residual_alpha(
    returns: pd.Series,
    factors: pd.DataFrame,
    horizon_days: int = 252,
    annualize: bool = True,
    slippage: float = 0.005,
) -> dict:
    """Regress excess returns on the FF5 factors and return residual alpha.

    When aligned factors are at a coarser (monthly) frequency, daily returns
    are resampled to monthly via geometric linking before regression.  Alpha is
    annualised by ×12 for monthly data, or ×252 for daily data.

    Uses numpy lstsq over the last observations within the horizon window.
    Returns a dict with alpha_daily (per-period intercept), alpha_annualized,
    t_stat, p_value, ci_lower, ci_upper (95 % CI on annualized alpha),
    n_obs, r2, residual_std.  Returns ``None`` when too few observations.
    """
    if returns is None or len(returns) == 0 or factors is None or factors.empty:
        return None
    if not all(c in factors.columns for c in _FACTOR_COLS) or "RF" not in factors.columns:
        return None
    aligned = align_factors(returns, factors)
    if aligned.empty:
        return None

    # Detect whether aligned factors sit at a coarser (monthly) frequency.
    is_monthly = _is_coarse_index(pd.DatetimeIndex(aligned.index))

    if is_monthly:
        # Geometric-link daily returns to monthly to match factor frequency.
        returns_m = returns.resample("ME").apply(lambda x: (1 + x).prod() - 1)
        df = pd.concat(
            [returns_m.rename("ret"), aligned[_FACTOR_COLS + ["RF"]]], axis=1
        ).dropna()
        if len(df) == 0:
            return None
        horizon_months = max(horizon_days // 21, 1)
        df = df.iloc[-horizon_months:]
        y = (df["ret"] - df["RF"]).values
        X = df[_FACTOR_COLS].values
        annualize_factor = 12
        min_obs = 10
    else:
        df = pd.concat(
            [returns.rename("ret"), aligned[_FACTOR_COLS + ["RF"]]], axis=1
        ).dropna()
        if len(df) == 0:
            return None
        df = df.iloc[-horizon_days:]
        y = (df["ret"] - df["RF"]).values
        X = df[_FACTOR_COLS].values
        annualize_factor = _TRADING_DAYS
        min_obs = 60

    n_obs = len(y)
    if n_obs < min_obs:
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
    cov = _conditioned_inverse(Xd.T @ Xd)
    se_alpha = residual_std * np.sqrt(cov[0, 0])
    t_stat = alpha_daily / se_alpha if se_alpha > 0 else 0.0
    p_value = 2.0 * (1.0 - stats.t.cdf(abs(t_stat), dof)) if dof > 0 else 1.0
    alpha_annualized = alpha_daily * annualize_factor if annualize else alpha_daily
    se_ann = se_alpha * annualize_factor if annualize else se_alpha
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


def apply_slippage(
    returns: pd.Series,
    weights: "pd.DataFrame | None" = None,
    slippage: float = 0.005,
    min_turnover: float = 0.05,
) -> pd.Series:
    """Subtract slippage cost proportional to daily turnover.

    On each day the cost is ``|Δw| * slippage`` where ``Δw`` is the total
    weight change across all positions.  Only days where ``|Δw| > min_turnover``
    are charged (filters rounding noise).

    Falls back to the old streak-based logic when *weights* is ``None``
    (deprecated).
    """
    if weights is None:
        warnings.warn(
            "apply_slippage(weights=None) is deprecated. "
            "Pass a daily weights DataFrame to charge per-rebalance turnover.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _apply_slippage_streak(returns, slippage)

    w = weights.reindex(returns.index).fillna(0.0)
    weight_diff = w.diff().abs().sum(axis=1)
    fee = weight_diff.where(weight_diff > min_turnover, 0.0) * slippage
    return returns - fee


def _apply_slippage_streak(returns: pd.Series, slippage: float = 0.005) -> pd.Series:
    """Legacy streak-based slippage — deprecated, kept for backward compat."""
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