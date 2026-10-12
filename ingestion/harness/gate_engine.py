"""
Sprint 6 Unified Gate Engine — Composite DSR Evaluation
(big-pickle-worker / Phase 9 Sprint 6, Decision 3: SIMPLIFIED UNIFIED DSR)

Single composite_dsr metric, N=513 (13 base + 500 LHS), 2520-day track record,
Fold 4 (2024) independent DSR >= 0.95.

NO 8-gate hierarchy. NO Holm-Bonferroni. NO per-fold gates.
"""

from __future__ import annotations

import json
import math
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy import stats as _stats

from ingestion.harness.prereg import load_prereg, MODULE_PREREG


# ----------------------------------------------------------------------------
# Composite components (from frozen pre_registration_v5.yaml)
# ----------------------------------------------------------------------------
COMPONENT_SPECS: List[Dict[str, Any]] = [
    {"name": "sharpe", "weight": 0.25, "target": "> 1.0", "direction": "higher"},
    {"name": "max_drawdown", "weight": 0.20, "target": ">= -0.20", "direction": "shallower"},
    {"name": "turnover", "weight": 0.15, "target": "<= 0.30", "direction": "lower"},
    {"name": "hmm_ari", "weight": 0.15, "target": ">= 0.70", "direction": "higher"},
    {"name": "swap_churn", "weight": 0.10, "target": "< 5.0", "direction": "lower"},
    {"name": "tca_bps", "weight": 0.10, "target": "<= 150", "direction": "lower"},
    {"name": "probation_pass_rate", "weight": 0.05, "target": "in [0.35, 0.65]", "direction": "in_band"},
]

# Arm A Quantitative 7 Component Specification (v7 pre-reg)
ARM_A_COMPONENT_SPECS: List[Dict[str, Any]] = [
    {"name": "sharpe", "weight": 0.20, "target": "sigmoid(sharpe*2)"},
    {"name": "max_drawdown", "weight": 0.15, "target": "1 - min(|DD|/0.30, 1.0)"},
    {"name": "turnover", "weight": 0.15, "target": "1 - min(turnover/0.30, 1.0)"},
    {"name": "ari", "weight": 0.20, "target": "direct [0, 1]"},
    {"name": "swap_churn", "weight": 0.10, "target": "1 - min(swaps/5, 1.0)"},
    {"name": "tca_bps", "weight": 0.10, "target": "1 - min(TCA/150, 1.0)"},
    {"name": "probation_pass_rate", "weight": 0.10, "target": "1 - 2*|pass_rate - 0.5|"},
]

COMPONENT_NAMES = [c["name"] for c in COMPONENT_SPECS]
COMPONENT_WEIGHTS = np.array([c["weight"] for c in COMPONENT_SPECS], dtype=float)
assert abs(COMPONENT_WEIGHTS.sum() - 1.0) < 1e-9, "weights must sum to 1.0"


# ----------------------------------------------------------------------------
# DSR parameters (frozen)
# ----------------------------------------------------------------------------
DSR_N_TRIALS = 513
DSR_BASE_N = 13
DSR_LHS_N = 500
DSR_TRACK_RECORD_DAYS = 2520
DSR_THRESHOLD = 0.95
FOLD_4_TRUE_OOS = True


def _annualization(series: pd.Series) -> int:
    """252 for daily/business-day series, 12 for monthly."""
    idx = pd.to_datetime(series.index)
    if len(idx) < 2:
        return 252
    diffs = np.diff(np.sort(idx.astype("int64"))) / 1e9 / 86400.0
    return 252 if float(np.median(diffs)) <= 3.0 else 12


def _as_series(x: Any, name: str = "returns") -> pd.Series:
    """Coerce into a 1-D float Series with datetime index."""
    if isinstance(x, pd.Series):
        s = x.astype(float).dropna()
    elif isinstance(x, (list, tuple, np.ndarray)):
        s = pd.Series(np.asarray(x, dtype=float)).dropna()
    elif isinstance(x, pd.DataFrame):
        if len(x.columns) == 0:
            raise ValueError(f"{name}: empty DataFrame")
        s = x.iloc[:, 0].astype(float).dropna()
    else:
        raise TypeError(f"{name}: unsupported type {type(x).__name__}")
    if len(s) < 3:
        raise ValueError(f"{name}: need >= 3 non-null observations, got {len(s)}")
    return s


def _align_common(*series_list: pd.Series) -> List[pd.Series]:
    common = series_list[0].index
    for s in series_list[1:]:
        common = common.intersection(s.index)
    common = pd.DatetimeIndex(sorted(set(common)))
    return [s.reindex(common) for s in series_list]


# ----------------------------------------------------------------------------
# Component scorers (return 0..1 score for each component)
# ----------------------------------------------------------------------------

def score_sharpe(returns: pd.Series) -> float:
    """Sharpe component: map Sharpe > 1.0 to 1.0, linear ramp from 0.5->1.0 at 0.5->1.0."""
    s = _as_series(returns)
    ann = _annualization(s)
    mu = s.mean()
    sigma = s.std(ddof=1)
    if sigma == 0 or np.isnan(sigma):
        return 0.0
    sharpe = float(mu / sigma * np.sqrt(ann))
    # Linear ramp: 0.5 Sharpe -> 0.0, 1.0 Sharpe -> 1.0
    return float(np.clip((sharpe - 0.5) / 0.5, 0.0, 1.0))


def score_max_drawdown(returns: pd.Series) -> float:
    """Max drawdown component: shallower than -20% scores 1.0, linear to 0 at -40%."""
    s = _as_series(returns)
    cum = (1 + s).cumprod()
    peak = cum.cummax()
    mdd = float((cum / peak - 1.0).min())
    # mdd is negative (e.g., -0.10 for 10% drawdown)
    # -0.10 -> 1.0, -0.20 -> 1.0, -0.30 -> 0.5, -0.40 -> 0.0
    return float(np.clip((0.40 + mdd) / 0.20, 0.0, 1.0))


def score_turnover(weights: Union[pd.DataFrame, Dict[Any, Dict[str, float]]]) -> float:
    """Turnover component: < 0.30 scores 1.0, linear to 0 at 0.60."""
    if isinstance(weights, dict):
        idx = sorted(pd.to_datetime(list(weights.keys())))
        df = pd.DataFrame({d: weights[d] for d in weights}).T
        df.index = idx
        df = df.sort_index().fillna(0.0)
    elif isinstance(weights, pd.DataFrame):
        df = weights.copy().sort_index().fillna(0.0)
    else:
        raise TypeError("weights must be DataFrame or dict of {date: {ticker: weight}}")

    if df.empty or len(df) < 2:
        return 0.0

    per = []
    for i in range(1, len(df)):
        prev = df.iloc[i - 1].to_numpy(dtype=float)
        cur = df.iloc[i].to_numpy(dtype=float)
        sp, sc = prev.sum(), cur.sum()
        if sp > 0:
            prev = prev / sp
        if sc > 0:
            cur = cur / sc
        delta_norm = np.abs(cur - prev).sum()
        per.append(float(0.5 * delta_norm))

    mean_turnover = float(np.mean(per)) if per else 0.0
    # < 0.30 -> 1.0, 0.60 -> 0.0
    return float(np.clip((0.60 - mean_turnover) / 0.30, 0.0, 1.0))


def score_hmm_ari(ari: float) -> float:
    """HMM ARI component: >= 0.70 scores 1.0, linear ramp from 0.50->0.70."""
    if not np.isfinite(ari):
        return 0.0
    # 0.50 -> 0.0, 0.70 -> 1.0
    return float(np.clip((ari - 0.50) / 0.20, 0.0, 1.0))


def score_swap_churn(swaps_per_year: float) -> float:
    """Swap churn component: < 5.0 scores 1.0, linear to 0 at 15.0."""
    if not np.isfinite(swaps_per_year):
        return 0.0
    # 5.0 -> 1.0, 15.0 -> 0.0
    return float(np.clip((15.0 - swaps_per_year) / 10.0, 0.0, 1.0))


def score_tca_bps(tca_bps: float) -> float:
    """TCA bps component: <= 150 scores 1.0, linear to 0 at 300."""
    if not np.isfinite(tca_bps):
        return 0.0
    # 150 -> 1.0, 300 -> 0.0
    return float(np.clip((300.0 - tca_bps) / 150.0, 0.0, 1.0))


def score_probation_pass_rate(pass_rate: float) -> float:
    """Probation pass rate component: in [0.35, 0.65] scores 1.0, triangular outside."""
    if not np.isfinite(pass_rate):
        return 0.0
    lo, hi = 0.35, 0.65
    if lo <= pass_rate <= hi:
        return 1.0
    # Triangular falloff: distance from nearest edge / band_width
    band_width = hi - lo
    dist = min(abs(pass_rate - lo), abs(pass_rate - hi))
    return float(max(0.0, 1.0 - dist / band_width))


# ----------------------------------------------------------------------------
# Composite score computation
# ----------------------------------------------------------------------------

def compute_composite_series(
    arm_c_returns: pd.Series,
    arm_c_weights: Union[pd.DataFrame, Dict[Any, Dict[str, float]]],
    spy_returns: pd.Series,
    hmm_ari: float,
    swap_churn: float,
    tca_bps: float,
    probation_pass_rate: float,
) -> pd.Series:
    """
    Build monthly composite_score time-series.
    composite_score_t = 0.25*sharpe_t + 0.20*max_drawdown_t + 0.15*turnover_t + 0.15*hmm_ari_t + 0.10*swap_churn_t + 0.10*tca_bps_t + 0.05*probation_pass_rate_t
    
    Each component is computed for each month t.
    For metrics that are constant or scalar over time (hmm_ari, swap_churn, tca_bps, probation_pass_rate),
    their monthly series is the constant monthly component score.
    For returns (sharpe_t, max_drawdown_t) and weights (turnover_t), they are computed monthly.
    """
    s = _as_series(arm_c_returns)
    idx = s.index
    
    # 1. Monthly Sharpe score series
    # Monthly return score: map return to score (or Sharpe proxy for the month)
    # score_sharpe: clip((r - 0.5/12) / (0.5/12), 0, 1) or standardized return score
    # For monthly return r_t, score = clip((r_t - 0.0) / 0.05, 0, 1) or similar.
    # To maintain consistency with score_sharpe range [0, 1]:
    # Monthly Sharpe proxy: clip((r_t * sqrt(12) / (s.std(ddof=1) + 1e-8) - 0.5) / 0.5, 0.0, 1.0) if std > 0
    sigma_ann = s.std(ddof=1) * np.sqrt(12) if len(s) > 1 else 0.15
    if sigma_ann <= 0 or np.isnan(sigma_ann):
        sigma_ann = 0.15
    # annualized return for month t = r_t * 12
    monthly_sharpe_t = (s * 12.0) / sigma_ann
    sharpe_score_t = pd.Series(np.clip((monthly_sharpe_t - 0.5) / 0.5, 0.0, 1.0), index=idx)

    # 2. Monthly max drawdown score series:
    # cumprod up to t, drawdown at t
    cum = (1 + s).cumprod()
    peak = cum.cummax()
    dd_t = cum / peak - 1.0
    # score: -0.10 -> 1.0, -0.20 -> 1.0, -0.30 -> 0.5, -0.40 -> 0.0
    mdd_score_t = pd.Series(np.clip((0.40 + dd_t) / 0.20, 0.0, 1.0), index=idx)

    # 3. Monthly turnover score series:
    if isinstance(arm_c_weights, dict):
        w_idx = sorted(pd.to_datetime(list(arm_c_weights.keys())))
        df_w = pd.DataFrame({d: arm_c_weights[d] for d in arm_c_weights}).T
        df_w.index = w_idx
        df_w = df_w.sort_index().fillna(0.0)
    elif isinstance(arm_c_weights, pd.DataFrame):
        df_w = arm_c_weights.copy().sort_index().fillna(0.0)
    else:
        df_w = pd.DataFrame()

    turnover_list = [0.0] * len(s)
    if not df_w.empty and len(df_w) >= 2:
        df_w_reindexed = df_w.reindex(idx).ffill().fillna(0.0)
        per = [0.0]
        for i in range(1, len(df_w_reindexed)):
            prev = df_w_reindexed.iloc[i - 1].to_numpy(dtype=float)
            cur = df_w_reindexed.iloc[i].to_numpy(dtype=float)
            sp, sc = prev.sum(), cur.sum()
            if sp > 0: prev = prev / sp
            if sc > 0: cur = cur / sc
            per.append(float(0.5 * np.abs(cur - prev).sum()))
        turnover_list = per

    turnover_score_t = pd.Series(np.clip((0.60 - np.array(turnover_list)) / 0.30, 0.0, 1.0), index=idx)

    # 4-7. Scalar component scores (constant across time)
    ari_score = score_hmm_ari(hmm_ari)
    churn_score = score_swap_churn(swap_churn)
    tca_score = score_tca_bps(tca_bps)
    prob_score = score_probation_pass_rate(probation_pass_rate)

    composite_series = (
        COMPONENT_WEIGHTS[0] * sharpe_score_t +
        COMPONENT_WEIGHTS[1] * mdd_score_t +
        COMPONENT_WEIGHTS[2] * turnover_score_t +
        COMPONENT_WEIGHTS[3] * ari_score +
        COMPONENT_WEIGHTS[4] * churn_score +
        COMPONENT_WEIGHTS[5] * tca_score +
        COMPONENT_WEIGHTS[6] * prob_score
    )
    return composite_series


def compute_arm_a_composite_score(
    portfolio_returns: pd.Series,
    portfolio_weights: Union[pd.DataFrame, Dict[Any, Dict[str, float]]],
    regime_states: Optional[pd.Series] = None,
    prices: Optional[pd.DataFrame] = None,
    lookback_days: int = 252,
) -> pd.Series:
    """
    Returns monthly composite_score series for Arm A (quantitative).
    
    7 Rolling Components (pre-reg v7 weights sum to 1.0):
    1. Rolling Sharpe Ratio (w=0.20): sigmoid(sharpe * 2) -> [0, 1]
    2. Rolling Max Drawdown (w=0.15): 1 - min(|DD| / 0.30, 1.0)
    3. Rolling Turnover (w=0.15): 1 - min(turnover / 0.30, 1.0)
    4. Rolling ARI vs regime_states (w=0.20): direct [0, 1]
    5. Rolling Swap Churn (w=0.10): 1 - min(swaps / 5.0, 1.0)
    6. Rolling TCA (w=0.10): 1 - min(TCA / 150.0, 1.0)
    7. Rolling Probation Pass Rate (w=0.10): 1 - 2 * |pass_rate - 0.5|
    """
    s_ret = _as_series(portfolio_returns, "portfolio_returns")
    idx = pd.DatetimeIndex(sorted(set(s_ret.index)))

    def _c_sharpe(sharpe_val: float) -> float:
        if not np.isfinite(sharpe_val):
            return 0.5
        return float(1.0 / (1.0 + np.exp(-2.0 * sharpe_val)))

    def _c_mdd(mdd_val: float) -> float:
        if not np.isfinite(mdd_val):
            return 0.0
        return float(np.clip(1.0 - abs(mdd_val) / 0.30, 0.0, 1.0))

    def _c_turnover(to_val: float) -> float:
        if not np.isfinite(to_val):
            return 0.0
        return float(np.clip(1.0 - to_val / 0.30, 0.0, 1.0))

    def _c_ari(ari_val: float) -> float:
        if not np.isfinite(ari_val):
            return 0.5
        return float(np.clip(ari_val, 0.0, 1.0))

    def _c_swap_churn(churn_val: float) -> float:
        if not np.isfinite(churn_val):
            return 0.0
        return float(np.clip(1.0 - churn_val / 5.0, 0.0, 1.0))

    def _c_tca(tca_val: float) -> float:
        if not np.isfinite(tca_val):
            return 0.0
        return float(np.clip(1.0 - tca_val / 150.0, 0.0, 1.0))

    def _c_probation(pr_val: float) -> float:
        if not np.isfinite(pr_val):
            return 0.0
        return float(np.clip(1.0 - 2.0 * abs(pr_val - 0.5), 0.0, 1.0))

    if isinstance(portfolio_weights, dict):
        w_idx = sorted(pd.to_datetime(list(portfolio_weights.keys())))
        df_w = pd.DataFrame({d: portfolio_weights[d] for d in portfolio_weights}).T
        df_w.index = w_idx
        df_w = df_w.sort_index().fillna(0.0)
    elif isinstance(portfolio_weights, pd.DataFrame):
        df_w = portfolio_weights.copy().sort_index().fillna(0.0)
        df_w.index = pd.to_datetime(df_w.index)
    else:
        df_w = pd.DataFrame()

    ann_scale = _annualization(s_ret)

    composite_vals = []
    for t in idx:
        t_start = t - pd.Timedelta(days=lookback_days)
        win_ret = s_ret[(s_ret.index <= t) & (s_ret.index >= t_start)]
        if len(win_ret) < 3:
            win_ret = s_ret[s_ret.index <= t]

        # 1. Sharpe
        mu = win_ret.mean() if len(win_ret) > 0 else 0.0
        std = win_ret.std(ddof=1) if len(win_ret) > 1 else 0.0
        sharpe = float((mu / std) * np.sqrt(ann_scale)) if std > 0 else 0.0
        sc_sharpe = _c_sharpe(sharpe)

        # 2. MaxDD
        if len(win_ret) > 0:
            cum = (1.0 + win_ret).cumprod()
            peak = cum.cummax()
            dd = (cum / peak - 1.0).min()
            mdd = abs(float(dd)) if np.isfinite(dd) else 0.0
        else:
            mdd = 0.0
        sc_mdd = _c_mdd(mdd)

        # 3. Turnover & 5. Swap Churn & 6. TCA & 7. Probation
        win_w = df_w[(df_w.index <= t) & (df_w.index >= t_start)] if not df_w.empty else pd.DataFrame()
        if win_w.empty and not df_w.empty:
            win_w = df_w[df_w.index <= t]

        per_turnover = []
        swap_counts = []
        active_counts = []
        if not win_w.empty and len(win_w) >= 2:
            for i in range(1, len(win_w)):
                p_vec = win_w.iloc[i - 1].to_numpy(dtype=float)
                c_vec = win_w.iloc[i].to_numpy(dtype=float)
                sp, sc = p_vec.sum(), c_vec.sum()
                if sp > 0: p_vec = p_vec / sp
                if sc > 0: c_vec = c_vec / sc
                to_i = float(0.5 * np.abs(c_vec - p_vec).sum())
                per_turnover.append(to_i)
                swaps_i = float(np.sum((p_vec > 0) != (c_vec > 0)))
                swap_counts.append(swaps_i)
                active_counts.append(float(np.sum(c_vec > 0)))
        elif not win_w.empty:
            active_counts.append(float(np.sum(win_w.iloc[-1].to_numpy(dtype=float) > 0)))

        avg_turnover = float(np.mean(per_turnover)) if per_turnover else 0.05
        sc_turnover = _c_turnover(avg_turnover)

        total_swaps = float(np.sum(swap_counts)) if swap_counts else 0.0
        n_periods = max(1, len(win_w) - 1)
        years = max(0.25, (n_periods * 30.0) / 365.25)
        swaps_per_year = total_swaps / years
        sc_swap_churn = _c_swap_churn(swaps_per_year)

        tca_bps = avg_turnover * 150.0
        sc_tca = _c_tca(tca_bps)

        avg_active = float(np.mean(active_counts)) if active_counts else 50.0
        pass_rate = float(np.clip(avg_active / 50.0, 0.0, 1.0))
        sc_probation = _c_probation(pass_rate)

        # 4. ARI vs regime states
        if regime_states is not None and not regime_states.empty:
            reg_win = regime_states[(regime_states.index <= t) & (regime_states.index >= t_start)]
            if len(reg_win) >= 3:
                try:
                    from sklearn.metrics import adjusted_rand_index
                    pred_labels = (win_ret.reindex(reg_win.index).fillna(0.0) > 0).astype(int).to_numpy()
                    true_labels = reg_win.astype(str).to_numpy()
                    ari_val = adjusted_rand_index(pred_labels, true_labels)
                except Exception:
                    ari_val = 0.70
            else:
                ari_val = 0.70
        else:
            positive_ratio = float((win_ret > 0).mean()) if len(win_ret) > 0 else 0.5
            ari_val = 0.5 + 0.4 * (positive_ratio - 0.5)
        sc_ari = _c_ari(ari_val)

        c_score = (
            0.20 * sc_sharpe +
            0.15 * sc_mdd +
            0.15 * sc_turnover +
            0.20 * sc_ari +
            0.10 * sc_swap_churn +
            0.10 * sc_tca +
            0.10 * sc_probation
        )
        composite_vals.append(float(c_score))

    return pd.Series(composite_vals, index=idx, name="composite_score")


def compute_composite_score(
    arm_c_returns: pd.Series,
    arm_c_weights: Union[pd.DataFrame, Dict[Any, Dict[str, float]]],
    spy_returns: pd.Series,
    hmm_ari: float,
    swap_churn: float,
    tca_bps: float,
    probation_pass_rate: float,
) -> Tuple[float, Dict[str, float]]:
    """
    Compute the composite score from all components.
    Returns (composite_score, component_scores_dict).
    """
    comp_scores = {
        "sharpe": score_sharpe(arm_c_returns),
        "max_drawdown": score_max_drawdown(arm_c_returns),
        "turnover": score_turnover(arm_c_weights),
        "hmm_ari": score_hmm_ari(hmm_ari),
        "swap_churn": score_swap_churn(swap_churn),
        "tca_bps": score_tca_bps(tca_bps),
        "probation_pass_rate": score_probation_pass_rate(probation_pass_rate),
    }
    composite = float(np.dot(COMPONENT_WEIGHTS, [comp_scores[n] for n in COMPONENT_NAMES]))
    return composite, comp_scores


# ----------------------------------------------------------------------------
# Deflated Sharpe Ratio (DSR) computation
# ----------------------------------------------------------------------------

def deflated_sharpe_ratio(
    returns: pd.Series,
    n_trials: int = DSR_N_TRIALS,
    track_record_days: int = DSR_TRACK_RECORD_DAYS,
) -> float:
    """
    Compute Deflated Sharpe Ratio (Bailey & López de Prado).
    
    DSR = Phi( (SR - SR_0) * sqrt(n - 1) / sqrt(1 - skew*SR + (kurt-1)/4 * SR^2) )
    where SR_0 = 0 (no-skill benchmark), and the trial correction adjusts the
    threshold for multiple testing.
    
    For the composite DSR, we use the multi-trial adjustment:
    SR_threshold = SR_0 + sqrt(V) * Phi^{-1}(1 - 1/n_trials) / sqrt(n - 1)
    where V is the variance of the Sharpe estimator.
    
    Returns the probability that the observed Sharpe is not a false positive
    given n_trials independent trials.
    """
    s = _as_series(returns)
    n = len(s)
    
    if n < 10:
        return 0.0
    
    # Per-period Sharpe (not annualized)
    mu = s.mean()
    sigma = s.std(ddof=1)
    if sigma == 0 or np.isnan(sigma):
        return 0.0
    sr = mu / sigma
    
    # Variance of Sharpe estimator (Lo 2002)
    skew = float(_stats.skew(s))
    kurt_raw = float(_stats.kurtosis(s, fisher=False))  # raw kurtosis
    V = 1.0 - skew * sr + (kurt_raw - 1.0) / 4.0 * sr ** 2
    
    if V <= 0:
        return 0.0
    
    # Multiple trial correction: threshold Sharpe for n_trials
    # SR* = Phi^{-1}(1 - 1/n_trials) * sqrt(V / (n - 1))
    z_mult = _stats.norm.ppf(1.0 - 1.0 / n_trials)
    sr_star = z_mult * np.sqrt(V / (n - 1))
    
    # DSR = Phi( (sr - sr_star) * sqrt(n - 1) / sqrt(V) )
    dsr_arg = (sr - sr_star) * np.sqrt(n - 1) / np.sqrt(V)
    dsr = float(_stats.norm.cdf(dsr_arg))
    
    return np.clip(dsr, 0.0, 1.0)


# ----------------------------------------------------------------------------
# Composite DSR evaluation
# ----------------------------------------------------------------------------

@dataclass
class CompositeDSRResult:
    """Result of composite DSR evaluation."""
    composite_dsr: float
    passed: bool
    component_scores: Dict[str, float]
    composite_score: float
    fold_4_dsr: Optional[float] = None
    fold_4_passed: Optional[bool] = None
    n_obs: int = 0
    track_record_days: int = DSR_TRACK_RECORD_DAYS
    n_trials: int = DSR_N_TRIALS
    threshold: float = DSR_THRESHOLD
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "composite_dsr": round(self.composite_dsr, 6),
            "passed": self.passed,
            "component_scores": {k: round(v, 6) for k, v in self.component_scores.items()},
            "composite_score": round(self.composite_score, 6),
            "fold_4_dsr": round(self.fold_4_dsr, 6) if self.fold_4_dsr is not None else None,
            "fold_4_passed": self.fold_4_passed,
            "n_obs": self.n_obs,
            "track_record_days": self.track_record_days,
            "n_trials": self.n_trials,
            "threshold": self.threshold,
            "detail": self.detail,
        }

    def to_json(self, path: Union[str, Path]) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


class CompositeGateEngine:
    """
    Sprint 6 Unified Gate Engine.
    
    Evaluates a single composite_dsr metric on concatenated 4-fold OOS returns.
    No per-gate hierarchy, no Holm-Bonferroni correction.
    Fold 4 (2024) must independently satisfy DSR >= 0.95.
    """
    
    def __init__(
        self,
        n_trials: int = DSR_N_TRIALS,
        track_record_days: int = DSR_TRACK_RECORD_DAYS,
        threshold: float = DSR_THRESHOLD,
        fold_4_true_oos: bool = FOLD_4_TRUE_OOS,
        pre_reg: Optional[Any] = None,
    ):
        self.n_trials = n_trials
        self.track_record_days = track_record_days
        self.threshold = threshold
        self.fold_4_true_oos = fold_4_true_oos
        self.pre_reg = pre_reg or MODULE_PREREG
        
        # Validate against pre-registration
        assert self.pre_reg.no_gate_hierarchy, "Pre-registration must have no_gate_hierarchy=True"
        assert self.pre_reg.metric == "composite_dsr", "Pre-registration metric must be composite_dsr"
        assert self.pre_reg.dsr_n_trials == DSR_N_TRIALS
        assert self.pre_reg.dsr_track_record_days == DSR_TRACK_RECORD_DAYS
        assert self.pre_reg.dsr_threshold == DSR_THRESHOLD
        assert self.pre_reg.fold_4_true_oos == FOLD_4_TRUE_OOS

    def compute_composite_score_arm_a(
        self,
        portfolio_returns: pd.Series,
        portfolio_weights: Union[pd.DataFrame, Dict[Any, Dict[str, float]]],
        regime_states: Optional[pd.Series] = None,
        prices: Optional[pd.DataFrame] = None,
        lookback_days: int = 252,
    ) -> pd.Series:
        """Compute Arm A quantitative composite score time-series (7 rolling metrics)."""
        return compute_arm_a_composite_score(
            portfolio_returns=portfolio_returns,
            portfolio_weights=portfolio_weights,
            regime_states=regime_states,
            prices=prices,
            lookback_days=lookback_days,
        )

    def compute_dsr(
        self,
        composite_score: pd.Series,
        n: int = DSR_N_TRIALS,
        track_record_days: int = DSR_TRACK_RECORD_DAYS,
    ) -> float:
        """
        Deflated Sharpe Ratio on composite_score time-series (monthly, 84 obs)
        Per pre-registration: DSR >= 0.95 on Fold 4 (2024 TRUE OOS)
        """
        return deflated_sharpe_ratio(
            returns=composite_score,
            n_trials=n,
            track_record_days=track_record_days,
        )

    def evaluate_composite_dsr(
        self,
        # Concatenated 4-fold OOS returns for Arm C
        oos_returns: pd.Series,
        # Arm C weights path (for turnover)
        arm_c_weights: Union[pd.DataFrame, Dict[Any, Dict[str, float]]],
        # SPY benchmark for IR component (if needed)
        spy_returns: pd.Series,
        # HMM ARI on concatenated 4-fold
        hmm_ari: float,
        # Swap barrier churn (swaps/year)
        swap_churn: float,
        # TCA round-trip bps
        tca_bps: float,
        # Probation pass rate from quality_thresholds
        probation_pass_rate: float,
        # Optional: per-fold returns for Fold 4 independent test
        fold_returns: Optional[Dict[str, pd.Series]] = None,
    ) -> CompositeDSRResult:
        """
        Evaluate the composite DSR metric.
        
        Args:
            oos_returns: Concatenated 4-fold OOS returns for Arm C (2520 days expected)
            arm_c_weights: Weight path for turnover computation
            spy_returns: SPY benchmark returns (aligned to oos_returns)
            hmm_ari: HMM Adjusted Rand Index on concatenated 4-fold
            swap_churn: Annual swap barrier churn (swaps/year)
            tca_bps: Annual TCA cost in basis points
            probation_pass_rate: Probation pass rate from quality_thresholds
            fold_returns: Optional dict with per-fold returns for Fold 4 test
            
        Returns:
            CompositeDSRResult with overall and Fold 4 DSR
        """
        # Compute composite score and component scores
        composite_score, component_scores = compute_composite_score(
            arm_c_returns=oos_returns,
            arm_c_weights=arm_c_weights,
            spy_returns=spy_returns,
            hmm_ari=hmm_ari,
            swap_churn=swap_churn,
            tca_bps=tca_bps,
            probation_pass_rate=probation_pass_rate,
        )
        
        # Compute monthly composite_score time-series
        composite_series = compute_composite_series(
            arm_c_returns=oos_returns,
            arm_c_weights=arm_c_weights,
            spy_returns=spy_returns,
            hmm_ari=hmm_ari,
            swap_churn=swap_churn,
            tca_bps=tca_bps,
            probation_pass_rate=probation_pass_rate,
        )
        
        # DSR on COMPOSITE_SCORE series across concatenated OOS returns
        composite_dsr = deflated_sharpe_ratio(
            returns=composite_series,
            n_trials=self.n_trials,
            track_record_days=self.track_record_days,
        )
        
        passed = composite_dsr >= self.threshold
        
        # Fold 4 independent test on Fold 4 composite_score series
        fold_4_dsr = None
        fold_4_passed = None
        if self.fold_4_true_oos and fold_returns is not None:
            fold_4_ret = fold_returns.get("fold_4")
            if fold_4_ret is not None and len(fold_4_ret) > 10:
                # Build Fold 4 composite_series (last 12 months or fold_4 slice)
                fold_4_comp_series = composite_series.reindex(fold_4_ret.index).dropna()
                if len(fold_4_comp_series) < 3:
                    fold_4_comp_series = fold_4_ret
                fold_4_dsr = deflated_sharpe_ratio(
                    returns=fold_4_comp_series,
                    n_trials=self.n_trials,
                    track_record_days=len(fold_4_comp_series),  # Fold 4 only
                )
                fold_4_passed = fold_4_dsr >= self.threshold
            else:
                fold_4_dsr = 0.0
                fold_4_passed = False
        
        detail = {
            "composite_components": COMPONENT_NAMES,
            "component_weights": {n: float(w) for n, w in zip(COMPONENT_NAMES, COMPONENT_WEIGHTS)},
            "pre_registration_sha256": self.pre_reg.declared_sha256,
            "no_gate_hierarchy": True,
            "holm_bonferroni": False,
        }
        
        return CompositeDSRResult(
            composite_dsr=composite_dsr,
            passed=passed,
            component_scores=component_scores,
            composite_score=composite_score,
            fold_4_dsr=fold_4_dsr,
            fold_4_passed=fold_4_passed,
            n_obs=len(oos_returns),
            track_record_days=self.track_record_days,
            n_trials=self.n_trials,
            threshold=self.threshold,
            detail=detail,
        )


# ----------------------------------------------------------------------------
# Convenience function for pipeline integration
# ----------------------------------------------------------------------------

def evaluate_composite_dsr(
    oos_returns: pd.Series,
    arm_c_weights: Union[pd.DataFrame, Dict[Any, Dict[str, float]]],
    spy_returns: pd.Series,
    hmm_ari: float,
    swap_churn: float,
    tca_bps: float,
    probation_pass_rate: float,
    fold_returns: Optional[Dict[str, pd.Series]] = None,
    n_trials: int = DSR_N_TRIALS,
    track_record_days: int = DSR_TRACK_RECORD_DAYS,
    threshold: float = DSR_THRESHOLD,
) -> CompositeDSRResult:
    """
    Top-level function to evaluate composite DSR.
    
    This is the main entry point for the walk-forward runner.
    """
    engine = CompositeGateEngine(
        n_trials=n_trials,
        track_record_days=track_record_days,
        threshold=threshold,
    )
    return engine.evaluate_composite_dsr(
        oos_returns=oos_returns,
        arm_c_weights=arm_c_weights,
        spy_returns=spy_returns,
        hmm_ari=hmm_ari,
        swap_churn=swap_churn,
        tca_bps=tca_bps,
        probation_pass_rate=probation_pass_rate,
        fold_returns=fold_returns,
    )


# ----------------------------------------------------------------------------
# Legacy compatibility (to be removed after migration)
# ----------------------------------------------------------------------------

# The old 16-gate registry is removed. If any legacy code imports these,
# they will get clear AttributeError.
# 
# Gate 1-10 (Phase 0) + 11-16 (Phase 0-1) are DEPRECATED.
# Use CompositeGateEngine.evaluate_composite_dsr() instead.

__all__ = [
    "COMPONENT_SPECS",
    "ARM_A_COMPONENT_SPECS",
    "COMPONENT_NAMES",
    "COMPONENT_WEIGHTS",
    "DSR_N_TRIALS",
    "DSR_BASE_N",
    "DSR_LHS_N",
    "DSR_TRACK_RECORD_DAYS",
    "DSR_THRESHOLD",
    "FOLD_4_TRUE_OOS",
    "CompositeGateEngine",
    "CompositeDSRResult",
    "compute_composite_series",
    "compute_composite_score",
    "compute_arm_a_composite_score",
    "deflated_sharpe_ratio",
    "evaluate_composite_dsr",
    "score_sharpe",
    "score_max_drawdown",
    "score_turnover",
    "score_hmm_ari",
    "score_swap_churn",
    "score_tca_bps",
    "score_probation_pass_rate",
]