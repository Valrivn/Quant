"""
test_probation_gates.py — unit tests for validation/probation_gates.py
(Phase 3, supersedes RS-29).

Covers the weighted-OR decision rule, the two-sided binomial-exact band test
(CEO Ruling #2: [0.40, 0.60], min_triggers 400), the 25/50/75/100% capital
ramp, and the full probationary_pass_rate_gate flow on synthetic fold frames.
Pure-function tests — no disk dependencies.
"""

import numpy as np
import pandas as pd
import pytest

from validation import probation_gates as pg


# --- Weighted-OR decision rule ------------------------------------------------

def test_weighted_or_default_weights():
    assert pg.weighted_or_score({}) == 0.0
    assert pg.weighted_or_score({"asymmetric_entry": True}) == pytest.approx(0.4)
    assert pg.weighted_or_score({"asymmetric_entry": True,
                                 "intrinsic_value": True}) == pytest.approx(0.8)
    assert pg.weighted_or_score({"asymmetric_entry": True,
                                 "intrinsic_value": True,
                                 "disciplined_exit": True}) == pytest.approx(1.0)


def test_weighted_or_missing_check_scores_zero():
    # missing checks are treated as evidence absent -> 0
    assert pg.weighted_or_score({"intrinsic_value": True}) == pytest.approx(0.4)
    assert pg.weighted_or_score({"unknown_gate": True}) == 0.0


def test_weighted_or_custom_weights():
    w = {"asymmetric_entry": 0.5, "intrinsic_value": 0.5, "disciplined_exit": 0.0}
    assert pg.weighted_or_score({"asymmetric_entry": True}, w) == pytest.approx(0.5)


def test_queue_decision_threshold():
    assert pg.queue_decision(0.5) is True
    assert pg.queue_decision(0.49) is False
    assert pg.queue_decision(0.799, decision_threshold=0.8) is False


# --- Two-sided binomial exact band test ----------------------------------------

def test_band_pass_mid_rate():
    # k=250/500 = 0.50: significantly above 0.40 AND below 0.60 -> PASS
    res = pg.band_test(n=500, k=250)
    assert res["status"] == "PASS"
    assert res["metric_value"] == pytest.approx(0.5)


def test_band_fail_on_high_edge():
    # k=300/500 = 0.60 exactly: point-in-band but NOT significantly below 0.60
    res = pg.band_test(n=500, k=300)
    assert res["status"] == "FAIL"
    assert res["lower_tail_p"] > res["alpha"]


def test_band_fail_on_low_edge():
    # k=200/500 = 0.40 exactly: NOT significantly above 0.40 (anti-drift)
    res = pg.band_test(n=500, k=200)
    assert res["status"] == "FAIL"
    assert res["upper_tail_p"] > res["alpha"]


def test_band_fail_out_of_band():
    res = pg.band_test(n=500, k=120)   # 0.24 << 0.40
    assert res["status"] == "FAIL"
    assert res["metric_value"] == pytest.approx(0.24)


# ---------------------------------------------------------------------------
# _binom_cdf numerics — regression defence
# ---------------------------------------------------------------------------

def test_binom_cdf_matches_naive_where_naive_is_representable():
    """
    The log-space implementation must agree with the textbook summation to
    ~1e-12, so fixing the overflow did not quietly change the gate.
    """
    import math

    def naive(k, n, p):
        return float(sum(math.comb(n, i) * (p ** i) * ((1 - p) ** (n - i))
                         for i in range(0, int(k) + 1)))

    for k, n, p in [(200, 400, 0.4), (240, 400, 0.6), (0, 400, 0.4),
                    (399, 400, 0.4), (80, 100, 0.5), (250, 500, 0.5)]:
        got, want = pg._binom_cdf(k, n, p), naive(k, n, p)
        assert got == pytest.approx(want, rel=1e-9, abs=1e-15), (k, n, p)


def test_binom_cdf_does_not_overflow_at_large_n():
    """
    comb(n, n/2) overflows the float range past n~1000 (comb(1600,800)~1e467),
    which used to raise OverflowError inside band_test and crash the
    calibration sweep.
    """
    val = pg._binom_cdf(800, 1600, 0.4)
    assert 0.0 <= val <= 1.0
    assert math_isclose(val, 0.9999999999994, rel_tol=1e-6)
    res = pg.band_test(n=1600, k=800)
    assert res["status"] == "PASS"          # dead centre, large n
    assert 0.0 <= res["lower_tail_p"] <= 1.0


def math_isclose(a, b, rel_tol=1e-9):
    return abs(a - b) <= rel_tol * max(abs(a), abs(b), 1e-300)


def test_band_skips_insufficient_triggers():
    res = pg.band_test(n=50, k=25, min_triggers=400)
    assert res["status"] == "SKIP"
    assert res["reason"] == "insufficient_triggers"


def test_band_honors_frozen_band_from_config():
    res = pg.band_test(n=500, k=250)
    assert list(res["band"]) == [0.40, 0.60]


# --- Capital ramp ---------------------------------------------------------------

def test_ramp_pct_schedule():
    assert [pg.ramp_pct(q) for q in (1, 2, 3, 4, 7)] == \
        [0.25, 0.50, 0.75, 1.00, 1.00]


def test_capital_ramp_pass():
    pos = pd.DataFrame({
        "ticker": ["A", "B", "C"],
        "data_gate_state": ["PROBATION", "PROBATION", "FULL"],
        "max_weight_cap": [0.010, 0.010, 1.0],
        "quarters_on_ramp": [1, 2, 4],
        "weight": [0.002, 0.005, 0.05],     # <= 0.0025, 0.005, 1.0
    })
    res = pg.check_capital_ramp(pos)
    assert res["status"] == "PASS"
    assert res["violations"] == []


def test_capital_ramp_fail_violation():
    pos = pd.DataFrame({
        "ticker": ["A"],
        "data_gate_state": ["PROBATION"],
        "max_weight_cap": [0.010],
        "quarters_on_ramp": [1],
        "weight": [0.004],                  # 0.004 > 0.25 * 0.010 = 0.0025
    })
    res = pg.check_capital_ramp(pos)
    assert res["status"] == "FAIL"
    assert len(res["violations"]) == 1
    assert res["violations"][0]["ticker"] == "A"


def test_capital_ramp_skips_without_metadata():
    pos = pd.DataFrame({"ticker": ["A"], "weight": [0.01]})
    assert pg.check_capital_ramp(pos)["status"] == "SKIP"


# --- Full gate flow (synthetic fold frames) --------------------------------------

def _trig(n, anchor="2026-01-15"):
    return pd.DataFrame({
        "rebalance_date": pd.to_datetime([anchor] * n),
        "ticker": [f"T{i:03d}" for i in range(n)],
        "stage": [2] * n,
        "gate_name": ["asymmetric_entry"] * n,
        "passed": [True] * n,
        "reason": ["trigger"] * n,
        "filed_date": pd.to_datetime([anchor] * n),
        "retrieval_timestamp": pd.to_datetime(["2026-01-16T00:00:00Z"] * n),
    })


def _synth_fold(n_trig, n_conv, anchor="2026-01-15"):
    """One fold with n_trig stage-2 triggers, all intrinsically valid, with
    n_conv BUY conversions inside the [10, 25] day window (no exits)."""
    trig = _trig(n_trig, anchor)
    s1 = trig.copy()
    s1["stage"] = 1
    s1["gate_name"] = "intrinsic_value"
    parts = [trig, s1]                       # no disciplined-exit evidence
    sg = pd.concat(parts, ignore_index=True)
    buys = pd.DataFrame({
        "date": pd.to_datetime([anchor] * n_conv) + pd.Timedelta(days=15),
        "ticker": [f"T{i:03d}" for i in range(n_conv)],
        "action": ["BUY"] * n_conv,
    })
    return {"stage_gates": sg, "trades": buys, "positions": pd.DataFrame()}


def test_full_flow_pass_mid_band():
    folds = {"fold1": _synth_fold(n_trig=500, n_conv=250)}   # rate = 0.50
    res = pg.probationary_pass_rate_gate({}, folds, min_triggers=400)
    assert res["status"] == "PASS"
    assert res["triggers_total"] == 500
    assert res["or_qualified_total"] == 500
    assert res["conversions_total"] == 250
    assert res["metric_value"] == pytest.approx(0.5)


def test_full_flow_fail_at_high_edge():
    folds = {"fold1": _synth_fold(n_trig=500, n_conv=300)}   # rate = 0.60
    res = pg.probationary_pass_rate_gate({}, folds, min_triggers=400)
    assert res["status"] == "FAIL"
    assert res["band_min"] == pytest.approx(0.40)
    assert res["band_max"] == pytest.approx(0.60)
    assert res["conversions_total"] == 300


def test_full_flow_skips_on_empty_folds():
    res = pg.probationary_pass_rate_gate({}, {}, min_triggers=400)
    assert res["status"] == "SKIP"
    assert res["triggers_total"] == 0


def test_full_flow_capital_ramp_skipped_when_no_metadata():
    folds = {"fold1": _synth_fold(500, 250)}
    res = pg.probationary_pass_rate_gate({}, folds, min_triggers=400)
    assert res["capital_ramp"]["status"] == "SKIP"
    assert res["status"] == "PASS"    # band evidence alone is sufficient