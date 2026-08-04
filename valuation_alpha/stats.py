"""Statistical verification battery for the P2 selection layer."""

import numpy as np
import pandas as pd
from scipy import stats

from valuation_alpha.alpha import (
    ff5_residual_alpha,
    apply_slippage,
    portfolio_returns,
    align_factors,
)

_EULER_GAMMA = 0.5772
_TRADING_DAYS = 252


def _block_indices(n: int, block_len: int, rng) -> np.ndarray:
    if n == 0 or block_len <= 0:
        return np.array([], dtype=int)
    block_len = min(block_len, n)
    n_blocks = int(np.ceil(n / block_len))
    starts = rng.integers(0, n - block_len + 1, size=n_blocks)
    return np.concatenate([np.arange(s, s + block_len) for s in starts])[:n]


def _bootstrap_aligned(returns, factors, block_len, rng):
    if isinstance(returns, pd.Series):
        df = pd.concat([returns.rename("ret"), align_factors(returns, factors)], axis=1).dropna()
        if len(df) == 0:
            return returns.iloc[:0], pd.DataFrame(columns=factors.columns)
        idx = _block_indices(len(df), block_len, rng)
        boot = df.iloc[idx].reset_index(drop=True)
        return boot["ret"], boot[factors.columns]
    df = pd.concat([returns, align_factors(returns, factors)], axis=1).dropna()
    if len(df) == 0:
        return returns.iloc[:0], pd.DataFrame(columns=factors.columns)
    idx = _block_indices(len(df), block_len, rng)
    boot = df.iloc[idx].reset_index(drop=True)
    return boot[returns.columns], boot[factors.columns]


def block_bootstrap_alpha(
    returns: pd.Series,
    factors: pd.DataFrame,
    block_len: int = 252,
    n_boot: int = 1000,
    horizon_days: int = 756,
    seed: int = 42,
) -> dict:
    """Moving-block bootstrap of the residual alpha.

    Resamples the return series in ~1y blocks and re-runs ff5_residual_alpha on
    each resample. Returns boot_alpha_mean, boot_alpha_std, boot_ci_lower,
    boot_ci_upper (2.5/97.5 percentiles), and n_boot.
    """
    rng = np.random.default_rng(seed)
    alphas = []
    for _ in range(n_boot):
        br, bf = _bootstrap_aligned(returns, factors, block_len, rng)
        a = ff5_residual_alpha(br, bf, horizon_days=horizon_days)
        if a is not None:
            alphas.append(a["alpha_annualized"])
    alphas = np.asarray(alphas, dtype=float)
    if len(alphas) == 0:
        return {
            "boot_alpha_mean": np.nan,
            "boot_alpha_std": np.nan,
            "boot_ci_lower": np.nan,
            "boot_ci_upper": np.nan,
            "n_boot": 0,
        }
    return {
        "boot_alpha_mean": float(alphas.mean()),
        "boot_alpha_std": float(alphas.std(ddof=1)),
        "boot_ci_lower": float(np.percentile(alphas, 2.5)),
        "boot_ci_upper": float(np.percentile(alphas, 97.5)),
        "n_boot": len(alphas),
    }


def deflated_sharpe(sharpe: float, n_obs: int, n_trials: int) -> dict:
    """Bailey & Lopez de Prado deflated Sharpe ratio.

    E[max SR] uses the closed-form sqrt(2 ln N) with gamma3/gamma4 defaulting
    to 0/3 (Gaussian). Returns dsr, p_value (1 - DSR), and expected_max_sr.
    """
    if n_trials <= 1:
        expected_max = 0.0
    else:
        expected_max = np.sqrt(2.0 * np.log(n_trials))
    gamma3 = 0.0
    gamma4 = 3.0
    denom = np.sqrt(1.0 - gamma3 * sharpe + (gamma4 - 1.0) / 4.0 * sharpe ** 2)
    z = ((sharpe - expected_max) * np.sqrt(n_obs - 1.0)) / denom if denom > 0 else 0.0
    dsr = float(stats.norm.cdf(z))
    return {"dsr": dsr, "p_value": 1.0 - dsr, "expected_max_sr": expected_max}


def confidence_interval_of(alpha_res: dict) -> dict:
    """Return ci_lower, ci_upper, and p_value from an ff5_residual_alpha dict.

    Passes through existing ci_lower/ci_upper when present; otherwise computes a
    t-based CI from alpha_annualized, t_stat, and n_obs.
    """
    if alpha_res is None:
        return {"ci_lower": np.nan, "ci_upper": np.nan, "p_value": np.nan}
    if "ci_lower" in alpha_res and "ci_upper" in alpha_res:
        ci_lower = alpha_res["ci_lower"]
        ci_upper = alpha_res["ci_upper"]
    else:
        t = alpha_res.get("t_stat", 0.0)
        n = alpha_res.get("n_obs", 0)
        se = alpha_res["alpha_annualized"] / t if t != 0 else np.nan
        crit = stats.t.ppf(0.975, n - 1) if n > 1 else 1.96
        ci_lower = alpha_res["alpha_annualized"] - crit * se
        ci_upper = alpha_res["alpha_annualized"] + crit * se
    p_value = alpha_res.get("p_value", np.nan)
    return {"ci_lower": ci_lower, "ci_upper": ci_upper, "p_value": p_value}


def reality_check_mc(
    candidates: list,
    returns_df: pd.DataFrame,
    factors: pd.DataFrame,
    sp500: pd.Series,
    n_sims: int = 100000,
    seed: int = 7,
    horizon_days: int = 756,
) -> dict:
    """White's Reality Check via Monte Carlo block bootstrap.

    Resamples the return series n_sims times; for each resample computes the max
    alpha across all candidate portfolios. The p-value is the fraction of
    resamples whose max alpha >= the observed best candidate alpha. n_sims is
    capped at 10000 to keep runtime under ~20s per candidate set.
    """
    n_sims = min(n_sims, 10000)
    rng = np.random.default_rng(seed)
    block_len = 252
    best_observed = -np.inf
    for cand in candidates:
        port = portfolio_returns(returns_df, cand["weights"])
        port_net = apply_slippage(port, slippage=0.005)
        a = ff5_residual_alpha(port_net, factors, horizon_days=horizon_days)
        if a is not None:
            best_observed = max(best_observed, a["alpha_annualized"])
    null_max = []
    for _ in range(n_sims):
        br, bf = _bootstrap_aligned(returns_df, factors, block_len, rng)
        mx = -np.inf
        for cand in candidates:
            port = portfolio_returns(br, cand["weights"])
            port_net = apply_slippage(port, slippage=0.005)
            a = ff5_residual_alpha(port_net, bf, horizon_days=horizon_days)
            if a is not None:
                mx = max(mx, a["alpha_annualized"])
        null_max.append(mx)
    null_max = np.asarray(null_max, dtype=float)
    p_value = float(np.mean(null_max >= best_observed)) if len(null_max) else np.nan
    return {
        "p_value": p_value,
        "best_observed_alpha": best_observed,
        "null_max_alpha_mean": float(null_max.mean()) if len(null_max) else np.nan,
        "null_max_alpha_p95": float(np.percentile(null_max, 95)) if len(null_max) else np.nan,
        "n_sims": n_sims,
    }


def bias_ablation(names_all: pd.DataFrame, names_no_bias: pd.DataFrame) -> dict:
    """Compare per-group alpha stats for Run A (all) vs Run B (no bias).

    Verdict thresholds: EDGE_REAL when both runs are positive and run_b >=
    0.6 * run_a; RIDING_BIAS when run_a is positive and run_b <= 0; NEW_SECTORS
    when run_b is positive and run_a <= 0; otherwise INSIGNIFICANT.
    """
    def _stats(df):
        a = df["alpha_3y_ann"].dropna()
        if len(a) == 0:
            return {
                "mean_alpha_3y": np.nan,
                "share_positive": np.nan,
                "best": None,
                "worst": None,
            }
        return {
            "mean_alpha_3y": float(a.mean()),
            "share_positive": float((a > 0).mean()),
            "best": df.loc[a.idxmax(), "ticker"],
            "worst": df.loc[a.idxmin(), "ticker"],
        }

    run_a = _stats(names_all)
    run_b = _stats(names_no_bias)
    ma, mb = run_a["mean_alpha_3y"], run_b["mean_alpha_3y"]
    if ma > 0 and mb > 0 and mb >= 0.6 * ma:
        verdict = "EDGE_REAL"
    elif ma > 0 and mb <= 0:
        verdict = "RIDING_BIAS"
    elif mb > 0 and ma <= 0:
        verdict = "NEW_SECTORS"
    else:
        verdict = "INSIGNIFICANT"
    return {"run_a": run_a, "run_b": run_b, "verdict": verdict}