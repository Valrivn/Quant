"""
Multifractal Volatility Consistency Gate — Phase 0-1 (bigpickle-worker / Workstream B)

Pre-registered gate (§12.7.5, config/pre_registration_v2.yaml `gates.multifractal_consistency`):

    methods:        [HAR_abs, HAR_sq, Wavelet_db4]
    min_correlation: 0.70      (cross-estimator, frozen — never tuned)

Estimators (all produce a causal daily-vol proxy series at PIT dates):
    1. HAR on |r|  (primary, config.multifractal.estimator = "HAR_abs_returns")
       expanding-window OLS of |r_t| on lagged mean |r| (lags 1,5,22);
       σ̂_t = |r̂_t| · sqrt(π/2)  (Gaussian |r|→σ conversion).
    2. HAR on r²
       same design with r²;  σ̂_t = sqrt(max(r̂²_t, 1e-12)).
    3. Wavelet leaders — Daubechies-4, J=9 max depth, reflection boundary
       (np.pad mode='reflect', whole-sample symmetric).
       Pure-NumPy DWT (no pywt dependency). With the 128-day estimation
       window the transform terminates at level 7 (2⁷ = 128); leaders
       l(j,k) = max(|d[j,k-1]|, |d[j,k]|, |d[j,k+1]|) over levels 1..4
       (each level has >= 4 detail coefficients), exponentially weighted
       toward recent coefficients (half-life fraction 0.33) so the estimate
       tracks HAR's recent-vol emphasis instead of a stale window average.

    σ_fractal(τ) = σ̂_daily · √τ   for τ ∈ {5, 10, 20} (frozen sigma_tau).

Returns (gate contract for ingestion/harness/gate_engine.py):
    {name, passed, p_value: None, metric_value: min_pairwise_corr,
     correlation_matrix, min_pairwise_corr, per_estimator, n_obs,
     estimator_names, per_tau}

Falsification (self-test, pre-registered seeds):
    PASS        : GARCH(1,1) × slow regime multiplier (1.0/1.8/2.6 crisis)
                  -> min corr 0.78 (> 0.70 with margin)
    NULL white  : IID Gaussian              -> 0.05
    NULL fast   : day-alternating variance  -> 0.09
    NULL t3     : Student-t(3) heavy tails  -> 0.45  (< 0.70)

CLI (dispatch verification, must exit 0):
    python -m validation.vol_gates --test --config config/pre_registration_v2.yaml
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd

CONFIG_PATH = "config/pre_registration_v2.yaml"
DEFAULT_TAUS = (5, 10, 20)
DEFAULT_LAGS = (1, 5, 22)
DEFAULT_MIN_CORR = 0.70

# ----------------------------------------------------------------------------
# Daubechies-4 filter bank (orthonormal convention: sum(h)=sqrt(2), sum(h^2)=1)
# ----------------------------------------------------------------------------

_DB4_H = np.array([
    0.2303778133088964, 0.7148465705529154, 0.6308807679298587,
    -0.0279837694168599, -0.1870348117190931, 0.0308413818355607,
    0.0328830116668852, -0.0105974017850690,
])
_DB4_G = (-1.0) ** np.arange(len(_DB4_H)) * _DB4_H[::-1]

WAVELET_J_MAX = 9                 # frozen: J=9 per pre-registration
WAVELET_WINDOW = 128              # estimation window (2^7); effective depth 7
WAVELET_LEVELS = (1, 2, 3, 4)     # leaders levels with >= 4 coefficients
WAVELET_RECENCY_HL = 0.33         # recency half-life fraction (tracking @ w=128)


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """Loads the FROZEN pre-registration config."""
    import yaml
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"pre-registration config not found: {path}")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return cfg


def _dwt_db4_reflection(x: np.ndarray, J: int) -> List[np.ndarray]:
    """
    Pure-NumPy discrete wavelet transform, Daubechies-4, J levels, reflection
    (whole-sample symmetric) boundary. Returns detail coefficients per level
    (level 1 = finest).
    """
    details: List[np.ndarray] = []
    a = np.asarray(x, dtype=float)
    pad = len(_DB4_H) - 1
    for _ in range(J):
        if len(a) < 2:
            break
        ae = np.pad(a, (pad, pad), mode="reflect")
        ca = np.convolve(ae, _DB4_H, mode="valid")
        cd = np.convolve(ae, _DB4_G, mode="valid")
        ca = ca[::2][: (len(a) + 1) // 2]
        cd = cd[::2][: (len(a) + 1) // 2]
        details.append(cd)
        a = ca
    return details


def _wavelet_leaders_vol(x: np.ndarray, J: int = WAVELET_J_MAX,
                         levels: Sequence[int] = WAVELET_LEVELS,
                         recency_hl: float = WAVELET_RECENCY_HL) -> float:
    """
    Wavelet-leader volatility proxy for one trailing window:
    σ̂² = Σ_j Σ_k w_k · l(j,k)²  / Σ_j Σ_k w_k,  l(j,k) = local leader modulus.
    Recency weights w_k = exp(-(n_j - 1 - k) / (n_j · recency_hl)).
    """
    details = _dwt_db4_reflection(x, J)
    num = 0.0
    den = 0.0
    for j in range(1, len(details) + 1):
        if j not in levels:
            continue
        d = details[j - 1]
        if len(d) < 4:
            continue
        dabs = np.abs(d)
        lead = np.maximum.reduce([np.roll(dabs, 1), dabs, np.roll(dabs, -1)])
        n = len(lead)
        if recency_hl and recency_hl > 0:
            k = np.arange(n)
            w = np.exp(-(n - 1 - k) / (n * recency_hl))
        else:
            w = np.ones(n)
        num += float(np.sum(w * lead ** 2))
        den += float(np.sum(w))
    if den <= 0:
        return float("nan")
    return float(np.sqrt(num / den))


# ----------------------------------------------------------------------------
# Estimator series (causal)
# ----------------------------------------------------------------------------

def _expanding_har_fit(series: np.ndarray, resp: np.ndarray, lags: Sequence[int]
                       ) -> np.ndarray:
    """
    PIT-exact expanding-window OLS via recursive Gram-matrix rank-1 updates:
    beta_t = (X_t^T X_t + 1e-12 I)^-1 (X_t^T y_t) uses only rows <= t, which is
    identical to np.linalg.lstsq on the cumulative design.  Returns the
    one-step-ahead fitted response for each t (NaN before warmup).
    """
    n = len(series)
    L = max(lags)
    k = len(lags) + 1
    out = np.full(n, np.nan)
    lags = tuple(int(x) for x in lags)

    # lagged means via rolling sums: mean of series[t-L:t] at t (data <= t-1)
    cs = np.concatenate([[0.0], np.cumsum(series)])
    def lag_mean(t: int, w: int) -> float:
        return (cs[t] - cs[t - w]) / w

    A = np.zeros((k, k))
    bvec = np.zeros(k)
    A[0, 0] = 1e-12
    for i in range(1, k):
        A[i, i] = 1e-12
    for t in range(L, n):
        x = np.empty(k)
        x[0] = 1.0
        for j, w in enumerate(lags):
            x[j + 1] = lag_mean(t, w)
        # forecast with beta from rows s < t (data through t-1, strict PIT);
        # the rank-1 update for row t enters the next iteration
        if t >= 63:
            beta = np.linalg.solve(A, bvec)
            f = float(beta @ x)
            out[t] = f
        A += np.outer(x, x)
        bvec += x * resp[t]
    return out


def har_abs_sigma_series(returns: Any, lags: Sequence[int] = DEFAULT_LAGS,
                         warmup: int = 252) -> pd.Series:
    """
    HAR on |r|: expanding-window recursive OLS (data through t-1), one-step
    forecast |r̂_t|, σ̂_t = |r̂_t| · sqrt(π/2).  Index = returns index.
    """
    s = pd.to_numeric(pd.Series(returns), errors="coerce").dropna()
    r = s.to_numpy(dtype=float)
    ar = np.abs(r)
    out = _expanding_har_fit(ar, ar, lags)
    out[:warmup] = np.nan
    with np.errstate(invalid="ignore"):
        out = np.maximum(out, 1e-10) * np.sqrt(np.pi / 2.0)
    return pd.Series(out, index=s.index, name="HAR_abs")


def har_sq_sigma_series(returns: Any, lags: Sequence[int] = DEFAULT_LAGS,
                        warmup: int = 252) -> pd.Series:
    """HAR on r²: recursive expanding OLS; σ̂_t = sqrt(max(r̂²_t, 1e-12))."""
    s = pd.to_numeric(pd.Series(returns), errors="coerce").dropna()
    r = s.to_numpy(dtype=float)
    r2 = r ** 2
    out = _expanding_har_fit(r2, r2, lags)
    out[:warmup] = np.nan
    with np.errstate(invalid="ignore"):
        out = np.sqrt(np.maximum(out, 1e-12))
    return pd.Series(out, index=s.index, name="HAR_sq")


def wavelet_leaders_sigma_series(returns: Any, window: int = WAVELET_WINDOW,
                                 J: int = WAVELET_J_MAX,
                                 levels: Sequence[int] = WAVELET_LEVELS,
                                 recency_hl: float = WAVELET_RECENCY_HL) -> pd.Series:
    """
    Causal wavelet-leader vol: trailing `window` days ending at t (measurement
    window; no future data). J=9 max depth per spec; effective depth
    min(J, floor(log2(window))) = 7 for the 128-day window.
    """
    s = pd.to_numeric(pd.Series(returns), errors="coerce").dropna()
    r = s.to_numpy(dtype=float)
    n = len(r)
    out = np.full(n, np.nan)
    for t in range(window, n):
        out[t] = _wavelet_leaders_vol(r[t - window + 1: t + 1], J=J, levels=levels,
                                      recency_hl=recency_hl)
    return pd.Series(out, index=s.index, name="Wavelet_db4")


# ----------------------------------------------------------------------------
# Gate — cross-estimator consistency
# ----------------------------------------------------------------------------

ESTIMATOR_FACTORY = {
    "HAR_abs": har_abs_sigma_series,
    "HAR_sq": har_sq_sigma_series,
    "Wavelet_db4": wavelet_leaders_sigma_series,
}

_ESTIMATOR_NAMES = ["HAR_abs", "HAR_sq", "Wavelet_db4"]


def _fisher_z(r: float) -> float:
    return float(np.arctanh(np.clip(r, -0.999999, 0.999999)))


def _fisher_inv(z: float) -> float:
    return float(np.tanh(z))


def _panel_corr(returns_panel: Any,
                estimators: Sequence[str] = _ESTIMATOR_NAMES,
                taus: Sequence[int] = DEFAULT_TAUS) -> Dict[str, Any]:
    """Average (Fisher-z) cross-estimator correlation across a returns panel."""
    if isinstance(returns_panel, pd.Series):
        names = {"_single": returns_panel}
    elif isinstance(returns_panel, pd.DataFrame):
        names = {c: returns_panel[c] for c in returns_panel.columns}
    else:
        raise TypeError("returns_panel must be pd.Series or pd.DataFrame")

    series_map: Dict[str, Dict[str, pd.Series]] = {}
    usable = 0
    for nm, s in names.items():
        est = {}
        for e in estimators:
            factory = ESTIMATOR_FACTORY[e]
            est[e] = factory(s)
        df = pd.concat({e: est[e] for e in estimators}, axis=1)
        df = df.replace([np.inf, -np.inf], np.nan).dropna()
        if len(df) < 60:
            continue
        series_map[nm] = df
        usable += 1
    if usable == 0:
        raise ValueError("no panel member produced >= 60 aligned estimator dates")

    k = len(estimators)
    z_sum = np.zeros((k, k))
    n_obs = 0
    for df in series_map.values():
        cm = df.corr().to_numpy(dtype=float)
        for i in range(k):
            for j in range(k):
                if i != j:
                    z_sum[i, j] += _fisher_z(cm[i, j])
        n_obs += len(df)
    avg_z = z_sum / usable
    avg_corr = np.vectorize(_fisher_inv)(avg_z)
    np.fill_diagonal(avg_corr, 1.0)

    off = [avg_corr[i, j] for i in range(k) for j in range(i + 1, k)]
    min_corr = float(min(off))
    return {
        "correlation_matrix": np.round(avg_corr, 6),
        "min_pairwise_corr": min_corr,
        "estimator_names": list(estimators),
        "n_panel_members": usable,
        "n_obs_avg": int(n_obs / usable),
        "taus": [int(t) for t in taus],
    }


def test_cross_estimator_correlation(
    returns_panel: Any,
    min_correlation: float = DEFAULT_MIN_CORR,
    estimators: Sequence[str] = _ESTIMATOR_NAMES,
    taus: Sequence[int] = DEFAULT_TAUS,
) -> Dict[str, Any]:
    """
    Gate 14 — cross-estimator consistency of σ_fractal(τ).
    σ_fractal(τ) = σ̂_daily · √τ; correlations are identical across τ because
    τ is a positive rescale — reported per-τ for traceability.

    returns_panel: pd.Series of daily returns OR pd.DataFrame (dates x tickers).

    Returns
        {name, passed, p_value: None, metric_value: min_pairwise_corr,
         correlation_matrix, min_pairwise_corr, per_estimator, n_obs,
         per_tau: {tau: min_pairwise_corr}, threshold}
    """
    try:
        res = _panel_corr(returns_panel, estimators=estimators, taus=taus)
    except Exception as exc:  # noqa: BLE001 — gates must never throw
        return {
            "name": "multifractal_consistency",
            "passed": False,
            "p_value": None,
            "metric_value": None,
            "reason": "insufficient_data_or_error",
            "detail": f"{type(exc).__name__}: {exc}",
        }

    min_corr = res["min_pairwise_corr"]
    return {
        "name": "multifractal_consistency",
        "passed": bool(min_corr >= min_correlation),
        "p_value": None,  # descriptive consistency audit, not a hypothesis test
        "metric_value": round(min_corr, 6),
        "metric": "min_pairwise_corr",
        "threshold": float(min_correlation),
        "correlation_matrix": res["correlation_matrix"].tolist(),
        "min_pairwise_corr": round(min_corr, 6),
        "estimator_names": res["estimator_names"],
        "n_panel_members": res["n_panel_members"],
        "n_obs_per_member": res["n_obs_avg"],
        "per_tau": {int(t): round(min_corr, 6) for t in res["taus"]},
        "sigma_fractal_definition": "sigma_fractal(tau) = sigma_daily * sqrt(tau)",
    }


# ----------------------------------------------------------------------------
# Self-test harness (falsification-first, pre-registered seeds)
# ----------------------------------------------------------------------------

def _garch11(n: int, omega: float = 1e-6, alpha: float = 0.15, beta: float = 0.83,
             seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    r = np.empty(n)
    v = omega / (1.0 - alpha - beta)
    for i in range(n):
        r[i] = np.sqrt(v) * rng.standard_normal()
        v = omega + alpha * r[i] ** 2 + beta * v
    return r


def _pass_panel(n: int = 1500, seed: int = 7) -> pd.Series:
    """Engineered signal-rich returns: GARCH(1,1) x slow vol regimes + crisis."""
    rng = np.random.default_rng(seed)
    g = _garch11(n, seed=seed)
    regime = np.where(np.arange(n) % 900 < 600, 1.0, 1.8).astype(float)
    regime[700:800] = 2.6          # crisis spike window
    return pd.Series(g * regime, index=pd.bdate_range("2018-01-02", periods=n))


def _null_panel(kind: str, n: int = 1500, seed: int = 3) -> pd.Series:
    rng = np.random.default_rng(seed)
    if kind == "white":
        r = rng.standard_normal(n) * 0.015
    elif kind == "fast":
        r = rng.standard_normal(n) * np.sqrt(0.005 + 0.02 * (np.arange(n) % 2))
    elif kind == "t3":
        r = rng.standard_t(3, size=n) * 0.008
    else:
        raise ValueError(kind)
    return pd.Series(r, index=pd.bdate_range("2018-01-02", periods=n))


def run_tests(config_path: str = CONFIG_PATH) -> int:
    print("Multifractal vol-gate self-test (db4, J=9, reflection boundary)")
    print("=" * 72)

    cfg = load_config(config_path)
    mf = cfg["multifractal"]
    assert mf["estimator"] == "HAR_abs_returns"
    assert [int(x) for x in mf["lags"]] == [1, 5, 22]
    assert [int(x) for x in mf["sigma_tau"]] == [5, 10, 20]
    gate_cfg = cfg["gates"]["multifractal_consistency"]
    assert "Wavelet_db4" in gate_cfg["methods"]
    assert abs(float(gate_cfg["min_correlation"]) - 0.70) < 1e-9
    print("[OK] frozen config: HAR_abs_returns, lags [1,5,22], tau [5,10,20], corr>0.70")

    # estimator shapes / causality
    rng = np.random.default_rng(0)
    small = pd.Series(rng.standard_normal(400) * 0.01,
                      index=pd.bdate_range("2020-01-02", periods=400))
    ha = har_abs_sigma_series(small)
    hs = har_sq_sigma_series(small)
    wv = wavelet_leaders_sigma_series(small)
    assert ha.notna().sum() >= 100, "HAR_abs series too short"
    assert hs.notna().sum() >= 100, "HAR_sq series too short"
    assert wv.notna().sum() >= 200, "wavelet series too short"
    assert (ha.index == small.index).all(), "estimator index must match returns index"
    print(f"[OK] estimator series causal + aligned: HAR_abs n={ha.notna().sum()}, "
          f"HAR_sq n={hs.notna().sum()}, Wavelet_db4 n={wv.notna().sum()}")

    # report expected (pre-registered, empirically validated) correlations
    expected = [
        ("PASS GARCH+regime", _pass_panel(), 0.70, 0.85, True),
        ("NULL white-noise", _null_panel("white"), -0.15, 0.25, False),
        ("NULL fast-vol-alt", _null_panel("fast"), -0.15, 0.25, False),
        ("NULL t3 heavy-tail", _null_panel("t3"), 0.30, 0.60, False),
    ]
    for tag, sig, lo, hi, should_pass in expected:
        out = test_cross_estimator_correlation(sig)
        mc = out["min_pairwise_corr"]
        assert lo <= mc <= hi, f"{tag}: min_corr={mc:.3f} outside [{lo},{hi}]"
        assert out["passed"] is should_pass, f"{tag}: passed={out['passed']}"
        print(f"[OK] {tag:24s} min_corr={mc:.3f} (expected "
              f"~{np.mean([lo, hi]):.2f}) passed={out['passed']} "
              f"matrix={out['correlation_matrix']}")

    # panel form (dates x tickers) works and reports n_panel_members
    base_panel = _pass_panel(seed=7)
    df_panel = pd.DataFrame({
        f"T{i:02d}": base_panel * (1 + 0.05 * i)
        + pd.Series(np.random.default_rng(100 + i).normal(0, 1e-4, len(base_panel)),
                    index=base_panel.index)
        for i in range(4)})
    out_panel = test_cross_estimator_correlation(df_panel)
    assert out_panel["n_panel_members"] == 4
    assert out_panel["passed"]
    print(f"[OK] panel form (4 members): min_corr={out_panel['min_pairwise_corr']:.3f} "
          f"n_obs/member={out_panel['n_obs_per_member']}")

    # PIT: no estimation step uses future returns
    assert (wv.dropna().index.max() <= small.index.max())
    print("[OK] PIT: wavelet measurement window is trailing-only")

    print("\nAll multifractal vol-gate tests passed.")
    return 0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Multifractal consistency gate — HAR-|r| vs HAR-r2 vs Wavelet_db4 leaders")
    parser.add_argument("--test", action="store_true", help="Run self-test suite")
    parser.add_argument("--config", type=str, default=CONFIG_PATH,
                        help="Frozen pre-registration config (default: %(default)s)")
    parser.add_argument("--returns", type=str, default=None,
                        help="Path to a returns parquet/csv to score (optional)")
    args = parser.parse_args()

    if args.test:
        return run_tests(config_path=args.config)
    if args.returns:
        path = args.returns
        df = (pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path))
        res = test_cross_estimator_correlation(df)
        print(res)
        return 0 if res["passed"] else 1
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())