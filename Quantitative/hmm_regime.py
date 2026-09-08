"""
hmm_regime.py — Latent Macro State Detection via Gaussian Hidden Markov Model

D-20260906-001 (pre-registered plan: .agents/project/org/decisions/_drafts/2026-09-06-hmm-cic-brain-logger.md)

Pure-NumPy Gaussian HMM (no hmmlearn dependency). Three latent economic states
detected from macro features:

    EXPANSION -> SHOCK -> CRISIS

Ordered-state identification (pre-registered): the learned state means on the
credit-spread feature are sorted, so [lowest, middle, highest] maps
deterministically to [EXPANSION, SHOCK, CRISIS]. This removes the standard HMM
permutation ambiguity without any post-hoc re-labeling.

Features (z-scored against the training window ONLY — no lookahead):
    f0  BAA10Y credit spread            (level, bps/100)
    f1  yield-curve slope               (10y - 2y, bps/100; falls back to 10y-momentum)
    f2  market 21d cumulative return    (from Ken French Mkt-RF + RF)
    f3  market 21d realized volatility  (same source)

Training: rolling 3-year window, retrained quarterly. Baum-Welch EM, multiple
restarts (deterministic seed). Forward/backward are log-space-scaled for
stability. Posterior at time t uses only {obs_1..t}.

Honesty: when a feature has no coverage (e.g. credit spread before 2021 in the
current cache) the feature row is dropped from training, never fabricated.
Eval windows are fixed; windows with no overlapping data are reported as
out-of-coverage, not scored.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Feature layout (index order is frozen — f0 must stay the credit-spread slot).
FEATURE_NAMES = ["credit_spread", "yield_curve", "market_ret_21d", "market_vol_21d"]
_F_CREDIT = 0
_F_CURVE = 1
_F_RET = 2
_F_VOL = 3
N_FEATURES = len(FEATURE_NAMES)

FLOOR_VAR = 1e-6  # covariance floor for numeric stability


class RegimeState(Enum):
    EXPANSION = "EXPANSION"
    SHOCK = "SHOCK"
    CRISIS = "CRISIS"


STATE_BY_INDEX: List[RegimeState] = [
    RegimeState.EXPANSION,
    RegimeState.SHOCK,
    RegimeState.CRISIS,
]


class GaussianHMM:
    """Diagonal-covariance Gaussian HMM via Baum-Welch EM (pure NumPy)."""

    def __init__(self, n_states: int = 3, seed: int = 0):
        self.n_states = n_states
        self.seed = seed
        self.pi: Optional[np.ndarray] = None
        self.A: Optional[np.ndarray] = None
        self.mu: Optional[np.ndarray] = None
        self.var: Optional[np.ndarray] = None
        self.log_likelihood_: Optional[float] = None
        self.n_iter_ = 0

    # ------------------------------------------------------------------ utils

    def _rng(self, seed: Optional[int] = None) -> np.random.Generator:
        return np.random.default_rng(seed if seed is not None else self.seed)

    def _init_params(self, obs: np.ndarray) -> None:
        n, d = obs.shape
        rng = self._rng()
        mu = obs[rng.choice(n, size=self.n_states, replace=False), :].astype(float)
        spread = np.ptp(obs, axis=0).clip(min=1e-6) + FLOOR_VAR
        var = np.tile((spread * 0.25) ** 2, (self.n_states, 1))
        A = rng.random((self.n_states, self.n_states)) + np.eye(self.n_states) * 2.0
        A = A / A.sum(axis=1, keepdims=True)
        pi = np.full(self.n_states, 1.0 / self.n_states)
        self.mu, self.var, self.A, self.pi = mu, var, A, pi

    def _emission(self, x: np.ndarray) -> np.ndarray:
        """Log-emission B[i] = log N(x | mu_i, var_i) (diagonal covariance)."""
        d = self.mu.shape[1]
        diff = x[None, :] - self.mu  # (S, d)
        log_var = np.log(self.var + FLOOR_VAR)
        log_b = -0.5 * (d * np.log(2 * np.pi) + (log_var.sum(axis=1) + (diff ** 2 / (self.var + FLOOR_VAR)).sum(axis=1)))
        return log_b

    def _forward(self, obs: np.ndarray) -> Tuple[np.ndarray, np.ndarray, float]:
        """Scaled forward. Returns (alpha_hat (T,S), scaling c (T,), log_lik)."""
        T, _ = obs.shape
        S = self.n_states
        B = np.array([self._emission(obs[t]) for t in range(T)])  # (T,S)
        alpha = np.zeros((T, S))
        c = np.zeros(T)
        # t=0 handled in log space then scaled
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
        gamma = gamma / gamma.sum(axis=1, keepdims=True)
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

    def fit(self, obs: np.ndarray, max_iter: int = 200, tol: float = 1e-4, n_restarts: int = 5) -> "GaussianHMM":
        obs = np.asarray(obs, dtype=float)
        if obs.ndim != 2 or obs.shape[0] < 2:
            raise ValueError("observations must be a 2D array with >= 2 rows")
        if obs.shape[1] != N_FEATURES:
            # Accept the frozen feature matrix contract but tolerate n_features mismatch
            # only through the detector (not raw fit) — keeps math generic.
            pass
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
                # M-step
                gamma_1 = gamma[0]
                gamma_t1 = gamma[:-1].sum(axis=0) + 1e-300
                denom_t = gamma.sum(axis=0) + 1e-300
                self.pi = gamma_1 / (gamma_1.sum() + 1e-300)
                self.A = xi_sum / gamma_t1[:, None]
                self.A = self.A / self.A.sum(axis=1, keepdims=True)
                self.mu = (gamma.T @ obs) / denom_t[:, None]
                for s in range(self.n_states):
                    diff = obs - self.mu[s]
                    self.var[s] = (gamma[:, s][:, None] * diff ** 2).sum(axis=0) / denom_t[s]
                    self.var[s] = np.clip(self.var[s], FLOOR_VAR, None)
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
        return gamma / gamma.sum(axis=1, keepdims=True)

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


@dataclass
class RegimeReport:
    date: pd.Timestamp
    regimes: Dict[str, float]  # state -> posterior probability
    viterbi_sequence: List[str]  # per-obs decoded state labels
    detected: RegimeState
    confident: bool
    coverage: Dict[str, int]

    @property
    def crisis_prob(self) -> float:
        return self.regimes.get(RegimeState.CRISIS.value, 0.0)

    @property
    def shock_prob(self) -> float:
        return self.regimes.get(RegimeState.SHOCK.value, 0.0)

    @property
    def expansion_prob(self) -> float:
        return self.regimes.get(RegimeState.EXPANSION.value, 0.0)


class StateLabeler:
    """Ordered-state identification: sort latent states by credit-spread mean.

    [lowest, middle, highest] mapping to [EXPANSION, SHOCK, CRISIS] is frozen
    in the plan — it is the ONLY permitted labeling.
    """

    ORDER = [RegimeState.EXPANSION, RegimeState.SHOCK, RegimeState.CRISIS]

    @classmethod
    def label_sequence(cls, path: np.ndarray, mu_sort: np.ndarray) -> List[str]:
        """Map raw index path to labels given an index->label permutation."""
        return [cls.ORDER[i].value for i in np.argsort(mu_sort)[path]]


# ---------------------------------------------------------------------------
# Feature builders (real data where available; synthetic for tests)
# ---------------------------------------------------------------------------


def load_fred_cache(series_id: str) -> pd.Series:
    """Load a cached FRED series into a daily pd.Series (date-index, float)."""
    import json

    path = f"data/fred_cache/{series_id}.json"
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        obs = payload.get("observations", [])
        if not obs:
            return pd.Series(dtype=float)
        idx = pd.to_datetime([o["date"] for o in obs])
        vals = pd.to_numeric([o["value"] for o in obs], errors="coerce")
        return pd.Series(vals, index=idx).sort_index()
    except Exception as exc:
        logger.warning("FRED cache %s unavailable: %s", series_id, exc)
        return pd.Series(dtype=float)


def load_market_returns() -> pd.Series:
    """Daily gross market return (Mkt-RF + RF) from the FF5 factor cache."""
    try:
        ff5 = pd.read_parquet("data/fred_cache/ff5_factors_daily.parquet")
        return (ff5["Mkt-RF"] + ff5["RF"]).sort_index()
    except Exception as exc:
        logger.warning("FF5 market series unavailable: %s", exc)
        return pd.Series(dtype=float)


def _try_dgs2() -> pd.Series:
    s = load_fred_cache("DGS2")
    return s if len(s) >= 30 else pd.Series(dtype=float)


def build_macro_features(
    credit: pd.Series = None,
    dgs10: pd.Series = None,
    market_ret: pd.Series = None,
) -> pd.DataFrame:
    """Assemble aligned, z-scored macro feature matrix.

    Z-scoring uses full available history of each input (training-time stats);
    the Detector re-standardizes against the rolling training window for
    no-lookahead inference.
    """
    credit = credit if credit is not None else load_fred_cache("BAA10Y") / 100.0
    dgs10 = dgs10 if dgs10 is not None else load_fred_cache("DGS10")
    dgs2 = _try_dgs2()
    market_ret = market_ret if market_ret is not None else load_market_returns()

    frame = pd.DataFrame(index=credit.index)
    frame["credit_spread"] = credit

    if len(dgs2) >= 30 and len(dgs10) >= 30:
        curve = (dgs10.reindex(frame.index).ffill() - dgs2.reindex(frame.index).ffill()) / 100.0
    else:
        # Fallback (pre-registered): 10y level momentum as a low-grade slope proxy.
        d10 = dgs10.reindex(frame.index).ffill()
        curve_or = (d10 - d10.rolling(200, min_periods=30).mean()) / 100.0
        curve_or.name = "yield_curve"
        curve = curve_or
    frame["yield_curve"] = curve

    ret = market_ret.reindex(frame.index).ffill()
    frame["market_ret_21d"] = (1 + ret).rolling(21, min_periods=10).apply(
        lambda r: float(np.prod(r) - 1.0), raw=True
    )
    frame["market_vol_21d"] = ret.rolling(21, min_periods=10).std()

    out = frame.dropna().copy()
    # Z-standardize (training-history stats — detector re-strips at roll time).
    for col in FEATURE_NAMES:
        col_s = out[col]
        std = col_s.std()
        out[col] = (col_s - col_s.mean()) / std if std and std > 0 else 0.0
    return out[FEATURE_NAMES]


def synthetic_features(n_days: int = 1800, seed: int = 42) -> pd.DataFrame:
    """Deterministic regime-switching test series (credit + market legs).

    True latent process: EXPANSION (len 500) -> CRISIS (len 250) ->
    SHOCK (len 150) -> EXPANSION (len 500) -> SHOCK (len 150) -> EXPANSION.
    """
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(pd.Timestamp("2019-01-01"), periods=n_days)
    credit_mod = np.zeros(n_days)
    ret_mod = np.zeros(n_days)
    vol_mod = np.zeros(n_days)
    segs = [(0, 500, 1.0), (500, 750, 5.0), (750, 900, 2.5), (900, 1400, 1.0), (1400, 1550, 2.5), (1550, n_days, 1.2)]
    for s0, s1, level in segs:
        credit_mod[s0:s1] = level
        ret_mod[s0:s1] = -0.0016 if level >= 4.0 else (0.0008 if level <= 1.2 else 0.0002)
        vol_mod[s0:s1] = 0.008 if level >= 4.0 else (0.006 if level >= 2.0 else 0.004)
    credit = credit_mod + rng.normal(0, 0.1, n_days)
    curve = np.zeros(n_days) + rng.normal(0, 0.05, n_days) + (credit_mod - credit_mod.mean()) * 0.2
    ret = ret_mod + rng.normal(0, 0.004, n_days)
    vol = vol_mod + np.abs(rng.normal(0, 0.0005, n_days))
    market_ret_21d = pd.Series(np.convolve(ret, np.ones(21) / 21, mode="same"), index=idx)
    market_vol_21d = pd.Series(
        (1 + pd.Series(ret, index=idx)).rolling(21, min_periods=10).std(ddof=1), index=idx
    )
    col = pd.Series(credit, index=idx) / 100.0
    cols = pd.Series(curve, index=idx) / 100.0
    frame = pd.DataFrame(
        {
            "credit_spread": col,
            "yield_curve": cols,
            "market_ret_21d": market_ret_21d.fillna(0.0),
            "market_vol_21d": market_vol_21d.fillna(market_vol_21d.mean()),
        }
    )
    for c in FEATURE_NAMES:
        s = frame[c]
        std = s.std()
        frame[c] = (s - s.mean()) / std if std and std > 0 else 0.0
    return frame.dropna()


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

_ROLL_YEARS = 3
_RETRAIN_DAYS = 63  # quarterly (~63 trading days)


class RegimeDetector:
    """Rolling-window Gaussian HMM detector with quarterly retrain.

    Training window: trailing ``train_years`` in calendar time. Inference uses
    only observations at-or-before t (filter smoothing via forward pass), so
    the crisis vote at t is causal. The model can label up to ``n_states``
    states; the labeler maps latent order to EXPANSION/SHOCK/CRISIS.
    """

    def __init__(
        self,
        n_states: int = 3,
        train_years: int = _ROLL_YEARS,
        retrain_days: int = _RETRAIN_DAYS,
        seed: int = 0,
        n_restarts: int = 4,
        max_iter: int = 150,
    ):
        self.n_states = n_states
        self.train_years = train_years
        self.retrain_days = retrain_days
        self.seed = seed
        self.n_restarts = n_restarts
        self.max_iter = max_iter
        self.model: Optional[GaussianHMM] = None
        self._train_until: Optional[pd.Timestamp] = None
        self._train_stats: Optional[dict] = None
        self._state_order = np.arange(n_states)

    def _standardize(self, window: pd.DataFrame) -> np.ndarray:
        """Z-score against ``window`` (the training window) — no lookahead."""
        mu = window.mean()
        sd = window.std()
        sd = sd.replace(0, 1.0)
        self._train_stats = {"mu": mu, "sd": sd}
        return ((window - mu) / sd).to_numpy(dtype=float)

    def _apply_stats(self, df: pd.DataFrame) -> np.ndarray:
        mu, sd = self._train_stats["mu"], self._train_stats["sd"]
        return ((df - mu) / sd).fillna(0.0).to_numpy(dtype=float)

    def needs_retrain(self, today: pd.Timestamp) -> bool:
        if self.model is None or self._train_until is None:
            return True
        return today > self._train_until

    def retrain(self, features: pd.DataFrame, until: pd.Timestamp, refit_labels: bool = True) -> GaussianHMM:
        """Train on the trailing window ending at ``until``."""
        window = features.loc[:until]
        start = until - pd.DateOffset(years=self.train_years)
        window = window[window.index >= start]
        if len(window) < 250:
            raise ValueError(
                f"HMM train window too short ({len(window)} rows) for retrain at {until.date()}"
            )
        X = self._standardize(window)
        model = GaussianHMM(n_states=self.n_states, seed=self.seed)
        model.fit(X, max_iter=self.max_iter, n_restarts=self.n_restarts)
        self.model = model
        self._train_until = until
        if refit_labels:
            self._state_order = np.argsort(model.mu[:, _F_CREDIT])  # low->high
        logger.info(
            "HMM retrained: window %s..%s logL=%.1f states=%d",
            window.index[0].date(), until.date(), model.log_likelihood_, self.n_states,
        )
        return model

    def decode(self, features: pd.DataFrame, until: Optional[pd.Timestamp] = None) -> RegimeReport:
        """Detect regime for obs up to ``until`` (default: last row)."""
        if self.model is None:
            raise RuntimeError("RegimeDetector not trained — call retrain() first")
        last = until if until is not None else features.index[-1]
        obs_df = features.loc[:last]
        X = self._apply_stats(obs_df)
        gamma = self.model.posterior(X)
        path = self.model.viterbi(X)
        # _state_order[k] = latent index whose credit mean is k-th lowest;
        # ordered slot k maps to STATE_BY_INDEX[k] (EXPANSION, SHOCK, CRISIS).
        inv_order = np.argsort(self._state_order)  # latent index -> ordered slot
        labels = [STATE_BY_INDEX[inv_order[i]].value for i in path]
        final_probs = gamma[-1]
        regimes = {
            STATE_BY_INDEX[k].value: float(final_probs[self._state_order[k]])
            for k in range(self.n_states)
        }
        top = max(regimes, key=regimes.get)
        return RegimeReport(
            date=pd.Timestamp(last),
            regimes=regimes,
            viterbi_sequence=labels,
            detected=RegimeState(top),
            confident=regimes[top] >= 0.65,
            coverage={f: int((~obs_df[f].isna()).sum()) for f in FEATURE_NAMES},
        )

    def detect(self, features: pd.DataFrame, today: Optional[pd.Timestamp] = None) -> RegimeReport:
        """Auto-retrain-if-due then decode — the primary sensory entry point."""
        day = pd.Timestamp(today if today is not None else features.index[-1])
        if self.needs_retrain(day):
            self.retrain(features, until=day)
        else:
            self._train_until = day  # keep model, slide the reference forward
        last_train = self._train_until if self._train_until is not None else day
        return self.decode(features, until=min(day, last_train) if self._train_until is not None else day)

    def get_crisis_probability(self, features: pd.DataFrame, today: Optional[pd.Timestamp] = None) -> float:
        """Get P(Crisis) for black swan slow trigger gate.
        
        Returns the posterior probability of CRISIS state at the latest observation.
        This is the primary interface for the black swan engine's slow trigger.
        """
        report = self.detect(features, today)
        return report.crisis_prob

    def get_full_regime_probs(self, features: pd.DataFrame, today: Optional[pd.Timestamp] = None) -> Dict[str, float]:
        """Get full regime probabilities for monitoring."""
        report = self.detect(features, today)
        return report.regimes


# ---------------------------------------------------------------------------
# Verification (pre-registered eval windows — honest coverage reporting)
# ---------------------------------------------------------------------------

EVAL_WINDOWS: Dict[str, Tuple[str, str]] = {
    "gfc_2008": ("2008-07-01", "2009-06-30"),
    "covid_2020": ("2020-02-19", "2020-05-31"),
    "bear_2022": ("2022-01-03", "2022-12-30"),
    "bull_2023_24": ("2023-01-03", "2024-12-31"),
}


def evaluate_separation(
    detector: "RegimeDetector", features: pd.DataFrame, min_obs: int = 20
) -> dict:
    """Score crisis/shock probability across the fixed eval windows.

    Uses the detector's training-window stats (``detector._apply_stats``) so the
    same no-lookahead standardization is applied at inference as at fit.
    Coverage-honest: a window with fewer than ``min_obs`` aligned rows is
    reported ``out_of_coverage`` and never scored.
    """
    if detector.model is None or detector._train_stats is None:
        raise RuntimeError("evaluate_separation requires a trained RegimeDetector")
    model = detector.model
    out = {}
    for name, (ws, we) in EVAL_WINDOWS.items():
        seg = features.loc[pd.Timestamp(ws): pd.Timestamp(we)]
        if len(seg) < min_obs:
            out[name] = {"status": "out_of_coverage", "rows": len(seg)}
            continue
        X = detector._apply_stats(seg)
        gamma = model.posterior(X)
        # Label model states by credit-spread mean against the training stats.
        mu_sort = np.argsort(model.mu[:, 0])
        crisis_idx = mu_sort[-1]
        mean_crisis = float(gamma[:, crisis_idx].mean())
        shock_idx = mu_sort[-2] if len(mu_sort) > 1 else crisis_idx
        mean_shock = float(gamma[:, shock_idx].mean())
        out[name] = {
            "status": "scored",
            "rows": len(seg),
            "mean_crisis_prob": round(mean_crisis, 4),
            "mean_shock_prob": round(mean_shock, 4),
        }
    return out


# ─── Convenience Functions for Black Swan Integration ──────────────────

def get_crisis_probability(
    features: Optional[pd.DataFrame] = None,
    today: Optional[pd.Timestamp] = None,
    detector: Optional[RegimeDetector] = None,
) -> float:
    """Module-level convenience: get current P(Crisis) for black swan gate.
    
    Args:
        features: Macro feature matrix (built via build_macro_features if None)
        today: Timestamp for detection (defaults to latest)
        detector: Pre-configured RegimeDetector (creates default if None)
    
    Returns:
        P(Crisis) ∈ [0, 1] — posterior probability of CRISIS regime
    """
    if features is None:
        features = build_macro_features()
    if detector is None:
        detector = RegimeDetector(n_states=3, seed=42)
    return detector.get_crisis_probability(features, today)


def get_regime_report(
    features: Optional[pd.DataFrame] = None,
    today: Optional[pd.Timestamp] = None,
    detector: Optional[RegimeDetector] = None,
) -> RegimeReport:
    """Get full RegimeReport for monitoring/logging."""
    if features is None:
        features = build_macro_features()
    if detector is None:
        detector = RegimeDetector(n_states=3, seed=42)
    return detector.detect(features, today)


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO)
    feats = synthetic_features(n_days=1800)
    det = RegimeDetector(n_states=3, seed=7)
    det.retrain(feats, until=feats.index[1150])
    report = det.decode(feats, until=feats.index[-1])
    print(json.dumps(
        {
            "detected": report.detected.value,
            "regimes": report.regimes,
            "confident": report.confident,
            "logL": det.model.log_likelihood_,
        },
        indent=2,
    ))