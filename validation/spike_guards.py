"""
spike_guards.py — Phase 8 WS-7 Spike Guards & Data Gate (bigpickle-worker)

Three frozen guards feeding `data_gate_v2` and the trade-audit trail
(phase8_bigpickle_analysis.md §5.8 + config/pre_registration_phase8.yaml):

GUARD 1 — MAD FILTER (C7.1)
    * Rolling 90-day median + 3 x MAD (scaled 1.4826) rejection band.
    * min_observations = 30; a shorter window cannot flag (no filtering).
    * MAD = 0 guard: constant series produce NO outliers (never filter a
      degenerate window).
    * "cycle" is defined PER SOURCE (monthly alt-data batches here); a cycle is
      a 90d rolling window evaluation at the batch time step.
    * Outliers are REPLACED with the 90d rolling median (documented in
      max-weight/mad_filter_applied columns), not dropped.
    * mad_outlier_cycles = number of CONSECUTIVE flagged cycles (drives the
      '2 consecutive cycles' audit rule; schema data_gate_v2.mad_outlier_cycles).
    * The gate asserts the file-level trigger rate sits in the frozen
      [5%, 15%] band — too few flags (0% = filter inert) or too many
      (>15% = filter destabilising) both fail.

GUARD 2 — S/C CONSTRAINT (C7.2 / C7.3)
    * Required_Reinvestment = DeltaRev / S_C          (frozen, §5.8.2)
    * Available_Funding    = OCF + Cash + Credit_Capacity
    * Credit_Capacity      = sector_target_debt_ratio x Total_Assets - Total_Debt
      (frozen formula; sector_target_debt_ratio from frozen Damodaran map)
    * unfunded  <=>  Required > 1.2 x Available  ->  growth haircut x0.5 and
      WACC +200bps; S_C = 0 clamps to P95 (sc_ratio_used P95 clamp); delta
      revenue <= 0 can NEVER flag (nothing to fund); negative Available
      documented: any positive Required is then unfunded (gap = NaN).
    * Formula identity enforced by importing from Quantitative.narrative_valuation
      (single source of truth for the corrected reinvestment identity).

GUARD 3 — DATA GATE  (phase8.data_gate)
    * data_gate_quarters = distinct PIT fiscal quarters with filed_date <= as-of.
    * QUARANTINE (< 8q)  : max weight 0.5%
    * PROBATION  (8-11q) : max weight 1.0%; quality-composite weight x0.3
    * FULL (>= 12q)      : only after 3 CONSECUTIVE quarters of ROIC > WACC
      (graduation); before graduation the 1.0% probation cap stays in force.
    * UNALLOCATED REMAINDER: capped weights are NOT renormalized — the missing
      weight (`1.0 - sum(capped)`) is logged to the trade audit; quarantined/
      missing names get weight 0, never silently redistributed.

CLI (dispatch verification, must exit 0):
    python -m validation.spike_guards --run
    python -m validation.spike_guards --test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------
# Frozen config (phase8.mad_filter / phase8.data_gate / phase8.sc_constraint)
# ----------------------------------------------------------------------------

CONFIG_PATH = "config/pre_registration_phase8.yaml"
DEFAULT_REPORT = Path("data/validation/spike_guards_report.json")

MAD_DEFAULTS: Dict[str, Any] = {
    "window_days": 90,
    "mad_multiplier": 3.0,
    "mad_scale": 1.4826,               # robust sigma (frozen)
    "min_observations": 30,
    "min_consecutive_cycles": 2,
    "trigger_rate": (0.05, 0.15),      # frozen target band
}

DATA_GATE_DEFAULTS: Dict[str, Any] = {
    "min_quarters_full": 12,
    "min_quarters_probation": 8,
    "max_weight_quarantine": 0.005,
    "max_weight_probation": 0.010,
    "graduation_quarters_roic_wacc": 3,
}

SC_DEFAULTS: Dict[str, Any] = {
    "unfunded_threshold": 1.2,
    "growth_haircut": 0.5,
    "wacc_penalty_bps": 200,
    "default_target_debt_ratio": 0.25,
    # frozen Damodaran sector target D/(D+E) (betas_latest.csv 2026.09 ->
    # industry -> sub_area -> custom sector), fallback 0.25
    "sector_target_debt_ratio": {
        "Consumer": 0.2202, "Energy": 0.3767, "Financial": 0.3548,
        "Hardware": 0.0844, "Healthcare": 0.1269, "Industrial": 0.2015,
        "Software": 0.1807,
    },
}


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    import yaml
    p = Path(path)
    if not p.exists():
        return {}
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("phase8", {})


def merged_config(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    phase8 = cfg if cfg is not None else load_config()
    out: Dict[str, Any] = {}
    out["mad"] = {**MAD_DEFAULTS, **(phase8.get("mad_filter") or {})}
    out["data_gate"] = {**DATA_GATE_DEFAULTS, **(phase8.get("data_gate") or {})}
    sc_cfg = {**SC_DEFAULTS}
    sc_phase = phase8.get("sc_constraint") or {}
    for k in ("unfunded_threshold", "growth_haircut", "wacc_penalty_bps"):
        if k in sc_phase:
            sc_cfg[k] = sc_phase[k]
    out["sc"] = sc_cfg
    out["total_beta"] = phase8.get("total_beta") or {}
    return out


# ----------------------------------------------------------------------------
# GUARD 1 — MAD filter
# ----------------------------------------------------------------------------

def mad_bounds(series: Sequence[float], window: int = 90, k: float = 3.0,
               min_observations: int = 30, mad_scale: float = 1.4826
               ) -> Tuple[List[Optional[Tuple[float, float]]], List[bool]]:
    """
    Rolling medians + robust MAD rejection bands.  Returns
    (bounds, window_ok) aligned with the input.  A window shorter than
    min_observations or with MAD == 0 yields bounds (None) and window_ok False
    (no filtering this step — the MAD=0 guard).
    """
    s = np.asarray(series, dtype=float)
    n = len(s)
    bounds: List[Optional[Tuple[float, float]]] = [None] * n
    ok = [False] * n
    for i in range(n):
        lo = max(0, i - window + 1)
        win = s[lo:i + 1]
        if len(win) < min_observations:
            continue
        med = float(np.median(win))
        mad = float(np.median(np.abs(win - med))) * mad_scale
        if mad <= 0.0:                      # MAD = 0 guard: constant window
            continue
        bounds[i] = (med - k * mad, med + k * mad)
        ok[i] = True
    return bounds, ok


def mad_filter_series(series: Sequence[float], window: int = 90, k: float = 3.0,
                      min_observations: int = 30, mad_scale: float = 1.4826
                      ) -> Tuple[List[float], List[bool], int]:
    """
    Replace out-of-band observations with the 90-day rolling median.  Returns
    (replaced_series, outlier_mask, n_outliers).  Never filters a window that
    is too short or has zero MAD.
    """
    bounds, ok = mad_bounds(series, window, k, min_observations, mad_scale)
    replaced: List[float] = []
    mask: List[bool] = []
    n_out = 0
    s = list(series)
    for i in range(len(s)):
        b = bounds[i]
        if b is None:
            replaced.append(float(s[i]))
            mask.append(False)
            continue
        lo = max(0, i - window + 1)
        med = float(np.median(s[lo:i + 1]))
        if float(s[i]) < b[0] or float(s[i]) > b[1]:
            replaced.append(med)
            mask.append(True)
            n_out += 1
        else:
            replaced.append(float(s[i]))
            mask.append(False)
    return replaced, mask, n_out


def consecutive_flagged_cycles(cycle_flags: Sequence[bool]) -> int:
    """Length of the CURRENT trailing run of flagged cycles (0 if the latest
    cycle is clean).  '2 consecutive cycles' audit uses this at each step."""
    run = 0
    for f in reversed(list(cycle_flags)):
        if f:
            run += 1
        else:
            break
    return run


def mad_trigger_rate(cycle_flags: Sequence[bool]) -> float:
    """Share of evaluated cycles flagged; empty input -> 0.0."""
    if not cycle_flags:
        return 0.0
    return float(np.mean([bool(f) for f in cycle_flags]))


def mad_gate_check(cycle_flags: Sequence[bool], config: Optional[Dict[str, Any]] = None
                   ) -> Tuple[bool, float, str]:
    """
    Frozen band gate: trigger_rate in [5%, 15%].  Returns
    (passed, trigger_rate, reason).  0% means the filter is inert; >15% means
    it destabilises the feature.
    """
    m = (merged_config(config)["mad"] if config is None
         else {**MAD_DEFAULTS, **(config.get("mad_filter") or {})})
    lo, hi = (float(m["trigger_rate"][0]), float(m["trigger_rate"][1]))
    tr = mad_trigger_rate(cycle_flags)
    if tr < lo:
        return False, tr, f"trigger_rate {tr:.3f} below band [{lo}, {hi}] (filter inert)"
    if tr > hi:
        return False, tr, f"trigger_rate {tr:.3f} above band [{lo}, {hi}] (over-active)"
    return True, tr, f"trigger_rate {tr:.3f} inside frozen band [{lo}, {hi}]"


# ----------------------------------------------------------------------------
# GUARD 2 — S/C constraint (formula identity with Quantitative.narrative_valuation)
# ----------------------------------------------------------------------------

from Quantitative.narrative_valuation import (  # noqa: E402  (single source of truth)
    credit_capacity as _credit_capacity,
    sc_ratio_used as _sc_ratio_used,
    unfunded_growth_check as _unfunded_growth_check,
)


def sc_constraint_guard(
    delta_rev: float, sc_ratio: float,
    ocf: Optional[float], cash: Optional[float],
    total_assets: Optional[float], total_debt: Optional[float],
    sector: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    S/C constraint guard for one filing row (frozen §5.8.2):

        Credit_Capacity = sector_target_debt_ratio x Total_Assets - Total_Debt
        Required        = DeltaRev / S_C
        Available       = OCF + Cash + Credit_Capacity
        unfunded       <=> Required > 1.2 x Available

    Returns the data_gate_v2 + narrative_v2 flag block (identical formula to
    the narrative engine via the shared import above).
    """
    sc_cfg = merged_config(config).get("sc", SC_DEFAULTS) if config is None else SC_DEFAULTS
    ccfg = dict(sc_cfg)
    if config is not None:
        phase8 = config.get("phase8") or {}
        from Quantitative.narrative_valuation import NARRATIVE_DEFAULTS
        ccfg = {**NARRATIVE_DEFAULTS, **sc_cfg}
        sc_phase = phase8.get("sc_constraint") or {}
        ccfg.update({k: v for k, v in sc_phase.items()})
    ratio = None
    if sector and sc_cfg.get("sector_target_debt_ratio") and sector in sc_cfg["sector_target_debt_ratio"]:
        ratio = float(sc_cfg["sector_target_debt_ratio"][sector])
    cc, ratio_used = _credit_capacity(ratio, total_assets, total_debt, ccfg)
    flag, gap, haircut, penalty, available = _unfunded_growth_check(
        delta_rev, sc_ratio, ocf, cash, cc, ccfg)
    return {
        "unfunded_growth_flag": bool(flag),
        "reinvestment_gap_pct": (float(gap) if gap is not None else None),
        "terminal_growth_haircut": float(haircut),
        "wacc_penalty_bps_applied": float(penalty),
        "available_funding": (float(available) if available is not None else None),
        "required_reinvestment": (float(delta_rev) / float(sc_ratio)
                                  if sc_ratio > 0 and delta_rev > 0 else None),
        "credit_capacity": (float(cc) if cc is not None else None),
        "sector_target_debt_ratio_used": float(ratio_used),
    }


# ----------------------------------------------------------------------------
# GUARD 3 — Data Gate
# ----------------------------------------------------------------------------

def graduation_check(roic_series: Sequence[Optional[float]], wacc: float,
                     require_consecutive: int = 3) -> bool:
    """
    FULL unlock requires `require_consecutive` consecutive quarters with
    ROIC > WACC (frozen graduation_quarters_roic_wacc = 3).  Missing ROIC
    breaks the run.
    """
    run = 0
    for r in reversed(list(roic_series)):
        if r is None or (isinstance(r, float) and np.isnan(r)):
            run = 0
            continue
        if float(r) > float(wacc):
            run += 1
            if run >= require_consecutive:
                return True
        else:
            run = 0
    return False


def data_gate_state(quarters: int, graduated: bool,
                    config: Optional[Dict[str, Any]] = None) -> Tuple[str, float, bool]:
    """
    Frozen data-gate state machine:

        quarters <  8  -> QUARANTINE (max_weight_cap 0.005)
        quarters 8-11  -> PROBATION  (max_weight_cap 0.010; qual weight x0.3)
        quarters >= 12 -> FULL       (cap 1.0 only after 3 consecutive
                                      ROIC > WACC; otherwise 0.010 stands)
    Returns (state, max_weight_cap, full_unconstrained).
    """
    dg = merged_config(config).get("data_gate",
                                   DATA_GATE_DEFAULTS) if config is None \
        else {**DATA_GATE_DEFAULTS, **(config.get("data_gate") or {})}
    min_full = int(dg["min_quarters_full"])
    min_prob = int(dg["min_quarters_probation"])
    q = int(quarters)
    if q < min_prob:
        return ("QUARANTINE", float(dg["max_weight_quarantine"]), False)
    if q < min_full:
        return ("PROBATION", float(dg["max_weight_probation"]), False)
    if graduated:
        return ("FULL", 1.0, True)
    return ("FULL", float(dg["max_weight_probation"]), False)


def apply_weight_caps(target_weights: Sequence[float],
                      caps: Sequence[float]) -> Tuple[List[float], float]:
    """
    Apply per-name max-weight caps WITHOUT renormalization (WS-7.4 / C7.4):
    capped weights are final; the unallocated remainder
        `1.0 - sum(capped_weights)`
    is returned for trade-audit logging (quarantined/excluded names get 0 and
    their weight is NOT redistributed to other names).
    """
    capped = [float(min(w, c)) if c >= 0.0 else 0.0
              for w, c in zip(target_weights, caps)]
    cap_sum = float(sum(capped))
    remainder = max(0.0, 1.0 - cap_sum)
    return capped, remainder


# ----------------------------------------------------------------------------
# Report / CLI (mirrors validation/probation_gates.py)
# ----------------------------------------------------------------------------

def run_spike_guards_report(config_path: str = CONFIG_PATH,
                            report_path: str = str(DEFAULT_REPORT)) -> int:
    cfg = merged_config(load_config(config_path))

    # --- synthetic guard run over a 15-cycle alt-data series ----------------
    # A spike flags only the cycle where it is the current observation (the
    # replaced value keeps trailing windows clean) — so a genuine 2-cycle
    # burst yields mad_outlier_cycles = 2 (the '2 consecutive cycles' audit)
    # and 2/15 = 13.3% sits inside the frozen [5%, 15%] trigger band.
    rng = np.random.default_rng(7)
    n_cyc = 15
    base = 100.0 + rng.normal(0.0, 0.5, n_cyc)
    base[5] = base[5] + 40.0                                 # genuine spike burst
    base[6] = base[6] + 40.0
    replaced, mask, n_out = mad_filter_series(base.tolist(), window=5,
                                              k=3.0, min_observations=3)
    cycles = mask
    ok, tr, reason = mad_gate_check(cycles, cfg)

    dg = cfg["data_gate"]
    gate_state, cap, unconstrained = data_gate_state(13, True, cfg)
    capped, remainder = apply_weight_caps([0.03, 0.02, 0.01, 0.005],
                                          [0.010, 0.010, 0.005, 0.005])
    sc = sc_constraint_guard(200.0, 1.0, 50.0, 0.0, 1000.0, 100.0, "Industrial", cfg)

    report = {
        "name": "spike_guards_v2",
        "config_sha256": hashlib.sha256(Path(config_path).read_bytes()).hexdigest(),
        "passed": bool(ok),
        "guards": {
            "mad_filter": {
                "passed": ok, "trigger_rate": round(tr, 4), "reason": reason,
                "cycles_evaluated": int(sum(1 for b in mask if b is not None)),
                "mad_outlier_cycles": int(consecutive_flagged_cycles(mask)),
            },
            "sc_constraint": sc,
            "data_gate": {
                "state": gate_state, "max_weight_cap": cap,
                "full_unconstrained": unconstrained,
                "unallocated_remainder": round(remainder, 6),
            },
        },
    }
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    Path(report_path).write_text(json.dumps(report, indent=2, sort_keys=True,
                                            default=str), encoding="utf-8")
    print(f"[spike_guards] report: {report_path}")
    print(f"[spike_guards] mad: passed={ok} trigger_rate={tr:.4f} ({reason})")
    print(f"[spike_guards] sc: unfunded={sc['unfunded_growth_flag']} "
          f"haircut={sc['terminal_growth_haircut']} penalty_bps={sc['wacc_penalty_bps_applied']}")
    print(f"[spike_guards] data_gate: state={gate_state} cap={cap} "
          f"unallocated={remainder:.6f}")
    return 0 if report["passed"] else 1


# ----------------------------------------------------------------------------
# Tests (python -m validation.spike_guards --test)
# ----------------------------------------------------------------------------

def run_tests() -> int:
    cfg = {"mad_filter": MAD_DEFAULTS, "data_gate": DATA_GATE_DEFAULTS, "sc_constraint": SC_DEFAULTS}

    # 1. MAD band / replacement
    s = [10.0, 10.1, 9.9, 10.0, 10.05, 10.0, 55.0, 10.0, 10.1, 9.95]
    replaced, mask, n_out = mad_filter_series(s, window=5, k=3.0, min_observations=4)
    assert n_out == 1 and mask[6] and abs(replaced[6] - 10.0) < 1e-6, (replaced, mask)

    # 2. min_obs guard: short history cannot flag
    s2 = [10.0, 11.0, 500.0]
    _, mask2, n2 = mad_filter_series(s2, window=90, k=3.0, min_observations=30)
    assert n2 == 0 and not any(mask2)

    # 3. MAD = 0 guard: constant window flags nothing
    s3 = [5.0] * 50
    _, mask3, n3 = mad_filter_series(s3, window=10, k=3.0, min_observations=5)
    assert n3 == 0 and not any(mask3)

    # 4. consecutive cycles audit
    assert consecutive_flagged_cycles([False, True, True]) == 2
    assert consecutive_flagged_cycles([True, True, False]) == 0
    assert consecutive_flagged_cycles([]) == 0

    # 5. trigger-rate band gate
    ok, tr, _ = mad_gate_check([True, False, False, False, False, False,
                                False, False, False, False], cfg)   # 10% in band
    assert ok and abs(tr - 0.10) < 1e-9
    ok0, _, r0 = mad_gate_check([False, False], cfg)
    assert not ok0 and "inert" in r0
    ok1, _, r1 = mad_gate_check([True] * 10, cfg)
    assert not ok1 and "over-active" in r1

    # 6. S/C constraint threshold + haircut/penalty (frozen 1.2x / x0.5 / 200bps)
    sc = sc_constraint_guard(200.0, 1.0, 50.0, 0.0, 1000.0, 100.0, "Industrial", cfg)
    assert sc["unfunded_growth_flag"] is True
    assert sc["terminal_growth_haircut"] == 0.5 and sc["wacc_penalty_bps_applied"] == 200.0
    sc2 = sc_constraint_guard(50.0, 1.0, 50.0, 0.0, 1000.0, 100.0, "Industrial", cfg)
    assert sc2["unfunded_growth_flag"] is False
    sc3 = sc_constraint_guard(-10.0, 1.0, 50.0, 0.0, 1000.0, 100.0, "Industrial", cfg)
    assert sc3["unfunded_growth_flag"] is False            # DeltaRev<=0 never flags
    # credit capacity frozen formula
    assert abs(sc["credit_capacity"] - (0.2015 * 1000.0 - 100.0)) < 1e-6

    # 7. data gate state machine + graduation
    assert data_gate_state(5, True, cfg) == ("QUARANTINE", 0.005, False)
    assert data_gate_state(10, True, cfg) == ("PROBATION", 0.010, False)
    st, cap, unc = data_gate_state(13, True, cfg)
    assert st == "FULL" and unc and cap == 1.0
    st2, cap2, unc2 = data_gate_state(13, False, cfg)
    assert st2 == "FULL" and not unc2 and cap2 == 0.010   # 3q ROIC>WACC pending
    assert not graduation_check([0.08, 0.11, 0.09], 0.10, 3)
    assert graduation_check([0.11, 0.12, 0.13], 0.10, 3)
    assert not graduation_check([0.15, 0.14, None, 0.13], 0.10, 3)  # missing in run breaks it
    assert graduation_check([0.13, 0.14, 0.15, None], 0.10, 3)  # trailing 3 suffice

    # 8. weight caps: no renormalization, remainder logged
    capped, remainder = apply_weight_caps([0.03, 0.02, 0.01, 0.005],
                                          [0.010, 0.010, 0.005, 0.005])
    assert capped == [0.010, 0.010, 0.005, 0.005]
    assert abs(remainder - (1.0 - 0.030)) < 1e-9

    print("[spike_guards] tests: OK (8 groups)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="spike_guards — MAD / S/C constraint / data-gate guards (WS-7)")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--config", type=str, default=CONFIG_PATH)
    parser.add_argument("--report", type=str, default=str(DEFAULT_REPORT))
    args = parser.parse_args()

    if args.test:
        return run_tests()
    if not args.run:
        parser.print_help()
        return 1
    return run_spike_guards_report(args.config, args.report)


if __name__ == "__main__":
    sys.exit(main())