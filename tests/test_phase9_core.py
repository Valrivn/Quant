"""
test_phase9_core.py — Unit test suite for Phase 9 Core Infrastructure
"""

import tempfile
import numpy as np
import pandas as pd
import pytest

from Quantitative.macro_pca import MacroPCA, generate_pit_macro_data
from Quantitative.regime_shield_v3 import FactorHMMv3, validate_regime_shield_v3, build_factor_hmm
from Quantitative.swap_barrier_v3 import SwapBarrierV3, exponential_decay
from research.ml_parameter_optimization import (
    PurgedEmbargoCV,
    TrialTracker,
    GroupLasso,
    generate_lhs_sobol_samples,
    run_joint_optimization,
)
from research.phase9_full_pipeline import Phase9Orchestrator, verify_preregistration_hash


def test_macro_pca_output_and_variance():
    df = generate_pit_macro_data()
    pca = MacroPCA(n_components=3, lookback=252)
    pcs = pca.fit_transform_pit(df)

    assert pcs.shape == (len(df), 3)
    assert set(pcs.columns) == {"PC1", "PC2", "PC3"}
    assert pca.validate_variance_explained(pcs)
    assert pca.latest_explained_variance_ > 0.80


def test_regime_shield_v3_validation():
    passed = validate_regime_shield_v3(
        folds=4,
        ari_threshold=0.70,
        occupancy_cap=0.60,
        crisis_cap=0.20,
        min_sojourn=5,
    )
    assert passed is True


def test_hmm_monotonicity_and_occupancy():
    df = generate_pit_macro_data()
    hmm, pcs, probs, states = build_factor_hmm(df)

    assert probs.shape == (len(df), 3)
    assert len(states) == len(df)

    is_mono, ke_vals = hmm.check_monotonicity()
    assert is_mono is True
    assert ke_vals[0] < ke_vals[1] < ke_vals[2]

    occ_pass, occ_map = hmm.validate_occupancy_caps(states)
    assert occ_pass is True
    assert occ_map["CRISIS"] <= 0.20
    assert max(occ_map.values()) <= 0.60


def test_swap_barrier_decay():
    C = 0.0250
    b0 = exponential_decay(C=C, gamma=0.25, t_target=30.0, t_held=0.0)
    b30 = exponential_decay(C=C, gamma=0.25, t_target=30.0, t_held=30.0)
    assert b0 > b30

    sb = SwapBarrierV3(C=C, gamma=0.25, t_target=30.0)
    churn = sb.simulate_churn(n_periods_months=120) / 10.0
    assert churn < 5.0


def test_ml_parameter_optimization_sobol():
    samples = generate_lhs_sobol_samples(n_samples=50)
    assert len(samples) == 50
    for s in samples:
        assert "ke_var_threshold" in s
        assert "swap_barrier_C" in s
        assert 0.0075 <= s["ke_var_threshold"] <= 0.0100


def test_trial_tracker_ledger():
    tt = TrialTracker(base_n=13, target_n=513)
    tt.log_trial(1, {"param": 1}, 1.25)
    dsr = tt.compute_dsr(best_sharpe=1.25)
    assert dsr > 0.0


def test_preregistration_hash_verification():
    valid = verify_preregistration_hash()
    assert valid is True


def test_phase9_orchestrator_dry_run():
    with tempfile.TemporaryDirectory() as tmpdir:
        orchestrator = Phase9Orchestrator(output_dir=tmpdir)
        summary = orchestrator.run_pipeline(n_samples=10, dry_run=True)
        assert summary["status"] == "SUCCESS"
        assert summary["gates_summary"]["all_gates_pass"] is True
