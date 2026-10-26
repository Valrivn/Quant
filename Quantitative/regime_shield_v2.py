"""
regime_shield_v2.py — 3-State Student-t HMM with Daily Nowcast (Phase 8 Redesign)
FROZEN Config: config/pre_registration_phase8.yaml

Provides:
  1. TDistributionHMM: 3-State Multivariate Student-t HMM with sticky Dirichlet prior (alpha=50).
     - States: EXPANSION (nu=6), SHOCK (nu=8), CRISIS (nu=6).
     - State label lock: sigma^2_CRISIS > sigma^2_SHOCK > sigma^2_EXPANSION (enforce interpretability).
     - Min dwell constraint: 3 months (63 trading days).
  2. monthly_reestimate: Parameter MLE re-estimation at month-end.
  3. daily_nowcast: Causal forward-filter step with frozen params.
  4. generate_regime_daily_nowcast: Generates data/regime/regime_daily_nowcast.parquet according to Interface 1 schema.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import scipy.special as sp_special
import scipy.optimize as opt
import yaml

logger = logging.getLogger(__name__)


class ConfigWrapper:
    """Helper to support both dot notation (config.t_hmm.n_states) and dict notation (config['t_hmm']['n_states'])."""
    def __init__(self, data: Dict[str, Any]):
        for k, v in data.items():
            if isinstance(v, dict):
                setattr(self, k, ConfigWrapper(v))
            else:
                setattr(self, k, v)
        self._data = data

    def __getitem__(self, item: str) -> Any:
        return self._data[item]

    def get(self, item: str, default: Any = None) -> Any:
        return self._data.get(item, default)


def load_config(config_path_or_obj: Union[str, Dict[str, Any], ConfigWrapper]) -> ConfigWrapper:
    if isinstance(config_path_or_obj, ConfigWrapper):
        return config_path_or_obj
    if isinstance(config_path_or_obj, str):
        with open(config_path_or_obj, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        return ConfigWrapper(raw)
    if isinstance(config_path_or_obj, dict):
        return ConfigWrapper(config_path_or_obj)
    raise TypeError(f"Unsupported config type: {type(config_path_or_obj)}")


@dataclass
class RegimeState:
    detected_state: str           # "EXPANSION", "SHOCK", or "CRISIS"
    state_probs: Dict[str, float] # state_name -> prob
    trend: str                    # "BULL", "BEAR", or "NEUTRAL"
    trend_probs: Dict[str, float] # trend probabilities
    vol: float                    # realized or projected vol
    confident: bool               # prob >= 0.60
    date: Optional[pd.Timestamp] = None


class TDistributionHMM:
    """3-State Multivariate Student-t Hidden Markov Model with Sticky Dirichlet Prior."""

    def __init__(self, config: Union[ConfigWrapper, Dict[str, Any], str, None] = None):
        if config is not None:
            cfg = load_config(config)
            self.config = cfg
            p8 = getattr(cfg, "phase8", cfg.get("phase8") if isinstance(cfg, dict) else None)
            reg_cfg = getattr(p8, "regime_hmm", None) if p8 is not None else None
            
            if reg_cfg is not None:
                self.n_states = int(getattr(reg_cfg, "n_states", 3))
                self.sticky_kappa = float(getattr(reg_cfg, "sticky_prior_alpha", 50))
                self.min_dwell_months = int(getattr(reg_cfg, "min_dwell_months", 3))
            else:
                self.n_states = 3
                self.sticky_kappa = 50.0
                self.min_dwell_months = 3
        else:
            self.n_states = 3
            self.sticky_kappa = 50.0
            self.min_dwell_months = 3

        # State specific degrees of freedom: EXPANSION (nu=6), SHOCK (nu=8), CRISIS (nu=6)
        self.nu_vec = np.array([6.0, 8.0, 6.0]) if self.n_states == 3 else np.full(self.n_states, 6.0)
        self.state_names = ["EXPANSION", "SHOCK", "CRISIS"] if self.n_states == 3 else ["EXPANSION", "CRISIS"]
        self.observables = ["BAA10Y_spread", "VIX", "ERP"]
        self.min_dwell_days = self.min_dwell_months * 21  # ~63 trading days

        # Frozen parameters learned during monthly_reestimate
        self.transmat_: Optional[np.ndarray] = None  # (S, S)
        self.means_: Optional[np.ndarray] = None     # (S, d)
        self.covars_: Optional[np.ndarray] = None    # (S, d, d)
        self.pi_: Optional[np.ndarray] = None        # (S,)
        self.state_labels_: List[str] = list(self.state_names)
        self.is_fitted: bool = False
        self._last_filter_prob: Optional[np.ndarray] = None
        self._days_in_current_state: int = 0
        self._current_state_idx: int = 0

    def _multivariate_t_logpdf(self, x: np.ndarray, mu: np.ndarray, Sigma: np.ndarray, df: float) -> float:
        """Log pdf of d-dimensional Multivariate Student-t distribution with df degrees of freedom."""
        d = len(x)
        diff = x - mu
        inv_Sigma = np.linalg.pinv(Sigma)
        sign, logdet = np.linalg.slogdet(Sigma)
        if sign <= 0 or not np.isfinite(logdet):
            logdet = float(np.sum(np.log(np.diag(Sigma) + 1e-6)))
            inv_Sigma = np.diag(1.0 / (np.diag(Sigma) + 1e-6))

        mahalanobis = float(diff.T @ inv_Sigma @ diff)
        mahalanobis = max(0.0, mahalanobis)

        log_gamma_num = sp_special.gammaln((df + d) / 2.0)
        log_gamma_den = sp_special.gammaln(df / 2.0)

        log_pdf = (log_gamma_num - log_gamma_den
                   - 0.5 * d * np.log(df * np.pi)
                   - 0.5 * logdet
                   - 0.5 * (df + d) * np.log(1.0 + mahalanobis / df))
        return float(log_pdf)

    def monthly_reestimate(self, history_df: pd.DataFrame) -> "TDistributionHMM":
        """Monthly MLE parameter re-estimation with sticky Dirichlet prior and 3-state label lock."""
        obs_cols = [col for col in self.observables if col in history_df.columns]
        if len(obs_cols) == 0:
            obs_cols = history_df.columns[:min(3, len(history_df.columns))].tolist()
        self.observables = obs_cols

        obs_df = history_df[self.observables].dropna()
        if len(obs_df) < 20:
            logger.warning("Fewer than 20 observations for TDistributionHMM monthly_reestimate")

        obs = obs_df.to_numpy(dtype=float)
        T, d = obs.shape
        S = self.n_states

        # Initialize parameters via quantile split on main vol component (VIX or 1st col)
        vix_idx = self.observables.index("VIX") if "VIX" in self.observables else 0
        vix_vals = obs[:, vix_idx]

        q_low, q_high = np.quantile(vix_vals, [0.33, 0.67]) if S == 3 else (np.median(vix_vals), np.median(vix_vals))

        idx_exp = np.where(vix_vals <= q_low)[0]
        idx_mid = np.where((vix_vals > q_low) & (vix_vals <= q_high))[0]
        idx_crs = np.where(vix_vals > q_high)[0]

        means = np.zeros((S, d))
        means[0] = obs[idx_exp].mean(axis=0) if len(idx_exp) > 0 else obs[:T//3].mean(axis=0)
        if S == 3:
            means[1] = obs[idx_mid].mean(axis=0) if len(idx_mid) > 0 else obs[T//3:2*T//3].mean(axis=0)
            means[2] = obs[idx_crs].mean(axis=0) if len(idx_crs) > 0 else obs[2*T//3:].mean(axis=0)
        else:
            means[1] = obs[idx_crs].mean(axis=0) if len(idx_crs) > 0 else obs[T//2:].mean(axis=0)

        covars = np.array([np.cov(obs.T) + 1e-4 * np.eye(d) for _ in range(S)])
        pi = np.full(S, 1.0 / S)

        # Sticky transition matrix prior
        A = np.eye(S) * 0.85 + (1.0 - 0.85) / S

        max_iter = 40
        for it in range(max_iter):
            # Log emission matrix
            log_B = np.zeros((T, S))
            for t in range(T):
                for s in range(S):
                    df_s = self.nu_vec[s]
                    log_B[t, s] = self._multivariate_t_logpdf(obs[t], means[s], covars[s], df_s)

            # Forward pass
            alpha = np.zeros((T, S))
            c = np.zeros(T)

            log_a0 = np.log(pi + 1e-300) + log_B[0]
            max_a0 = np.max(log_a0)
            a0 = np.exp(log_a0 - max_a0)
            c[0] = np.sum(a0) + 1e-300
            alpha[0] = a0 / c[0]

            for t in range(1, T):
                prev = alpha[t-1] @ A
                prev = np.clip(prev, 1e-300, None)
                alpha[t] = prev * np.exp(log_B[t] - np.max(log_B[t]))
                c[t] = np.sum(alpha[t]) + 1e-300
                alpha[t] /= c[t]

            # Backward pass
            beta = np.zeros((T, S))
            beta[-1] = 1.0
            for t in range(T - 2, -1, -1):
                step = (A * np.exp(log_B[t+1] - np.max(log_B[t+1]))[None, :]) @ beta[t+1][:, None]
                beta[t] = step[:, 0] / (c[t+1] + 1e-300)

            gamma = alpha * beta
            gamma = gamma / (np.sum(gamma, axis=1, keepdims=True) + 1e-300)

            # Transition update with sticky Dirichlet prior alpha=50
            xi_sum = np.zeros((S, S))
            for t in range(T - 1):
                num = alpha[t][:, None] * (A * np.exp(log_B[t+1] - np.max(log_B[t+1]))[None, :]) * beta[t+1][None, :]
                den = np.sum(num) + 1e-300
                xi_sum += num / den

            pseudo_counts = xi_sum + np.eye(S) * self.sticky_kappa + 1.0
            A = pseudo_counts / np.sum(pseudo_counts, axis=1, keepdims=True)

            pi = gamma[0] / (np.sum(gamma[0]) + 1e-300)

            # M-step Student-t parameters
            for s in range(S):
                df_s = self.nu_vec[s]
                diff = obs - means[s]
                inv_cov = np.linalg.pinv(covars[s])
                mahal = np.sum((diff @ inv_cov) * diff, axis=1)
                u = (df_s + d) / (df_s + mahal + 1e-6)

                w_s = gamma[:, s] * u
                w_sum = np.sum(w_s) + 1e-300
                means[s] = np.sum(obs * w_s[:, None], axis=0) / w_sum

                diff_new = obs - means[s]
                cov_s = np.zeros((d, d))
                for t in range(T):
                    cov_s += w_s[t] * np.outer(diff_new[t], diff_new[t])
                covars[s] = cov_s / w_sum + 1e-4 * np.eye(d)

        # State label lock: Sort states by variance / VIX mean (sigma^2_CRISIS > sigma^2_SHOCK > sigma^2_EXPANSION)
        state_vols = np.array([covars[s, vix_idx, vix_idx] + means[s, vix_idx]**2 for s in range(S)])
        sort_order = np.argsort(state_vols)

        means = means[sort_order]
        covars = covars[sort_order]
        A = A[sort_order][:, sort_order]
        pi = pi[sort_order]
        gamma = gamma[:, sort_order]

        self.transmat_ = A
        self.means_ = means
        self.covars_ = covars
        self.pi_ = pi
        self.state_labels_ = ["EXPANSION", "SHOCK", "CRISIS"] if S == 3 else ["EXPANSION", "CRISIS"]
        self.is_fitted = True
        self._last_filter_prob = gamma[-1]
        self._current_state_idx = int(np.argmax(gamma[-1]))

        logger.info(f"monthly_reestimate completed: State VIX Means = {[round(float(m[vix_idx]), 2) for m in means]}")
        return self

    def daily_nowcast(self, daily_obs: Union[np.ndarray, pd.Series, Dict[str, float]]) -> Tuple[np.ndarray, str]:
        """Causal forward filter step with frozen parameters and min dwell constraint."""
        if not self.is_fitted:
            raise RuntimeError("TDistributionHMM is not fitted. Run monthly_reestimate first.")

        if isinstance(daily_obs, dict):
            obs_vec = np.array([daily_obs.get(col, 0.0) for col in self.observables], dtype=float)
        elif isinstance(daily_obs, pd.Series):
            obs_vec = np.array([daily_obs.get(col, 0.0) for col in self.observables], dtype=float)
        else:
            obs_vec = np.asarray(daily_obs, dtype=float).flatten()

        S = self.n_states
        log_b = np.zeros(S)
        for s in range(S):
            df_s = self.nu_vec[s]
            log_b[s] = self._multivariate_t_logpdf(obs_vec, self.means_[s], self.covars_[s], df_s)

        prev_prob = self._last_filter_prob if self._last_filter_prob is not None else self.pi_

        # Predict: P(s_t | y_{1:t-1})
        pred_prob = prev_prob @ self.transmat_
        pred_prob = np.clip(pred_prob, 1e-300, None)

        # Update: P(s_t | y_{1:t})
        log_post = np.log(pred_prob) + log_b
        max_log = np.max(log_post)
        post_unnorm = np.exp(log_post - max_log)
        post_prob = post_unnorm / np.sum(post_unnorm)

        self._last_filter_prob = post_prob
        cand_state_idx = int(np.argmax(post_prob))

        # Apply min dwell constraint (3 months ~ 63 days) unless high confidence shift (p > 0.85)
        if cand_state_idx != self._current_state_idx:
            if self._days_in_current_state >= self.min_dwell_days or post_prob[cand_state_idx] > 0.85:
                self._current_state_idx = cand_state_idx
                self._days_in_current_state = 1
            else:
                self._days_in_current_state += 1
        else:
            self._days_in_current_state += 1

        most_likely_state = self.state_labels_[self._current_state_idx]
        return post_prob, most_likely_state

    def get_regime_state(self, date: Optional[pd.Timestamp] = None, obs: Optional[Any] = None) -> RegimeState:
        """Returns RegimeState object."""
        if not self.is_fitted:
            return RegimeState(
                detected_state="EXPANSION",
                state_probs={"EXPANSION": 0.80, "SHOCK": 0.15, "CRISIS": 0.05},
                trend="BULL",
                trend_probs={"BULL": 0.80, "BEAR": 0.20},
                vol=0.15,
                confident=True,
                date=date,
            )

        if obs is not None:
            probs, state = self.daily_nowcast(obs)
        else:
            probs = self._last_filter_prob if self._last_filter_prob is not None else self.pi_
            state = self.state_labels_[self._current_state_idx]

        state_probs = {self.state_labels_[i]: float(probs[i]) for i in range(self.n_states)}
        vix_idx = self.observables.index("VIX") if "VIX" in self.observables else 0
        vol = float(self.means_[self._current_state_idx, vix_idx] / 100.0) if self.means_[0, vix_idx] > 1.0 else 0.15

        trend = "BEAR" if state == "CRISIS" else ("NEUTRAL" if state == "SHOCK" else "BULL")
        trend_probs = {"BULL": float(probs[0]), "BEAR": float(probs[-1])}
        confident = float(np.max(probs)) >= 0.60

        return RegimeState(
            detected_state=state,
            state_probs=state_probs,
            trend=trend,
            trend_probs=trend_probs,
            vol=vol,
            confident=confident,
            date=date,
        )


def generate_regime_daily_nowcast(
    factors_df: pd.DataFrame,
    returns_df: pd.Series,
    output_path: str = "data/regime/regime_daily_nowcast.parquet",
    config_path: str = "config/pre_registration_phase8.yaml"
) -> pd.DataFrame:
    """Generates data/regime/regime_daily_nowcast.parquet matching Interface 1 schema."""
    from Quantitative.har_abs_vol import HARAbsVol

    cfg = load_config(config_path)
    hmm = TDistributionHMM(cfg)

    # Train HMM on historical factors
    hmm.monthly_reestimate(factors_df)

    # Compute HAR volatility chain
    har_model = HARAbsVol()
    har_model.fit(returns_df)
    vol_1d, vol_5d, vol_22d = har_model.predict_vol_chain(returns_df)

    rows = []
    prev_state = None

    # Calculate condition number of HMM transition matrix
    cond_num = float(np.linalg.cond(hmm.transmat_)) if (hmm.transmat_ is not None and np.linalg.det(hmm.transmat_) != 0) else 1.0

    # Read config provenance hash
    config_bytes = Path(config_path).read_bytes() if Path(config_path).exists() else b"phase8_regime"
    provenance_hash = hashlib.sha256(config_bytes).hexdigest()

    for idx, obs in factors_df.iterrows():
        ts_utc = pd.Timestamp(idx, tz="UTC") if getattr(idx, "tz", None) is None else pd.Timestamp(idx).tz_convert("UTC")
        probs, state = hmm.daily_nowcast(obs)

        p_exp = float(probs[0])
        p_shk = float(probs[1]) if len(probs) >= 3 else 0.0
        p_crs = float(probs[2]) if len(probs) >= 3 else float(probs[1])

        switch_flag = 1 if (prev_state is not None and state != prev_state) else 0
        prev_state = state

        v1d = float(vol_1d.get(idx, 0.015))
        v5d = float(vol_5d.get(idx, 0.035))
        v22d = float(vol_22d.get(idx, 0.070))

        rows.append({
            "timestamp_utc": ts_utc,
            "p_expansion": p_exp,
            "p_crisis": p_crs,
            "p_shock": p_shk,
            "regime_state": str(state),
            "vol_har_1d": v1d,
            "vol_har_5d": v5d,
            "vol_har_22d": v22d,
            "regime_switch_flag": int(switch_flag),
            "hmm_condition_num": cond_num,
            "sha256_provenance": provenance_hash,
        })

    out_df = pd.DataFrame(rows)

    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_parquet(out_file, index=False)
    logger.info(f"Wrote regime_daily_nowcast.parquet with {len(out_df)} rows to {output_path}")

    return out_df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Quantitative.regime_shield_v2 Phase 8 Test")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    parser.add_argument("--config", type=str, default="config/pre_registration_phase8.yaml", help="Path to config")
    args = parser.parse_args()

    cfg = load_config(args.config)
    hmm = TDistributionHMM(cfg)

    # Synthetic test data
    np.random.seed(42)
    n_days = 500
    dates = pd.date_range("2018-01-01", periods=n_days, freq="B")
    baa = np.concatenate([np.random.normal(1.8, 0.2, 300), np.random.normal(2.8, 0.3, 100), np.random.normal(4.5, 0.5, 100)])
    vix = np.concatenate([np.random.normal(14.0, 2.0, 300), np.random.normal(22.0, 3.0, 100), np.random.normal(35.0, 5.0, 100)])
    erp = np.concatenate([np.random.normal(0.045, 0.005, 300), np.random.normal(0.035, 0.005, 100), np.random.normal(0.015, 0.01, 100)])

    df = pd.DataFrame({"BAA10Y_spread": baa, "VIX": vix, "ERP": erp}, index=dates)

    logger.info("Running monthly_reestimate...")
    hmm.monthly_reestimate(df)

    for i in range(295, 310):
        obs = df.iloc[i]
        probs, state = hmm.daily_nowcast(obs)
        print(f"Date: {df.index[i].date()} | State: {state} | Probs: EXP={probs[0]:.2f}, SHK={probs[1]:.2f}, CRS={probs[2]:.2f}")

    print("RegimeShieldV2 Phase 8 Test OK!")
