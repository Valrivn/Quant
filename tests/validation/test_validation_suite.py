"""
test_validation_suite.py — Unit test wrapper for validation modules (hmm_gates, vol_gates, trailing_cv)

Executes all gate functions explicitly to avoid pytest fixture parameter matching on validation module gate functions.
"""

import numpy as np
import pandas as pd
import pytest

from validation import hmm_gates
from validation import vol_gates
from validation import trailing_cv


# --- HMM Gates Tests ---

def test_hmm_crisis_precision_recall_pass():
    syn = hmm_gates._crisis_calendar(
        "2019-01-01", "2024-12-31",
        crisis_windows=[("2020-02-15", "2020-04-30"), ("2022-06-01", "2022-10-15")],
        seed=42
    )
    res = hmm_gates.test_crisis_precision_recall(syn["states"], syn["baa10y"], threshold=300)
    assert res["passed"] is True
    assert res["precision"] >= 0.80
    assert res["recall"] >= 0.70
    assert res["metric"] == "f1"


def test_hmm_crisis_precision_recall_fail():
    syn = hmm_gates._crisis_calendar(
        "2019-01-01", "2024-12-31",
        crisis_windows=[("2020-02-15", "2020-04-30")],
        seed=42
    )
    null_states = hmm_gates._random_states("2019-01-01", "2024-12-31", seed=7)
    res = hmm_gates.test_crisis_precision_recall(null_states, syn["baa10y"], threshold=300)
    assert res["passed"] is False


def test_hmm_state_stability_pass():
    folds = {
        f"fold_{f}": hmm_gates._crisis_calendar(
            "2019-01-01", "2024-12-31",
            crisis_windows=[("2020-02-15", "2020-04-30"), ("2022-06-01", "2022-10-15")],
            seed=10 + f
        )["states"]
        for f in range(4)
    }
    res = hmm_gates.test_state_stability(folds, threshold=0.70)
    assert res["passed"] is True
    assert res["mean_ARI"] >= 0.70
    assert res["min_ARI"] >= 0.70


def test_hmm_state_stability_fail():
    folds = {
        f"fold_{f}": hmm_gates._random_states("2019-01-01", "2024-12-31", seed=100 + f)
        for f in range(4)
    }
    res = hmm_gates.test_state_stability(folds, threshold=0.70)
    assert res["passed"] is False
    assert res["mean_ARI"] < 0.70


def test_hmm_labeling_consistency_pass():
    syn = hmm_gates._crisis_calendar(
        "2019-01-01", "2024-12-31",
        crisis_windows=[("2020-02-15", "2020-04-30")],
        seed=15
    )
    res = hmm_gates.test_labeling_consistency(syn["states"], syn["vix"])
    assert res["passed"] is True
    assert res["mean_vix_crisis"] > res["mean_vix_expansion"]


def test_hmm_labeling_consistency_fail():
    syn = hmm_gates._crisis_calendar(
        "2019-01-01", "2024-12-31",
        crisis_windows=[("2020-02-15", "2020-04-30")],
        seed=15
    )
    flipped = 1 - syn["states"]
    res = hmm_gates.test_labeling_consistency(flipped, syn["vix"])
    assert res["passed"] is False


def test_hmm_run_tests_integration():
    exit_code = hmm_gates.run_tests("config/pre_registration_v2.yaml")
    assert exit_code == 0


# --- Vol Gates Tests ---

def test_vol_estimators_causal_output():
    rng = np.random.default_rng(42)
    ret = pd.Series(
        rng.standard_normal(400) * 0.015,
        index=pd.bdate_range("2020-01-02", periods=400)
    )
    ha = vol_gates.har_abs_sigma_series(ret)
    hs = vol_gates.har_sq_sigma_series(ret)
    wv = vol_gates.wavelet_leaders_sigma_series(ret)

    assert len(ha) == 400
    assert len(hs) == 400
    assert len(wv) == 400
    assert ha.notna().sum() > 100
    assert hs.notna().sum() > 100
    assert wv.notna().sum() > 100


def test_vol_garch_panel_passes_gate():
    panel = vol_gates._pass_panel()
    res = vol_gates.test_cross_estimator_correlation(panel, min_correlation=0.70)
    assert res["passed"] is True
    assert res["min_pairwise_corr"] >= 0.70


def test_vol_null_white_noise_fails_gate():
    panel = vol_gates._null_panel("white", seed=42)
    res = vol_gates.test_cross_estimator_correlation(panel, min_correlation=0.70)
    assert res["passed"] is False
    assert res["min_pairwise_corr"] < 0.70


def test_vol_null_fast_vol_fails_gate():
    panel = vol_gates._null_panel("fast", seed=42)
    res = vol_gates.test_cross_estimator_correlation(panel, min_correlation=0.70)
    assert res["passed"] is False
    assert res["min_pairwise_corr"] < 0.70


def test_vol_run_tests_integration():
    exit_code = vol_gates.run_tests("config/pre_registration_v2.yaml")
    assert exit_code == 0


# --- Trailing CV Tests ---

def test_trailing_synthetic_panel_generator():
    panel, sector_map = trailing_cv.synthetic_panel(seed=20260919, tickers_per_sector=5)
    assert isinstance(panel, pd.DataFrame)
    assert panel.shape[0] == 1826
    assert panel.shape[1] == 35  # 7 sectors * 5 tickers
    assert len(sector_map) == 35


def test_trailing_cluster_bootstrap_p():
    rng = np.random.default_rng(42)
    positive_diffs = rng.normal(0.05, 0.02, 50)
    p_pos, mean_pos = trailing_cv.cluster_bootstrap_p(positive_diffs, rng, b=500)
    assert p_pos < 0.01
    assert mean_pos > 0.0

    negative_diffs = rng.normal(-0.05, 0.02, 50)
    p_neg, mean_neg = trailing_cv.cluster_bootstrap_p(negative_diffs, rng, b=500)
    assert p_neg > 0.90
    assert mean_neg < 0.0


def test_trailing_paired_diff_calculation():
    rng = np.random.default_rng(123)
    r = pd.Series(rng.normal(0.0005, 0.015, 252), index=pd.bdate_range("2020-01-02", periods=252))
    diff = trailing_cv.paired_diff_for_ticker(r, entry_i=0, end_i=250, ema_tau=5, k=1.5, floor_dd=0.13)
    assert isinstance(diff, float)


def test_trailing_nested_cv_synthetic_runs():
    cfg = trailing_cv.load_config("config/pre_registration_v2.yaml")
    panel, sector_map = trailing_cv.synthetic_panel(seed=20260919, tickers_per_sector=20)
    sector_map = pd.Series({t: trailing_cv.SECTOR_ALIAS.get(str(s), s) for t, s in sector_map.items()})

    res = trailing_cv.run_nested_cv(panel, sector_map, cfg, inner_folds=2, n_bootstrap=100)
    assert "passed" in res
    assert "folds" in res
    assert len(res["folds"]) == 4
    assert res["n_sector_tests"] == 28  # 4 outer folds * 7 sectors
