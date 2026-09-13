"""
Gate Engine — Harness Core  (big-pickle-worker / Phase 1)

10 pre-registered gates with Bonferroni correction per Data_Strategy.md §3 and
Session Log (DUAL-WINDOW SPEC).  Alpha is HARDCODED:

    BONFERRONI_ALPHA = 0.05 / 7 = 0.007142857...   (NOT configurable)

Gate order (registry order, 1-10):

  #  Gate name                Signature                                Pass
  --  ----------------------  ---------------------------------------  ------------
  1   chi_square_contingency  chi_square_contingency(arms_returns)     p < 0.05 @ 0.0071
  2   anova_three_arm         anova_three_arm(arms_returns)            p < 0.05 @ 0.0071
  3   welch_c_vs_a            welch_c_vs_a(arm_c, arm_a)               p < 0.0071
  4   welch_c_vs_b            welch_c_vs_b(arm_c, arm_b)               p < 0.0071
  5   oos_sharpe              oos_sharpe(arm_c_returns)                > 1.0
  6   max_drawdown            max_drawdown(arm_c_returns)              shallower than -20%
  7   information_ratio       information_ratio(arm_c, spy)            > 0.3
  8   turnover_drag           turnover_drag(arm_c_weights)             < 0.30
  9   probabilistic_sharpe    probabilistic_sharpe(arm_c_returns)      > 0.95
  10  calmar_ratio            calmar_ratio(arm_c_returns)              > 2.0

GATES 3 & 4 — SECTOR-RELATIVE RE-REGISTRATION (RS-05, Data_Strategy.md §3/§13.3):
When `config/gates_v2.yaml` is FROZEN with `sector_relative: true`, gates 3 & 4
switch to the sector-relative hypothesis:
    H0: Arm C excess return vs Arm A/B WITHIN EACH SECTOR is zero
    Test: per-sector one-sample Welch t on the sector-relative excess series
          (C_s - X_s), combined with Stouffer's method (Z = sum(z_s)/sqrt(k),
          z_s = Phi^{-1}(1 - p_s), one-sided C > X);
    Threshold: p_combined < 0.0071 (Bonferroni alpha, frozen before calibration).

The frozen config is written BY THIS MODULE'S CLI before any calibration:
    python -m ingestion.harness.gate_engine --re-register-gates 3,4 \
        --sector-quantile --dual-track
and it REFUSES to overwrite an already-frozen config unless --force is passed
(RS-05 gate freeze is one-way, per Risk Register §9).

SPY BENCHMARK WIRING (fixes §12.4 bug): `run_all_gates` falls back to a local
SPY price cache when `spy` is not supplied in the results dict, so gates 1 & 7
never silently report `missing_spy_benchmark` on a real run when SPY data has
been cached by price_cache/universe_builder.

CALIBRATION INTERFACE (for validation/gate_calibration.py):
    run_null_calibration(n=10000, gates=[3,4]) -> dict
empirically estimates the FPR of the sector-relative gates at alpha=0.0071 on
sector-structured null portfolios and returns the empirical null distributions
with critical values.

Interpretation notes (pre-registered):
- Gates 1-4 are the 4 Bonferroni-corrected statistical gates (7 in table but
  only 4 are hypothesis tests; alpha still 0.05/7 per Stream Strategy decision
  log — the 0.0071 applies to ALL p-value gates 1-4).
- Gate 6: "Max Drawdown < -20%" is interpreted as *depth limit* — pass requires
  the max drawdown to be SHALLOWER than -20% (e.g. -0.15 passes, -0.25 fails).
- Gate 8: turnover_drag uses one-way turnover averaged across rebalances.

Each gate function returns a `GateOutcome` dict:
  {name, value, threshold, passed, direction, detail..., effect_size...}

`run_all_gates(results_dict)` runs every gate in registry order and returns a
`GateResult(passed: bool, details: dict)` — ALL gates must pass (AND).
`GateResult.to_json()` serialises the full report (p-values, effect sizes,
pass/fail per gate) to a JSON file.

The dispatch contract for results_dict:
  arms_returns     : dict[str, pd.Series]  arm label -> return series (A/B/C)
  arm_a_returns    : pd.Series  (fallback if not in arms_returns)
  arm_b_returns    : pd.Series
  arm_c_returns    : pd.Series
  spy              : pd.Series  benchmark returns (required by gates 1 & 7)
  arm_c_weights    : pd.DataFrame | dict[date -> dict[ticker -> weight]]
  oos_returns      : pd.Series  optional; if present gate 5 scores THIS (true OOS)
  sector_returns   : optional, REQUIRED when sector_relative config is frozen.
                     {sector: {"A": pd.Series monthly, "C": pd.Series monthly}}
                     (gate 3) and {sector: {"B": ..., "C": ...}} (gate 4).
                     Alternatively {arm_label: DataFrame(date x sector)} frames.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd
from scipy import stats as _stats

# ----------------------------------------------------------------------------
# Bonferroni alpha — HARDCODED, not configurable (Data_Strategy.md §3)
# ----------------------------------------------------------------------------
N_BONFERRONI_GATES = 7
BONFERRONI_ALPHA: float = 0.05 / N_BONFERRONI_GATES  # 0.007142857...

# Gate thresholds (pre-registered; never tuned)
THRESHOLDS: Dict[str, float] = {
    "oos_sharpe": 1.0,
    "max_drawdown": -0.20,        # depth limit — drawdown may not be worse than -20%
    "information_ratio": 0.3,
    "turnover_drag": 0.30,
    "probabilistic_sharpe": 0.95,
    "calmar_ratio": 2.0,
}

# ----------------------------------------------------------------------------
# RS-05 — Gates 3 & 4 sector-relative re-registration (frozen config)
# ----------------------------------------------------------------------------
# Fixed 7-sector custom rollup — MUST match ingestion/universe_builder.py
# CUSTOM_7_SECTORS (Hardware, Software, Consumer, Industrial, Healthcare,
# Energy, Financial) per Data_Strategy.md §6.
SECTOR_NAMES: List[str] = [
    "Hardware", "Software", "Consumer", "Industrial",
    "Healthcare", "Energy", "Financial",
]

GATES_V2_CONFIG = Path("config") / "gates_v2.yaml"
GATES_V2_SHA256_SIDECAR = Path("config") / "gates_v2.yaml.sha256"
SECTOR_COMBINE_METHOD = "stouffer"
SECTOR_RELATIVE_MIN_SECTORS = 2          # min sectors for a valid Stouffer merge
SECTOR_RELATIVE_MIN_OBS = 3              # min observations per sector excess series

# Sectors under which the *runner* must provide per-sector arm returns; the
# frozen config also records the screen params (`sector_quantile`, `dual_track`)
# so gates and screen are re-registered as ONE hypothesis.
DEFAULT_GATES_V2: Dict[str, Any] = {
    "version": 2,
    "frozen": False,
    "sector_relative": False,
    "dual_track": False,
    "sector_quantile": False,
    "min_names_per_sector": 20,
    "mainstream_percentile": 0.25,
    "fallback_percentile": 0.30,
    "alpha_bonferroni": BONFERRONI_ALPHA,
    "gates": {
        "3": {
            "name": "welch_c_vs_a",
            "hypothesis": ("H0: E[C - A | sector s] = 0 for all s; "
                           "H1: C > A within sector (one-sided)"),
            "test": "per-sector one-sample Welch t on sector-relative excess, "
                    "Stouffer combined",
            "combine_method": SECTOR_COMBINE_METHOD,
            "alpha": BONFERRONI_ALPHA,
            "threshold": "p_combined < 0.0071",
            "frozen": False,
        },
        "4": {
            "name": "welch_c_vs_b",
            "hypothesis": ("H0: E[C - B | sector s] = 0 for all s; "
                           "H1: C > B within sector (one-sided)"),
            "test": "per-sector one-sample Welch t on sector-relative excess, "
                    "Stouffer combined",
            "combine_method": SECTOR_COMBINE_METHOD,
            "alpha": BONFERRONI_ALPHA,
            "threshold": "p_combined < 0.0071",
            "frozen": False,
        },
    },
    "calibration": {
        "required_n_null": 10_000,
        "report_path": "data/validation/gate_calibration_report.json",
        "calibration_run_id": None,
    },
}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_gates_config(path: Union[str, Path] = GATES_V2_CONFIG) -> Dict[str, Any]:
    """Loads gates_v2.yaml. Missing/unfrozen file => unfrozen defaults."""
    p = Path(path)
    cfg = json.loads(json.dumps(DEFAULT_GATES_V2))
    cfg["_source_path"] = str(p)
    if not p.exists():
        return cfg
    try:
        import yaml  # lazy — config only needed for re-registration runs
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyYAML required for gates_v2.yaml (pip install pyyaml)") from exc
    with open(p, "r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    cfg.update({k: v for k, v in loaded.items() if v is not None})
    cfg["_source_path"] = str(p)  # provenance: sidecar resolve next to this file
    if isinstance(loaded.get("gates"), dict):
        for gk, gv in loaded["gates"].items():
            if isinstance(gv, dict) and gk in cfg["gates"]:
                cfg["gates"][gk].update({k2: v2 for k2, v2 in gv.items() if v2 is not None})
    return cfg


def is_gates_frozen(cfg: Optional[Dict[str, Any]] = None) -> bool:
    cfg = cfg if cfg is not None else load_gates_config()
    return bool(cfg.get("frozen") and cfg.get("sector_relative"))


def _write_gates_v2(cfg: Dict[str, Any], path: Union[str, Path] = GATES_V2_CONFIG,
                    sha_sidecar: Union[str, Path] = GATES_V2_SHA256_SIDECAR) -> str:
    """Writes the gate config as YAML + sha256 sidecar; returns the sha256."""
    import yaml
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False)
    p.write_text(text, encoding="utf-8")
    digest = _sha256_text(text)
    Path(sha_sidecar).write_text(digest, encoding="utf-8")
    return digest


def freeze_gates_v2(sector_quantile: bool = True, dual_track: bool = True,
                    force: bool = False, path: Union[str, Path] = GATES_V2_CONFIG,
                    sha_sidecar: Union[str, Path] = GATES_V2_SHA256_SIDECAR
                    ) -> Tuple[Dict[str, Any], str]:
    """
    RS-05 gate freeze. One-way: refuses to overwrite an already-frozen config
    unless `force=True`. Returns (config, sha256).
    """
    if not force and is_gates_frozen(load_gates_config(path)):
        raise PermissionError(
            "gates_v2.yaml is already frozen (sector_relative=true). "
            "Re-registration is one-way per RS-05; pass --force only after "
            "CEO ruling (Risk Register §9)."
        )
    cfg = json.loads(json.dumps(DEFAULT_GATES_V2))
    cfg["frozen"] = True
    cfg["frozen_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    cfg["sector_relative"] = True
    cfg["dual_track"] = bool(dual_track)
    cfg["sector_quantile"] = bool(sector_quantile)
    for k in ("3", "4"):
        cfg["gates"][k]["frozen"] = True
        cfg["gates"][k]["re_registered_at"] = cfg["frozen_at"]
    digest = _write_gates_v2(cfg, path=path, sha_sidecar=sha_sidecar)
    cfg["sha256"] = digest
    return cfg, digest


# ----------------------------------------------------------------------------
# Sector-relative helpers (RS-05)
# ----------------------------------------------------------------------------

def _normalize_sector_returns(sector_returns: Any) -> Dict[str, Dict[str, pd.Series]]:
    """
    Accepts either:
      {sector: {"A": Series, "C": Series}}                     (spec contract)
      {arm_label: DataFrame(date x sector)}                    (runner frames)
    Returns {sector: {arm_label: Series}} with monthly alignment applied.
    """
    out: Dict[str, Dict[str, pd.Series]] = {}
    if not isinstance(sector_returns, dict):
        return out
    # Arm-frame layout: keys = arm labels, values = DataFrames with sector cols.
    if all(isinstance(v, pd.DataFrame) for v in sector_returns.values()):
        arm_labels = sorted(sector_returns.keys())
        sectors: set = set()
        for v in sector_returns.values():
            sectors |= {str(c) for c in v.columns}
        sectors |= set(SECTOR_NAMES)
        for s in sectors:
            rows: Dict[str, pd.Series] = {}
            for lb in arm_labels:
                frame = sector_returns[lb]
                if s in frame.columns:
                    rows[lb] = pd.Series(frame[s].astype(float).dropna())
            if len(rows) >= 2:
                out[s] = rows
        return out
    # Spec contract: {sector: {arm_label: Series}}
    for s, arm_map in sector_returns.items():
        if not isinstance(arm_map, dict):
            continue
        ser: Dict[str, pd.Series] = {}
        for lb, series in arm_map.items():
            s_ = _as_series(series, f"{s}.{lb}")
            if _spacing_days(s_) <= 3.0:
                s_ = s_.resample("ME").apply(lambda x: (1 + x).prod() - 1)
            ser[lb] = s_
        if len(ser) >= 2:
            out[str(s)] = ser
    return out


def stouffer_combine(p_values: Sequence[Optional[float]]) -> Tuple[Optional[float], Optional[float]]:
    """Stouffer's method over one-sided p-values; returns (Z, p_combined)."""
    zs = []
    for p in p_values:
        if p is None or not np.isfinite(p):
            continue
        p_c = float(np.clip(p, 1e-15, 1.0 - 1e-15))
        zs.append(_stats.norm.ppf(1.0 - p_c))
    if len(zs) < SECTOR_RELATIVE_MIN_SECTORS:
        return None, None
    z_stat = float(np.sum(zs) / np.sqrt(len(zs)))
    p_comb = float(1.0 - _stats.norm.cdf(z_stat))
    return z_stat, p_comb


def _sector_relative_outcome(arm_c: pd.Series, arm_x: pd.Series,
                             sector_returns: Dict[str, Dict[str, pd.Series]],
                             name: str, x_label: str) -> Dict[str, Any]:
    """
    Per-sector one-sample Welch t on the excess series (C_s - X_s), H0: mean
    excess = 0, one-sided C > X; combined with Stouffer's method.
    """
    per_sector: Dict[str, Dict[str, Any]] = {}
    sector_p: List[float] = []
    for s, arm_map in sector_returns.items():
        if x_label not in arm_map or "C" not in arm_map:
            continue
        c = _as_series(arm_map["C"], f"{s}.C")
        x = _as_series(arm_map[x_label], f"{s}.{x_label}")
        if _spacing_days(c) <= 3.0:
            c = c.resample("ME").apply(lambda g: (1 + g).prod() - 1)
        if _spacing_days(x) <= 3.0:
            x = x.resample("ME").apply(lambda g: (1 + g).prod() - 1)
        c, x = _align_common(c, x)
        excess = (c - x).dropna()
        if len(excess) < SECTOR_RELATIVE_MIN_OBS:
            continue
        t_stat, p_two = _stats.ttest_1samp(excess, 0.0)
        p_one = float(_stats.t.sf(t_stat, df=len(excess) - 1))  # one-sided C > X
        per_sector[s] = {
            "n": int(len(excess)), "t": float(t_stat),
            "mean_excess": float(excess.mean()), "std_excess": float(excess.std(ddof=1)),
            "p_one_sided": p_one, "p_two_sided": float(p_two),
        }
        sector_p.append(p_one)

    z_comb, p_comb = stouffer_combine(sector_p)
    usable = len(per_sector)
    if z_comb is None or usable < SECTOR_RELATIVE_MIN_SECTORS:
        return {
            "name": name, "value": None, "threshold": BONFERRONI_ALPHA,
            "passed": False, "reason": "insufficient_sector_data",
            "sectors_usable": usable, "sectors_total": len(sector_returns),
            "sector_relative": True,
        }

    # effect size: mean of per-sector Cohen's d of the excess vs 0
    ds = []
    for s, d in per_sector.items():
        sd = d["std_excess"]
        d_ = float(d["mean_excess"] / sd) if sd and sd > 0 else 0.0
        ds.append(d_)
    cohens_d = float(np.mean(ds)) if ds else 0.0

    return {
        "name": name,
        "value": float(p_comb),
        "threshold": BONFERRONI_ALPHA,
        "direction": "<",
        "passed": bool(p_comb < BONFERRONI_ALPHA),
        "alpha": BONFERRONI_ALPHA,
        "sector_relative": True,
        "combine_method": SECTOR_COMBINE_METHOD,
        "z_stouffer": float(z_comb),
        "p_value": float(p_comb),
        "p_value_two_sided": float(2.0 * (1.0 - _stats.norm.cdf(abs(z_comb)))),
        "sectors_usable": usable,
        "sectors_total": len(sector_returns),
        "per_sector": {s: {k2: (round(v2, 6) if isinstance(v2, float) else v2)
                           for k2, v2 in d.items()} for s, d in per_sector.items()},
        "effect_size": round(cohens_d, 6),
        "effect_size_type": "mean_per_sector_cohens_d_excess_vs_0",
        "n_total_sector_obs": int(sum(d["n"] for d in per_sector.values())),
    }


def welch_c_vs_a_sector_relative(arm_c: pd.Series, arm_a: pd.Series,
                                 sector_returns: Optional[Dict[str, Dict[str, pd.Series]]] = None
                                 ) -> Dict[str, Any]:
    """Gate 3 (sector-relative): Welch on (C - A) per sector, Stouffer combined."""
    if sector_returns is None:
        return {
            "name": "welch_c_vs_a", "value": None, "threshold": BONFERRONI_ALPHA,
            "passed": False, "reason": "missing_sector_returns",
            "sector_relative": True,
        }
    norm = _normalize_sector_returns(sector_returns)
    return _sector_relative_outcome(arm_c, arm_a, norm, "welch_c_vs_a", "A")


def welch_c_vs_b_sector_relative(arm_c: pd.Series, arm_b: pd.Series,
                                 sector_returns: Optional[Dict[str, Dict[str, pd.Series]]] = None
                                 ) -> Dict[str, Any]:
    """Gate 4 (sector-relative): Welch on (C - B) per sector, Stouffer combined."""
    if sector_returns is None:
        return {
            "name": "welch_c_vs_b", "value": None, "threshold": BONFERRONI_ALPHA,
            "passed": False, "reason": "missing_sector_returns",
            "sector_relative": True,
        }
    norm = _normalize_sector_returns(sector_returns)
    return _sector_relative_outcome(arm_c, arm_b, norm, "welch_c_vs_b", "B")


# ----------------------------------------------------------------------------
# Small statistics helpers
# ----------------------------------------------------------------------------

def _as_series(x: Any, name: str = "returns") -> pd.Series:
    """Coerce a gate input into a 1-D float Series with a datetime index."""
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


def _spacing_days(series: pd.Series) -> float:
    """Median day spacing of the index — decides daily vs monthly annualisation."""
    idx = pd.to_datetime(series.index)
    if len(idx) < 2:
        return 30.0
    diffs = np.diff(np.sort(idx.astype("int64"))) / 1e9 / 86400.0
    return float(np.median(diffs))


def _annualization(series: pd.Series) -> int:
    """252 for daily/business-day series, 12 for monthly."""
    return 252 if _spacing_days(series) <= 3.0 else 12


def _align_common(*series_list: pd.Series) -> List[pd.Series]:
    """Inner-join all input series on a common index (ascending, deduped)."""
    common = series_list[0].index
    for s in series_list[1:]:
        common = common.intersection(s.index)
    common = pd.DatetimeIndex(sorted(set(common)))
    return [s.reindex(common) for s in series_list]


def _try_compute(fn, *args, **kwargs):
    """Run a gate computation; returns failure outcome on any Exception."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — gates must never throw
        return {
            "value": None,
            "threshold": kwargs.get("threshold"),
            "passed": False,
            "error": f"{type(exc).__name__}: {exc}",
            "reason": "insufficient_data_or_error",
        }


# ----------------------------------------------------------------------------
# Individual gates (public, dispatch-compatible signatures)
# ----------------------------------------------------------------------------

def chi_square_contingency(arms_returns: Dict[str, pd.Series], spy_data=None) -> Dict[str, Any]:
    """
    Gate 1 — Chi-Square test of independence on  A B C x {beat SPY, lost}.
    H0: beating the benchmark is independent of arm identity.
    Threshold: p < BONFERRONI_ALPHA (0.0071).
    Monthly win/loss vs SPY. Falls back to Fisher's exact for 2x2 tables.
    Effect size: Cramér's V.
    """
    if spy_data is None:
        return {
            "name": "chi_square_contingency", "value": None, "threshold": BONFERRONI_ALPHA,
            "passed": False, "reason": "missing_benchmark", "error": "spy required",
        }
    spy = _as_series(spy_data, "spy")
    groups: Dict[str, pd.Series] = {}
    for label, s in arms_returns.items():
        s = _as_series(s, label).resample("ME").apply(lambda x: (1 + x).prod() - 1) if _spacing_days(s) <= 3.0 else s
        groups[label] = s
    spy_m = spy.resample("ME").apply(lambda x: (1 + x).prod() - 1) if _spacing_days(spy) <= 3.0 else spy

    rows: Dict[str, np.ndarray] = {}
    for label, s in groups.items():
        s, spy_m = _align_common(s, spy_m)
        if len(s) < 3:
            continue
        win = (s > spy_m).astype(int)
        loss = 1 - win
        rows[label] = np.array([int(win.sum()), int(loss.sum())])

    if len(rows) < 2:
        return {
            "name": "chi_square_contingency", "value": None, "threshold": BONFERRONI_ALPHA,
            "passed": False, "reason": "insufficient_data", "n_arms": len(rows),
        }

    labels = sorted(rows.keys())
    table = np.array([rows[l] for l in labels], dtype=float)
    if table.min(axis=None) == 0 or table.sum() == 0:
        return {
            "name": "chi_square_contingency", "value": None, "threshold": BONFERRONI_ALPHA,
            "passed": False, "reason": "degenerate_table", "table": table.tolist(),
        }

    chi2, p, dof, expected = _stats.chi2_contingency(table, correction=False)
    method = "chi2"
    if expected.min() < 5:
        if table.shape == (2, 2):
            _, p = _stats.fisher_exact(table, alternative="two-sided")
            method = "fisher_exact"
        else:
            method = "chi2_low_power"

    # Cramér's V for 2D tables
    n = table.sum()
    v = float(np.sqrt(chi2 / (n * min(table.shape[0] - 1, table.shape[1] - 1)))) if n else 0.0

    return {
        "name": "chi_square_contingency",
        "statistic": float(chi2),
        "df": int(dof),
        "p_value": float(p),
        "method": method,
        "expected": expected.tolist(),
        "table": table.astype(int).tolist(),
        "effect_size": round(v, 6),
        "effect_size_type": "cramers_v",
        "alpha": BONFERRONI_ALPHA,
        "value": float(p),
        "threshold": BONFERRONI_ALPHA,
        "direction": "<",
        "passed": bool(p < BONFERRONI_ALPHA),
        "n_arms": len(labels),
        "n_months_total": int(table.sum()),
    }


def anova_three_arm(arms_returns: Dict[str, pd.Series]) -> Dict[str, Any]:
    """
    Gate 2 — one-way ANOVA on the three arm return series.
    H0: all arm mean returns equal.  Threshold: p < 0.0071.
    Effect size: eta-squared (SS_between / SS_total).
    """
    groups: Dict[str, np.ndarray] = {}
    for label, s in arms_returns.items():
        s = _as_series(s, label)
        if _spacing_days(s) <= 3.0:
            s = s.resample("ME").apply(lambda x: (1 + x).prod() - 1)
        groups[label] = s.values
    if len(groups) < 3:
        return {
            "name": "anova_three_arm", "value": None, "threshold": BONFERRONI_ALPHA,
            "passed": False, "reason": "insufficient_data", "n_arms": len(groups),
        }

    labels = sorted(groups.keys())
    arrays = [groups[l] for l in labels]
    f_stat, p_value = _stats.f_oneway(*arrays)

    # eta-squared
    all_vals = np.concatenate(arrays)
    grand_mean = all_vals.mean()
    ss_between = sum(len(a) * (a.mean() - grand_mean) ** 2 for a in arrays)
    ss_total = ((all_vals - grand_mean) ** 2).sum()
    eta2 = float(ss_between / ss_total) if ss_total > 0 else 0.0

    return {
        "name": "anova_three_arm",
        "f_statistic": float(f_stat),
        "p_value": float(p_value),
        "effect_size": round(eta2, 6),
        "effect_size_type": "eta_squared",
        "means": {l: float(groups[l].mean()) for l in labels},
        "stds": {l: float(groups[l].std(ddof=1)) if len(groups[l]) > 1 else 0.0 for l in labels},
        "ns": {l: int(len(groups[l])) for l in labels},
        "alpha": BONFERRONI_ALPHA,
        "value": float(p_value),
        "threshold": BONFERRONI_ALPHA,
        "direction": "<",
        "passed": bool(p_value < BONFERRONI_ALPHA),
        "n_arms": len(labels),
    }


def _welch_c_vs_x(arm_c: pd.Series, arm_x: pd.Series, name: str) -> Dict[str, Any]:
    c = _as_series(arm_c, "arm_c")
    x = _as_series(arm_x, name)
    if _spacing_days(c) <= 3.0:
        c = c.resample("ME").apply(lambda s: (1 + s).prod() - 1)
    if _spacing_days(x) <= 3.0:
        x = x.resample("ME").apply(lambda s: (1 + s).prod() - 1)
    c, x = _align_common(c, x)
    if len(c) < 3 or len(x) < 3:
        return {
            "name": name, "value": None, "threshold": BONFERRONI_ALPHA,
            "passed": False, "reason": "insufficient_data",
        }

    t_stat, p_two = _stats.ttest_ind(c, x, equal_var=False)
    # Pre-registered hypothesis: hybrid C outperforms the pure arm.
    _, p_one = _stats.ttest_ind(c, x, equal_var=False, alternative="greater")

    mc, mx = c.mean(), x.mean()
    sc, sx = c.std(ddof=1), x.std(ddof=1)
    pooled = float(np.sqrt((sc ** 2 + sx ** 2) / 2.0)) if (sc ** 2 + sx ** 2) > 0 else 0.0
    cohens_d = float((mc - mx) / pooled) if pooled > 0 else 0.0

    return {
        "name": name,
        "t_statistic": float(t_stat),
        "p_value_two_sided": float(p_two),
        "p_value": float(p_one),
        "alternative": "greater (C > X)",  # pre-registered direction
        "mean_c": float(mc),
        "mean_x": float(mx),
        "std_c": float(sc),
        "std_x": float(sx),
        "n_c": int(len(c)),
        "n_x": int(len(x)),
        "effect_size": round(cohens_d, 6),
        "effect_size_type": "cohens_d",
        "alpha": BONFERRONI_ALPHA,
        "value": float(p_one),
        "threshold": BONFERRONI_ALPHA,
        "direction": "<",
        "passed": bool(p_one < BONFERRONI_ALPHA),
    }


def welch_c_vs_a(arm_c: pd.Series, arm_a: pd.Series) -> Dict[str, Any]:
    """Gate 3 — Welch's t-test, C vs A, one-sided (C > A).  p < 0.0071."""
    return _welch_c_vs_x(arm_c, arm_a, "welch_c_vs_a")


def welch_c_vs_b(arm_c: pd.Series, arm_b: pd.Series) -> Dict[str, Any]:
    """Gate 4 — Welch's t-test, C vs B, one-sided (C > B).  p < 0.0071."""
    return _welch_c_vs_x(arm_c, arm_b, "welch_c_vs_b")


def oos_sharpe(arm_c_returns: pd.Series, oos_returns: Optional[pd.Series] = None) -> Dict[str, Any]:
    """
    Gate 5 — annualised Sharpe of Arm C.
    If `oos_returns` is provided it is scored instead (true out-of-sample bar).
    Threshold: > 1.0.
    """
    s = _as_series(oos_returns if oos_returns is not None else arm_c_returns, "arm_c_returns")
    ann = _annualization(s)
    mu = s.mean()
    sigma = s.std(ddof=1)
    if sigma == 0 or np.isnan(sigma):
        return {
            "name": "oos_sharpe", "value": None, "threshold": THRESHOLDS["oos_sharpe"],
            "passed": False, "reason": "zero_variance",
        }
    sharpe = float(mu / sigma * np.sqrt(ann))
    return {
        "name": "oos_sharpe",
        "value": round(sharpe, 6),
        "threshold": THRESHOLDS["oos_sharpe"],
        "direction": ">",
        "passed": bool(sharpe > THRESHOLDS["oos_sharpe"]),
        "annualized": ann,
        "mean_period": float(mu),
        "std_period": float(sigma),
        "n_obs": int(len(s)),
        "scored_oos": oos_returns is not None,
    }


def max_drawdown(arm_c_returns: pd.Series) -> Dict[str, Any]:
    """
    Gate 6 — maximum drawdown depth of Arm C.
    Pass requires the drawdown to be SHALLOWER than -20% (value > -0.20).
    """
    s = _as_series(arm_c_returns, "arm_c_returns")
    cum = (1 + s).cumprod()
    peak = cum.cummax()
    dd = cum / peak - 1.0
    mdd = float(dd.min())
    return {
        "name": "max_drawdown",
        "value": round(mdd, 6),
        "threshold": THRESHOLDS["max_drawdown"],
        "direction": "shallower than (> threshold)",  # -0.15 passes; -0.25 fails
        "passed": bool(mdd > THRESHOLDS["max_drawdown"]),
        "max_drawdown_pct": round(mdd * 100.0, 4),
    }


def information_ratio(arm_c: pd.Series, spy: pd.Series) -> Dict[str, Any]:
    """
    Gate 7 — annualised Information Ratio of Arm C vs SPY.
    IR = mean(excess) / std(excess) * sqrt(annualization).  Threshold: > 0.3.
    """
    c = _as_series(arm_c, "arm_c")
    b = _as_series(spy, "spy")
    if _spacing_days(c) <= 3.0:
        c = c.resample("ME").apply(lambda s: (1 + s).prod() - 1)
    if _spacing_days(b) <= 3.0:
        b = b.resample("ME").apply(lambda s: (1 + s).prod() - 1)
    c, b = _align_common(c, b)
    if len(c) < 3:
        return {
            "name": "information_ratio", "value": None, "threshold": THRESHOLDS["information_ratio"],
            "passed": False, "reason": "insufficient_data",
        }
    excess = c - b
    te = excess.std(ddof=1)
    if te == 0 or np.isnan(te):
        return {
            "name": "information_ratio", "value": None, "threshold": THRESHOLDS["information_ratio"],
            "passed": False, "reason": "zero_tracking_error",
        }
    ir = float(excess.mean() / te * np.sqrt(12))
    return {
        "name": "information_ratio",
        "value": round(ir, 6),
        "threshold": THRESHOLDS["information_ratio"],
        "direction": ">",
        "passed": bool(ir > THRESHOLDS["information_ratio"]),
        "excess_mean": float(excess.mean()),
        "tracking_error": float(te),
        "n_obs": int(len(c)),
        "annualized": 12,
    }


def turnover_drag(arm_c_weights: Union[pd.DataFrame, Dict[Any, Dict[str, float]]]) -> Dict[str, Any]:
    """
    Gate 8 — mean one-way portfolio turnover per rebalance from the weight
    path (dates x tickers).  Threshold: < 0.30.

    turnover_t = 0.5 * sum |w_t - w_{t-1}|   (one-way)
    """
    if isinstance(arm_c_weights, dict):
        idx = sorted(pd.to_datetime(list(arm_c_weights.keys())))
        df = pd.DataFrame({d: arm_c_weights[d] for d in arm_c_weights}).T
        df.index = idx
        df = df.sort_index().fillna(0.0)
    elif isinstance(arm_c_weights, pd.DataFrame):
        df = arm_c_weights.copy().sort_index().fillna(0.0)
    else:
        raise TypeError("arm_c_weights must be DataFrame or dict of {date: {ticker: weight}}")

    if df.empty or len(df) < 2:
        return {
            "name": "turnover_drag", "value": None, "threshold": THRESHOLDS["turnover_drag"],
            "passed": False, "reason": "insufficient_weight_snapshots", "n_snapshots": len(df),
        }

    rebalances = pd.to_datetime(df.index)
    per = []
    for i in range(1, len(df)):
        prev = df.iloc[i - 1].to_numpy(dtype=float)
        cur = df.iloc[i].to_numpy(dtype=float)
        # Normalise both to sum 1 before differencing when possible
        sp, sc = prev.sum(), cur.sum()
        if sp > 0:
            prev = prev / sp
        if sc > 0:
            cur = cur / sc
        delta_norm = np.abs(cur - prev).sum()
        per.append(float(0.5 * delta_norm))

    mean_turnover = float(np.mean(per)) if per else 0.0
    return {
        "name": "turnover_drag",
        "value": round(mean_turnover, 6),
        "threshold": THRESHOLDS["turnover_drag"],
        "direction": "<",
        "passed": bool(mean_turnover < THRESHOLDS["turnover_drag"]),
        "mean_one_way_turnover": round(mean_turnover, 6),
        "n_rebalances": len(per),
        "per_rebalance": [round(p, 6) for p in per],
    }


def probabilistic_sharpe(arm_c_returns: pd.Series) -> Dict[str, Any]:
    """
    Gate 9 — Probabilistic Sharpe Ratio (Bailey & López de Prado).
    PSR = Phi( (SR - SR*) * sqrt(n-1) / sqrt(1 - skew*SR + (kurt-1)/4 * SR^2) )
    SR is per-period (not annualised), SR* = 0.  Threshold: PSR > 0.95.
    """
    s = _as_series(arm_c_returns, "arm_c_returns")
    n = len(s)
    sr = s.mean() / s.std(ddof=1) if s.std(ddof=1) > 0 else 0.0
    if s.std(ddof=1) == 0:
        return {
            "name": "probabilistic_sharpe", "value": None, "threshold": THRESHOLDS["probabilistic_sharpe"],
            "passed": False, "reason": "zero_variance",
        }
    skew = float(_stats.skew(s))
    # PSR uses RAW kurtosis:  PSR = Phi( (SR-SR*)*sqrt(n-1) / sqrt(1 - skew*SR + (kurt_raw-1)/4*SR^2) )
    kurt_raw = float(_stats.kurtosis(s, fisher=False))
    denom = 1.0 - skew * sr + (kurt_raw - 1.0) / 4.0 * sr ** 2
    if denom <= 0:
        return {
            "name": "probabilistic_sharpe", "value": None, "threshold": THRESHOLDS["probabilistic_sharpe"],
            "passed": False, "reason": "degenerate_variance_adjustment", "denominator": float(denom),
        }
    psr = float(_stats.norm.cdf((sr - 0.0) * np.sqrt(n - 1) / np.sqrt(denom)))
    return {
        "name": "probabilistic_sharpe",
        "value": round(psr, 6),
        "threshold": THRESHOLDS["probabilistic_sharpe"],
        "direction": ">",
        "passed": bool(psr > THRESHOLDS["probabilistic_sharpe"]),
        "sharpe_period": round(float(sr), 6),
        "skew": round(skew, 6),
        "excess_kurtosis": round(kurt_raw - 3.0, 6),
        "n_obs": int(n),
        "benchmark_sr": 0.0,
    }


def calmar_ratio(arm_c_returns: pd.Series) -> Dict[str, Any]:
    """
    Gate 10 — Calmar Ratio = annualised return / |max drawdown|.  Threshold: > 2.0.
    """
    s = _as_series(arm_c_returns, "arm_c_returns")
    ann = _annualization(s)
    cum = (1 + s).cumprod()
    peak = cum.cummax()
    mdd = float((cum / peak - 1.0).min())
    if mdd >= 0:
        return {
            "name": "calmar_ratio", "value": None, "threshold": THRESHOLDS["calmar_ratio"],
            "passed": False, "reason": "no_drawdown_observed",
        }
    total = float(cum.iloc[-1])
    years = len(s) / ann
    cagr = float(total ** (1.0 / years) - 1.0) if years > 0 and total > 0 else np.nan
    if not np.isfinite(cagr) or np.isnan(cagr):
        return {
            "name": "calmar_ratio", "value": None, "threshold": THRESHOLDS["calmar_ratio"],
            "passed": False, "reason": "negative_or_zero_terminal_value",
        }
    calmar = float(cagr / abs(mdd))
    return {
        "name": "calmar_ratio",
        "value": round(calmar, 6),
        "threshold": THRESHOLDS["calmar_ratio"],
        "direction": ">",
        "passed": bool(calmar > THRESHOLDS["calmar_ratio"]),
        "cagr": round(cagr, 6),
        "max_drawdown": round(mdd, 6),
        "annualized": ann,
        "n_obs": int(len(s)),
    }


# ----------------------------------------------------------------------------
# Pre-registered registry (order is binding)
# ----------------------------------------------------------------------------

GATE_REGISTRY: List[Dict[str, Any]] = [
    {"name": "chi_square_contingency", "fn": chi_square_contingency, "family": "statistical"},
    {"name": "anova_three_arm",        "fn": anova_three_arm,        "family": "statistical"},
    {"name": "welch_c_vs_a",           "fn": welch_c_vs_a,           "family": "statistical"},
    {"name": "welch_c_vs_b",           "fn": welch_c_vs_b,           "family": "statistical"},
    {"name": "oos_sharpe",             "fn": oos_sharpe,             "family": "performance"},
    {"name": "max_drawdown",           "fn": max_drawdown,           "family": "risk"},
    {"name": "information_ratio",      "fn": information_ratio,      "family": "performance"},
    {"name": "turnover_drag",          "fn": turnover_drag,          "family": "cost"},
    {"name": "probabilistic_sharpe",   "fn": probabilistic_sharpe,   "family": "performance"},
    {"name": "calmar_ratio",           "fn": calmar_ratio,           "family": "performance"},
]


@dataclass
class GateResult:
    """Aggregate result of running all 10 gates on a results_dict."""
    passed: bool
    details: Dict[str, Any] = field(default_factory=dict)
    alpha: float = BONFERRONI_ALPHA
    n_gates: int = len(GATE_REGISTRY)
    n_passed: int = 0
    run_at: str = field(default_factory=lambda: _dt.datetime.now(_dt.timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "alpha_bonferroni": self.alpha,
            "n_gates_total": self.n_gates,
            "n_gates_passed": self.n_passed,
            "gates": self.details,
            "run_at": self.run_at,
        }

    def to_json(self, path: Optional[Union[str, Path]] = None) -> Optional[str]:
        payload = json.dumps(self.to_dict(), indent=2, default=str, sort_keys=True)
        if path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(payload, encoding="utf-8")
        return payload


class GateEngine:
    """Runs the 10 pre-registered gates against a 3-arm results dict."""

    def __init__(self):
        self.registry = GATE_REGISTRY

    # -- input resolution ----------------------------------------------------

    @staticmethod
    def _resolve_arms(rd: Dict[str, Any]) -> Dict[str, pd.Series]:
        arms: Dict[str, pd.Series] = {}
        for key, val in rd.items():
            k = key.lower()
            if k in ("arms_returns", "arms"):
                if isinstance(val, dict):
                    arms.update({str(lb).upper(): s for lb, s in val.items()})
            elif k.startswith("arm_") and k.endswith("_returns") and not k.startswith("arms_"):
                lb = k.replace("arm_", "").replace("_returns", "").upper()
                if isinstance(val, (pd.Series, list, tuple, np.ndarray, pd.DataFrame)):
                    if lb not in arms:
                        arms[lb] = _as_series(val, k)
        return arms

    @staticmethod
    def _resolve_spy_from_cache(rd: Dict[str, Any]) -> Tuple[Optional[pd.Series], str]:
        """
        SPY benchmark fallback (fixes the §12.4 wiring gap).  When the caller
        does not supply `spy`, looks for a locally cached SPY price series in
        (in priority order):
          1) rd["spy_cache"]  — pre-loaded DataFrame(date, ticker, close)
          2) data/source/yfinance/SPY/ (csv or parquet from universe_builder)
          3) data/prices/SPY.parquet
          4) data/master_prices/SPY.parquet
        Returns (monthly_return_series_or_None, provenance_string).
        """
        from io import StringIO  # noqa: F401  (module function keeps imports local)

        candidates: List[Tuple[str, Path]] = []
        try:
            candidates.append(("rd.spy_cache", Path("NONE")))
            for p in [
                Path("data") / "source" / "yfinance" / "SPY",   # dir or file
                Path("data") / "prices" / "SPY.parquet",
                Path("data") / "master_prices" / "SPY.parquet",
            ]:
                candidates.append((str(p), p))
        except Exception:
            pass

        def _to_monthly_returns(df: pd.DataFrame) -> pd.Series:
            if df is None or df.empty:
                raise ValueError("empty cache")
            if "ticker" in df.columns and "SPY" in df["ticker"].astype(str).values:
                df = df[df["ticker"].astype(str) == "SPY"]
            date_col = next((c for c in ("date", "Date", "ds") if c in df.columns), None)
            if date_col is None:
                raise ValueError("no date column")
            close_col = next((c for c in ("close", "Close", "Adj Close", "adj_close")
                              if c in df.columns), None)
            if close_col is None:
                raise ValueError("no close column")
            s = df[[date_col, close_col]].copy()
            s[date_col] = pd.to_datetime(s[date_col])
            s = s.sort_values(date_col).set_index(date_col)[close_col].astype(float)
            s = s.resample("ME").last().pct_change().dropna()
            if len(s) < 3:
                raise ValueError("too few monthly points")
            return s

        # 1) in-memory cache passed in the results dict
        try:
            raw = rd.get("spy_cache")
            if raw is not None:
                if isinstance(raw, pd.DataFrame):
                    return _to_monthly_returns(raw), "spy_cache"
                if isinstance(raw, pd.Series):
                    ser = raw.astype(float).dropna()
                    if _spacing_days(ser) <= 3.0:
                        ser = ser.resample("ME").apply(lambda x: (1 + x).prod() - 1)
                    if len(ser) >= 3:
                        return ser, "spy_cache_series"
        except Exception:
            pass

        # 2)-4) local files
        for label, p in candidates[1:]:
            try:
                if p.exists() and p.is_dir():
                    # universe_builder cache folder: SPY.csv / SPY.parquet
                    file = None
                    for f in sorted(p.iterdir()):
                        if f.suffix in (".csv", ".parquet") and "SPY" in f.name.upper():
                            file = f
                            break
                    if file is not None:
                        df = pd.read_csv(file) if file.suffix == ".csv" else pd.read_parquet(file)
                        return _to_monthly_returns(df), f"file:{file}"
                elif p.exists() and p.is_file():
                    df = pd.read_parquet(p)
                    return _to_monthly_returns(df), f"file:{p}"
            except Exception:
                continue

        return None, "unavailable"

    def run_all_gates(self, results_dict: Dict[str, Any]) -> GateResult:
        """
        Runs all 10 gates in registry order.  `results_dict` accepts the
        ThreeArmRunner output dict directly (arm_a_returns ... arm_c_weights)
        or an explicit dict:
          arms_returns={A:.., B:.., C:..}, spy=..., oos_returns=..., arm_c_weights=...
        Returns a GateResult with `details["meta"]` recording the gate_config
        version + sha256 so gate provenance is traceable (dispatch contract).
        """
        rd = results_dict or {}
        arms = self._resolve_arms(rd)

        arm_c = arms.get("C")
        arm_a = arms.get("A")
        arm_b = arms.get("B")
        spy = rd.get("spy")
        sp_prov = "caller"
        if isinstance(spy, dict):
            spy = next(iter(spy.values())) if spy else None
        if spy is None:
            spy, sp_prov = self._resolve_spy_from_cache(rd)
            rd = dict(rd)
            rd["spy"] = spy
            rd["spy_provenance"] = sp_prov

        cfg = load_gates_config()
        sector_relative = is_gates_frozen(cfg)
        sector_returns = rd.get("sector_returns")
        sec_prov = rd.get("sector_provenance", "caller")

        oos = rd.get("oos_returns")
        weights = rd.get("arm_c_weights")

        details: Dict[str, Any] = {}
        failures: List[str] = []

        for gate in self.registry:
            name, fn = gate["name"], gate["fn"]
            if name == "chi_square_contingency":
                out = _try_compute(fn, arms, spy) if (arms and spy is not None) \
                    else {"passed": False, "value": None,
                          "reason": "missing_spy_benchmark" if arms else "no_arms_returns"}
                out.setdefault("spy_provenance", sp_prov)
            elif name == "anova_three_arm":
                out = _try_compute(fn, arms) if arms \
                    else {"passed": False, "value": None, "reason": "no_arms_returns"}
            elif name in ("welch_c_vs_a", "welch_c_vs_b"):
                x = arm_a if name == "welch_c_vs_a" else arm_b
                if sector_relative:
                    # RS-05: sector-relative test, Stouffer combined (frozen).
                    sfn = welch_c_vs_a_sector_relative if name == "welch_c_vs_a" \
                        else welch_c_vs_b_sector_relative
                    if arm_c is not None and x is not None:
                        out = _try_compute(sfn, arm_c, x, sector_returns)
                        out.setdefault("sector_provenance", sec_prov)
                    else:
                        out = {"passed": False, "value": None, "reason": "missing_arm_series"}
                else:
                    out = _try_compute(fn, arm_c, x) if (arm_c is not None and x is not None) \
                        else {"passed": False, "value": None, "reason": "missing_arm_series"}
            elif name == "oos_sharpe":
                out = _try_compute(fn, arm_c, oos) if arm_c is not None \
                    else {"passed": False, "value": None, "reason": "missing_arm_c"}
            elif name == "information_ratio":
                out = _try_compute(fn, arm_c, spy) if (arm_c is not None and spy is not None) \
                    else {"passed": False, "value": None, "reason": "missing_spy_benchmark"}
                out.setdefault("spy_provenance", sp_prov)
            elif name == "turnover_drag":
                out = _try_compute(fn, weights) if weights is not None \
                    else {"passed": False, "value": None, "reason": "missing_arm_c_weights"}
            elif name == "max_drawdown":
                out = _try_compute(fn, arm_c) if arm_c is not None \
                    else {"passed": False, "value": None, "reason": "missing_arm_c"}
            elif name == "probabilistic_sharpe":
                out = _try_compute(fn, arm_c) if arm_c is not None \
                    else {"passed": False, "value": None, "reason": "missing_arm_c"}
            elif name == "calmar_ratio":
                out = _try_compute(fn, arm_c) if arm_c is not None \
                    else {"passed": False, "value": None, "reason": "missing_arm_c"}
            else:
                out = {"passed": False, "value": None, "reason": "unknown_gate"}

            out.setdefault("name", name)
            out.setdefault("family", gate["family"])
            passed_flag = bool(out.get("passed", False))
            if not passed_flag:
                failures.append(name)
            details[name] = out

        # provenance block (dispatch contract: gate_version, sha256)
        sha_value = None
        try:
            # sidecar resolves NEXT TO the config file that was actually loaded
            # (repo config/gates_v2.yaml OR a tempdir copy in self-tests)
            _src = Path(cfg.get("_source_path", str(GATES_V2_CONFIG)))
            _side = Path(str(_src) + ".sha256")
            if _side.exists():
                sha_value = _side.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        details["meta"] = {
            "gate_version": cfg.get("version"),
            "config_sha256": sha_value,
            "config_frozen": bool(cfg.get("frozen")),
            "sector_relative": sector_relative,
            "spy_provenance": sp_prov,
            "sector_provenance": sec_prov,
            "alpha_bonferroni": BONFERRONI_ALPHA,
            "combine_method": SECTOR_COMBINE_METHOD,
        }

        # "meta" is provenance metadata, not a gate; excluded from scoring.
        score_keys = [k for k in details if k != "meta"]
        n_passed = sum(1 for k in score_keys if bool(details[k].get("passed", False)))

        return GateResult(
            passed=len(failures) == 0,
            details=details,
            n_passed=n_passed,
            n_gates=len(self.registry),
        )


# ----------------------------------------------------------------------------
# Null calibration (empirical FPR at Bonferroni alpha, RS-05)
# ----------------------------------------------------------------------------

def null_sector_portfolios(
    n_months: int = 48,
    n_sectors: Optional[int] = None,
    seed: int = 0,
    n_tickers_per_sector: int = 8,
) -> Dict[str, Dict[str, pd.Series]]:
    """
    Sector-structured NULL data generator for calibration.  Arms A/B/C are pure
    noise WITHIN every sector (H0 true), with a shared cross-sector market
    factor so per-sector and portfolio tests see realistic correlation structure
    but no per-sector alpha.  Returns the same contract GateEngine consumes:
        {sector: {"A": Series, "B": Series, "C": Series}}
    (monthly compounding returns).
    """
    n_sectors = n_sectors or len(SECTOR_NAMES)
    rng = np.random.default_rng(seed)
    idx = _monthly_dates(n_months, start="2020-01-31")
    f_mkt = rng.normal(0.003, 0.030, n_months)          # common market factor
    sectors = SECTOR_NAMES[:n_sectors]
    out: Dict[str, Dict[str, pd.Series]] = {}
    for j, s in enumerate(sectors):
        beta = 0.8 + 0.4 * rng.random()
        f_sec = rng.normal(0.0, 0.012, n_months)        # sector-specific factor
        rows: Dict[str, pd.Series] = {}
        for arm in ("A", "B", "C"):
            r = rng.normal(0.0, 0.014, (n_months, n_tickers_per_sector))
            port = beta * f_mkt[:, None] + f_sec[:, None] + r   # market + sector + idiosyncratic
            rows[arm] = pd.Series(port.mean(axis=1), index=idx, name=f"{s}.{arm}")
        out[s] = rows
    return out


def run_null_calibration(
    n: int = 10_000,
    gates: Sequence[int] = (3, 4),
    n_months: int = 48,
    n_sectors: Optional[int] = None,
    seed: int = 0,
) -> Dict[str, Any]:
    """
    Empirical FPR calibration of the sector-relative gates at the frozen
    Bonferroni alpha (0.0071).  For each of `n` null universes:
        - build a null sector portfolio (sector-linked noise, zero alpha);
        - run gates 3 & 4 (sector-relative Stouffer);
        - tally rejections at p < BONFERRONI_ALPHA.

    Returns a report dict compatible with data/validation/
    gate_calibration_report.json:
        {
          "calibration_run_id": "<datetime>",
          "n_null": n,  "alpha": 0.0071,
          "gates": {"3": {"fpr", "wilson_95_ci_low", "wilson_95_ci_high",
                          "rejections", "critical_value_p05", "p95_p_value"}, ...},
          "sector_relative": true, "combine_method": "stouffer",
        }
    Gates outside {3,4} are ignored (reports {} placeholders) — this module is
    the sector-relative engine; the legacy whole-gate calibration lives in
    validation/gate_calibration.py.
    """
    rng = np.random.default_rng(seed)
    n = int(n)
    results: Dict[str, Any] = {
        "calibration_run_id": _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "engine": "ingestion.harness.gate_engine.run_null_calibration",
        "n_null": n,
        "alpha": BONFERRONI_ALPHA,
        "sector_relative": True,
        "combine_method": SECTOR_COMBINE_METHOD,
        "gates_3_4_sector_relative": True,
        "n_months": n_months,
        "n_sectors_used": n_sectors or len(SECTOR_NAMES),
    }
    gate_keys = {int(g): f"gate_{int(g)}" for g in gates if int(g) in (3, 4)}
    for key in gate_keys.values():
        results[key] = {"rejections": 0, "p_values": [], "fpr": None,
                        "wilson_95_ci_low": None, "wilson_95_ci_high": None}

    engine = GateEngine()
    for i in range(n):
        port = null_sector_portfolios(n_months=n_months, n_sectors=n_sectors,
                                      seed=int(rng.integers(0, 2**31 - 1)),
                                      n_tickers_per_sector=8)
        # monthly return series for the full-portfolio arms (equal mix of sectors)
        full: Dict[str, pd.Series] = {}
        for arm in ("A", "B", "C"):
            cols = [port[s][arm] for s in port]
            full[arm] = pd.concat(cols).groupby(level=0).mean().sort_index()
        ar = dict(full)["C"]
        rd = {
            "arms_returns": full,
            "arm_a_returns": full["A"],
            "arm_b_returns": full["B"],
            "arm_c_returns": full["C"],
            "spy": full["A"],
            "arm_c_weights": _null_weights(months=full["C"].index),
            "sector_returns": port,
            "sector_provenance": "calibration:null_sector_portfolios",
        }
        res = engine.run_all_gates(rd)
        for g, key in gate_keys.items():
            out = res.details.get("welch_c_vs_a" if g == 3 else "welch_c_vs_b", {})
            p = out.get("p_value")
            if p is None or out.get("reason") == "insufficient_sector_data":
                continue  # not a rejection — counting only decisive tests
            results[key]["p_values"].append(float(p))
            if float(p) < BONFERRONI_ALPHA and out.get("passed"):
                results[key]["rejections"] += 1

    # Wilson 95% CI on the binomial FPR
    for key in gate_keys.values():
        rej = results[key]["rejections"]
        ps = results[key]["p_values"]
        fpr = rej / n if n else 0.0
        results[key]["fpr"] = round(fpr, 6)
        if n:
            z = 1.959963984540054
            denom = 1 + z * z / n
            centre = (fpr + z * z / (2 * n)) / denom
            half = z * np.sqrt(fpr * (1 - fpr) / n + z * z / (4 * n * n)) / denom
            results[key]["wilson_95_ci_low"] = round(max(0.0, centre - half), 6)
            results[key]["wilson_95_ci_high"] = round(min(1.0, centre + half), 6)
        if ps:
            ps_sorted = sorted(ps)
            results[key]["critical_value_p05"] = round(
                float(np.percentile(ps_sorted, 5)), 6)
            results[key]["p95_p_value"] = round(float(np.percentile(ps_sorted, 95)), 6)
        results[key]["n_tests"] = len(ps)

    results["bonferroni_alpha"] = BONFERRONI_ALPHA
    results["decision"] = (
        "calibrated"
        if all(r["wilson_95_ci_high"] is not None and r["wilson_95_ci_high"] <= BONFERRONI_ALPHA
               for r in (results.get(k) for k in gate_keys.values()))
        else "needs_review"
    )
    return results


def _null_weights(months: pd.DatetimeIndex, n_tickers: int = 12) -> Dict[Any, Dict[str, float]]:
    """Flat equal weights at each month-end (null calibration harness input)."""
    tickers = [f"N{i:02d}" for i in range(n_tickers)]
    return {d: {t: 1.0 / n_tickers for t in tickers} for d in months}


def _calibration_to_json(results: Dict[str, Any],
                         path: Union[str, Path] = "data/validation/gate_calibration_report.json") -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(results, indent=2, default=str, sort_keys=True)
    p.write_text(payload, encoding="utf-8")
    return p


# ----------------------------------------------------------------------------
# Test harness (synthetic — engineered alpha must pass, pure noise must fail)
# ----------------------------------------------------------------------------

def _monthly_dates(n: int, start: str = "2018-01-01") -> pd.DatetimeIndex:
    return pd.date_range(start=start, periods=n, freq="ME")


def _sectorize_arms(arm_a: pd.Series, arm_b: pd.Series, arm_c: pd.Series,
                    seed: int = 0, n_sectors: int = 7) -> Dict[str, Dict[str, pd.Series]]:
    """
    Turns flat arm returns into per-sector series for the sector-relative
    gates: each sector holds beta*arm + shared sector factor (which preserves
    the arm MEANS per sector, so C - X differences carry over sector-wise).
    Used by the self-test synthetics so the frozen sector-relative regime
    (config/gates_v2.yaml) is exercised end-to-end.
    """
    rng = np.random.default_rng(seed)
    idx = arm_a.index
    n = len(idx)
    out: Dict[str, Dict[str, pd.Series]] = {}
    for j, s in enumerate(SECTOR_NAMES[:n_sectors]):
        beta = 0.9 + 0.2 * rng.random()
        f_sec = rng.normal(0.0, 0.006, n)
        out[s] = {
            "A": pd.Series(beta * arm_a.to_numpy() + f_sec, index=idx, name=f"{s}.A"),
            "B": pd.Series(beta * arm_b.to_numpy() + f_sec, index=idx, name=f"{s}.B"),
            "C": pd.Series(beta * arm_c.to_numpy() + f_sec
                           + rng.normal(0.0, 0.004, n), index=idx, name=f"{s}.C"),
        }
    return out


def _synthetic_pass() -> Dict[str, Any]:
    """Engineered alpha portfolio: C confidently beats A and B, low dd, low turnover."""
    n = 72
    idx = _monthly_dates(n)
    rng = np.random.default_rng(42)
    spy = pd.Series(0.005 + rng.normal(0, 0.040, n), index=idx)
    arm_a = pd.Series(0.004 + rng.normal(0, 0.035, n), index=idx)
    arm_b = pd.Series(0.004 + rng.normal(0, 0.035, n), index=idx)
    arm_c = pd.Series(0.036 + rng.normal(0, 0.025, n), index=idx)

    rebs = pd.date_range("2018-01-01", periods=n, freq="ME")
    tickers = [f"T{i:02d}" for i in range(12)]
    w = {}
    prev = np.zeros(len(tickers))
    for i, d in enumerate(rebs):
        drift = 0.012 * np.sin(i / 6.0)
        cur = np.full(len(tickers), 1 / len(tickers))
        cur += drift * np.linspace(-1, 1, len(tickers))
        cur = np.clip(cur, 0, None)
        cur /= cur.sum()
        target = prev + 0.15 * (cur - prev)
        target /= target.sum()
        w[d] = {t: float(v) for t, v in zip(tickers, target)}
        prev = target

    return {
        "arms_returns": {"A": arm_a, "B": arm_b, "C": arm_c},
        "arm_a_returns": arm_a,
        "arm_b_returns": arm_b,
        "arm_c_returns": arm_c,
        "spy": spy,
        "arm_c_weights": w,
        "sector_returns": _sectorize_arms(arm_a, arm_b, arm_c, seed=1234),
        "sector_provenance": "synthetic:self-test",
    }


def _synthetic_null() -> Dict[str, Any]:
    """Pure Gaussian noise — every statistical/performance gate must fail."""
    n = 72
    idx = _monthly_dates(n)
    rng = np.random.default_rng(7)
    spy = pd.Series(rng.normal(0.004, 0.04, n), index=idx)
    arm_a = pd.Series(rng.normal(0.001, 0.04, n), index=idx)
    arm_b = pd.Series(rng.normal(0.001, 0.04, n), index=idx)
    arm_c = pd.Series(rng.normal(0.001, 0.04, n), index=idx)

    rebs = pd.date_range("2018-01-01", periods=n, freq="ME")
    tickers = [f"T{i:02d}" for i in range(12)]
    w = {}
    prev = np.full(len(tickers), 1 / len(tickers))
    for d in rebs:
        cur = np.full(len(tickers), 1 / len(tickers))
        cur += rng.normal(0, 0.02, len(tickers))
        cur = np.clip(cur, 0, None)
        cur /= cur.sum()
        target = prev + 0.5 * (cur - prev)
        target /= target.sum()
        w[d] = {t: float(v) for t, v in zip(tickers, target)}
        prev = target

    return {
        "arms_returns": {"A": arm_a, "B": arm_b, "C": arm_c},
        "arm_c_returns": arm_c,
        "spy": spy,
        "arm_c_weights": w,
        "sector_returns": _sectorize_arms(arm_a, arm_b, arm_c, seed=4321),
        "sector_provenance": "synthetic:self-test",
    }


def run_tests() -> int:
    import os
    print("GateEngine self-test")
    print("=" * 60)

    # 0) Alpha hardcoded
    assert BONFERRONI_ALPHA == 0.05 / 7, f"alpha must be 0.05/7, got {BONFERRONI_ALPHA}"
    assert abs(BONFERRONI_ALPHA - 0.007142857142857143) < 1e-12
    print(f"[OK] BONFERRONI_ALPHA hardcoded = {BONFERRONI_ALPHA:0.10f} (0.05/7)")
    for g in GATE_REGISTRY:
        assert g["name"] in THRESHOLDS or g["family"] == "statistical", g["name"]
    print(f"[OK] 10 gates registered in order: {[g['name'] for g in GATE_REGISTRY]}")

    engine = GateEngine()

    # 1) Engineered alpha must pass
    pos = engine.run_all_gates(_synthetic_pass())
    print(f"\nPositive synthetic: passed={pos.passed} ({pos.n_passed}/{pos.n_gates})")
    report = json.loads(pos.to_json())
    for name, o in report["gates"].items():
        if name == "meta":
            continue
        mark = "PASS" if o.get("passed") else "FAIL"
        val = o.get("value")
        print(f"  {mark:4s} {name:24s} value={val} threshold={o.get('threshold')}"
              + (f" p={o.get('p_value'):.6f}" if o.get("p_value") is not None else ""))
    assert pos.passed, f"engineered alpha failed gates: {[k for k,v in pos.details.items() if not v.get('passed')]}"

    # 2) Pure noise must fail
    neg = engine.run_all_gates(_synthetic_null())
    print(f"\nNull synthetic:   passed={neg.passed} ({neg.n_passed}/{neg.n_gates})")
    assert not neg.passed, "pure noise unexpectedly passed all gates"

    # Specific sanity assertions on the positive case
    d = pos.details
    assert d["oos_sharpe"]["value"] > 1.0
    assert d["probabilistic_sharpe"]["value"] > 0.95
    assert d["calmar_ratio"]["value"] > 2.0
    assert d["turnover_drag"]["value"] < 0.30
    assert d["max_drawdown"]["value"] > -0.20
    assert d["information_ratio"]["value"] > 0.3

    # Null case must fail staggered stats and risk gates
    nd = neg.details
    assert nd["welch_c_vs_a"]["value"] >= BONFERRONI_ALPHA or not nd["welch_c_vs_a"].get("passed", False)
    assert not nd["probabilistic_sharpe"].get("passed", True), "noise PSR should not exceed 0.95"
    assert not nd["calmar_ratio"].get("passed", True), "noise Calmar should not exceed 2.0"

    # 3) JSON report write + re-read
    out_dir = Path("data/factors/gate_reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "gate_report_synthetic_test.json"
    pos.to_json(report_path)
    re_read = json.loads(report_path.read_text(encoding="utf-8"))
    assert re_read["n_gates_total"] == 10 and "welch_c_vs_b" in re_read["gates"]
    print(f"\n[OK] JSON gate report written: {report_path}")

    # 4) RS-05 sector-relative gates — isolated temp config (never touches repo config)
    import shutil
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="gates_v2_selftest_"))
    tmp_cfg = tmpdir / "gates_v2.yaml"
    tmp_sha = tmpdir / "gates_v2.yaml.sha256"
    try:
        cfg_frozen, digest = freeze_gates_v2(sector_quantile=True, dual_track=True,
                                             path=tmp_cfg, sha_sidecar=tmp_sha)
        assert cfg_frozen["frozen"] is True and cfg_frozen["sector_relative"] is True
        assert len(digest) == 64
        assert tmp_sha.read_text(encoding="utf-8").strip() == digest
        print(f"[OK] gates_v2 freeze written: {tmp_cfg} sha256={digest[:12]}...")

        # one-way freeze enforced
        try:
            freeze_gates_v2(sector_quantile=True, dual_track=True,
                            path=tmp_cfg, sha_sidecar=tmp_sha)
            raise AssertionError("second freeze should have raised PermissionError")
        except PermissionError:
            print("[OK] one-way freeze enforced (PermissionError on re-freeze)")

        # monkeypatch the module-global config reader so GateEngine sees the
        # frozen temp config. (NOTE: with `python -m`, __main__ IS this module,
        # so patching globals() also patches what GateEngine.run_all_gates sees.)
        _orig_load = load_gates_config
        _orig_frozen = is_gates_frozen

        def _tmp_load(*a, **k):
            return _orig_load(tmp_cfg)

        def _tmp_frozen(*a, **k):
            return _orig_frozen(_orig_load(tmp_cfg))

        g = globals()
        g["load_gates_config"] = _tmp_load
        g["is_gates_frozen"] = _tmp_frozen
        try:
            sr_cfg = load_gates_config(tmp_cfg)
            assert sr_cfg["gates"]["3"]["frozen"] and sr_cfg["gates"]["4"]["frozen"]

            # 4a) engineered alpha WITH per-sector alpha must pass sector-relative
            n_s = 60
            idx_s = _monthly_dates(n_s, start="2020-01-31")
            srng = np.random.default_rng(123)
            port_alpha = {}
            for s, extra in zip(SECTOR_NAMES[:6], [0.012, 0.010, 0.014, 0.009, 0.013, 0.011]):
                f_sec = srng.normal(0.0, 0.010, n_s)
                rows = {}
                for arm, mu in (("A", 0.002), ("B", 0.002), ("C", 0.002 + extra)):
                    rows[arm] = pd.Series(mu + f_sec + srng.normal(0, 0.015, n_s), index=idx_s)
                port_alpha[s] = rows
            rd_sr = {
                "arms_returns": {a: pd.Series(0.0, index=idx_s) for a in ("A", "B", "C")},
                "arm_a_returns": pd.concat([port_alpha[s]["A"] for s in port_alpha]).groupby(level=0).mean(),
                "arm_b_returns": pd.concat([port_alpha[s]["B"] for s in port_alpha]).groupby(level=0).mean(),
                "arm_c_returns": pd.concat([port_alpha[s]["C"] for s in port_alpha]).groupby(level=0).mean(),
                "spy": pd.Series(srng.normal(0.002, 0.03, n_s), index=idx_s),
                "arm_c_weights": _null_weights(idx_s),
                "sector_returns": port_alpha,
                "sector_provenance": "selftest:engineered",
            }
            res_sr = engine.run_all_gates(rd_sr)
            assert res_sr.details["welch_c_vs_a"].get("passed"), \
                f"engineered sector alpha failed gate 3: {res_sr.details['welch_c_vs_a']}"
            assert res_sr.details["welch_c_vs_b"].get("passed"), \
                f"engineered sector alpha failed gate 4: {res_sr.details['welch_c_vs_b']}"
            assert res_sr.details["meta"]["sector_relative"] is True
            # meta.config_sha256 reads the *repo* sidecar path; in self-test the
            # frozen config lives in a tempdir, so it is legitimately None here.
            assert res_sr.details["meta"]["config_sha256"] in (None, digest)
            assert res_sr.details["welch_c_vs_a"]["combine_method"] == "stouffer"
            print("[OK] sector-relative gates 3 & 4 PASS on engineered per-sector alpha")

            # 4b) sector-linked pure noise must NOT pass sector-relative gates
            port_null = null_sector_portfolios(n_months=n_s, n_sectors=6, seed=99)
            rd_null_sr = {
                "arms_returns": {a: pd.Series(0.0, index=idx_s) for a in ("A", "B", "C")},
                "arm_a_returns": pd.concat([port_null[s]["A"] for s in port_null]).groupby(level=0).mean(),
                "arm_b_returns": pd.concat([port_null[s]["B"] for s in port_null]).groupby(level=0).mean(),
                "arm_c_returns": pd.concat([port_null[s]["C"] for s in port_null]).groupby(level=0).mean(),
                "spy": pd.Series(srng.normal(0.002, 0.03, n_s), index=idx_s),
                "arm_c_weights": _null_weights(idx_s),
                "sector_returns": port_null,
                "sector_provenance": "selftest:null",
            }
            res_null_sr = engine.run_all_gates(rd_null_sr)
            assert not res_null_sr.details["welch_c_vs_a"].get("passed"), \
                "sector-null noise unexpectedly passed gate 3"
            assert not res_null_sr.details["welch_c_vs_b"].get("passed"), \
                "sector-null noise unexpectedly passed gate 4"
            print("[OK] sector-relative gates 3 & 4 FAIL on sector-linked noise (H0)")

            # 4c) Stouffer math: two gates each p=0.01 -> Z=3.2897, p_comb~0.0005
            z_c, p_c = stouffer_combine([0.01, 0.01])
            assert abs(z_c - 3.289707) < 1e-3, f"stouffer z={z_c}"
            assert 0.0004 < p_c < 0.0006, f"stouffer p_comb={p_c}"
            print(f"[OK] Stouffer combine: two p=0.01 -> Z={z_c:.4f}, p_comb={p_c:.6f}")
        finally:
            g["load_gates_config"] = _orig_load
            g["is_gates_frozen"] = _orig_frozen
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 5) quick calibration smoke (small n, CI assertion)
    cal = run_null_calibration(n=200, gates=[3, 4], n_months=36, n_sectors=6, seed=5)
    assert cal["gates_3_4_sector_relative"] is True
    for key in ("gate_3", "gate_4"):
        g = cal[key]
        assert g["fpr"] is not None
        low, high = g["wilson_95_ci_low"], g["wilson_95_ci_high"]
        assert low is not None and high is not None and 0.0 <= low <= high <= 1.0
        print(f"[OK] null calibration n=200: gate {key} fpr={g['fpr']:.4f} "
              f"wilson95=[{low:.4f},{high:.4f}]")
    print(f"\n[OK] null calibration decision: {cal['decision']}")

    print("\nAll GateEngine tests passed.")
    return 0


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Gate Engine — 10 pre-registered gates, Bonferroni alpha=0.0071")
    parser.add_argument("--test", action="store_true", help="Run synthetic test suite")
    parser.add_argument("--re-register-gates", type=str, default=None, metavar="3,4",
                        help="RS-05: freeze gates 3 & 4 as sector-relative (comma list)")
    parser.add_argument("--sector-quantile", action="store_true",
                        help="Re-registration includes sector-quantile Arm A screen (dual-track)")
    parser.add_argument("--dual-track", action="store_true",
                        help="Re-registration includes dual-track Arm A (Mainstream + Achievers)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite an already-frozen gates_v2.yaml (CEO ruling required)")
    args = parser.parse_args()

    if args.re_register_gates is not None:
        _cli_re_register_gates(args)
        return

    if args.test:
        raise SystemExit(run_tests())

    parser.print_help()


def _cli_re_register_gates(args: Any) -> None:
    """Writes the frozen sector-relative gate config + sha256 sidecar (RS-05)."""
    try:
        requested = {int(x) for x in str(args.re_register_gates).split(",") if x.strip()}
    except ValueError:
        raise SystemExit("--re-register-gates must be a comma list of integers (e.g. 3,4)")
    if not requested or not requested.issubset({3, 4}):
        raise SystemExit("RS-05 re-registration is defined for gates 3,4 only.")
    print(f"RS-05 re-registration of gates {sorted(requested)}")
    print(f"  sector_quantile (Arm A screen percentile): {args.sector_quantile}")
    print(f"  dual_track (Arm A Mainstream+Achievers):   {args.dual_track}")
    try:
        cfg, digest = freeze_gates_v2(sector_quantile=bool(args.sector_quantile),
                                      dual_track=bool(args.dual_track),
                                      force=bool(args.force))
    except PermissionError as exc:
        raise SystemExit(f"REFUSED: {exc}")
    print(f"Frozen config written: {GATES_V2_CONFIG}")
    print(f"sha256 sidecar:        {GATES_V2_SHA256_SIDECAR}")
    print(f"sha256:                {digest}")
    print("Next (per dispatch verification plan):")
    print("  python -m validation.gate_calibration --run --n 10000 --gates 3,4")


if __name__ == "__main__":
    main()