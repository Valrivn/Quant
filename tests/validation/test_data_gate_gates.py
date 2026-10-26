"""
test_data_gate_gates.py — unit tests for validation/data_gate_gates.py
(Phase 3 quarantine & data-quality gates).

Covers: G1 weight caps (frozen 0.5% / 1.0% caps, zero tolerance, cap-value
contract), G2 renormalization (either redistribute-to-qualified OR audited
remainder in the trade ledger — silent drops FAIL), G3 MAD trigger-rate band
(frozen [5%, 15%] with implied-cycle consistency), and the run_gates wrapper.
Pure-function tests — no disk dependencies.
"""

import numpy as np
import pandas as pd
import pytest

from validation import data_gate_gates as dg


def _positions(caps_ok=True):
    w = [0.004, 0.009, 0.05, 0.937] if caps_ok else [0.006, 0.009, 0.05, 0.937]
    return pd.DataFrame({
        "ticker": ["Q1", "P1", "F1", "F2"],
        "asof_date": pd.to_datetime(["2026-09-01"] * 4),
        "weight": w,
    })


def _states():
    return pd.DataFrame({
        "ticker": ["Q1", "P1", "F1", "F2"],
        "asof_date": pd.to_datetime(["2026-09-01"] * 4),
        "data_gate_state": ["QUARANTINE", "PROBATION", "FULL", "FULL"],
        "max_weight_cap": [0.005, 0.010, 1.0, 1.0],
    })


# --- G1: weight caps ------------------------------------------------------------

def test_weight_caps_pass():
    res = dg.weight_caps_gate(_positions(), _states())
    assert res["status"] == "PASS"
    assert res["violations"] == []


def test_weight_caps_fail_zero_tolerance():
    res = dg.weight_caps_gate(_positions(caps_ok=False), _states())
    assert res["status"] == "FAIL"
    assert len(res["violations"]) == 1
    assert res["violations"][0]["ticker"] == "Q1"
    assert res["violations"][0]["weight"] == pytest.approx(0.006)


def test_weight_caps_skips_on_missing_state_panel():
    assert dg.weight_caps_gate(_positions(), pd.DataFrame())["status"] == "SKIP"


def test_weight_caps_cap_value_contract():
    # producer must carry the FROZEN cap value for each state
    bad = _states()
    bad.loc[1, "max_weight_cap"] = 0.02          # PROBATION should be 0.010
    res = dg.weight_caps_gate(_positions(), bad)
    assert res["status"] == "FAIL"
    assert any("PROBATION row cap" in v.get("finding", "") for v in res["violations"])


# --- G2: renormalization / audited remainder ---------------------------------------

def test_renormalization_pass_when_sum_equals_target():
    sums = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-01", "2026-10-01"]),
        "sum_capped_weights": [1.0, 1.0],
        "sum_target_weights": [1.0, 1.0],
    })
    assert dg.renormalization_gate(sums)["status"] == "PASS"


def test_renormalization_fail_on_silent_remainder():
    sums = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-01"]),
        "sum_capped_weights": [0.97],
        "sum_target_weights": [1.0],
        "unallocated_remainder": [0.03],
    })
    res = dg.renormalization_gate(sums)
    assert res["status"] == "FAIL"
    assert len(res["violations"]) == 1


def test_renormalization_pass_with_audit_evidence():
    sums = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-01"]),
        "sum_capped_weights": [0.97],
        "sum_target_weights": [1.0],
        "unallocated_remainder": [0.03],
    })
    audit = pd.DataFrame({
        "asof_date": pd.to_datetime(["2026-09-01"]),
        "action": ["QUARANTINE"],
        "ticker": ["Q1"],
    })
    res = dg.renormalization_gate(sums, audit_blocks=audit)
    assert res["status"] == "PASS"
    assert res["audited_remainder_dates"] == 1


def test_renormalization_skips_empty_summary():
    assert dg.renormalization_gate(pd.DataFrame())["status"] == "SKIP"


# --- G3: MAD trigger-rate band -------------------------------------------------------

def _mad_val(n_flags, n_total, cycles=None):
    flags = [True] * n_flags + [False] * (n_total - n_flags)
    if cycles is None:
        # one run of 2 consecutive cycles when at least 2 flags exist
        # (mad.min_consecutive_cycles == 2), else a single flag cycle
        cyc = ([1] * max(0, n_flags - 2) + [2] * min(2, n_flags)
               + [0] * (n_total - n_flags)) if n_flags >= 2 \
            else ([0] * n_total)
    else:
        cyc = cycles
    return pd.DataFrame({"mad_filter_applied": flags, "mad_outlier_cycles": cyc})


def test_mad_trigger_pass_inside_band():
    v = _mad_val(10, 100)                        # 10% inside frozen [5%, 15%]
    res = dg.mad_trigger_gate(v)
    assert res["status"] == "PASS"
    assert res["metric_value"] == pytest.approx(0.10)


def test_mad_trigger_fail_over_active():
    res = dg.mad_trigger_gate(_mad_val(40, 100))  # 40% > 15%
    assert res["status"] == "FAIL"


def test_mad_trigger_fail_inert():
    res = dg.mad_trigger_gate(_mad_val(0, 100))   # 0% < 5%
    assert res["status"] == "FAIL"


def test_mad_trigger_fail_flagged_without_cycles():
    v = _mad_val(5, 100, cycles=[0] * 5 + [0] * 95)
    assert dg.mad_trigger_gate(v)["status"] == "FAIL"


def test_mad_trigger_skips_missing_columns():
    assert dg.mad_trigger_gate(pd.DataFrame())["status"] == "SKIP"


# --- run_gates wrapper ----------------------------------------------------------------

def test_run_gates_end_to_end_pass():
    val = _mad_val(10, 100)
    rpt = dg.run_gates(_positions(), _states(), val)
    assert rpt["status"] == "PASS"
    assert rpt["gates"]["weight_caps"]["status"] == "PASS"
    assert rpt["gates"]["renormalization"]["status"] == "PASS"
    assert rpt["gates"]["mad_trigger"]["status"] == "PASS"


def test_run_gates_end_to_end_fail_cap():
    val = _mad_val(10, 100)
    rpt = dg.run_gates(_positions(caps_ok=False), _states(), val)
    assert rpt["status"] == "FAIL"
    assert rpt["summary"]["failed_gates"] == ["weight_caps"]


def test_run_gates_skips_when_no_artifacts():
    rpt = dg.run_gates(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    assert rpt["status"] == "SKIP"