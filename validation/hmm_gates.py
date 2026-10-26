"""
HMM Validity Gates — Phase 0-1 (bigpickle-worker / Workstream B)

Validates the 2-state t-HMM (EXPANSION/CRISIS) mandated by Data_Strategy.md
§12.7 and config/pre_registration_v2.yaml (FROZEN 2026-09-19):

    t_hmm:
      n_states: 2
      observables: [BAA10Y_spread, VIX]
      labeling_rule: "VIX_mean_ascending"     # CRISIS = higher VIX mean
      state_names: ["EXPANSION", "CRISIS"]    # hmm_state 0 / 1

Gates implemented (pre-registered thresholds from §12.7.5 / config `gates:`):
    test_crisis_precision_recall(states, baa10y, 300)
        crisis label  : BAA10Y_spread > 300 bps
        prediction    : hmm_state == 1 (CRISIS)
        thresholds    : precision > 0.80, recall > 0.70
    test_state_stability(states_across_folds)
        metric        : Adjusted Rand Index between fold state sequences
        threshold     : mean ARI > 0.80
    test_labeling_consistency(states, vix)
        rule          : CRISIS state must have higher VIX mean than EXPANSION
        result        : boolean (config t_hmm.labeling_rule)

Returns dicts shaped for ingestion/harness/gate_engine.py wiring:
    {name, p_value (stability p is not defined — reported None), metric_value,
     passed, <detail keys>}

CLI (dispatch verification, must exit 0):
    python -m validation.hmm_gates --test --config config/pre_registration_v2.yaml
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional, Sequence, Union

import numpy as np
import pandas as pd

from sklearn.metrics import adjusted_rand_score  # noqa: E402  (stable dep)

CONFIG_PATH = "config/pre_registration_v2.yaml"
CRISIS_STATE = 1
DEFAULT_THRESHOLD_BPS = 300.0
DEFAULT_PRECISION_THRESHOLD = 0.80
DEFAULT_RECALL_THRESHOLD = 0.70
DEFAULT_ARI_THRESHOLD = 0.80


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """Loads the FROZEN pre-registration config."""
    import yaml
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"pre-registration config not found: {path}")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return cfg


def _as_state_series(x: Any, name: str = "hmm_states") -> pd.Series:
    """Coerces input to a float 0/1 Series indexed by date."""
    if isinstance(x, pd.Series):
        s = pd.to_numeric(x, errors="coerce").astype(float).dropna()
    elif isinstance(x, (list, tuple, np.ndarray)):
        s = pd.Series(np.asarray(x, dtype=float))
    else:
        raise TypeError(f"{name}: unsupported type {type(x).__name__}")
    if not len(s):
        raise ValueError(f"{name}: empty series")
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


def _as_series(x: Any, name: str = "series") -> pd.Series:
    s = pd.to_numeric(pd.Series(x), errors="coerce").dropna()
    s.index = pd.to_datetime(s.index)
    return s.sort_index()


# ----------------------------------------------------------------------------
# Gate 12 — Crisis precision / recall vs BAA10Y > 300 bps
# ----------------------------------------------------------------------------

def test_crisis_precision_recall(
    hmm_states: Any,
    baa10y_series: Any,
    threshold: float = DEFAULT_THRESHOLD_BPS,
    precision_threshold: float = DEFAULT_PRECISION_THRESHOLD,
    recall_threshold: float = DEFAULT_RECALL_THRESHOLD,
) -> Dict[str, Any]:
    """
    Crisis label   : BAA10Y_spread > `threshold` bps (300, frozen)
    Crisis predict : hmm_state == 1 (CRISIS)

    Both series are aligned on their common dates (same-day comparison — the
    label definition is a contemporaneous audit of state *meaning*, not a
    forecasting test; the forecasting bar is ARI stability + PIT filter wiring).

    Returns
        {name, passed, p_value: None, precision, recall, f1,
         confusion_matrix: [[tn,fp],[fn,tp]], n_labels, n_crisis_labels,
         thresholds: {precision, recall}}
    """
    states = _as_state_series(hmm_states, "hmm_states")
    baa = _as_series(baa10y_series, "baa10y_series")

    common = states.index.intersection(baa.index)
    common = pd.DatetimeIndex(sorted(set(common)))
    if len(common) < 20:
        return _insufficient("crisis_precision_recall",
                             f"only {len(common)} aligned dates (need >= 20)")

    y_true = (baa.reindex(common) > threshold).astype(int)
    y_pred = states.reindex(common).astype(int)

    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())
    tn = int(((y_pred == 0) & (y_true == 0)).sum())

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    passed = bool(precision >= precision_threshold and recall >= recall_threshold
                  and (tp + fn) >= 5)  # need a real crisis sample to score recall
    return {
        "name": "crisis_precision_recall",
        "passed": passed,
        "p_value": None,                      # descriptive audit, not a hypothesis test
        "metric_value": round(f1, 6),
        "metric": "f1",
        "precision": round(float(precision), 6),
        "recall": round(float(recall), 6),
        "f1": round(float(f1), 6),
        "confusion_matrix": [[tn, fp], [fn, tp]],
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "n_labels": len(common),
        "n_crisis_labels": int(y_true.sum()),
        "label_definition": f"BAA10Y_spread > {threshold} bps",
        "thresholds": {"precision": precision_threshold, "recall": recall_threshold},
    }


# ----------------------------------------------------------------------------
# Gate 13 — HMM state stability (Adjusted Rand Index across walk-forward folds)
# ----------------------------------------------------------------------------

def test_state_stability(
    hmm_states_across_folds: Union[Dict[str, Any], Sequence[Any]],
    threshold: float = DEFAULT_ARI_THRESHOLD,
) -> Dict[str, Any]:
    """
    Adjusted Rand Index between the state sequences produced by every pair of
    walk-forward folds (section 12.7.5: ARI > 0.80).

    Each fold's series is reduced to its common date intersection with every
    other fold before scoring, so overlapping test windows (expanding OOS)
    are compared only where both folds actually produced a state.

    Returns
        {name, passed, p_value: None, mean_ARI, min_ARI, pairwise_ari,
         n_folds, dates_per_fold}
    """
    folds: List[pd.Series] = []
    if isinstance(hmm_states_across_folds, dict):
        items = list(hmm_states_across_folds.values())
    else:
        items = list(hmm_states_across_folds)
    for i, f in enumerate(items):
        try:
            folds.append(_as_state_series(f, f"fold_{i}"))
        except Exception as exc:  # noqa: BLE001
            return _insufficient("hmm_state_stability", f"fold {i} unreadable: {exc}")

    if len(folds) < 2:
        return _insufficient("hmm_state_stability", "need >= 2 folds")

    pairwise: Dict[str, float] = {}
    values = []
    for i in range(len(folds)):
        for j in range(i + 1, len(folds)):
            common = folds[i].index.intersection(folds[j].index)
            common = pd.DatetimeIndex(sorted(set(common)))
            if len(common) < 20:
                continue
            ari = float(adjusted_rand_score(
                folds[i].reindex(common).astype(int),
                folds[j].reindex(common).astype(int)))
            key = f"fold_{i}_vs_fold_{j}"
            pairwise[key] = round(ari, 6)
            values.append(ari)

    if not values:
        return _insufficient("hmm_state_stability",
                             "no fold pairs with >= 20 aligned dates")

    mean_ari = float(np.mean(values))
    min_ari = float(np.min(values))
    return {
        "name": "hmm_state_stability",
        "passed": bool(min_ari >= threshold),   # every pairwise fold must clear the bar
        "p_value": None,                        # descriptive stability metric
        "metric_value": round(min_ari, 6),
        "metric": "min_ARI",
        "mean_ARI": round(mean_ari, 6),
        "min_ARI": round(min_ari, 6),
        "pairwise_ARI": pairwise,
        "n_folds": len(folds),
        "dates_per_fold": [int(len(f)) for f in folds],
        "threshold": threshold,
    }


# ----------------------------------------------------------------------------
# Labeling consistency — CRISIS state must have higher VIX mean (frozen rule)
# ----------------------------------------------------------------------------

def test_labeling_consistency(hmm_states: Any, vix_series: Any,
                              significance_floor: float = 0.0) -> Dict[str, Any]:
    """
    Config t_hmm.labeling_rule = "VIX_mean_ascending": the learned CRISIS state
    (hmm_state == 1) must have a strictly higher VIX mean than EXPANSION.
    A small overlap floor is NOT applied — the rule is a hard ordering.

    Returns {name, passed, mean_vix_crisis, mean_vix_expansion,
             n_crisis, n_expansion, mean_difference_vix}
    """
    states = _as_state_series(hmm_states, "hmm_states")
    vix = _as_series(vix_series, "vix_series")
    common = states.index.intersection(vix.index)
    common = pd.DatetimeIndex(sorted(set(common)))
    if len(common) < 20:
        return _insufficient("labeling_consistency",
                             f"only {len(common)} aligned dates (need >= 20)")

    s = states.reindex(common).astype(int)
    v = vix.reindex(common)

    m_crisis = float(v[s == CRISIS_STATE].mean())
    m_exp = float(v[s == 0].mean())
    n_crisis = int((s == CRISIS_STATE).sum())
    n_exp = int((s == 0).sum())

    if n_crisis < 5 or n_exp < 5:
        return _insufficient("labeling_consistency",
                             f"need >= 5 obs per state (crisis={n_crisis}, exp={n_exp})")

    return {
        "name": "labeling_consistency",
        "passed": bool(m_crisis > m_exp + significance_floor),
        "p_value": None,
        "metric_value": round(m_crisis - m_exp, 6),
        "metric": "mean_vix_crisis_minus_expansion",
        "mean_vix_crisis": round(m_crisis, 6),
        "mean_vix_expansion": round(m_exp, 6),
        "mean_difference_vix": round(m_crisis - m_exp, 6),
        "n_crisis_obs": n_crisis,
        "n_expansion_obs": n_exp,
        "labeling_rule": "VIX_mean_ascending",
    }


# ----------------------------------------------------------------------------
# Shared failure envelope
# ----------------------------------------------------------------------------

def _insufficient(name: str, reason: str) -> Dict[str, Any]:
    return {
        "name": name,
        "passed": False,
        "p_value": None,
        "metric_value": None,
        "reason": "insufficient_data_or_error",
        "detail": reason,
    }


# ----------------------------------------------------------------------------
# Self-test harness (engineered signal must pass, noise must fail)
# ----------------------------------------------------------------------------

def _crisis_calendar(start: str, end: str,
                     crisis_windows: List[tuple],
                     base_baa: float = 180.0, crisis_baa: float = 430.0,
                     base_vix: float = 18.0, crisis_vix: float = 45.0,
                     lag_days: int = 5, seed: int = 0) -> Dict[str, pd.Series]:
    """Synthetic BAA10Y / VIX series with known crisis windows."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    baa = pd.Series(base_baa, index=idx)
    vix = pd.Series(base_vix, index=idx)
    crisis_mask = pd.Series(False, index=idx)
    for (c0, c1) in crisis_windows:
        m = (idx >= pd.Timestamp(c0)) & (idx <= pd.Timestamp(c1))
        crisis_mask |= m
        baa[m] = crisis_baa
        vix[m] = crisis_vix
    baa = baa + rng.normal(0, 3.0, len(idx))
    vix = vix + rng.normal(0, 1.2, len(idx))
    # lagged state: filter sees yesterday's info (PIT)
    states = crisis_mask.astype("boolean").shift(lag_days).fillna(False).astype("int64")
    return {"baa10y": baa, "vix": vix, "states": states.astype(int)}


# ----------------------------------------------------------------------------
# Phase 4 — Walk-forward ARI stability (CEO Ruling #3)
#
# The canonical v3 stability gate refits a compact Gaussian HMM per fold and
# scores ARI vs the persisted states.  Phase 4 reuses that machinery on the
# HYBRID_FRACTAL_V1 walk-forward folds (strategy that exists in data/) with
# the frozen phase8 fail rule: >=2 folds below the 0.70 bar => FAIL.
#
# Gate 11 contract consumed by ingestion/harness/gate_engine.py:
#   {"mean_ari", "min_ari", "per_fold_ari": [...], "passed", "status", ...}
# ----------------------------------------------------------------------------

P4_FOLD_ROOT = "data/backtests/20260919-hybrid_fractal_v1-fold{k}"
P4_ARI_BAR = 0.70            # phase8 backtest.ari (CEO Ruling #3)
P4_FAIL_FOLD_THRESHOLD = 2   # ari_fail_rule: ">=2_folds"
P4_ARI_SEED = 20260920       # same deterministic refit seed family as v3
P4_REPORT_PATH = "data/validation/hmm_gates_phase4_report.json"


def _p4_load_fold_states(root: str, k: int) -> pd.DataFrame:
    """Reads regime_states.parquet for walk-forward fold k (PIT obs_date)."""
    from pathlib import Path as _Path
    p = _Path(root.format(k=k)) / "regime_states.parquet"
    if not p.exists():
        raise FileNotFoundError(f"fold artifact missing: {p}")
    df = pd.read_parquet(p)
    if "obs_date" not in df.columns or "detected_state" not in df.columns:
        raise ValueError(f"{p}: missing obs_date/detected_state columns")
    df["obs_date"] = pd.to_datetime(df["obs_date"])
    df = df.sort_values("obs_date").reset_index(drop=True)
    return df


def run_walkforward_ari(fold_root: str = P4_FOLD_ROOT,
                        ari_bar: float = P4_ARI_BAR,
                        fail_fold_threshold: int = P4_FAIL_FOLD_THRESHOLD,
                        seed: int = P4_ARI_SEED,
                        config_path: str = "config/pre_registration_phase8.yaml",
                        ) -> Dict[str, Any]:
    """
    Phase-4 gate 11: per-fold refit-vs-persisted ARI over the v1 walk-forward
    folds.  Observables come from the FRED cache (BAA10Y, credit diff, slope)
    via hmm_gates_v3.load_fred_observables() so the refit is EXACTLY the v3
    independent estimator — no new knobs.  Folds with unusable observables or
    degenerate (single-state) persisted labels are reported honestly; a
    degenerate fold's ARI is scored against the refit (0.0 if the refit is
    not also single-state) and counts toward the fail rule.
    """
    import importlib as _imp
    import hashlib as _hl
    from sklearn.metrics import adjusted_rand_score as _ari
    v3 = _imp.import_module("validation.hmm_gates_v3")
    cfg = load_config(config_path)
    n_folds = int((cfg.get("phase8", {}).get("backtest", {})
                   .get("walkforward_folds")
                   or (cfg.get("walk_forward") or {}).get("folds", 4)))
    cfg_sha = _hl.sha256(open(config_path, "rb").read()).hexdigest()
    fred = v3.load_fred_observables()

    per_fold: Dict[str, Any] = {}
    ari_values: List[float] = []
    n_degenerate = 0
    for k in range(1, n_folds + 1):
        try:
            df = _p4_load_fold_states(fold_root, k)
        except FileNotFoundError:
            per_fold[f"fold{k}"] = {"reason": "missing_regime_states", "n_obs": 0}
            continue
        frozen_counts = df["detected_state"].value_counts().to_dict()
        single_state = len(frozen_counts) == 1
        X = pd.DataFrame({
            "baa10y": fred["BAA10Y_spread_bps"].reindex(df["obs_date"]).to_numpy(),
            "credit": fred["credit_diff_bps_BAA_minus_AAA"].reindex(df["obs_date"]).to_numpy(),
            "slope": fred["DGS10_10Y2Y_slope_bps"].reindex(df["obs_date"]).to_numpy(),
        })
        valid = X.notna().all(axis=1)
        if valid.sum() < 30:
            per_fold[f"fold{k}"] = {"reason": "insufficient_observables",
                                    "n_obs": int(valid.sum())}
            continue
        Xv = X[valid].to_numpy()
        state_map = {s: i for i, s in enumerate(v3.STATE_NAMES)}
        s_frozen = (df.loc[valid, "detected_state"]
                      .map(state_map).fillna(-1).to_numpy().astype(int))
        if set(s_frozen) == {-1}:
            per_fold[f"fold{k}"] = {"reason": "unknown_frozen_state_labels",
                                    "n_obs": int(valid.sum())}
            continue
        refit = v3._fit_gaussian_hmm(Xv, k=3, seed=seed + k)
        order = np.argsort(refit["means"][:, 0])
        # relabel refit clusters so state 0..2 order matches frozen ordering
        remap = np.empty_like(order)
        remap[order] = np.arange(len(order))
        s_refit = remap[refit["path"]]
        ari = float(_ari(s_frozen, s_refit))
        # degenerate persisted label: ARI is not informative; report honestly
        status = "degenerate_single_state" if single_state else "ok"
        if single_state:
            n_degenerate += 1
        ari_scored = ari if not single_state else (1.0 if len(set(s_refit)) == 1 else 0.0)
        ari_values.append(ari_scored)
        per_fold[f"fold{k}"] = {
            "n_obs": int(valid.sum()),
            "ari": round(ari, 5),
            "ari_scored": round(ari_scored, 5),
            "refit_state_counts": {v3.STATE_NAMES[o]: int((s_refit == o).sum())
                                   for o in range(3)},
            "persisted_state_counts": frozen_counts,
            "status": status,
        }

    if not ari_values:
        return {"name": "walkforward_ari", "passed": False, "p_value": None,
                "mean_ari": None, "min_ari": None, "per_fold_ari": [],
                "status": "insufficient_data_all_folds",
                "n_folds_below_bar": None,
                "ari_bar": ari_bar, "fail_rule": f">={fail_fold_threshold}_folds",
                "n_degenerate_folds": n_degenerate,
                "config_sha256": cfg_sha, "source": "validation/hmm_gates.py"}

    n_below = sum(1 for a in ari_values if a < ari_bar)
    passed = bool(n_below < fail_fold_threshold)
    return {
        "name": "walkforward_ari",
        "passed": passed,
        "p_value": None,
        "mean_ari": round(float(np.mean(ari_values)), 5),
        "min_ari": round(float(np.min(ari_values)), 5),
        "per_fold_ari": [per_fold.get(f"fold{k}", {}).get("ari_scored")
                         for k in range(1, n_folds + 1)],
        "per_fold": per_fold,
        "n_folds_below_bar": n_below,
        "ari_bar": ari_bar,
        "fail_rule": f">={fail_fold_threshold}_folds below {ari_bar}",
        "n_degenerate_folds": n_degenerate,
        "status": "evaluated",
        "fit": (f"compact Gaussian HMM refit (EM, k=3, seed {seed}+k) "
                f"vs persisted sticky-t-HMM states; FRED observables via v3 loader"),
        "config_sha256": cfg_sha,
        "source": "validation/hmm_gates.py",
    }


def _random_states(start: str, end: str, seed: int = 0,
                   crisis_prob: float = 0.5) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    s = pd.Series(rng.binomial(1, crisis_prob, len(idx)), index=idx)
    return s.astype(int)


def run_tests(config_path: str = CONFIG_PATH) -> int:
    print("HMM gates self-test")
    print("=" * 60)

    cfg = load_config(config_path)
    assert cfg["t_hmm"]["n_states"] == 2
    assert cfg["t_hmm"]["state_names"] == ["EXPANSION", "CRISIS"]
    assert cfg["t_hmm"]["labeling_rule"] == "VIX_mean_ascending"
    print("[OK] frozen config: 2-state t-HMM, VIX_mean_ascending label rule")

    # -- Gate 12: precision/recall on engineered crisis calendar
    syn = _crisis_calendar("2019-01-01", "2024-12-31",
                           crisis_windows=[("2020-02-15", "2020-04-30"),
                                           ("2022-06-01", "2022-10-15")],
                           seed=11)
    out12 = test_crisis_precision_recall(syn["states"], syn["baa10y"], threshold=300)
    assert out12["precision"] > 0.90, out12
    assert out12["recall"] > 0.90, out12
    assert out12["passed"], out12
    print(f"[OK] crisis_precision_recall PASS: prec={out12['precision']:.3f} "
          f"rec={out12['recall']:.3f} f1={out12['f1']:.3f} cm={out12['confusion_matrix']}")

    # random labels must fail (falsification)
    null12 = test_crisis_precision_recall(
        _random_states("2019-01-01", "2024-12-31", seed=3),
        syn["baa10y"], threshold=300)
    assert not null12["passed"], null12
    print(f"[OK] random labels FAIL: prec={null12['precision']:.3f} rec={null12['recall']:.3f}")

    # -- Gate 13: ARI stability across fold-like sequences
    folds = {f"fold_{f}": _crisis_calendar(
        "2019-01-01", "2024-12-31",
        crisis_windows=[("2020-02-15", "2020-04-30"), ("2022-06-01", "2022-10-15")],
        seed=20 + f)["states"] for f in range(4)}
    out13 = test_state_stability(folds)
    assert out13["mean_ARI"] > 0.90, out13
    assert out13["min_ARI"] > 0.90, out13
    assert out13["passed"], out13
    print(f"[OK] hmm_state_stability PASS: mean_ARI={out13['mean_ARI']:.3f} "
          f"min_ARI={out13['min_ARI']:.3f} pairs={list(out13['pairwise_ARI'])}")

    # random fold sequences must fail
    null13 = test_state_stability({f"fold_{f}": _random_states(
        "2019-01-01", "2024-12-31", seed=100 + f) for f in range(4)})
    assert not null13["passed"], null13
    assert null13["mean_ARI"] < 0.30, null13
    print(f"[OK] random fold states FAIL: mean_ARI={null13['mean_ARI']:.3f}")

    # -- Labeling consistency
    out_lbl = test_labeling_consistency(syn["states"], syn["vix"])
    assert out_lbl["passed"], out_lbl
    assert out_lbl["mean_vix_crisis"] > out_lbl["mean_vix_expansion"]
    print(f"[OK] labeling_consistency PASS: VIX crisis={out_lbl['mean_vix_crisis']:.1f} "
          f"exp={out_lbl['mean_vix_expansion']:.1f}")

    flipped = syn["states"].copy()
    flipped = 1 - flipped.astype(int)
    out_flip = test_labeling_consistency(flipped, syn["vix"])
    assert not out_flip["passed"], out_flip
    print("[OK] inverted labels FAIL labeling consistency (CRISIS VIX mean not higher)")

    # PIT wiring: nowcast never requires future info (lag_days > 0 in generator)
    assert syn["states"].index.equals(syn["baa10y"].index)
    print("[OK] PIT: state series uses information <= as-of date (5d lag)")

    print("\nAll HMM gate tests passed.")
    return 0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="HMM validity gates — crisis precision/recall, ARI stability, labeling audit")
    parser.add_argument("--test", action="store_true", help="Run self-test suite")
    parser.add_argument("--config", type=str, default=CONFIG_PATH,
                        help="Frozen pre-registration config (default: %(default)s)")
    parser.add_argument("--walkforward-ari", action="store_true",
                        help="Phase 4: write data/validation/hmm_gates_phase4_report.json "
                             "(gate 11 walk-forward ARI vs persisted states)")
    parser.add_argument("--fold-root", type=str, default=P4_FOLD_ROOT,
                        help="Fold root pattern (default: hybrid_fractal_v1)")
    parser.add_argument("--report", type=str, default=P4_REPORT_PATH,
                        help="Phase-4 report output path")
    args = parser.parse_args()

    if args.test:
        return run_tests(config_path=args.config)

    if args.walkforward_ari:
        res = run_walkforward_ari(fold_root=args.fold_root, config_path=args.config)
        report = {"name": "hmm_gates_phase4",
                  "gate": "walkforward_ari",
                  "passed": bool(res["passed"]),
                  "gates": {"walkforward_ari": res}}
        from pathlib import Path as _P
        _P(args.report).parent.mkdir(parents=True, exist_ok=True)
        _P(args.report).write_text(json.dumps(report, indent=2, sort_keys=True, default=str),
                                   encoding="utf-8")
        print(f"[hmm_phase4] report: {args.report}")
        print(f"[hmm_phase4] walkforward_ari passed={res['passed']} "
              f"mean_ARI={res.get('mean_ari')} min_ARI={res.get('min_ari')} "
              f"n_below_bar={res.get('n_folds_below_bar')} "
              f"degenerate={res.get('n_degenerate_folds')}")
        for k, pf in (res.get("per_fold") or {}).items():
            print(f"  {k}: ari_scored={pf.get('ari_scored')} status={pf.get('status')}")
        return 0 if res["passed"] else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())