"""
Unit Tests for Phase 9 Full Pipeline Atomic Orchestrator
"""

from __future__ import annotations

import json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from research.phase9_full_pipeline import (
    FROZEN_CONFIG_PATH,
    GateEvaluationUnavailable,
    Phase9Orchestrator,
    verify_preregistration_hash,
)
from research.ml_parameter_optimization import (
    PurgedEmbargoCV,
    generate_lhs_sobol_samples,
    run_joint_optimization,
)


def test_verify_preregistration_hash():
    # Verify pre-registration config exists and matches its sha256 sidecar
    res = verify_preregistration_hash()
    assert isinstance(res, bool)
    assert res is True


def test_execute_hmm_gate_success():
    orchestrator = Phase9Orchestrator(output_dir="data/test_phase9_tmp")
    gate_res = orchestrator.execute_hmm_gate()
    assert gate_res is True


def test_execute_hmm_gate_missing_file_raises(tmp_path, monkeypatch):
    orchestrator = Phase9Orchestrator(output_dir=str(tmp_path))
    monkeypatch.setattr("research.phase9_full_pipeline.Path", lambda p: tmp_path / "non_existent.json")
    with pytest.raises(GateEvaluationUnavailable, match="unified regime params missing"):
        orchestrator.execute_hmm_gate()


def test_evaluate_gates_without_evidence_raises():
    orchestrator = Phase9Orchestrator()
    with pytest.raises(GateEvaluationUnavailable, match="No OOS evidence supplied"):
        orchestrator.evaluate_gates(evidence=None)


def test_evaluate_gates_incomplete_evidence_raises():
    orchestrator = Phase9Orchestrator()
    incomplete_evidence = {"oos_returns": pd.Series([0.01, 0.02])}
    with pytest.raises(GateEvaluationUnavailable, match="OOS evidence is incomplete"):
        orchestrator.evaluate_gates(evidence=incomplete_evidence)


def test_write_halt_record(tmp_path):
    orchestrator = Phase9Orchestrator(output_dir=str(tmp_path))
    halt_path = orchestrator.write_halt_record(reason="Testing halt record writing")
    assert halt_path.exists()
    assert halt_path.with_suffix(halt_path.suffix + ".sha256").exists()

    content = json.loads(halt_path.read_text(encoding="utf-8"))
    assert content["status"] == "NOT_EXECUTED"
    assert content["gate_evaluated"] is False
    assert content["reason"] == "Testing halt record writing"


def test_generate_lhs_sobol_samples():
    samples = generate_lhs_sobol_samples(n_samples=16, seed=42)
    assert len(samples) == 16
    for s in samples:
        assert isinstance(s, dict)
        assert len(s) > 0


def test_purged_embargo_cv_splits():
    cv = PurgedEmbargoCV(n_splits=4, embargo_months=12, purge_multiplier=5)
    X = np.random.normal(0, 1, (200, 5))
    splits = list(cv.split(X))
    assert len(splits) == 4
    for train_idx, test_idx in splits:
        assert len(train_idx) > 0
        assert len(test_idx) > 0


def test_execute_parameter_search_dry_run():
    orchestrator = Phase9Orchestrator()
    best_params = orchestrator.execute_parameter_search(n_samples=10, dry_run=True)
    assert isinstance(best_params, dict)
    assert len(orchestrator.tracker.trials) == 13 + 10  # 13 base + 10 LHS dry run


def test_evaluate_gates_with_mock_evidence(tmp_path):
    orchestrator = Phase9Orchestrator(output_dir=str(tmp_path))
    months = pd.date_range("2018-01-01", periods=84, freq="ME")
    returns = pd.Series(np.full(84, 0.02), index=months)
    weights = pd.DataFrame({"AAPL": np.full(84, 0.5)}, index=months)

    evidence = {
        "oos_returns": returns,
        "arm_c_weights": weights,
        "spy_returns": returns,
        "hmm_ari": 0.80,
        "swap_churn": 2.0,
        "tca_bps": 80.0,
        "probation_pass_rate": 0.50,
    }

    res = orchestrator.evaluate_gates(evidence=evidence)
    assert "composite_dsr" in res
    assert "all_gates_pass" in res
    assert isinstance(res["all_gates_pass"], bool)
