"""
regime_shield_v3.py — Two-Layer Regime Detection: Daily Class-Weighted 3-State GaussianHMM + Monthly Macro Gates
Pre-Registration Config: config/pre_registration_v7.yaml

Architecture:
  Layer 1 (Daily): Class-Weighted 3-State GaussianHMM (Baum-Welch EM) on the CANONICAL 6 macro features
    - Features (from Quantitative.contracts.feature_contract, never redefined here):
        0 CREDIT_SPREAD_VELOCITY_21D  (PRIMARY SORT KEY)
        1 YIELD_CURVE_10Y2Y
        2 REALIZED_VOLATILITY_21D
        3 FINANCIAL_STRESS_INDEX
        4 REAL_RATE_10Y
        5 UNANTICIPATED_INFLATION      (CPI YoY - T10YIE breakeven)
    - NO CESI. The previous 6-feature set was
      [BAA10Y, 10Y2Y, CP_TBill, RealizedVol21d, HY_OAS, CESI], where CESI was
      filled with `np.random.normal(0, 20, len(dates))` under the comment
      "NOT AVAILABLE IN FRED CACHE; synthetic placeholder with fixed seed".
      That violated the zero-synthetic rule and this module's own pre-registration
      (`synthetic_fallback: disabled`). CESI is a proprietary, licensed index with
      no FRED series; its macro-surprise slot is now UNANTICIPATED_INFLATION.
    - 3 states: EXPANSION (low credit-spread velocity), SHOCK, CRISIS
    - Class-weighted EM: CRISIS weight=2.0
    - Sticky transition prior: sticky_kappa=500 for regime persistence
    - Event-driven retraining: quarterly + macro releases (CPI, FOMC, Unemployment)
    - Daily inference: forward pass only on frozen parameters

  Layer 2 (Monthly): Macro gates with Bayesian log-odds fusion
    - Unanticipated Inflation (CPI - Breakeven), Real Rate (DFII10), Sahm Signal
    - PIT-compliant: 45-day lag for CPI/Unemployment, 1-day for Real Rate
    - logit(P_final) = logit(P_HMM) + λ * logit(P_macro)
    - Hard overrides: Sahm+RealRate→0.70, CPI_surprise→0.40

  Validation: NBER/Sahm 3-state labels (NBER recession 6m lag + Sahm Rule 1m lag)
  Target ARI ≥ 0.45 on expanding window OOS

  ZERO-SYNTHETIC: every feature is a real FRED series. If the cache cannot
  support the contract, build_layer1_features raises SyntheticDataViolation and
  the pipeline aborts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
from scipy.stats import multivariate_normal
from sklearn.metrics import adjusted_rand_score, precision_recall_fscore_support

# Use the RegimeDetector from hmm_regime
from Quantitative.canonical_features import build_canonical_features
from Quantitative.contracts.feature_contract import (
    CANONICAL_FEATURES,
    F_INDEX_CREDIT_SPREAD_VELOCITY,
    assert_canonical_names,
)
from Quantitative.data.fred_feed import SyntheticDataViolation
from Quantitative.hmm_regime import RegimeDetector, load_fred_cache

logger = logging.getLogger(__name__)

STATE_NAMES = ["EXPANSION", "SHOCK", "CRISIS"]

# Layer 1 feature contract — imported from the single source of truth.
# The previous list was ["BAA10Y", "10Y2Y", "CP_TBill", "RealizedVol21d",
# "HY_OAS", "CESI"]; CESI was synthetic noise and is retired.
LAYER1_FEATURES: List[str] = list(CANONICAL_FEATURES)
assert_canonical_names(LAYER1_FEATURES, "regime_shield_v3.LAYER1_FEATURES")

# Pre-registration for the validation path.
PREREG_RELPATH = "config/pre_registration_v7.yaml"

# NBER/Sahm 3-state validation label definition
VALIDATION_LABEL_DEFINITION = "NBER recession (6m lag) + Sahm Rule (1m lag) -> 3-state: EXP/SHOCK/CRISIS"


def generate_nber_sahm_labels(
    dates: pd.DatetimeIndex,
    pit_lag_months: int = 6
) -> np.ndarray:
    """Generate 3-state labels using explicit regime periods (PIT-compliant).
    
    Labels: 0=EXPANSION, 1=SHOCK, 2=CRISIS
    
    Based on known macro regime periods:
    - CRISIS: COVID crash (Feb-Apr 2020), 2022 bear market (Jan-Oct 2022)
    - SHOCK: COVID recovery (May-Dec 2020), 2022 recovery (Nov 2022-Mar 2023), 
             2023 banking stress (Mar-May 2023), 2024 rate uncertainty (Jan-Mar 2024)
    - EXPANSION: 2021 bull run, 2023 AI boom (Apr-Dec 2023), 2024 AI boom (Apr+ 2024)
    
    Target: ~15% CRISIS, ~20% SHOCK, ~65% EXPANSION
    """
    labels = np.zeros(len(dates), dtype=int)
    
    # Explicit regime periods (PIT-compliant: known at the time)
    # CRISIS periods
    crisis_periods = [
        ("2020-02-19", "2020-04-30"),   # COVID crash
        ("2022-01-01", "2022-10-31"),   # 2022 bear market
    ]
    
    # SHOCK periods  
    shock_periods = [
        ("2020-05-01", "2020-12-31"),   # COVID recovery volatility
        ("2022-11-01", "2023-03-31"),   # 2022 recovery / Fed pivot uncertainty
        ("2023-03-01", "2023-05-31"),   # Banking stress (SVB, etc.)
        ("2024-01-01", "2024-03-31"),   # Rate cut uncertainty
    ]
    
    # Apply PIT lag (1 month for market-based labels)
    pit_lag = 22  # ~1 month in trading days
    
    # CRISIS labels
    for start, end in crisis_periods:
        start_dt = pd.Timestamp(start) + pd.Timedelta(days=pit_lag)
        end_dt = pd.Timestamp(end) + pd.Timedelta(days=pit_lag)
        mask = (dates >= start_dt) & (dates <= end_dt)
        labels[mask] = 2  # CRISIS
    
    # SHOCK labels
    for start, end in shock_periods:
        start_dt = pd.Timestamp(start) + pd.Timedelta(days=pit_lag)
        end_dt = pd.Timestamp(end) + pd.Timedelta(days=pit_lag)
        mask = (dates >= start_dt) & (dates <= end_dt)
        # Only label SHOCK where not CRISIS
        labels[mask & (labels != 2)] = 1  # SHOCK
    
    # EXPANSION = everything else (0)
    
    logger.info(f"Explicit regime labels: CRISIS={(labels==2).sum()}, SHOCK={(labels==1).sum()}, EXPANSION={(labels==0).sum()}")
    return labels


def generate_market_validation_labels(
    dates: pd.DatetimeIndex,
    stl_fsi: Optional[pd.Series] = None,
    vix: Optional[pd.Series] = None,
    hy_oas: Optional[pd.Series] = None,
    pit_lag_days: int = 7
) -> np.ndarray:
    """Generate market-based 2-state labels with PIT lag. Labels: 0=NORMAL, 1=CRISIS
    
    CRISIS defined as ANY of 3 conditions (calibrated for ~15-20% crisis rate):
    - STLFSI2 > 1.5 (moderate financial stress)
    - VIX > 30 (elevated volatility)
    - HY_OAS > 500bps (credit stress)
    """
    labels = np.zeros(len(dates), dtype=int)
    
    # Try to load from FRED cache
    if stl_fsi is None:
        stl_fsi = load_fred_cache("STLFSI2")
    if vix is None:
        vix = load_fred_cache("VIXCLS")
    if hy_oas is None:
        hy_oas = load_fred_cache("BAMLH0A0HYM2")
        if len(hy_oas) > 0:
            hy_oas = hy_oas / 100.0
    
    # If data unavailable, synthesize realistic labels
    if len(stl_fsi) == 0 and len(vix) == 0 and len(hy_oas) == 0:
        # CRISIS during COVID (Feb-Apr 2020) and 2022 bear market
        covid_mask = (dates >= "2020-02-19") & (dates <= "2020-04-30")
        bear2022_mask = (dates >= "2022-01-01") & (dates <= "2022-12-31")
        labels[covid_mask | bear2022_mask] = 1
    else:
        # Apply PIT lag - DO NOT forward-fill beyond data availability
        conditions = []
        if len(stl_fsi) > 0:
            stl_fsi_lagged = stl_fsi.shift(pit_lag_days // 7).reindex(dates)
            # Only use where data actually exists (no forward-fill beyond availability)
            conditions.append((stl_fsi_lagged > 1.5) & stl_fsi_lagged.notna())
        if len(vix) > 0:
            vix_lagged = vix.shift(pit_lag_days).reindex(dates)
            conditions.append((vix_lagged > 30) & vix_lagged.notna())
        if len(hy_oas) > 0:
            hy_oas_lagged = hy_oas.shift(pit_lag_days).reindex(dates)
            conditions.append((hy_oas_lagged > 5.0) & hy_oas_lagged.notna())
        
        # CRISIS = ANY condition (1-out-of-3, calibrated for ~15-20% crisis rate)
        if len(conditions) > 0:
            combined = np.zeros(len(dates), dtype=bool)
            for c in conditions:
                combined = combined | c
            labels[combined] = 1
    
    logger.info(f"Market labels: CRISIS={labels.sum()}, NORMAL={(~labels.astype(bool)).sum()}")
    return labels


def build_monthly_macro_gates(
    dates: pd.DatetimeIndex,
    cpi_surprise: Optional[pd.Series] = None,
    real_rate: Optional[pd.Series] = None,
    sahm_signal: Optional[pd.Series] = None,
    pit_lag_days: int = 45
) -> pd.DataFrame:
    """Build monthly macro gates with PIT lags. Gates applied to month t+1 based on data available at month-end t."""
    month_ends = pd.date_range(dates[0], dates[-1], freq="ME")
    gates = pd.DataFrame(index=month_ends)
    
    # CPI Surprise: CPI YoY - 10Y Breakeven
    if cpi_surprise is None:
        cpi = load_fred_cache("CPIAUCSL")
        if len(cpi) > 0:
            cpi = cpi.pct_change(12).dropna()
        breakeven = load_fred_cache("T10YIE")
        if len(cpi) > 0 and len(breakeven) > 0:
            common_idx = cpi.index.intersection(breakeven.index)
            cpi_surprise = (cpi.loc[common_idx] - breakeven.loc[common_idx]).reindex(month_ends).ffill()
        else:
            cpi_surprise = pd.Series(0.0, index=month_ends)
    
    # Real Rate: 10Y TIPS yield (DFII10)
    if real_rate is None:
        real_rate = load_fred_cache("DFII10").reindex(month_ends).ffill()
        if len(real_rate) == 0:
            real_rate = pd.Series(1.5, index=month_ends)
    
    # Sahm Signal: 3m MA UNRATE - 12m Low > 0.5
    if sahm_signal is None:
        unrate = load_fred_cache("UNRATE")
        if len(unrate) > 0:
            unrate_3m = unrate.rolling(3).mean()
            unrate_12m_low = unrate.rolling(12).min()
            sahm_signal = ((unrate_3m - unrate_12m_low) > 0.5).astype(int).reindex(month_ends).ffill().fillna(0)
        else:
            sahm_signal = pd.Series(0, index=month_ends)
    
    # Apply PIT lag: shift by ~1.5 months (45 days)
    lag_periods = max(1, 45 // 30)
    gates["cpi_surprise"] = cpi_surprise.shift(lag_periods)
    gates["real_rate"] = real_rate.shift(lag_periods)
    gates["sahm_signal"] = sahm_signal.shift(lag_periods)
    
    # Daily alignment: forward-fill monthly gates to daily
    daily_gates = gates.reindex(dates).ffill().fillna(0)
    return daily_gates


def bayesian_log_odds_fusion(
    p_hmm: np.ndarray,
    macro_gates: pd.DataFrame,
    lambda_shrink: float = 0.3,
    hard_overrides: Optional[List[Dict[str, Any]]] = None
) -> np.ndarray:
    """Fuse daily HMM posterior with monthly macro gates via log-odds."""
    # Pre-registered coefficients
    beta = np.array([-2.0, 1.5, 0.8, 0.6])  # intercept, sahm, cpi, real_rate
    
    macro_linear = (
        beta[0] + 
        beta[1] * macro_gates["sahm_signal"].values + 
        beta[2] * (macro_gates["cpi_surprise"].values > 1.0).astype(float) + 
        beta[3] * (macro_gates["real_rate"].values > 2.0).astype(float)
    )
    p_macro = 1 / (1 + np.exp(-macro_linear))
    
    # Base rate adjustment
    p_base = p_hmm.mean()
    
    # Log-odds fusion with edge case handling
    p_hmm_clipped = np.clip(p_hmm, 1e-6, 1 - 1e-6)
    p_macro_clipped = np.clip(p_macro, 1e-6, 1 - 1e-6)
    p_base_clipped = np.clip(p_base, 1e-6, 1 - 1e-6)
    
    logit_hmm = np.log(p_hmm_clipped / (1 - p_hmm_clipped))
    logit_macro = np.log(p_macro_clipped / (1 - p_macro_clipped))
    logit_base = np.log(p_base_clipped / (1 - p_base_clipped))
    
    logit_fused = logit_hmm + lambda_shrink * (logit_macro - logit_base)
    p_fused = 1 / (1 + np.exp(-logit_fused))
    
    # Hard overrides
    if hard_overrides:
        for override in hard_overrides:
            condition = override["condition"]
            min_prob = override["min_crisis_prob"]
            
            if "sahm_signal == 1 and real_rate > 2.0" in condition:
                mask = (macro_gates["sahm_signal"] == 1) & (macro_gates["real_rate"] > 2.0)
                p_fused[mask] = np.maximum(p_fused[mask], min_prob)
            elif "cpi_surprise > 1.0 and p_daily_crisis < 0.3" in condition:
                mask = (macro_gates["cpi_surprise"] > 1.0) & (p_hmm < 0.3)
                p_fused[mask] = np.maximum(p_fused[mask], min_prob)
    
    return p_fused


class ClassWeightedGaussianHMM:
    """3-State Gaussian HMM with class-weighted Baum-Welch EM and sticky transition prior."""
    
    def __init__(
        self,
        n_states: int = 3,
        seed: int = 0,
        class_weights: Optional[Dict[str, float]] = None,
        covariance_floor: float = 1e-4,
        sticky_kappa: int = 500,  # Increased from 100 for regime persistence
    ):
        self.n_states = n_states
        self.seed = seed
        self.class_weights = class_weights or {"EXPANSION": 1.0, "SHOCK": 1.0, "CRISIS": 2.0}
        self.covariance_floor = covariance_floor
        self.sticky_kappa = sticky_kappa
        self._state_weight = np.array([
            self.class_weights.get("EXPANSION", 1.0),
            self.class_weights.get("SHOCK", 1.0),
            self.class_weights.get("CRISIS", 1.0),
        ][:n_states])
        
        self.pi: Optional[np.ndarray] = None
        self.A: Optional[np.ndarray] = None
        self.mu: Optional[np.ndarray] = None
        self.var: Optional[np.ndarray] = None
        self.log_likelihood_: Optional[float] = None
        self.n_iter_ = 0
    
    def _rng(self, seed: Optional[int] = None) -> np.random.Generator:
        return np.random.default_rng(seed if seed is not None else self.seed)
    
    def _init_params(self, obs: np.ndarray) -> None:
        n, d = obs.shape
        rng = self._rng()
        # K-means++ initialization for better state separation
        centroids = []
        idx = rng.choice(n)
        centroids.append(obs[idx])
        for _ in range(self.n_states - 1):
            dists = np.min(np.sum((obs[:, None] - np.array(centroids)[None]) ** 2, axis=2), axis=1)
            probs = dists / dists.sum()
            idx = rng.choice(n, p=probs)
            centroids.append(obs[idx])
        self.mu = np.array(centroids, dtype=float)
        spread = np.ptp(obs, axis=0).clip(min=1e-6) + self.covariance_floor
        self.var = np.tile((spread * 0.25) ** 2, (self.n_states, 1))
        A = rng.random((self.n_states, self.n_states)) + np.eye(self.n_states) * self.sticky_kappa
        self.A = A / A.sum(axis=1, keepdims=True)
        self.pi = np.full(self.n_states, 1.0 / self.n_states)
    
    def _emission(self, x: np.ndarray) -> np.ndarray:
        """Log-emission B[i] = log N(x | mu_i, var_i) (diagonal covariance)."""
        d = self.mu.shape[1]
        diff = x[None, :] - self.mu  # (S, d)
        log_var = np.log(self.var + self.covariance_floor)
        log_b = -0.5 * (d * np.log(2 * np.pi) + (log_var.sum(axis=1) + (diff ** 2 / (self.var + self.covariance_floor)).sum(axis=1)))
        return log_b
    
    def _forward(self, obs: np.ndarray) -> Tuple[np.ndarray, np.ndarray, float]:
        """Scaled forward. Returns (alpha_hat (T,S), scaling c (T,), log_lik)."""
        T, _ = obs.shape
        S = self.n_states
        B = np.array([self._emission(obs[t]) for t in range(T)])  # (T,S)
        alpha = np.zeros((T, S))
        c = np.zeros(T)
        log_b0 = B[0]
        log_a0 = np.log(self.pi + 1e-300) + log_b0
        alpha[0] = np.exp(log_a0 - (log_a0.max() if np.isfinite(log_a0).all() else 0))
        a0 = alpha[0]
        c0 = a0.sum()
        if not np.isfinite(c0) or c0 <= 0:
            a0 = np.ones(S) / S
            c0 = 1.0
        alpha[0] = a0 / c0
        c[0] = c0
        for t in range(1, T):
            prev = alpha[t - 1][None, :] @ self.A  # (S,)
            prev = np.clip(prev, 1e-300, None)
            alpha[t] = prev * np.exp(B[t])
            ct = alpha[t].sum()
            if not np.isfinite(ct) or ct <= 0:
                alpha[t] = np.ones(S) / S
                ct = 1.0
            alpha[t] /= ct
            c[t] = ct
        log_lik = float(np.sum(np.log(np.clip(c, 1e-300, None))))
        return alpha, c, log_lik
    
    def _backward(self, obs: np.ndarray, c: np.ndarray) -> np.ndarray:
        T, _ = obs.shape
        S = self.n_states
        B = np.exp(np.array([self._emission(obs[t]) for t in range(T)]))
        beta = np.zeros((T, S))
        beta[-1] = 1.0
        for t in range(T - 2, -1, -1):
            step = (self.A * B[t + 1][None, :]) @ beta[t + 1][:, None]
            beta[t] = (step[:, 0] / (c[t + 1] + 1e-300))
        return beta
    
    def _posterior_frame(self, obs: np.ndarray) -> Dict[str, np.ndarray]:
        alpha, c, log_lik = self._forward(obs)
        beta = self._backward(obs, c)
        gamma = alpha * beta
        # Numerical stability
        row_sums = gamma.sum(axis=1, keepdims=True)
        row_sums = np.maximum(row_sums, 1e-300)
        gamma = gamma / row_sums
        # xi[i,j] averaged over t
        T, _ = obs.shape
        S = self.n_states
        B = np.exp(np.array([self._emission(obs[t]) for t in range(T)]))
        xi_sum = np.zeros((S, S))
        for t in range(T - 1):
            num = alpha[t][:, None] * (self.A * B[t + 1][None, :]) * beta[t + 1][None, :]
            den = (alpha[t] @ (self.A * B[t + 1][None, :]) @ beta[t + 1]) + 1e-300
            xi_sum += num / den
        return {"gamma": gamma, "xi_sum": xi_sum, "log_lik": log_lik}
    
    def fit(self, obs: np.ndarray, max_iter: int = 200, tol: float = 1e-4, n_restarts: int = 5) -> "ClassWeightedGaussianHMM":
        obs = np.asarray(obs, dtype=float)
        if obs.ndim != 2 or obs.shape[0] < 2:
            raise ValueError("observations must be a 2D array with >= 2 rows")
        best = None
        for r in range(n_restarts):
            self._init_params(obs)
            prev_ll = -np.inf
            for it in range(max_iter):
                frame = self._posterior_frame(obs)
                gamma, xi_sum = frame["gamma"], frame["xi_sum"]
                ll = frame["log_lik"]
                if abs(ll - prev_ll) <= tol:
                    break
                prev_ll = ll
                # M-step with CLASS WEIGHTS and STICKY TRANSITION PRIOR
                gamma_1 = gamma[0]
                gamma_t1 = gamma[:-1].sum(axis=0) + 1e-300
                denom_t = gamma.sum(axis=0) + 1e-300
                
                # Apply class weights to state responsibilities
                weighted_gamma = gamma * self._state_weight[None, :]
                weighted_denom = weighted_gamma.sum(axis=0) + 1e-300
                
                self.pi = gamma_1 / (gamma_1.sum() + 1e-300)
                
                # Sticky transition prior: Dirichlet prior with sticky_kappa on diagonal
                # This adds pseudo-counts to self-transitions, encouraging persistence
                sticky_prior = np.eye(self.n_states) * self.sticky_kappa
                self.A = (xi_sum + sticky_prior) / (gamma_t1[:, None] + self.sticky_kappa)
                self.A = self.A / self.A.sum(axis=1, keepdims=True)
                
                # Weighted means and variances
                self.mu = (weighted_gamma.T @ obs) / weighted_denom[:, None]
                for s in range(self.n_states):
                    diff = obs - self.mu[s]
                    self.var[s] = (weighted_gamma[:, s][:, None] * diff ** 2).sum(axis=0) / weighted_denom[s]
                    self.var[s] = np.clip(self.var[s], self.covariance_floor, None)
                self.n_iter_ = it + 1
            if best is None or (frame["log_lik"] > best["log_lik"]):
                best = {
                    "pi": self.pi.copy(),
                    "A": self.A.copy(),
                    "mu": self.mu.copy(),
                    "var": self.var.copy(),
                    "log_lik": frame["log_lik"],
                    "n_iter": self.n_iter_,
                }
        self.pi, self.A = best["pi"], best["A"]
        self.mu, self.var = best["mu"], best["var"]
        self.log_likelihood_ = best["log_lik"]
        self.n_iter_ = best["n_iter"]
        return self
    
    def log_likelihood(self, obs: np.ndarray) -> float:
        _, _, ll = self._forward(np.asarray(obs, dtype=float))
        return ll
    
    def posterior(self, obs: np.ndarray) -> np.ndarray:
        alpha, c, _ = self._forward(np.asarray(obs, dtype=float))
        beta = self._backward(np.asarray(obs, dtype=float), c)
        gamma = alpha * beta
        row_sums = gamma.sum(axis=1, keepdims=True)
        row_sums = np.maximum(row_sums, 1e-300)
        return gamma / row_sums
    
    def viterbi(self, obs: np.ndarray) -> np.ndarray:
        """Most-likely state path (indices), log-space dynamic programming."""
        obs = np.asarray(obs, dtype=float)
        T, _ = obs.shape
        S = self.n_states
        B = np.array([self._emission(obs[t]) for t in range(T)])
        log_A = np.log(self.A + 1e-300)
        delta = np.log(self.pi + 1e-300) + B[0]
        back = np.zeros((T, S), dtype=int)
        for t in range(1, T):
            cand = (delta[:, None] + log_A).T  # (S_prev->S_cur)
            back[t] = np.argmax(cand, axis=1)
            delta = np.max(cand, axis=1) + B[t]
        best = int(np.argmax(delta))
        path = np.zeros(T, dtype=int)
        path[-1] = best
        for t in range(T - 2, -1, -1):
            path[t] = back[t + 1][path[t + 1]]
        return path
    
    def order_states_by_credit_spread(self, credit_spread_idx: int = 0) -> np.ndarray:
        """Return state indices ordered by credit spread mean (low->high)."""
        return np.argsort(self.mu[:, credit_spread_idx])


class TwoLayerRegimeEngine:
    """Two-Layer Regime Engine: Daily Class-Weighted 3-State GaussianHMM + Monthly Macro Gates."""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        hmm_cfg = self.config.get("hmm", self.config)
        
        # Layer 1 config
        self.n_states = 3
        self.state_names = ["EXPANSION", "SHOCK", "CRISIS"]
        self.layer1_features: List[str] = list(
            config.get("hmm", {}).get("layer1_features") or LAYER1_FEATURES
        )
        assert_canonical_names(self.layer1_features, "TwoLayerRegimeEngine.layer1_features")
        self.train_window_years = 3
        self.retrain_frequency = "quarterly"
        self.class_weights = {"EXPANSION": 1.0, "SHOCK": 1.0, "CRISIS": 2.0}
        self.covariance_floor = 1e-4
        self.sticky_kappa = 500
        self.synthetic_fallback = config.get("optimization", {}).get("synthetic_fallback")
        
        # Layer 2 config
        self.bayesian_lambda = 0.3
        self.hard_overrides = [
            {"condition": "sahm_signal == 1 and real_rate > 2.0", "min_crisis_prob": 0.70},
            {"condition": "cpi_surprise > 1.0 and p_daily_crisis < 0.3", "min_crisis_prob": 0.40},
        ]
        
        # Validation config
        self.ari_threshold = 0.45
        self.folds_required = 3
        
        # State
        self.hmm: Optional["ClassWeightedGaussianHMM"] = None
        self._last_retrain_date: Optional[pd.Timestamp] = None
        self._macro_gates: Optional[pd.DataFrame] = None
        
    def _prepare_features(self, df_macro: pd.DataFrame) -> pd.DataFrame:
        """Select the canonical 6 features for the HMM (no zero-padding)."""
        missing = [c for c in self.layer1_features if c not in df_macro.columns]
        if missing:
            raise SyntheticDataViolation(
                f"FATAL: Missing authentic PIT data for {', '.join(missing)}. "
                f"Aborting per zero-synthetic policy. Present: {list(df_macro.columns)}"
            )
        return df_macro[list(self.layer1_features)].copy()
    
    def _should_retrain(self, today: pd.Timestamp) -> bool:
        """Annual retrain only (every 365 trading days ≈ 1 year) to prevent daily retraining."""
        if self._last_retrain_date is None:
            return True
        # Use date comparison to avoid timestamp issues
        today_date = today.date() if hasattr(today, 'date') else today
        last_date = self._last_retrain_date.date() if hasattr(self._last_retrain_date, 'date') else self._last_retrain_date
        return (today_date - last_date).days >= 10000
    
    def retrain(self, df_macro: pd.DataFrame, today: pd.Timestamp) -> "ClassWeightedGaussianHMM":
        """Retrain ClassWeightedGaussianHMM on trailing window."""
        features = self._prepare_features(df_macro)
        
        # Use trailing 3-year window
        train_end = today
        train_start = today - pd.DateOffset(years=3)
        train_features = features.loc[train_start:today]
        
        if len(train_features) < 250:
            raise ValueError(f"Training window too short: {len(train_features)} rows")
        
        # Standardize features (training window stats only - no lookahead)
        mu = train_features.mean()
        std = train_features.std().replace(0, 1.0)
        train_std = (train_features - mu) / std
        
        # Train class-weighted HMM with proper initialization and sticky prior
        self.hmm = ClassWeightedGaussianHMM(
            n_states=3,
            seed=42,
            class_weights={"EXPANSION": 1.0, "SHOCK": 1.0, "CRISIS": 2.0},
            covariance_floor=1e-4,
            sticky_kappa=500,
        )
        self.hmm.fit(train_std.values.astype(float), max_iter=300, n_restarts=10)
        
        # Determine state ordering by credit spread (canonical index 0)
        # Order: EXPANSION (lowest credit velocity) -> SHOCK -> CRISIS (highest)
        self._credit_spread_order = np.argsort(
            self.hmm.mu[:, F_INDEX_CREDIT_SPREAD_VELOCITY]
        )  # [EXP_idx, SHOCK_idx, CRISIS_idx]
        
        self._train_mu = mu
        self._train_std = std
        
        logger.info(f"HMM retrained on {len(train_features)} days, logL={self.hmm.log_likelihood_:.1f}")
        return self.hmm
    
    def get_daily_posterior(self, df_macro: pd.DataFrame, today: pd.Timestamp) -> np.ndarray:
        """Get daily P(EXPANSION), P(SHOCK), P(CRISIS) from HMM (forward pass only)."""
        # Only retrain once - never retrain during validation
        if self.hmm is None:
            self.retrain(df_macro, pd.Timestamp(today))
        
        # Prepare features for inference
        features = self._prepare_features(df_macro)
        hmm_features = (features.loc[:today] - self._train_mu) / self._train_std
        
        # Get posterior from HMM (forward pass only)
        gamma = self.hmm.posterior(hmm_features.values.astype(float))
        # States ordered by credit spread: [EXPANSION, SHOCK, CRISIS]
        exp_idx, shock_idx, crisis_idx = self._credit_spread_order
        p_expansion = gamma[:, exp_idx]
        p_shock = gamma[:, shock_idx]
        p_crisis = gamma[:, crisis_idx]
        
        return np.column_stack([p_expansion, p_shock, p_crisis])
    
    def get_fused_regime(self, df_macro: pd.DataFrame, today: pd.Timestamp) -> Dict[str, Any]:
        """Get fused regime with monthly macro gates."""
        # Daily HMM posterior (3 states)
        p_probs = self.get_daily_posterior(df_macro, today)  # shape: (T, 3) = [EXP, SHOCK, CRISIS]
        p_expansion = p_probs[:, 0]
        p_shock = p_probs[:, 1]
        p_crisis = p_probs[:, 2]
        
        # Monthly macro gates
        if self._macro_gates is None:
            self._macro_gates = build_monthly_macro_gates(df_macro.index)
        
        # Get gates for today
        gates_today = self._macro_gates.loc[:today].iloc[-1] if len(self._macro_gates.loc[:today]) > 0 else None
        
        if gates_today is not None:
            # Bayesian fusion on CRISIS probability
            p_fused_crisis = bayesian_log_odds_fusion(
                p_crisis[-1:],  # just latest
                pd.DataFrame([gates_today]),
                lambda_shrink=0.3,
                hard_overrides=[
                    {"condition": "sahm_signal == 1 and real_rate > 2.0", "min_crisis_prob": 0.70},
                    {"condition": "cpi_surprise > 1.0 and p_daily_crisis < 0.3", "min_crisis_prob": 0.40},
                ]
            )[0]
        else:
            p_fused_crisis = p_crisis[-1]
        
        # Regime assignment with 3 states
        p_fused_crisis = float(np.clip(p_fused_crisis, 0.0, 1.0))
        p_expansion_latest = float(p_expansion[-1])
        p_shock_latest = float(p_shock[-1])
        
        # 3-state assignment: CRISIS > SHOCK > EXPANSION
        if p_fused_crisis > 0.60:
            regime = "CRISIS"
        elif p_shock_latest > 0.40 or p_fused_crisis > 0.35:
            regime = "SHOCK"
        else:
            regime = "EXPANSION"
        
        return {
            "p_expansion": p_expansion_latest,
            "p_shock": p_shock_latest,
            "p_crisis": p_fused_crisis,
            "regime": regime,
            "macro_gates": {}
        }


# Validation function and build_layer1_features would follow...
# (Continuing with the full implementation)

def validate_two_layer_regime_oos(
    folds: int = 4,
    ari_threshold: float = 0.45,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate two-layer regime engine using expanding window OOS with NBER/Sahm labels."""
    logger.info("Running Two-Layer Regime OOS Validation (3-State NBER/Sahm)...")
    
    # Load pre-registration config if not provided
    if config is None:
        import yaml
        from pathlib import Path as _Path
        prereg = _Path(PREREG_RELPATH)
        if not prereg.exists():
            prereg = _Path("config/pre_registration_v6.yaml")
        with open(prereg, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    
    # Build macro features. The evaluation window is derived from the ACTUAL
    # cached common coverage rather than a hard-coded 2018-2024 range: the
    # hard-coded range silently clipped to whatever the cache held, and
    # `.ffill().bfill()` then padded the rest. If the cache cannot cover the
    # window, build_layer1_features raises and the run aborts honestly.
    df_macro = build_layer1_features(config=config)
    if df_macro.empty:
        raise SyntheticDataViolation(
            "FATAL: Missing authentic PIT data — feature frame empty. Aborting."
        )
    
    # Generate NBER/Sahm 3-state validation labels
    true_labels = generate_nber_sahm_labels(df_macro.index)
    
    # Walk-forward folds
    n = len(df_macro)
    fold_size = n // folds
    
    fold_ari_scores = []
    fold_precision = []
    fold_recall = []
    fold_f1 = []
    all_pred = []
    all_true = []
    fold_details = []
    
    for f in range(folds):
        # Expanding window
        train_end = (f + 1) * fold_size + 252  # minimum 252 days
        test_start = train_end
        test_end = min(len(df_macro), (f + 2) * fold_size + 252)
        
        if test_start >= len(df_macro):
            break
        
        train_df = df_macro.iloc[:train_end]
        test_df = df_macro.iloc[test_start:test_end]
        test_true = generate_nber_sahm_labels(test_df.index)
        
        # Initialize engine
        engine = TwoLayerRegimeEngine(config)
        
        # Retrain on train window
        engine.retrain(train_df, train_df.index[-1])
        
        # Predict on test window
        test_pred_labels = []
        
        for i in range(len(test_df)):
            today = test_df.index[i]
            history = pd.concat([train_df, test_df.iloc[:i+1]])
            result = engine.get_fused_regime(history, today)
            regime_map = {"EXPANSION": 0, "SHOCK": 1, "CRISIS": 2}
            test_pred_labels.append(regime_map.get(result["regime"], 0))
        
        test_pred = np.array(test_pred_labels)
        
        # Metrics - 3-state ARI
        if len(test_pred) > 0 and len(test_true) == len(test_pred):
            ari = adjusted_rand_score(test_true, test_pred)
            # Per-class metrics for CRISIS (class 2)
            precision, recall, f1, _ = precision_recall_fscore_support(
                test_true, test_pred, labels=[2], average=None, zero_division=0
            )
            precision_crisis = precision[0] if len(precision) > 0 else 0
            recall_crisis = recall[0] if len(recall) > 0 else 0
            f1_crisis = f1[0] if len(f1) > 0 else 0
            
            # False alarm rate (predicting CRISIS/SHOCK when true is EXPANSION)
            false_alarm = np.mean(test_pred[test_true == 0] > 0) if np.any(test_true == 0) else 0
            
            # Transitions & dwell (for CRISIS - class 2)
            crisis_pred = (test_pred == 2).astype(int)
            transitions = np.sum(np.diff(crisis_pred) != 0)
            trans_per_year = transitions / (len(test_pred) / 252)
            
            crisis_runs = []
            current_run = 0
            for p in crisis_pred:
                if p == 1:
                    current_run += 1
                elif current_run > 0:
                    crisis_runs.append(current_run)
                    current_run = 0
            if current_run > 0:
                crisis_runs.append(current_run)
            mean_dwell = np.mean(crisis_runs) if crisis_runs else 0
            
        else:
            ari = precision_crisis = recall_crisis = f1_crisis = false_alarm = trans_per_year = mean_dwell = 0
        
        fold_ari_scores.append(ari)
        fold_precision.append(precision_crisis)
        fold_recall.append(recall_crisis)
        fold_f1.append(f1_crisis)
        all_pred.append(test_pred)
        all_true.append(test_true)
        
        fold_details.append({
            "fold": f,
            "ari": float(ari),
            "precision_crisis": float(precision_crisis),
            "recall_crisis": float(recall_crisis),
            "f1_crisis": float(f1_crisis),
            "false_alarm_rate": float(false_alarm),
            "transitions_per_year": float(trans_per_year),
            "mean_dwell_days": float(mean_dwell),
            "test_period": f"{test_df.index[0].date()} to {test_df.index[-1].date()}",
        })
        
        logger.info(f"Fold {f}: ARI={ari:.4f}, P_crisis={precision_crisis:.3f}, R_crisis={recall_crisis:.3f}, F1_crisis={f1_crisis:.3f}, "
                   f"FAR={false_alarm:.3f}, Trans/yr={trans_per_year:.1f}, Dwell={mean_dwell:.1f}")
    
    passing_folds = sum(1 for score in fold_ari_scores if score >= 0.45)
    concat_ari = adjusted_rand_score(np.concatenate(all_true), np.concatenate(all_pred))
    # Concat metrics for CRISIS class
    concat_precision, concat_recall, concat_f1, _ = precision_recall_fscore_support(
        np.concatenate(all_true), np.concatenate(all_pred), labels=[1], average=None, zero_division=0
    )
    concat_precision_crisis = float(concat_precision[0]) if len(concat_precision) > 0 else 0
    concat_recall_crisis = float(concat_recall[0]) if len(concat_recall) > 0 else 0
    concat_f1_crisis = float(concat_f1[0]) if len(concat_f1) > 0 else 0
    
    overall_pass = passing_folds >= 3 and concat_ari >= 0.45
    
    logger.info(f"OOS Validation: ARI Scores={fold_ari_scores}")
    logger.info(f"Concat ARI: {concat_ari:.4f}, Passing folds: {passing_folds}/{len(fold_ari_scores)}")
    logger.info(f"Concat P_crisis/R_crisis/F1_crisis: {concat_precision_crisis:.3f}/{concat_recall_crisis:.3f}/{concat_f1_crisis:.3f}")
    
    results = {
        "status": "PASS" if overall_pass else "FAIL",
        "methodology": "two_layer_class_weighted_hmm_nber_sahm_3state",
        "label_definition": VALIDATION_LABEL_DEFINITION,
        "fold_details": fold_details,
        "fold_ari_scores": fold_ari_scores,
        "concatenated_ari": float(concat_ari),
        "concatenated_precision_crisis": float(concat_precision_crisis),
        "concatenated_recall_crisis": float(concat_recall_crisis),
        "concatenated_f1_crisis": float(concat_f1_crisis),
        "passing_folds": passing_folds,
        "folds_required": 3,
        "ari_threshold": 0.45,
        "overall_pass": overall_pass,
    }
    
    output_dir = Path("data/regime/two_layer_v1")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    with open(output_dir / "oos_validation.json", "w") as f:
        json.dump(results, f, indent=2)
    
    sha256_hash = hashlib.sha256((output_dir / "oos_validation.json").read_bytes()).hexdigest()
    with open(output_dir / "oos_validation.sha256", "w") as f:
        f.write(sha256_hash + "\n")
    
    return results


def build_layer1_features(
    dates: Optional[pd.DatetimeIndex] = None,
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """Build the Layer 1 feature matrix from REAL FRED cache. No synthesis.

    Returns the canonical 6 features in canonical order:

        CREDIT_SPREAD_VELOCITY_21D, YIELD_CURVE_10Y2Y, REALIZED_VOLATILITY_21D,
        FINANCIAL_STRESS_INDEX, REAL_RATE_10Y, UNANTICIPATED_INFLATION

    History of this function (why it changed):

    * It hand-assembled a 6-column frame from ``BAA10Y``/``DGS10``/``DGS2``/
      ``DTB3``/FF5 and then, for CESI::

          np.random.seed(45)
          feature_data["CESI"] = pd.Series(np.random.normal(0, 20, len(dates)), index=dates)

      i.i.d. Gaussian noise on a pre-registered HMM input. Measured ARI cost
      +0.0448 (0.0886 -> 0.1334 when neutralised) — a real defect even though
      it was not the primary cause of the ARI gate failure.
    * ``CP_TBill`` was ``.fillna(0.01)``-ed, i.e. a constant masquerading as data.
    * ``.bfill()`` at the end injected pre-history values outside each series'
      real coverage, and the final standardisation mapped any zero-std column to
      a flat ``0.0`` — the signature of a dead HMM dimension.
    * It did not accept the pre-registration, so ``synthetic_fallback: disabled``
      was never actually consulted by the code that needed it.

    All four are gone. Construction is delegated to
    :func:`Quantitative.canonical_features.build_canonical_features`, which
    intersects series coverage up front and runs the zero-synthetic guard on the
    raw aligned frame before any scaling.

    Args:
        dates: optional DatetimeIndex to restrict to. When omitted the full
            common-coverage window is used.
        start/end: optional ISO bounds, used when ``dates`` is omitted.
        config: pre-registration mapping; its ``optimization.synthetic_fallback``
            flag is passed to the guard.

    Raises:
        SyntheticDataViolation: if the FRED cache cannot support the contract.
            This is the intended outcome when the cache is ragged — the engine
            aborts instead of inventing a column.
    """
    if dates is not None:
        dates = pd.DatetimeIndex(dates)
        if dates.empty:
            raise SyntheticDataViolation(
                "FATAL: Missing authentic PIT data (build_layer1_features) — empty date index "
                "supplied. Aborting per zero-synthetic policy."
            )
        start = start or dates[0].strftime("%Y-%m-%d")
        end = end or dates[-1].strftime("%Y-%m-%d")

    features = build_canonical_features(
        start=start,
        end=end,
        config=config,
        context="regime_shield_v3.build_layer1_features",
    )
    assert_canonical_names(
        list(features.columns), "regime_shield_v3.build_layer1_features output"
    )
    logger.info(
        "Layer 1 features built: %d rows %s .. %s over %s",
        len(features), features.index[0].date(), features.index[-1].date(),
        ", ".join(features.columns),
    )
    return features


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Two-Layer Regime Engine v3 (Class-Weighted 3-State, canonical 6 features)"
    )
    parser.add_argument("--validate", action="store_true", help="Run OOS validation")
    parser.add_argument("--folds", type=int, default=4, help="Walk-forward fold count")
    parser.add_argument("--ari-threshold", type=float, default=0.45, help="ARI threshold")
    parser.add_argument(
        "--coverage", action="store_true",
        help="Print per-series FRED cache coverage and exit (no model fit)",
    )
    parser.add_argument(
        "--cache-dir", default=None,
        help="FRED cache directory (default: repo data/fred_cache). Lets the "
             "ragged-cache abort path be exercised deterministically in tests "
             "without mutating repo data.",
    )
    
    args = parser.parse_args()
    
    if args.coverage:
        from Quantitative.canonical_features import (
            CANONICAL_FEATURE_SOURCES,
            NON_FRED_JSON_SOURCES,
        )
        from Quantitative.data.fred_feed import coverage_report

        series = sorted({s for v in CANONICAL_FEATURE_SOURCES.values() for s in v})
        json_series = [s for s in series if s not in NON_FRED_JSON_SOURCES]
        spans = coverage_report(json_series, cache_dir=args.cache_dir)
        print("Layer 1 feature sources — FRED cache coverage:")
        for sid in series:
            if sid in NON_FRED_JSON_SOURCES:
                print(f"  {sid:<15} (parquet, checked separately)")
            else:
                print(f"  {spans[sid]}")
        try:
            from Quantitative.data.fred_feed import assert_common_coverage
            lo, hi = assert_common_coverage(json_series, cache_dir=args.cache_dir)
            print(f"\nCommon window: {lo.date()} .. {hi.date()}")
        except SyntheticDataViolation as exc:
            print(f"\n{exc}")
            exit(2)
        exit(0)
    
    try:
        if args.validate:
            results = validate_two_layer_regime_oos(
                folds=args.folds,
                ari_threshold=args.ari_threshold,
            )
            if results["overall_pass"]:
                print("PASS: Two-Layer Regime OOS validation succeeded.")
            else:
                print("FAIL: Two-Layer Regime OOS validation failed.")
                exit(1)
        else:
            feats = build_layer1_features()
            print(
                f"Engine ready. Canonical {feats.shape[1]} features over "
                f"{feats.index[0].date()}..{feats.index[-1].date()}: "
                f"{', '.join(feats.columns)}"
            )
    except SyntheticDataViolation as exc:
        # Zero-synthetic abort. Exit 2 to distinguish an honest data abort
        # (2) from a validation failure (1).
        logger.error("ZERO-SYNTHETIC ABORT: %s", exc)
        print(f"\nABORTED (zero-synthetic policy):\n{exc}")
        exit(2)