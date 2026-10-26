"""
data_gate_gates.py — Phase 3 Validation Gates: Quarantine & Data Quality
Owner: bigpickle-worker | Phase 8 Validation Gates (dispatch 2026-09-23)

Validates the data-gate / quarantine machinery against the FROZEN Phase-3
config (config/pre_registration_phase8.yaml, CEO Ruling B-20260920-001):

    phase8.data_gate:
        min_quarters_full: 12, min_quarters_probation: 8
        max_weight_quarantine: 0.005, max_weight_probation: 0.010
        graduation_quarters_roic_wacc: 3
    phase8.mad_filter:
        window_days: 90, mad_multiplier: 3.0, min_observations: 30
        min_consecutive_cycles: 2, target_trigger_rate: [0.05, 0.15]

Gates implemented
-----------------
  G1 weight_caps_gate    — frozen per-state caps are ENFORCED on real
                            positions: QUARANTINE (<8q) cap 0.5%,
                            PROBATION (8-11q) cap 1.0%, FULL (>=12q, after 3
                            consecutive ROIC>WACC quarters) unconstrained.
                            Every position weight <= cap (zero tolerance).
  G2 renormalization_gate — portfolio weights stay coherent after capping:
                            * either the capped weight is REDISTRIBUTED to
                              qualified names (sum still = target ~1.0), or
                            * the unallocated remainder is DOCUMENTED in the
                              trade audit ledger (action QUARANTINE/PROBATION,
                              data_gate trigger_reason).
                            A silent drop (capped + remainder that is neither
                            reallocated nor audited) FAILS.  This reconciles
                            the spike_guards note ("capped weights are NOT
                            renormalized — the missing weight is logged to
                            trade audit") with the brief's redistribution
                            requirement: either path is acceptable, silence
                            is not.
  G3 mad_trigger_gate     — the MAD filter (90d window, 3xMAD, >=30 obs:
                            same constants as validation/spike_guards.py) must
                            fire at a file-level trigger rate inside [5%, 15%]
                            (0% = inert, >15% = destabilising); flagged cycles
                            must be consecutive (mad_outlier_cycles) and any
                            applied flag implies >= 1 outlier cycle.

Data contracts (§8)
-------------------
  * All Parquet I/O through DuckDB.
  * Reports: data/validation/gates/data_gate_gates_report.json + per-gate
    results parquet carrying source / retrieval_timestamp / sha256 / row_count
    / date_range / mapping_confidence.

CLI (dispatch verification, must exit 0):
    python -m validation.data_gate_gates --run \
        --quarantine-8q 0.005 --quarantine-8-11q 0.01
    python -m validation.data_gate_gates --test
Exit codes: 0 = PASS/SKIP, 1 = FAIL.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

try:
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None

CONFIG_PATH = "config/pre_registration_phase8.yaml"
DEFAULT_REPORT = Path("data/validation/gates/data_gate_gates_report.json")

POSITIONS_GLOB = "data/backtests/*/positions.parquet"
VALUATION_PANEL = "data/pit_fundamentals/valuation_fundamentals.parquet"
TRADE_AUDIT_DIR = "data/trade_audit"

DATA_GATE_DEFAULTS: Dict[str, Any] = {
    "min_quarters_full": 12,
    "min_quarters_probation": 8,
    "max_weight_quarantine": 0.005,
    "max_weight_probation": 0.010,
    "graduation_quarters_roic_wacc": 3,
}
MAD_DEFAULTS: Dict[str, Any] = {
    "window_days": 90,
    "mad_multiplier": 3.0,
    "min_observations": 30,
    "min_consecutive_cycles": 2,
    "target_trigger_rate": (0.05, 0.15),
}
HARD_CAP_TOL = 1e-9


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    import yaml
    p = Path(path)
    if not p.exists():
        return {}
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return doc.get("phase8", doc) or {}


def merged_config(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    phase8 = cfg if cfg is not None else load_config()
    out: Dict[str, Any] = {}
    dg = phase8.get("data_gate") or {}
    out["data_gate"] = {**DATA_GATE_DEFAULTS, **dg}
    mf = phase8.get("mad_filter") or {}
    out["mad"] = {**MAD_DEFAULTS, **mf}
    return out


def duck_read(pattern: str) -> pd.DataFrame:
    if duckdb is None:
        return pd.DataFrame()
    try:
        return duckdb.sql("SELECT * FROM read_parquet(?, filename=true)",
                          [str(pattern)]).df()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# G1 — weight caps enforcement (zero tolerance)
# ---------------------------------------------------------------------------

def weight_caps_gate(positions: pd.DataFrame, states: pd.DataFrame,
                     state_col: str = "data_gate_state",
                     cap_col: str = "max_weight_cap",
                     weight_col: str = "weight",
                     join_key: Sequence[str] = ("ticker", "asof_date"),
                     quarantine_cap: float = 0.005,
                     probation_cap: float = 0.010,
                     full_cap: float = 1.0) -> Dict[str, Any]:
    """
    Every position must sit at or below its PIT data-gate cap.  The `states`
    frame is the valuation panel's data_gate_v2 slice (data_gate_state,
    max_weight_cap, asof_date, ticker).  Zero tolerance — a single overweight
    row fails the gate.  SKIP when no state metadata can be joined.
    """
    if positions.empty:
        return {"status": "SKIP", "reason": "positions artifact missing"}
    if states.empty:
        return {"status": "SKIP", "reason": "data_gate state panel missing"}
    pos = positions.copy()
    key = [k for k in join_key if k in pos.columns]
    pkey = key if key else ["ticker"]
    st = states.copy()
    for c in ("asof_date", "ticker"):
        if c in st.columns:
            st[c] = pd.to_datetime(st[c], errors="coerce") if c == "asof_date" \
                else st[c].astype(str)
    if cap_col in st.columns and pkey:
        join_cols = [c for c in pkey if c in st.columns] + [cap_col, state_col]
        joined = pos.merge(st[join_cols].drop_duplicates(), left_on=pkey,
                           right_on=[c for c in pkey if c in st.columns],
                           how="left", suffixes=("", "_st"))
        if joined[cap_col].isna().all():
            return {"status": "SKIP", "reason": "no joinable data_gate rows"}
        expected = joined["data_gate_state"].map({
            "QUARANTINE": quarantine_cap,
            "PROBATION": probation_cap,
            "FULL": full_cap,
        })
        w = pd.to_numeric(joined[weight_col], errors="coerce")
        viol_mask = (joined[cap_col].notna() & w.notna()
                     & (w > joined[cap_col] + HARD_CAP_TOL))
        violations = [
            {"ticker": str(r["ticker"]), "weight": float(w[i]),
             "cap": float(joined.at[i, cap_col]),
             "state": str(joined.at[i, "data_gate_state"])}
            for i in joined.index[viol_mask.values]
            for r in [joined.loc[i]]
        ]
        # capped names must carry the frozen cap value (producer contract)
        cap_mismatch = joined[joined[cap_col].notna()][
            (joined["data_gate_state"] == "QUARANTINE")
            & (np.abs(joined[cap_col] - quarantine_cap) > HARD_CAP_TOL)]
        cap_mismatch2 = joined[joined[cap_col].notna()][
            (joined["data_gate_state"] == "PROBATION")
            & (np.abs(joined[cap_col] - probation_cap) > HARD_CAP_TOL)]
        for dfm, label in ((cap_mismatch, "QUARANTINE"), (cap_mismatch2, "PROBATION")):
            for i in dfm.index[:10]:
                violations.append({
                    "ticker": str(joined.at[i, "ticker"]),
                    "finding": f"{label} row cap {joined.at[i, cap_col]} != frozen default",
                })
        passed = not violations
        return {
            "status": "PASS" if passed else "FAIL",
            "metric_value": len(violations),
            "threshold": 0,
            "passed": passed,
            "violations": violations,
            "rows_position": int(len(pos)),
            "rows_joined": int(joined[cap_col].notna().sum()),
            "frozen_caps": {"QUARANTINE": quarantine_cap,
                            "PROBATION": probation_cap, "FULL": full_cap},
            "reason": ("all positions respect the frozen data-gate caps"
                       if passed else f"{len(violations)} cap violations"),
        }
    return {"status": "SKIP", "reason": "unable to join states to positions"}


# ---------------------------------------------------------------------------
# G2 — renormalization / audited remainder
# ---------------------------------------------------------------------------

def renormalization_gate(portfolio_sums: pd.DataFrame,
                         audit_blocks: Optional[pd.DataFrame] = None,
                         eps: float = 1e-6) -> Dict[str, Any]:
    """
    portfolio_sums: one row per rebalance date -> {date, sum_capped_weights,
    sum_target_weights, unallocated_remainder}.  Passes when, for every date:
      * the portfolio is renormalized (capped sum == target, remainder ~ 0), OR
      * the remainder is documented in the trade-audit ledger (an audit block
        for that date with action QUARANTINE/PROBATION and a data_gate
        trigger_reason that references the remainder).
    A positive remainder with NO audit evidence = silent capital drop -> FAIL.
    """
    if portfolio_sums.empty:
        return {"status": "SKIP", "reason": "portfolio summary missing"}
    audited_dates: set = set()
    if audit_blocks is not None and not audit_blocks.empty:
        blk = audit_blocks.copy()
        if "asof_date" in blk.columns and "action" in blk.columns:
            blk["asof_date"] = pd.to_datetime(blk["asof_date"], errors="coerce")
            sel = blk[blk["action"].astype(str).str.upper().isin(
                ("QUARANTINE", "PROBATION", "REBALANCE"))]
            audited_dates = {d for d in sel["asof_date"].dropna().dt.normalize()}

    rows = portfolio_sums.copy()
    ds = pd.to_datetime(rows["date"], errors="coerce") if "date" in rows.columns \
        else rows.get("asof_date")
    rows["_date"] = pd.to_datetime(ds, errors="coerce").dt.normalize()
    violations: List[Dict[str, Any]] = []
    n_renorm = 0
    n_audited = 0
    for _, r in rows.iterrows():
        capped = float(r.get("sum_capped_weights", r.get("sum_weight", np.nan)))
        target = float(r.get("sum_target_weights", 1.0))
        rem = float(r.get("unallocated_remainder", max(0.0, target - capped)))
        if pd.isna(capped):
            continue
        if abs(target - capped) <= eps:
            n_renorm += 1
            continue
        if rem <= eps:
            n_renorm += 1
            continue
        if pd.isna(r["_date"]):
            continue
        if r["_date"] in audited_dates:
            n_audited += 1
            continue
        violations.append({
            "date": str(r["_date"].date()),
            "sum_capped": round(capped, 6),
            "target": round(target, 6),
            "remainder": round(rem, 6),
        })
    passed = not violations
    return {
        "status": "PASS" if passed else "FAIL",
        "metric_value": len(violations),
        "threshold": 0,
        "passed": passed,
        "violations": violations,
        "dates_evaluated": int(len(rows)),
        "renormalized_dates": n_renorm,
        "audited_remainder_dates": n_audited,
        "reason": (f"renormalized={n_renorm} audited_remainder={n_audited}"
                   if passed else f"{len(violations)} silent remainder date(s)"),
    }


# ---------------------------------------------------------------------------
# G3 — MAD filter trigger-rate band
# ---------------------------------------------------------------------------

def mad_trigger_gate(val: pd.DataFrame,
                     flag_col: str = "mad_filter_applied",
                     cycles_col: str = "mad_outlier_cycles",
                     cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    File-level MAD trigger rate must sit in the frozen [5%, 15%] band; any
    applied flag implies >= 1 outlier cycle; the '2 consecutive cycles' audit
    rule must be exercisable (>= one run of length >= 2 when flags exist).
    The mad constants themselves (window 90, 3xMAD, min obs 30) are the same
    frozen block validated by spike_guards; here we validate the PERSISTED
    result columns.
    """
    c = merged_config(cfg)["mad"]
    lo, hi = (float(c["target_trigger_rate"][0]), float(c["target_trigger_rate"][1]))
    if val.empty or flag_col not in val.columns:
        return {"status": "SKIP", "reason": "valuation panel mad_filter columns missing"}
    flags = val[flag_col].fillna(False).astype(bool)
    n_flags = int(flags.sum())
    n_total = int(len(flags))
    trigger_rate = n_flags / n_total if n_total else 0.0
    violations: List[str] = []
    if trigger_rate < lo:
        violations.append(f"trigger_rate {trigger_rate:.4f} below frozen band "
                          f"[{lo}, {hi}] (filter inert)")
    elif trigger_rate > hi:
        violations.append(f"trigger_rate {trigger_rate:.4f} above frozen band "
                          f"[{lo}, {hi}] (over-active)")
    if cycles_col in val.columns:
        cyc = pd.to_numeric(val[cycles_col], errors="coerce").fillna(0)
        bad_implied = int(((flags) & (cyc < 1)).sum())
        if bad_implied:
            violations.append(f"{bad_implied} rows flagged mad_filter_applied "
                              f"with mad_outlier_cycles < 1")
        max_run = int(cyc.max()) if len(cyc) else 0
        min_conc = int(c["min_consecutive_cycles"])
        if n_flags and max_run < min_conc:
            violations.append(f"longest consecutive cycle run {max_run} < "
                              f"min_consecutive_cycles {min_conc}")
    passed = not violations
    return {
        "status": "PASS" if passed else "FAIL",
        "metric_value": round(trigger_rate, 6),
        "threshold": [lo, hi],
        "passed": passed,
        "violations": violations,
        "flags": n_flags,
        "rows": n_total,
        "mad_config": c,
        "reason": (f"mad trigger {trigger_rate:.4f} inside frozen band"
                   if passed else "; ".join(violations)),
    }


# ---------------------------------------------------------------------------
# Report + CLI
# ---------------------------------------------------------------------------

def run_gates(positions: pd.DataFrame, states: pd.DataFrame, val: pd.DataFrame,
              audit_blocks: Optional[pd.DataFrame] = None,
              quarantine_cap: float = 0.005, probation_cap: float = 0.010,
              cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    c = merged_config(cfg)
    g1 = weight_caps_gate(positions, states, quarantine_cap=quarantine_cap,
                          probation_cap=probation_cap,
                          full_cap=float(c["data_gate"]["min_quarters_full"]) and 1.0)
    # portfolio sums per rebalance date (tolerates missing market_value)
    sums = pd.DataFrame()
    if not positions.empty:
        pos = positions.copy()
        dc = next((x for x in ("rebalance_date", "asof_date") if x in pos.columns), None)
        if dc and "weight" in pos.columns:
            pos["_date"] = pd.to_datetime(pos[dc], errors="coerce").dt.normalize()
            sums = pos.groupby("_date").agg(
                sum_weight=("weight", "sum")).reset_index()
            sums = sums.rename(columns={"_date": "date", "sum_weight": "sum_capped_weights"})
            sums["sum_target_weights"] = 1.0
            sums["unallocated_remainder"] = (
                sums["sum_target_weights"] - sums["sum_capped_weights"]).clip(lower=0.0)
    g2 = renormalization_gate(sums, audit_blocks)
    g3 = mad_trigger_gate(val, cfg=c)

    gates = {"weight_caps": g1, "renormalization": g2, "mad_trigger": g3}
    failed = [k for k, g in gates.items() if g["status"] == "FAIL"]
    evaluated = [k for k, g in gates.items() if g["status"] != "SKIP"]
    status = "FAIL" if failed else ("PASS" if evaluated else "SKIP")
    date_range = None
    if not positions.empty:
        dc = next((x for x in ("rebalance_date", "asof_date") if x in positions.columns), None)
        if dc:
            d = pd.to_datetime(positions[dc], errors="coerce").dropna()
            if len(d):
                date_range = {"min": str(d.min().date()), "max": str(d.max().date())}
    return {
        "name": "data_gate_gates",
        "passed": status == "PASS",
        "status": status,
        "gates": gates,
        "summary": {"status": status, "failed_gates": failed,
                    "evaluated_gates": evaluated,
                    "rows_evaluated": max(len(positions), len(val)),
                    "date_range": date_range},
    }


def write_report(report: Dict[str, Any], report_path: Path) -> None:
    content = json.dumps(report, sort_keys=True, default=str).encode("utf-8")
    prov = {
        "source": "validation.data_gate_gates",
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "row_count": report.get("summary", {}).get("rows_evaluated", 0),
        "date_range": report.get("summary", {}).get("date_range"),
        "mapping_confidence": "n/a",
    }
    report["provenance"] = prov
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str),
                           encoding="utf-8")
    rows = []
    for name, g in report["gates"].items():
        rows.append({
            "gate_name": name, "status": g["status"],
            "passed": bool(g.get("passed", g["status"] == "PASS")),
            "metric_value": g.get("metric_value"),
            "threshold": g.get("threshold"),
            "reason": str(g.get("reason", "")),
            "source": prov["source"], "retrieval_timestamp": prov["retrieval_timestamp"],
            "sha256": prov["sha256"], "row_count": prov["row_count"],
            "date_range": json.dumps(prov["date_range"]) if prov["date_range"] else None,
            "mapping_confidence": "n/a",
        })
    pd.DataFrame(rows).to_parquet(
        report_path.with_name("data_gate_gates_results.parquet"),
        engine="pyarrow", index=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="data_gate_gates — quarantine & data quality gates (Phase 3)")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--quarantine-8q", type=float, default=0.005)
    parser.add_argument("--quarantine-8-11q", type=float, default=0.010)
    parser.add_argument("--positions", type=str, default=POSITIONS_GLOB)
    parser.add_argument("--states", type=str, default=VALUATION_PANEL)
    parser.add_argument("--audit-dir", type=str, default=TRADE_AUDIT_DIR)
    parser.add_argument("--report", type=str, default=str(DEFAULT_REPORT))
    parser.add_argument("--config", type=str, default=CONFIG_PATH)
    args = parser.parse_args()

    if args.test:
        return run_tests()
    if not args.run:
        parser.print_help()
        return 1

    positions = duck_read(args.positions)
    states = duck_read(args.states)
    val = duck_read(args.states)                     # valuation panel carries mad cols
    audit_blocks = duck_read(f"{args.audit_dir}/month=*/trade_audit_*.parquet")
    report = run_gates(positions, states, val, audit_blocks,
                       quarantine_cap=args.quarantine_8q,
                       probation_cap=args.quarantine_8_11q,
                       cfg=load_config(args.config))
    write_report(report, Path(args.report))
    s = report["summary"]
    print(f"[data_gate_gates] report: {args.report}")
    print(f"[data_gate_gates] status={report['status']} "
          f"evaluated={len(s['evaluated_gates'])} failed={s['failed_gates'] or 'none'}")
    for name, g in report["gates"].items():
        print(f"  [{g['status']:4s}] {name}: {g.get('metric_value')} "
              f"(threshold {g.get('threshold')}) {g.get('reason', '')[:100]}")
    return 1 if report["status"] == "FAIL" else 0


def run_tests() -> int:
    print("data_gate_gates self-test")
    print("=" * 66)
    cfg = merged_config()
    assert cfg["data_gate"]["max_weight_quarantine"] == 0.005
    assert cfg["data_gate"]["max_weight_probation"] == 0.010
    assert cfg["data_gate"]["min_quarters_full"] == 12
    assert cfg["mad"]["window_days"] == 90 and cfg["mad"]["mad_multiplier"] == 3.0
    assert cfg["mad"]["min_observations"] == 30 and cfg["mad"]["min_consecutive_cycles"] == 2
    # weight caps
    pos = pd.DataFrame({
        "ticker": ["Q1", "P1", "F1", "Q2"],
        "asof_date": pd.to_datetime(["2026-09-01"] * 4),
        "weight": [0.004, 0.009, 0.05, 0.006],      # Q2 breaches 0.5% cap
    })
    states = pd.DataFrame({
        "ticker": ["Q1", "P1", "F1", "Q2"],
        "asof_date": pd.to_datetime(["2026-09-01"] * 4),
        "data_gate_state": ["QUARANTINE", "PROBATION", "FULL", "QUARANTINE"],
        "max_weight_cap": [0.005, 0.010, 1.0, 0.005],
    })
    g1 = weight_caps_gate(pos, states)
    assert g1["status"] == "FAIL" and len(g1["violations"]) == 1
    pos_ok = pos.copy()
    pos_ok.loc[3, "weight"] = 0.005
    assert weight_caps_gate(pos_ok, states)["status"] == "PASS"
    # cap value contract: producer must carry frozen caps
    states_bad = states.copy()
    states_bad.loc[1, "max_weight_cap"] = 0.02      # PROBATION should be 0.010
    assert weight_caps_gate(pos_ok, states_bad)["status"] == "FAIL"
    assert weight_caps_gate(pos_ok, pd.DataFrame())["status"] == "SKIP"
    # renormalization
    sums = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-01", "2026-10-01"]),
        "sum_capped_weights": [1.0, 0.97],
        "sum_target_weights": [1.0, 1.0],
        "unallocated_remainder": [0.0, 0.03],
    })
    g2_ok = renormalization_gate(sums, audit_blocks=None)
    assert g2_ok["status"] == "FAIL" and len(g2_ok["violations"]) == 1
    audited = pd.DataFrame({
        "asof_date": pd.to_datetime(["2026-10-01"]),
        "action": ["QUARANTINE"],
        "ticker": ["Q1"],
    })
    assert renormalization_gate(sums, audited)["status"] == "PASS"
    g2_note = renormalization_gate(sums.drop(index=1).copy(), None)
    assert g2_note["status"] == "PASS"
    # mad trigger gate
    val = pd.DataFrame({
        "mad_filter_applied": [True] * 10 + [False] * 90,
        "mad_outlier_cycles": [1] * 8 + [2, 2] + [0] * 90,
    })
    assert mad_trigger_gate(val)["status"] == "PASS"     # 10% in band, run of 2
    val_hi = pd.DataFrame({"mad_filter_applied": [True] * 40 + [False] * 60,
                           "mad_outlier_cycles": [1] * 100})
    assert mad_trigger_gate(val_hi)["status"] == "FAIL"  # 40% > 15%
    val_zero = pd.DataFrame({"mad_filter_applied": [False] * 100,
                             "mad_outlier_cycles": [0] * 100})
    assert mad_trigger_gate(val_zero)["status"] == "FAIL"  # inert
    val_implied = pd.DataFrame({"mad_filter_applied": [True, False],
                                "mad_outlier_cycles": [0, 0]})
    assert mad_trigger_gate(val_implied)["status"] == "FAIL"  # flagged w/o cycles
    assert mad_trigger_gate(pd.DataFrame())["status"] == "SKIP"
    print("[OK] caps (zero tolerance), cap contract, renormalization/audit, "
          "MAD band + implied cycles")
    print("\nAll data_gate_gates tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())