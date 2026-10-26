"""
hmm_gates_v3.py — 3-State t-HMM Validity Gates (RS-28, RS-31)
Owner: bigpickle-worker | Phase 7B Validation

Validates the sticky 3-state t-HMM mandated by config/pre_registration_v3.yaml
(FROZEN, SHA-256 663939C38D...954D1):

    t_hmm:
      n_states: 3
      state_names: ["EXPANSION", "SHOCK", "CRISIS"]
      observables: ["BAA10Y_spread", "VIX", "DGS10_10Y2Y_slope"]
      labeling_rule: "VIX_mean_ascending"     # CRISIS = higher stress mean
      dwell_filter: {tau_base: 10, gamma: 2.0, tau_min: 3, tau_max: 15, epsilon: 1e-6}

Sub-gates (pre-registered thresholds from config `gates:`):
    crisis_precision_recall
        crisis label : BAA10Y_spread > 300 bps
        prediction   : detected_state == "CRISIS"
        thresholds   : precision > 0.80, recall > 0.70
        note         : zero-crisis folds are excluded (config exclude_zero_crisis_folds)
    hmm_state_stability
        metric       : ARI between frozen sticky-HMM states and an independent
                       per-fold compact Gaussian-HMM refit (hmmlearn absent ->
                       self-contained EM implementation in this module)
        threshold    : min ARI over folds > 0.80 (config across_folds: true, ARI)
    dwell_compliance (RS-31)
        formula      : tau_dwell = clamp(tau_base*(1+gamma/(D_M+epsilon)),
                                         tau_min, tau_max),  D_M = Mahalanobis
        falsifiable  : D_M >= 0  =>  required dwell >= int(tau_base) = 10 days
                       for ANY state; a completed episode shorter than 10 days
                       violates the formula regardless of D_M.
        metric       : compliance = fraction of completed episodes with
                       dwell >= 10 calendar days
        thresholds   : compliance_floor >= 0.90; tau_max-severity reported
    labeling_consistency (diagnostic)
        frozen rule  : CRISIS state must have the highest stress-observable mean.
        proxy        : VIX is not cached in data/fred_cache; the recorded
                       substitute is the (BAA-AAA) credit differential (same
                       directional signal). Both orderings are reported.

Observable / data notes
------------------------
* Fold artifacts: data/backtests/20260919-hybrid_fractal_v3-fold{1-4}/regime_states.parquet
* FRED cache:    data/fred_cache/{BAA10Y,DGS10,DGS2,AAA,BAA}.json  (no VIX cached)
* ARI is scored per fold: walk-forward test windows are sequential and disjoint,
  so the v2 pairwise-fold ARI is undefined here; refit-vs-persisted ARI is the
  pre-registered "across_folds" stability read on 4 independent windows.

CLI (dispatch verification, must exit 0):
    python -m validation.hmm_gates_v3 --run
    python -m validation.hmm_gates_v3 --dwell-compliance --3state-precision --ari
    python -m validation.hmm_gates_v3 --test
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import median
from typing import Any, Dict, List

import numpy as np
import pandas as pd

CONFIG_PATH = "config/pre_registration_v3.yaml"
FOLD_ROOT = "data/backtests/20260919-hybrid_fractal_v3-fold{k}"
FRED_DIR = Path("data/fred_cache")
DEFAULT_REPORT = Path("data/validation/hmm_gates_v3_report.json")

# Frozen gate bars (pre_registration_v3.yaml §gates)
CRISIS_LABEL_BPS = 300.0
PRECISION_BAR = 0.80
RECALL_BAR = 0.70
ARI_BAR = 0.80
DWELL_COMPLIANCE_BAR = 0.90
STATE_NAMES = ["EXPANSION", "SHOCK", "CRISIS"]
_ARI_SEED = 20260920


# ---------------------------------------------------------------------------
# Config + FRED loading
# ---------------------------------------------------------------------------

def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    import yaml
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"pre-registration config not found: {path}")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _fred_series(series_id: str) -> pd.Series:
    """Daily FRED observations -> Series indexed by Timestamp (last value per
    date wins; BAA10Y/Aaa/Baa cached in percent)."""
    p = FRED_DIR / f"{series_id}.json"
    if not p.exists():
        return pd.Series(dtype=float)
    data = json.loads(p.read_text(encoding="utf-8"))
    df = pd.DataFrame(data["observations"])
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"]).drop_duplicates(subset=["date"], keep="last")
    return df.set_index("date")["value"].sort_index()


def load_fred_observables() -> Dict[str, pd.Series]:
    """Validation observable space (all in basis points).

    VIX is NOT cached in data/fred_cache. Substitutes (documented):
    credit_diff = BAA - AAA (same fear/stress signal, Moody's family).
    slope       = DGS10 - DGS2 (frozen observable DGS10_10Y2Y_slope).

    Frequency: BAA10Y/DGS10/DGS2 daily; AAA/BAA monthly (1st of month).
    All series are forward-filled onto business days so the last PUBLISHED
    value as of each fold date is used (PIT-safe; same-day allowed).
    """
    baa10y = _fred_series("BAA10Y") * 100.0
    aaa = _fred_series("AAA") * 100.0
    baa = _fred_series("BAA") * 100.0
    dgs10 = _fred_series("DGS10") * 100.0
    dgs2 = _fred_series("DGS2") * 100.0
    credit = (baa - aaa).dropna()
    slope = (dgs10 - dgs2).dropna()
    return {
        "BAA10Y_spread_bps": _ffill_bdays(baa10y),
        "credit_diff_bps_BAA_minus_AAA": _ffill_bdays(credit),
        "DGS10_10Y2Y_slope_bps": _ffill_bdays(slope),
    }


def _ffill_bdays(ser: pd.Series) -> pd.Series:
    if ser.empty:
        return ser
    idx = pd.bdate_range(ser.index.min(), ser.index.max())
    return ser.reindex(idx).ffill()


def load_state_folds(config: Dict[str, Any]) -> Dict[str, pd.DataFrame]:
    n_folds = int(config["walk_forward"]["folds"])
    folds: Dict[str, pd.DataFrame] = {}
    for k in range(1, n_folds + 1):
        p = Path(FOLD_ROOT.format(k=k)) / "regime_states.parquet"
        if not p.exists():
            raise FileNotFoundError(f"fold artifact missing: {p}")
        df = pd.read_parquet(p)
        df["obs_date"] = pd.to_datetime(df["obs_date"])
        df = df.sort_values("obs_date").reset_index(drop=True)
        folds[f"fold{k}"] = df
    return folds


def _state_episodes(states: pd.Series) -> pd.DataFrame:
    """Contiguous runs of identical state -> episodes with dwell days.

    Folds sample states weekly. Dwell of a completed episode is the calendar
    span (end - start).days of the run. The final run of each fold is
    right-censored (no transition yet) and is EXCLUDED from compliance.
    """
    arr = states.to_numpy()
    idx = states.index.to_numpy()
    episodes: List[Dict[str, Any]] = []
    start = 0
    for i in range(1, len(arr) + 1):
        if i == len(arr) or arr[i] != arr[start]:
            episodes.append({
                "state": arr[start],
                "start_date": idx[start],
                "end_date": idx[i - 1],
                "n_rows": int(i - start),
                "dwell_days": int((pd.Timestamp(idx[i - 1]) - pd.Timestamp(idx[start])).days),
            })
            start = i
    df = pd.DataFrame(episodes)
    df["censored"] = False
    if len(df):
        df.loc[df.index[-1], "censored"] = True
    return df


# ---------------------------------------------------------------------------
# Dwell compliance (RS-31) — formula floor is falsifiable
# ---------------------------------------------------------------------------

def dwell_compliance_gate(config: Dict[str, Any],
                          folds: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    dw = config["t_hmm"]["dwell_filter"]
    tau_base = float(dw["tau_base"])
    gamma = float(dw["gamma"])
    tau_min = int(dw["tau_min"])
    tau_max = int(dw["tau_max"])
    eps = float(dw["epsilon"])
    # Frozen-parameter assertions: the exact shield formula
    assert (tau_base, gamma, tau_min, tau_max) == (10.0, 2.0, 3, 15), dw
    # D_M >= 0 => raw = tau_base*(1+gamma/(D_M+eps)) >= tau_base => required
    # dwell >= int(tau_base) = 10 for ANY state; D_M <= 4 clamps at tau_max=15.
    any_dm_min_dwell = int(np.floor(tau_base))
    deep_dm_required = tau_max

    per_fold: Dict[str, Any] = {}
    for fold_name, df in folds.items():
        # datetime index = obs_date (the Series index is positional otherwise)
        states = pd.Series(df["detected_state"].to_numpy(),
                           index=pd.to_datetime(df["obs_date"]))
        ep = _state_episodes(states)
        done = ep[~ep["censored"]]
        n_ep = int(len(done))
        n_viol_floor = int((done["dwell_days"] < any_dm_min_dwell).sum())
        n_viol_severe = int((done["dwell_days"] < deep_dm_required).sum())
        per_fold[fold_name] = {
            "n_completed_episodes": n_ep,
            "n_censored_episodes": int(ep["censored"].sum()),
            "mean_dwell_days": round(float(done["dwell_days"].mean()), 2) if n_ep else None,
            "median_dwell_days": round(float(median(done["dwell_days"])), 1) if n_ep else None,
            "min_dwell_days": int(done["dwell_days"].min()) if n_ep else None,
            "compliance_floor_tau10": round(comp_floor(done, any_dm_min_dwell), 5),
            "compliance_at_tau_max15": round(comp_floor(done, deep_dm_required), 5),
            "n_violations_below_floor10": n_viol_floor,
            "n_violations_below_tau_max15": n_viol_severe,
            "confidence_ratio": round(float(df["confident"].mean()), 4),
        }

    comps = [v["compliance_floor_tau10"] for v in per_fold.values() if v["n_completed_episodes"]]
    mean_comp = float(np.mean(comps)) if comps else 0.0
    min_comp = float(np.min(comps)) if comps else 0.0
    passed = bool(comps and min_comp >= DWELL_COMPLIANCE_BAR)
    return {
        "name": "dwell_compliance",
        "passed": passed,
        "p_value": None,                     # deterministic formula-falsification audit
        "metric_value": round(min_comp, 5),
        "metric": "min_compliance_floor",
        "mean_compliance_floor": round(mean_comp, 5),
        "min_compliance_floor": round(min_comp, 5),
        "formula": "tau_dwell = clamp(tau_base*(1+gamma/(D_M+epsilon)), tau_min, tau_max)",
        "falsifiable_floor_days": any_dm_min_dwell,
        "deep_state_required_days": deep_dm_required,
        "per_fold": per_fold,
        "thresholds": {"compliance_floor": DWELL_COMPLIANCE_BAR},
    }


def comp_floor(done: pd.DataFrame, min_days: int) -> float:
    if not len(done):
        return 0.0
    return float((done["dwell_days"] >= min_days).mean())


# ---------------------------------------------------------------------------
# Crisis precision / recall (3-state) — windowed folds, zero-crisis excluded
# ---------------------------------------------------------------------------

def crisis_precision_recall_gate(config: Dict[str, Any],
                                 folds: Dict[str, pd.DataFrame],
                                 fred: Dict[str, pd.Series]) -> Dict[str, Any]:
    baa10y = fred["BAA10Y_spread_bps"]
    per_fold: Dict[str, Any] = {}
    pooled_tp = pooled_fp = pooled_fn = pooled_tn = 0
    excluded: List[str] = []
    for fold_name, df in folds.items():
        common = pd.DatetimeIndex(sorted(set(df["obs_date"]).intersection(baa10y.index)))
        n_crisis_label = int((baa10y.reindex(common) > CRISIS_LABEL_BPS).sum())
        if n_crisis_label == 0:
            excluded.append(fold_name)
            continue
        y_true = (baa10y.reindex(common) > CRISIS_LABEL_BPS).astype(int)
        y_pred = (df.set_index("obs_date").loc[common, "detected_state"] == "CRISIS").astype(int)
        tp = int(((y_pred == 1) & (y_true == 1)).sum())
        fp = int(((y_pred == 1) & (y_true == 0)).sum())
        fn = int(((y_pred == 0) & (y_true == 1)).sum())
        tn = int(((y_pred == 0) & (y_true == 0)).sum())
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        per_fold[fold_name] = {
            "aligned_dates": int(len(common)),
            "n_crisis_labels": n_crisis_label,
            "confusion_matrix": [[tn, fp], [fn, tp]],
            "precision": round(prec, 5), "recall": round(rec, 5),
        }
        pooled_tp += tp; pooled_fp += fp; pooled_fn += fn; pooled_tn += tn

    if not per_fold:
        # Pre-registered exclusion (config: exclude_zero_crisis_folds). Also
        # report the frozen-label vs rule disagreement as an audit diagnostic:
        # persisted CRISIS days with BAA10Y <= 300 bps are label-rule mismatches.
        diag: Dict[str, Any] = {}
        for fold_name, df in folds.items():
            common = pd.DatetimeIndex(sorted(set(df["obs_date"]).intersection(baa10y.index)))
            sub = df.set_index(df["obs_date"]).loc[common]
            n_crisis = int((sub["detected_state"] == "CRISIS").sum())
            n_crisis_below_rule = int(((sub["detected_state"] == "CRISIS")
                                       & (baa10y.reindex(common) <= CRISIS_LABEL_BPS)).sum())
            diag[fold_name] = {
                "max_BAA10Y_bps_window": round(float(baa10y.reindex(common).max()), 1),
                "persisted_CRISIS_days": n_crisis,
                "persisted_CRISIS_days_where_BAA10Y_le_300bps": int(n_crisis_below_rule),
            }
        return {"name": "crisis_precision_recall",
                "passed": False,              # not evidence of failure: EXCLUDED by pre-registration
                "status": "excluded_zero_crisis_folds",
                "p_value": None, "metric_value": None,
                "reason": "no_crisis_labels_in_any_fold",
                "label_definition": f"BAA10Y_spread > {CRISIS_LABEL_BPS} bps",
                "excluded_folds": excluded,
                "per_fold_diagnostics": diag}

    prec = pooled_tp / (pooled_tp + pooled_fp) if (pooled_tp + pooled_fp) else 0.0
    rec = pooled_tp / (pooled_tp + pooled_fn) if (pooled_tp + pooled_fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    passed = bool(prec >= PRECISION_BAR and rec >= RECALL_BAR and pooled_fn >= 5)
    return {
        "name": "crisis_precision_recall",
        "passed": passed,
        "p_value": None,                       # descriptive audit, not a hypothesis test
        "metric_value": round(f1, 5),
        "metric": "f1",
        "precision": round(prec, 5),
        "recall": round(rec, 5),
        "f1": round(f1, 5),
        "pooled_confusion_matrix": [[pooled_tn, pooled_fp], [pooled_fn, pooled_tp]],
        "n_aligned_dates": pooled_tp + pooled_fp + pooled_fn + pooled_tn,
        "label_definition": f"BAA10Y_spread > {CRISIS_LABEL_BPS} bps",
        "thresholds": {"precision": PRECISION_BAR, "recall": RECALL_BAR},
        "excluded_zero_crisis_folds": excluded,
        "per_fold": per_fold,
    }


# ---------------------------------------------------------------------------
# Compact Gaussian HMM (self-contained EM + Viterbi; hmmlearn not installed)
# ---------------------------------------------------------------------------

def _fit_gaussian_hmm(obs: np.ndarray, k: int = 3, max_iter: int = 60,
                      seed: int = _ARI_SEED) -> Dict[str, Any]:
    from sklearn.cluster import KMeans
    n, d = obs.shape
    rng = np.random.default_rng(seed)
    obs_n = (obs - obs.mean(0)) / (obs.std(0) + 1e-12)

    km = KMeans(n_clusters=k, n_init=4, random_state=seed).fit(obs_n)
    means = km.cluster_centers_.copy()
    vars_ = np.array([((obs_n - means[m]) ** 2).mean(0) for m in range(k)]) + 1e-4
    tr = np.full((k, k), 0.02)
    np.fill_diagonal(tr, 0.94)                      # sticky init
    pi = np.full(k, 1.0 / k)

    def _log_emission(x: np.ndarray) -> np.ndarray:
        # x: (n, d) -> (k, n): diagonal Gaussian log-density per state
        c = -0.5 * (np.log(2 * np.pi) + np.log(vars_)).sum(1)          # (k,)
        z = -0.5 * (((x[:, None, :] - means[None, :, :]) ** 2) / vars_[None, :, :]).sum(2)
        return (c[:, None] + z.T)

    log_lik = -np.inf
    for _ in range(max_iter):
        le = _log_emission(obs_n)
        # forward
        n_ = n
        fwd = np.zeros((n_, k))
        fwd[0] = np.log(pi + 1e-300) + le[:, 0]
        for t in range(1, n_):
            fwd[t] = le[:, t] + _logsumexp_rows(fwd[t - 1] + np.log(tr + 1e-300))
        ll = _logsumexp(fwd[-1])
        if abs(ll - log_lik) < 1e-5:
            break
        log_lik = ll
        # backward: bwd[t][i] = logsumexp_j(tr[i,j] + le[j,t+1] + bwd[t+1][j])
        bwd = np.zeros((n_, k))
        bwd[-1] = 0.0
        for t in range(n_ - 2, -1, -1):
            bwd[t] = _logsumexp_rows(np.log(tr + 1e-300) + le[:, t + 1] + bwd[t + 1])
        gamma = np.exp(fwd + bwd - _logsumexp(fwd[-1]))
        gamma /= gamma.sum(1, keepdims=True)
        # M-step
        means = (gamma.T @ obs_n) / gamma.sum(0)[:, None]
        vars_ = (gamma.T @ (obs_n ** 2)) / gamma.sum(0)[:, None] - means ** 2
        vars_ = np.clip(vars_, 1e-4, None)
        xi = np.zeros((k, k))
        for t in range(n_ - 1):
            mys = fwd[t][:, None] + np.log(tr + 1e-300) + le[:, t + 1][None, :] + bwd[t + 1][None, :]
            mys -= _logsumexp(mys)
            xi += np.exp(mys)
        tr = xi / xi.sum(1, keepdims=True)
        tr = np.clip(tr, 1e-6, None)
        tr = (tr.T / tr.sum(1)).T
        pi = gamma[0]

    # Viterbi (log space)
    vp = np.zeros((n, k), dtype=int)
    v = np.zeros((n, k))
    v[0] = np.log(pi + 1e-300) + _logem_col(le0(obs_n, means, vars_), 0)
    for t in range(1, n):
        cand = v[t - 1][:, None] + np.log(tr + 1e-300) + le0(obs_n, means, vars_)[:, t]
        vp[t] = cand.argmax(0)
        v[t] = cand.max(0)
    path = np.zeros(n, dtype=int)
    path[-1] = int(v[-1].argmax())
    for t in range(n - 2, -1, -1):
        path[t] = int(vp[t + 1][path[t + 1]])
    return {"path": path, "means": means, "vars": vars_, "transitions": tr,
            "log_likelihood": float(log_lik)}


def _logsumexp(x: np.ndarray) -> float:
    m = float(np.max(x))
    return m + float(np.log(np.exp(x - m).sum()))


def _logsumexp_rows(x: np.ndarray) -> np.ndarray:
    m = x.max(1, keepdims=True)
    return (m + np.log(np.exp(x - m).sum(1, keepdims=True)))[:, 0]


def le0(obs_n: np.ndarray, means: np.ndarray, vars_: np.ndarray) -> np.ndarray:
    c = -0.5 * (np.log(2 * np.pi) + np.log(vars_)).sum(1)
    z = -0.5 * (((obs_n[:, None, :] - means[None, :, :]) ** 2) / vars_[None, :, :]).sum(2)
    return c[:, None] + z.T


def _logem_col(le: np.ndarray, t: int) -> np.ndarray:
    return le[:, t]


# ---------------------------------------------------------------------------
# ARI stability gate (per-fold refit vs persisted frozen states)
# ---------------------------------------------------------------------------

def hmm_state_stability_gate(config: Dict[str, Any],
                             folds: Dict[str, pd.DataFrame],
                             fred: Dict[str, pd.Series]) -> Dict[str, Any]:
    from sklearn.metrics import adjusted_rand_score
    baa10y, credit, slope = (fred["BAA10Y_spread_bps"], fred["credit_diff_bps_BAA_minus_AAA"],
                             fred["DGS10_10Y2Y_slope_bps"])
    per_fold: Dict[str, Any] = {}
    ari_values: List[float] = []
    label_diag: Dict[str, Any] = {}

    for fold_name, df in folds.items():
        obs_date = df["obs_date"]
        X = pd.DataFrame({
            "baa10y": baa10y.reindex(obs_date).to_numpy(),
            "credit": credit.reindex(obs_date).to_numpy(),
            "slope": slope.reindex(obs_date).to_numpy(),
        })
        valid = X.notna().all(axis=1)
        if valid.sum() < 30:
            per_fold[fold_name] = {"reason": "insufficient_observables",
                                   "n_obs": int(valid.sum())}
            continue
        Xv = X[valid].to_numpy()
        s_frozen = df.loc[valid, "detected_state"].map({s: i for i, s in enumerate(STATE_NAMES)}).to_numpy()
        refit = _fit_gaussian_hmm(Xv, k=3, seed=_ARI_SEED + int(fold_name[-1]))
        # order refit labels by BAA10Y mean ascending to name states
        order = np.argsort(refit["means"][:, 0])
        name_of_refit = {old: STATE_NAMES[new] for new, old in enumerate(order)}
        ari = float(adjusted_rand_score(s_frozen, refit["path"]))
        ari_values.append(ari)
        per_fold[fold_name] = {
            "n_obs": int(valid.sum()),
            "ARI_vs_persisted": round(ari, 5),
            "refit_state_counts": {STATE_NAMES[o]: int((refit["path"] == o).sum())
                                   for o in range(3)},
            "persisted_state_counts": df["detected_state"].value_counts().to_dict(),
        }
        # labeling consistency diagnostic: frozen rule CRISIS = highest stress mean
        for ob_name, ob_series in (("BAA10Y_bps", baa10y), ("credit_diff_bps", credit)):
            m = {s: float(ob_series.reindex(df.loc[df["detected_state"] == s, "obs_date"]).mean())
                 for s in STATE_NAMES}
            order_ok = bool(m["CRISIS"] >= m["SHOCK"] >= m["EXPANSION"])
            label_diag[fold_name] = label_diag.get(fold_name, {})
            label_diag[fold_name][ob_name] = {s: (None if np.isnan(v) else round(v, 1))
                                              for s, v in m.items()}
            label_diag[fold_name][ob_name + "_order_crisis_highest"] = order_ok

    if not ari_values:
        return {"name": "hmm_state_stability", "passed": False, "p_value": None,
                "metric_value": None, "reason": "insufficient_data_all_folds"}

    min_ari = float(np.min(ari_values))
    mean_ari = float(np.mean(ari_values))
    passed = bool(min_ari >= ARI_BAR)
    return {
        "name": "hmm_state_stability",
        "passed": passed,
        "p_value": None,                    # deterministic refit-vs-persisted stability
        "metric_value": round(min_ari, 5),
        "metric": "min_ARI",
        "min_ARI": round(min_ari, 5),
        "mean_ARI": round(mean_ari, 5),
        "per_fold_ARI": {k: v.get("ARI_vs_persisted") for k, v in per_fold.items()},
        "per_fold": per_fold,
        "labeling_consistency_diagnostics": label_diag,
        "threshold": ARI_BAR,
        "fit": "self-contained Gaussian HMM (EM, diagonal covariances, seed %d)" % _ARI_SEED,
    }


# ---------------------------------------------------------------------------
# Orchestration + report
# ---------------------------------------------------------------------------

def run_all_gates(config_path: str = CONFIG_PATH) -> Dict[str, Any]:
    config = load_config(config_path)
    assert config.get("version") == "3.0", "wrong config version"
    folds = load_state_folds(config)
    fred = load_fred_observables()
    results = {
        "dwell_compliance": dwell_compliance_gate(config, folds),
        "crisis_precision_recall": crisis_precision_recall_gate(config, folds, fred),
        "hmm_state_stability": hmm_state_stability_gate(config, folds, fred),
    }
    passed_all = all(r["passed"] for r in results.values())
    return {
        "name": "hmm_gates_v3",
        "config_sha256": _sha256(config_path),
        "passed": passed_all,
        "gates": results,
    }


def _sha256(path: str) -> str:
    import hashlib
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001
        return "unavailable"


def run_tests() -> int:
    print("HMM gates v3 self-test")
    print("=" * 66)
    cfg = load_config(CONFIG_PATH)
    assert cfg["t_hmm"]["n_states"] == 3
    assert cfg["t_hmm"]["state_names"] == STATE_NAMES
    dw = cfg["t_hmm"]["dwell_filter"]
    assert (dw["tau_base"], dw["gamma"], dw["tau_min"], dw["tau_max"]) == (10.0, 2.0, 3, 15)
    print("[OK] frozen config: 3-state sticky t-HMM, dwell {10,2.0,3,15}")

    # dwell metric: slow run complies, fast run violates
    slow = pd.Series(["CRISIS"] * 25 + ["SHOCK"] * 5, index=pd.bdate_range("2024-01-01", periods=30))
    fast = pd.Series(["CRISIS", "SHOCK", "CRISIS", "SHOCK", "CRISIS"],
                     index=pd.bdate_range("2024-01-01", periods=5))
    ep_slow = _state_episodes(slow)
    ep_fast = _state_episodes(fast)
    assert int(ep_slow.iloc[0]["dwell_days"]) >= 21   # 5-week CRISIS run
    assert int(ep_fast["dwell_days"].max()) <= 7
    print("[OK] episode extraction: weekly runs -> calendar-day dwell spans")

    # mini HMM refit on synthetic 3-cluster data must recover ~truth
    rng = np.random.default_rng(7)
    t = 120
    z = np.zeros(t, dtype=int)
    z[40:80] = 1
    z[80:] = 2
    locs = np.array([0.0, 3.0, 8.0])[z]
    obs = rng.normal(loc=locs, scale=0.5, size=t)          # (t,) stress feature
    obs = np.column_stack([obs, rng.normal(0, 2.0, t)])    # (t, 2)
    fit = _fit_gaussian_hmm(obs, k=3, seed=1)
    ari = adjusted_rand_score_import(z, fit["path"])
    assert ari > 0.80, f"refit ARI {ari:.3f}"
    print(f"[OK] compact Gaussian HMM refit recovers synthetic regime path ARI={ari:.3f}")

    print("\nAll HMM gates v3 tests passed.")
    return 0


def adjusted_rand_score_import(a, b) -> float:
    from sklearn.metrics import adjusted_rand_score
    return float(adjusted_rand_score(a, b))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="hmm_gates_v3 — 3-state t-HMM validity gates (RS-28/RS-31)")
    parser.add_argument("--run", action="store_true", help="Run all gates + write report")
    parser.add_argument("--dwell-compliance", action="store_true")
    parser.add_argument("--3state-precision", action="store_true")
    parser.add_argument("--ari", action="store_true")
    parser.add_argument("--config", type=str, default=CONFIG_PATH)
    parser.add_argument("--report", type=str, default=str(DEFAULT_REPORT))
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args()

    if args.test:
        return run_tests()
    if not args.run:
        parser.print_help()
        return 1

    full = run_all_gates(args.config)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(full, indent=2, sort_keys=True, default=str),
                                 encoding="utf-8")
    g = full["gates"]
    print(f"[hmm_v3] report: {args.report}")
    for name, r in g.items():
        print(f"[hmm_v3] {name}: passed={r['passed']} metric={r.get('metric_value')} "
              f"({r.get('metric', '')})")
    if args.dwell_compliance or args.ari or getattr(args, "3state_precision", False):
        print(f"[hmm_v3] --dwell-compliance: floor={g['dwell_compliance']['min_compliance_floor']}")
        print(f"[hmm_v3] --3state-precision: f1={g['crisis_precision_recall'].get('f1')}")
        print(f"[hmm_v3] --ari: min ARI={g['hmm_state_stability']['min_ARI']}")
    return 0 if full["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())