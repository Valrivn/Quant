"""
probation_gates.py — Probationary Pass-Rate Validation (Phase 3, supersedes RS-29)
Owner: bigpickle-worker | Phase 8 Validation Gates (dispatch 2026-09-23)

Validates the probationary-queue implementation against fold artifacts under
the FROZEN Phase-3 rule set (config/pre_registration_phase8.yaml, CEO Ruling
B-20260920-001 / Ruling #2):

    phase8.backtest.probation_pass_band: [0.40, 0.60]   # CEO Ruling #2
    phase8.backtest.walkforward_folds: 4
    phase8.cooldown.probation_ramp_quarters: 4          # 25/50/75/100% ramp
    phase8.trade_audit.stage_gates[5] = PROBATION_DATA  # C2.4

What changed vs the RS-29 gate (validation/probation_gates.py v1):
  * one-sided "pass_rate > 0.40" test          -> two-sided BAND [0.40, 0.60]
      (below 0.40 the queue adds no value; above 0.60 it is not selective —
      it front-runs / rubber-stamps the naive bar).
  * min_triggers 50                            -> min_triggers 400 (CLI default)
  * single stage-2 asymmetric_entry AND gate   -> WEIGHTED-OR decision rule
      (the brief: "weighted OR logic (not 3-gate AND)" — a candidate clears the
      queue when the weighted union of its gate checks >= decision threshold).
  * no capital-ramp check                      -> capital ramp validated:
      probation names may hold at most 25/50/75/100% of their allowed cap in
      ramp quarters 1/2/3/4+ (cooldown.probation_ramp_quarters = 4).

Decision rule (weighted OR, frozen defaults)
--------------------------------------------
    score = sum(weight_i * check_i)   for all i in evaluable checks
    queue_decision = score >= decision_threshold (default 0.5)
    weights default: {asymmetric_entry: 0.4, intrinsic_value: 0.4,
                      disciplined_exit: 0.2}   (CLI-overridable, must sum ~1)

    trigger        = stage-2 asymmetric_entry PASSED row (per (ticker, date))
    checks         = the other stage gates observable at decision time for the
                     same (ticker, date): stage-1 intrinsic_value pass and a
                     stage-3 disciplined_exit that occurred within the
                     [10, 25] day conversion window.  Missing checks score 0
                     (conservative — the OR only adds evidence that exists).
    or_qualified   = triggers with score >= decision_threshold
    conversion     = a BUY in trades.parquet for an or_qualified (ticker, date)
                     within [10, 25] calendar days
    pass_rate      = conversions / len(or_qualified)

Capital ramp (validated when data-gate state is available)
----------------------------------------------------------
    ramp_pct(q) = {1: 0.25, 2: 0.50, 3: 0.75, q>=4: 1.00}
    position weight <= ramp_pct(quarters_on_ramp) * allowed_cap
    Requires the valuation panel's data_gate_v2 columns joined to positions;
    SKIP (not silently pass) when the columns are absent.

CLI (dispatch verification, must exit 0):
    python -m validation.probation_gates --run --pass-rate-min 0.40 \
        --pass-rate-max 0.60 --min-triggers 400
    python -m validation.probation_gates --test
Exit codes: 0 = PASS/SKIP, 1 = FAIL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None

FOLD_CFG_PATH = "config/pre_registration_v3.yaml"
PHASE8_CFG_PATH = "config/pre_registration_phase8.yaml"
FOLD_ROOT = "data/backtests/20260919-hybrid_fractal_v3-fold{k}"
DEFAULT_REPORT = Path("data/validation/gates/probation_gates_report.json")

ALPHA = 0.05
MIN_WIN, MAX_WIN = 10, 25          # conversion window [days]
RAMPS: Dict[int, float] = {1: 0.25, 2: 0.50, 3: 0.75, 4: 1.00}
DEFAULT_WEIGHTS: Dict[str, float] = {
    "asymmetric_entry": 0.4, "intrinsic_value": 0.4, "disciplined_exit": 0.2,
}
DECISION_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# Config loaders
# ---------------------------------------------------------------------------

def load_yaml(path: str) -> Dict[str, Any]:
    import yaml
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def frozen_band(cfg: Optional[Dict[str, Any]] = None) -> Tuple[float, float]:
    phase8 = cfg if cfg is not None else (load_yaml(PHASE8_CFG_PATH).get("phase8") or {})
    band = (phase8.get("backtest") or {}).get("probation_pass_band", [0.40, 0.60])
    return float(band[0]), float(band[1])


def frozen_ramp_quarters(cfg: Optional[Dict[str, Any]] = None) -> int:
    phase8 = cfg if cfg is not None else (load_yaml(PHASE8_CFG_PATH).get("phase8") or {})
    return int((phase8.get("cooldown") or {}).get("probation_ramp_quarters", 4))


def fold_count(cfg: Dict[str, Any]) -> int:
    for cand in (cfg.get("walk_forward") or {}, (cfg.get("multifractal") or {})
                 .get("nested_cv") or {}):
        if cand.get("folds"):
            return int(cand["folds"])
    return 4


# ---------------------------------------------------------------------------
# Weighted-OR decision rule
# ---------------------------------------------------------------------------

def weighted_or_score(checks: Dict[str, bool],
                      weights: Optional[Dict[str, float]] = None) -> float:
    """sum(weight_i * check_i) over the checks present in `checks` (missing
    checks are treated as evidence absent -> 0).  Weights missing from the
    supplied map fall back to DEFAULT_WEIGHTS; unknown keys score 0 (never
    invented evidence)."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    score = 0.0
    for k, v in checks.items():
        if v:
            score += float(w.get(k, 0.0))
    return float(score)


def queue_decision(score: float, decision_threshold: float = DECISION_THRESHOLD) -> bool:
    return bool(score >= decision_threshold - 1e-12)


# ---------------------------------------------------------------------------
# Band compliance (two-sided binomial exact)
# ---------------------------------------------------------------------------

def _binom_cdf(k: int, n: int, p: float) -> float:
    """
    P(X <= k) for X ~ Binomial(n, p) via exact summation.

    Sums in log space with a log-sum-exp rescale.  The direct
    `comb(n, i) * p**i * (1-p)**(n-i)` form overflows the float range once
    n gets large (comb(1600, 800) ~ 1e467), which crashed the calibration
    sweep and would equally crash any production trigger count above ~1000.
    """
    k = int(k)
    if k < 0 or p <= 0.0:
        return 0.0
    if k >= n or p >= 1.0:
        return 1.0
    log_p = math.log(p)
    log_q = math.log1p(-p)
    log_n_fact = math.lgamma(n + 1.0)
    terms = [log_n_fact - math.lgamma(i + 1.0) - math.lgamma(n - i + 1.0)
             + i * log_p + (n - i) * log_q
             for i in range(0, k + 1)]
    m = max(terms)
    return float(math.exp(m) * sum(math.exp(t - m) for t in terms))


def band_test(n: int, k: int, band: Tuple[float, float] = (0.40, 0.60),
              min_triggers: int = 400,
              alpha: float = ALPHA) -> Dict[str, Any]:
    """
    Two-sided compliance of an observed pass rate k/n against the frozen band
    (two-sided binomial EXACT — Rule A: the rate must be statistically INSIDE
    the band, not merely touching an edge):

        upper_tail_p = P(X >= k | Bin(n, band_min))  -> small = significantly
                                                        ABOVE the low edge
        lower_tail_p = P(X <= k | Bin(n, band_max))  -> small = significantly
                                                        BELOW the high edge

    passed = n >= min_triggers AND band_min <= k/n <= band_max AND
             upper_tail_p <= alpha AND lower_tail_p <= alpha.

    Anti-drift property (CEO Ruling B-20260920-001): with large n an on-edge
    rate (k/n == 0.40 or == 0.60 exactly) is NOT statistically distinguishable
    from the edge point hypothesis and therefore FAILS — the probation queue
    must not equilibrate at the band edges.  With n < min_triggers the gate
    SKIPs (insufficient evidence, never a silent pass).
    """
    lo, hi = float(band[0]), float(band[1])
    rate = k / n if n else 0.0
    upper_tail = 1.0 - _binom_cdf(max(0, k - 1), n, lo)
    lower_tail = _binom_cdf(k, n, hi)
    insufficient = bool(n < min_triggers)
    in_band = bool(lo <= rate <= hi)
    interior = bool(upper_tail <= alpha and lower_tail <= alpha)
    passed = bool(n >= min_triggers and in_band and interior)
    return {
        "status": "PASS" if passed else ("SKIP" if insufficient else "FAIL"),
        "metric_value": round(rate, 6),
        "band": [lo, hi],
        "min_triggers": min_triggers,
        "triggers": int(n),
        "conversions": int(k),
        "upper_tail_p": round(float(upper_tail), 6),
        "lower_tail_p": round(float(lower_tail), 6),
        "alpha": alpha,
        "passed": passed,
        "reason": (None if n >= min_triggers else "insufficient_triggers"),
    }


# ---------------------------------------------------------------------------
# Capital ramp
# ---------------------------------------------------------------------------

def ramp_pct(quarters_on_ramp: int, ramp_quarters: int = 4) -> float:
    """Frozen ramp schedule: 25/50/75/100% over `ramp_quarters` (default 4)."""
    q = max(1, int(quarters_on_ramp))
    if ramp_quarters <= 1:
        return 1.0
    if q <= ramp_quarters:
        return RAMPS.get(q, 1.0)
    return 1.0


def check_capital_ramp(positions: pd.DataFrame,
                       state_col: str = "data_gate_state",
                       cap_col: str = "max_weight_cap",
                       ramp_col: str = "quarters_on_ramp",
                       weight_col: str = "weight",
                       ramp_quarters: int = 4) -> Dict[str, Any]:
    """
    Every position in a probation/ramp state must respect
        weight <= ramp_pct(quarters_on_ramp) * max_weight_cap.
    FULL names are unconstrained (cap 1.0).  Returns violations; SKIP when the
    ramp metadata columns are not present.
    """
    need = {state_col, cap_col, ramp_col, weight_col}
    if positions.empty or not need.issubset(positions.columns):
        return {"status": "SKIP", "reason": "positions/ramp metadata columns missing"}
    pos = positions.copy()
    pos[cap_col] = pd.to_numeric(pos[cap_col], errors="coerce")
    pos[weight_col] = pd.to_numeric(pos[weight_col], errors="coerce")
    pos[ramp_col] = pd.to_numeric(pos[ramp_col], errors="coerce")
    mask = pos[cap_col].notna() & pos[weight_col].notna() & pos[ramp_col].notna()
    if not mask.any():
        return {"status": "SKIP", "reason": "no rows with ramp metadata"}
    sub = pos[mask]
    allowed = sub[ramp_col].apply(lambda q: ramp_pct(q, ramp_quarters)) * sub[cap_col]
    viol_mask = (sub[weight_col] > allowed + 1e-9).values
    violations = []
    for idx in sub.index[viol_mask]:
        violations.append({
            "ticker": str(sub.at[idx, "ticker"]),
            "weight": float(sub.at[idx, weight_col]),
            "allowed": float(allowed[idx]),
        })
    passed = not violations
    return {
        "status": "PASS" if passed else "FAIL",
        "metric_value": len(violations),
        "threshold": 0,
        "passed": passed,
        "violations": violations,
        "rows_checked": int(len(sub)),
        "ramp_schedule": RAMPS,
        "reason": ("all probation positions respect the 25/50/75/100% ramp"
                   if passed else f"{len(violations)} ramp violations"),
    }


# ---------------------------------------------------------------------------
# Fold loading (same artifact contract as RS-29 v1)
# ---------------------------------------------------------------------------

def duck_read(pattern: str) -> pd.DataFrame:
    if duckdb is None:
        return pd.DataFrame()
    try:
        return duckdb.sql("SELECT * FROM read_parquet(?, filename=true)",
                          [str(pattern)]).df()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def load_folds(config: Dict[str, Any]) -> Dict[str, Dict[str, pd.DataFrame]]:
    n_folds = fold_count(config)
    out: Dict[str, Dict[str, pd.DataFrame]] = {}
    for k in range(1, n_folds + 1):
        root = FOLD_ROOT.format(k=k)
        sg = duck_read(f"{root}/stage_gate_decisions.parquet")
        tr = duck_read(f"{root}/trades.parquet")
        pos = duck_read(f"{root}/positions.parquet")
        out[f"fold{k}"] = {"stage_gates": sg, "trades": tr, "positions": pos}
    return out


def _conv_window_hits(buys: pd.DataFrame, ticker: str, anchor: pd.Timestamp) -> bool:
    if buys.empty:
        return False
    dd = (buys["date"] - anchor).abs()
    hit = (buys["ticker"] == ticker) & (dd >= pd.Timedelta(days=MIN_WIN)) \
        & (dd <= pd.Timedelta(days=MAX_WIN))
    return bool(hit.any())


# ---------------------------------------------------------------------------
# Main gate
# ---------------------------------------------------------------------------

def probationary_pass_rate_gate(config: Dict[str, Any],
                                folds: Dict[str, Dict[str, pd.DataFrame]],
                                pass_rate_min: Optional[float] = None,
                                pass_rate_max: Optional[float] = None,
                                min_triggers: int = 400,
                                weights: Optional[Dict[str, float]] = None,
                                decision_threshold: float = DECISION_THRESHOLD,
                                ramp_quarters: Optional[int] = None,
                                cfg_phase8: Optional[Dict[str, Any]] = None
                                ) -> Dict[str, Any]:
    band_lo, band_hi = frozen_band(cfg_phase8)
    band = (float(pass_rate_min if pass_rate_min is not None else band_lo),
            float(pass_rate_max if pass_rate_max is not None else band_hi))
    rq = ramp_quarters or frozen_ramp_quarters(cfg_phase8)

    tot_trig = tot_qual = tot_conv = 0
    per_fold: Dict[str, Any] = {}
    for fold_name, d in folds.items():
        sg = d["stage_gates"]
        tr = d["trades"]
        pos = d["positions"]
        if sg.empty:
            per_fold[fold_name] = {"note": "no stage_gate_decisions artifact"}
            continue
        sg = sg.copy()
        sg["rebalance_date"] = pd.to_datetime(sg["rebalance_date"], errors="coerce")
        trig = sg[(sg.get("stage") == 2) & (sg["gate_name"] == "asymmetric_entry")
                  & (sg["passed"] == True)]  # noqa: E712
        if trig.empty:
            per_fold[fold_name] = {"triggers": 0, "or_qualified": 0,
                                   "conversions": 0, "note": "no stage-2 triggers"}
            continue
        buys = tr[tr["action"].astype(str).str.upper().isin(("BUY", "B"))][["date", "ticker"]] \
            if "action" in tr.columns and not tr.empty else pd.DataFrame(columns=["date", "ticker"])
        if "date" in buys.columns:
            buys["date"] = pd.to_datetime(buys["date"], errors="coerce")
        # stage-1 intrinsic_value pass keyed by (ticker, rebalance_date) —
        # canonicalize BOTH sides to YYYY-MM-DD strings (str(Timestamp) carries
        # time-of-day, DataFrame.astype(str) does not -> the two must never be
        # compared inconsistently).
        s1 = sg[(sg.get("stage") == 1) & (sg["gate_name"] == "intrinsic_value")
                & (sg["passed"] == True)][["ticker", "rebalance_date"]]  # noqa: E712
        s1 = s1.drop_duplicates().copy()
        s1["ticker"] = s1["ticker"].astype(str)
        s1["rebalance_date"] = pd.to_datetime(
            s1["rebalance_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        key1 = set(map(tuple, s1[["ticker", "rebalance_date"]].values.tolist()))
        # conversions already recorded per trigger
        n_or = 0
        n_conv = 0
        for _, r in trig.iterrows():
            key1_hit = ((str(r["ticker"]),
                         pd.Timestamp(r["rebalance_date"]).strftime("%Y-%m-%d"))
                        in key1)
            checks = {
                "asymmetric_entry": True,          # by construction
                "intrinsic_value": key1_hit,
                "disciplined_exit": False,         # forward evidence, filled below
            }
            # disciplined_exit: a stage-3 pass for the same ticker within window
            if not sg.empty and "disciplined_exit" in set(sg["gate_name"].unique()):
                s3 = sg[(sg["gate_name"] == "disciplined_exit") & (sg["passed"] == True)]  # noqa: E712
                if not s3.empty:
                    dd = (pd.to_datetime(s3["rebalance_date"], errors="coerce")
                          - r["rebalance_date"]).abs()
                    hit = (s3["ticker"] == r["ticker"]) & dd.between(
                        pd.Timedelta(days=MIN_WIN), pd.Timedelta(days=MAX_WIN))
                    checks["disciplined_exit"] = bool(hit.any())
            if weighted_or_score(checks, weights) >= decision_threshold - 1e-12:
                n_or += 1
                if _conv_window_hits(buys, str(r["ticker"]), r["rebalance_date"]):
                    n_conv += 1
        tot_trig += int(len(trig))
        tot_qual += n_or
        tot_conv += n_conv
        per_fold[fold_name] = {
            "triggers": int(len(trig)),
            "or_qualified": n_or,
            "conversions": n_conv,
            "pass_rate": round(n_conv / n_or, 5) if n_or else None,
            "weighted_or_score_used": weights,
        }

    band_res = band_test(tot_qual, tot_conv, band, min_triggers)
    # capital ramp across all folds (validated only when metadata exists)
    pos_frames = [d["positions"] for d in folds.values()
                  if not d["positions"].empty]
    all_pos = pd.concat(pos_frames, ignore_index=True) if pos_frames \
        else pd.DataFrame()
    ramp_res = check_capital_ramp(all_pos, ramp_quarters=rq)

    passed = bool(band_res["status"] == "PASS" and ramp_res["status"] != "FAIL")
    reason = None
    if band_res["reason"]:
        reason = band_res["reason"]
    elif ramp_res["status"] == "FAIL":
        reason = "capital_ramp_violations"

    return {
        "name": "probationary_pass_rate",
        "passed": passed,
        "status": "PASS" if passed else ("SKIP" if band_res["status"] == "SKIP"
                                         and ramp_res["status"] == "SKIP" else "FAIL"),
        "test": "binomial_exact_two_sided_band",
        "band": band,
        "band_min": band[0],
        "band_max": band[1],
        "metric": "pass_rate",
        "metric_value": band_res["metric_value"],
        "min_triggers": min_triggers,
        "alpha": ALPHA,
        "triggers_total": tot_trig,
        "or_qualified_total": tot_qual,
        "conversions_total": tot_conv,
        "upper_tail_p": band_res["upper_tail_p"],
        "lower_tail_p": band_res["lower_tail_p"],
        "weighted_decision": {
            "rule": "weighted_or",
            "weights": {**DEFAULT_WEIGHTS, **(weights or {})},
            "decision_threshold": decision_threshold,
        },
        "capital_ramp": ramp_res,
        "per_fold": per_fold,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Report + CLI
# ---------------------------------------------------------------------------

def write_report(report: Dict[str, Any], report_path: Path) -> None:
    content = json.dumps(report, sort_keys=True, default=str).encode("utf-8")
    gate = report["gates"]["probationary_pass_rate"]
    prov = {
        "source": "validation.probation_gates",
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "row_count": int(gate.get("triggers_total", 0)),
        "date_range": None,
        "mapping_confidence": "n/a",
    }
    report["provenance"] = prov
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str),
                           encoding="utf-8")
    df = pd.DataFrame([{
        "gate_name": "probationary_pass_rate", "status": gate["status"],
        "passed": bool(gate["passed"]), "metric_value": gate["metric_value"],
        "threshold": gate["band"],
        "reason": str(gate.get("reason", "")),
        "source": prov["source"], "retrieval_timestamp": prov["retrieval_timestamp"],
        "sha256": prov["sha256"], "row_count": prov["row_count"],
        "date_range": None, "mapping_confidence": "n/a",
    }])
    df.to_parquet(report_path.with_name("probation_gates_results.parquet"),
                  engine="pyarrow", index=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="probation_gates — probationary pass-rate band gate (Phase 3)")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--pass-rate-min", type=float, default=None)
    parser.add_argument("--pass-rate-max", type=float, default=None)
    parser.add_argument("--min-triggers", type=int, default=400)
    parser.add_argument("--weights", type=str, default=None,
                        help='JSON dict e.g. \'{"asymmetric_entry":0.4,'
                             '"intrinsic_value":0.4,"disciplined_exit":0.2}\'')
    parser.add_argument("--config", type=str, default=FOLD_CFG_PATH)
    parser.add_argument("--report", type=str, default=str(DEFAULT_REPORT))
    args = parser.parse_args()

    if args.test:
        return run_tests()
    if not args.run:
        parser.print_help()
        return 1

    cfg = load_yaml(args.config)
    weights = json.loads(args.weights) if args.weights else None
    folds = load_folds(cfg)
    gate = probationary_pass_rate_gate(
        cfg, folds, pass_rate_min=args.pass_rate_min,
        pass_rate_max=args.pass_rate_max, min_triggers=args.min_triggers,
        weights=weights)
    report = {
        "name": "probation_gates_phase3",
        "config_sha256": hashlib.sha256(Path(args.config).read_bytes()).hexdigest(),
        "passed": bool(gate["passed"]),
        "status": gate["status"],
        "gates": {"probationary_pass_rate": gate},
    }
    write_report(report, Path(args.report))
    print(f"[prob_gates] report: {args.report}")
    print(f"[prob_gates] status={gate['status']} band={gate['band']} "
          f"or_qualified={gate['or_qualified_total']} "
          f"conversions={gate['conversions_total']} "
          f"pass_rate={gate['metric_value']} "
          f"lower_p={gate['lower_tail_p']} upper_p={gate['upper_tail_p']} "
          f"ramp={gate['capital_ramp']['status']} reason={gate.get('reason')}")
    return 0 if gate["status"] != "FAIL" else 1


# ---------------------------------------------------------------------------
# Self-test (pytest contract lives in tests/validation/)
# ---------------------------------------------------------------------------

def run_tests() -> int:
    print("probation_gates self-test")
    print("=" * 66)
    # frozen band from config
    assert frozen_band() == (0.40, 0.60)
    assert frozen_ramp_quarters() == 4
    # weighted OR
    checks_on = {"asymmetric_entry": True, "intrinsic_value": True}
    assert weighted_or_score(checks_on) == 0.8
    assert queue_decision(weighted_or_score(checks_on)) is True
    assert weighted_or_score({"asymmetric_entry": True}) == 0.4
    assert queue_decision(0.4) is False                # 0.4 < 0.5
    assert queue_decision(0.4 + 1e-9) is False
    assert queue_decision(0.5) is True
    # band test
    res = band_test(400, 200)
    assert res["status"] == "PASS" and res["metric_value"] == 0.5
    # Rule A: rate must be statistically INTERIOR — both edge tails significant
    assert res["upper_tail_p"] <= res["alpha"] and res["lower_tail_p"] <= res["alpha"]
    low = band_test(400, 30)
    assert low["status"] == "FAIL" and low["metric_value"] == 0.075
    high = band_test(400, 340)
    assert high["status"] == "FAIL" and high["metric_value"] == 0.85
    assert band_test(100, 50)["status"] == "SKIP"      # < 400 triggers
    # ramp
    assert ramp_pct(1) == 0.25 and ramp_pct(2) == 0.5
    assert ramp_pct(3) == 0.75 and ramp_pct(4) == 1.0 and ramp_pct(9) == 1.0
    pos = pd.DataFrame({
        "ticker": ["A", "B", "C"], "data_gate_state": ["PROBATION"] * 3,
        "max_weight_cap": [0.01, 0.01, 0.01],
        "quarters_on_ramp": [1, 2, 4], "weight": [0.0025, 0.005, 0.01],
    })
    assert check_capital_ramp(pos)["status"] == "PASS"
    pos_bad = pos.copy()
    pos_bad.loc[0, "weight"] = 0.006                     # > 0.25 * 0.01
    assert check_capital_ramp(pos_bad)["status"] == "FAIL"
    assert check_capital_ramp(pd.DataFrame({"ticker": ["A"]}))["status"] == "SKIP"
    # end-to-end on a synthetic fold
    sg = pd.DataFrame({
        "rebalance_date": pd.to_datetime(["2026-01-05"] * 600),
        "ticker": [f"T{i % 60}" for i in range(600)],
        "stage": [2] * 600, "gate_name": ["asymmetric_entry"] * 600,
        "passed": [True] * 600,
    })
    inv = pd.DataFrame({
        "rebalance_date": [pd.Timestamp("2026-01-05")] * 600,
        "ticker": [f"T{i % 60}" for i in range(600)],
        "stage": [1] * 600, "gate_name": ["intrinsic_value"] * 600,
        "passed": [True] * 600,
    })
    # half convert (rate 0.5 -> in band): only T0..T29 ever buy
    tr = pd.DataFrame({
        "date": pd.to_datetime([pd.Timestamp("2026-01-05") + pd.Timedelta(days=12)]
                               * 300),
        "ticker": [f"T{i % 30}" for i in range(300)],
        "action": ["BUY"] * 300,
    })
    folds = {"fold1": {"stage_gates": pd.concat([sg, inv], ignore_index=True),
                       "trades": tr, "positions": pd.DataFrame()}}
    gate = probationary_pass_rate_gate({}, folds, min_triggers=400)
    assert gate["status"] == "PASS", gate["status"]
    assert gate["triggers_total"] == 600
    assert gate["or_qualified_total"] == 600          # OR score 0.8 >= 0.5
    assert gate["conversions_total"] == 300
    assert abs(gate["metric_value"] - 0.5) < 1e-9
    print("[OK] band, weighted-OR, ramp, end-to-end synthetic fold")
    print("\nAll probation_gates tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())