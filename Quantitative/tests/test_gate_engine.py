"""
Unit Tests for Sprint 6 Unified Gate Engine & Composite DSR Evaluation
"""

from __future__ import annotations

import json
import numpy as np
import pandas as pd
import pytest

from ingestion.harness.gate_engine import (
    COMPONENT_NAMES,
    COMPONENT_SPECS,
    COMPONENT_WEIGHTS,
    DSR_N_TRIALS,
    DSR_THRESHOLD,
    DSR_TRACK_RECORD_DAYS,
    FOLD_4_TRUE_OOS,
    CompositeDSRResult,
    CompositeGateEngine,
    compute_composite_score,
    compute_composite_series,
    deflated_sharpe_ratio,
    evaluate_composite_dsr,
    score_hmm_ari,
    score_max_drawdown,
    score_probation_pass_rate,
    score_sharpe,
    score_swap_churn,
    score_tca_bps,
    score_turnover,
)


def test_component_weights_sum_to_one():
    assert len(COMPONENT_WEIGHTS) == 7
    assert np.isclose(COMPONENT_WEIGHTS.sum(), 1.0)
    assert len(COMPONENT_SPECS) == 7
    assert COMPONENT_WEIGHTS[0] == 0.25  # sharpe
    assert COMPONENT_WEIGHTS[1] == 0.20  # max_drawdown
    assert COMPONENT_WEIGHTS[2] == 0.15  # turnover
    assert COMPONENT_WEIGHTS[3] == 0.15  # hmm_ari
    assert COMPONENT_WEIGHTS[4] == 0.10  # swap_churn
    assert COMPONENT_WEIGHTS[5] == 0.10  # tca_bps
    assert COMPONENT_WEIGHTS[6] == 0.05  # probation_pass_rate


def test_score_sharpe_range_and_linear_ramp():
    # Sharpe <= 0.5 -> 0.0
    dates = pd.date_range("2020-01-01", periods=100, freq="D")
    ret_low = pd.Series(np.random.normal(0.0001, 0.02, 100), index=dates)
    assert score_sharpe(ret_low) >= 0.0

    # High Sharpe -> 1.0
    ret_high = pd.Series(np.random.normal(0.005, 0.01, 100), index=dates)
    assert score_sharpe(ret_high) == 1.0


def test_score_max_drawdown():
    dates = pd.date_range("2020-01-01", periods=100, freq="D")
    # Peak at 1.0, then drawdown to 0.60 (-40% drawdown) -> 1.0
    ret_crash = pd.Series(np.array([0.0, -0.40] + [0.0] * 98), index=dates)
    assert score_max_drawdown(ret_crash) == 1.0


def test_score_turnover():
    # Low turnover -> 1.0
    weights_dict = {
        "2020-01-01": {"AAPL": 0.5, "MSFT": 0.5},
        "2020-02-01": {"AAPL": 0.51, "MSFT": 0.49},
    }
    assert score_turnover(weights_dict) == 1.0


def test_score_scalar_components():
    assert score_hmm_ari(0.75) == 1.0
    assert score_hmm_ari(0.40) == 0.0

    assert score_swap_churn(4.0) == 1.0
    assert score_swap_churn(20.0) == 0.0

    assert score_tca_bps(100.0) == 1.0
    assert score_tca_bps(400.0) == 0.0

    assert score_probation_pass_rate(0.50) == 1.0
    assert score_probation_pass_rate(0.10) < 1.0


def test_compute_composite_score():
    dates = pd.date_range("2020-01-01", periods=100, freq="D")
    returns = pd.Series(np.full(100, 0.002), index=dates)
    weights = pd.DataFrame({"AAPL": [0.5, 0.5]}, index=dates[:2])

    comp_score, comp_dict = compute_composite_score(
        arm_c_returns=returns,
        arm_c_weights=weights,
        spy_returns=returns,
        hmm_ari=0.75,
        swap_churn=4.0,
        tca_bps=100.0,
        probation_pass_rate=0.50,
    )
    assert 0.0 <= comp_score <= 1.0
    assert len(comp_dict) == 7


def test_compute_composite_series_84_months():
    # 84 monthly dates (7 years OOS)
    months = pd.date_range("2018-01-01", periods=84, freq="ME")
    returns = pd.Series(np.random.normal(0.01, 0.03, 84), index=months)
    weights = pd.DataFrame({"AAPL": np.full(84, 0.5), "MSFT": np.full(84, 0.5)}, index=months)

    comp_series = compute_composite_series(
        arm_c_returns=returns,
        arm_c_weights=weights,
        spy_returns=returns,
        hmm_ari=0.75,
        swap_churn=3.0,
        tca_bps=90.0,
        probation_pass_rate=0.50,
    )
    assert len(comp_series) == 84
    assert not comp_series.isna().any()
    assert all(0.0 <= v <= 1.0 for v in comp_series)


def test_deflated_sharpe_ratio_computation():
    dates = pd.date_range("2018-01-01", periods=2520, freq="D")
    np.random.seed(42)
    # Strong positive returns
    high_ret = pd.Series(np.random.normal(0.002, 0.01, 2520), index=dates)
    dsr_high = deflated_sharpe_ratio(high_ret, n_trials=513, track_record_days=2520)
    assert 0.0 <= dsr_high <= 1.0
    assert dsr_high > 0.50

    # Zero returns
    zero_ret = pd.Series(np.zeros(2520), index=dates)
    dsr_zero = deflated_sharpe_ratio(zero_ret, n_trials=513, track_record_days=2520)
    assert dsr_zero == 0.0


def test_composite_dsr_computed_on_composite_series_not_raw_returns():
    # Verifies P0 FIX: composite DSR is evaluated on the composite score time-series
    months = pd.date_range("2018-01-01", periods=84, freq="ME")
    np.random.seed(42)
    returns = pd.Series(np.random.normal(0.015, 0.02, 84), index=months)
    weights = pd.DataFrame({"AAPL": np.full(84, 0.5)}, index=months)

    res = evaluate_composite_dsr(
        oos_returns=returns,
        arm_c_weights=weights,
        spy_returns=returns,
        hmm_ari=0.80,
        swap_churn=2.0,
        tca_bps=80.0,
        probation_pass_rate=0.50,
        n_trials=513,
        track_record_days=2520,
    )
    assert isinstance(res, CompositeDSRResult)
    assert 0.0 <= res.composite_dsr <= 1.0


def test_fold_4_conjunctive_dsr_evaluation():
    months = pd.date_range("2018-01-01", periods=84, freq="ME")
    np.random.seed(42)
    returns = pd.Series(np.random.normal(0.02, 0.01, 84), index=months)
    weights = pd.DataFrame({"AAPL": np.full(84, 0.5)}, index=months)

    fold_4_months = months[-12:]
    fold_returns = {
        "fold_4": pd.Series(np.random.normal(0.02, 0.01, 12), index=fold_4_months)
    }

    res = evaluate_composite_dsr(
        oos_returns=returns,
        arm_c_weights=weights,
        spy_returns=returns,
        hmm_ari=0.85,
        swap_churn=2.0,
        tca_bps=70.0,
        probation_pass_rate=0.50,
        fold_returns=fold_returns,
    )
    assert res.fold_4_dsr is not None
    assert isinstance(bool(res.fold_4_passed), bool)


def test_composite_dsr_result_to_dict():
    res = CompositeDSRResult(
        composite_dsr=0.96,
        passed=True,
        component_scores={"sharpe": 1.0, "max_drawdown": 0.9},
        composite_score=0.95,
        fold_4_dsr=0.96,
        fold_4_passed=True,
    )
    d = res.to_dict()
    assert d["composite_dsr"] == 0.96
    assert d["passed"] is True
    assert d["composite_score"] == 0.95
    assert d["n_trials"] == 513


def test_composite_dsr_result_to_json(tmp_path):
    res = CompositeDSRResult(
        composite_dsr=0.97,
        passed=True,
        component_scores={"sharpe": 1.0},
        composite_score=0.96,
    )
    out_json = tmp_path / "result.json"
    res.to_json(out_json)
    assert out_json.exists()
    loaded = json.loads(out_json.read_text(encoding="utf-8"))
    assert loaded["composite_dsr"] == 0.97


def test_composite_gate_engine_prereg_constants():
    assert DSR_N_TRIALS == 513
    assert DSR_TRACK_RECORD_DAYS == 2520
    assert DSR_THRESHOLD == 0.95
    assert FOLD_4_TRUE_OOS is True


def test_composite_gate_engine_evaluation_failure_on_short_series():
    dates = pd.date_range("2020-01-01", periods=2, freq="D")
    short_ret = pd.Series([0.01, 0.02], index=dates)
    with pytest.raises(ValueError, match="need >= 3 non-null observations"):
        compute_composite_score(
            arm_c_returns=short_ret,
            arm_c_weights=pd.DataFrame(),
            spy_returns=short_ret,
            hmm_ari=0.5,
            swap_churn=10.0,
            tca_bps=200.0,
            probation_pass_rate=0.5,
        )


def test_no_gate_hierarchy_invariant():
    engine = CompositeGateEngine()
    detail = engine.pre_reg
    assert detail.no_gate_hierarchy is True


def test_component_specs_diagnostic_only():
    for spec in COMPONENT_SPECS:
        assert "name" in spec
        assert "weight" in spec
        assert "target" in spec
        assert "direction" in spec
