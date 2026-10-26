"""
Unit Tests for Unified Regime Calibration & Serialization (Phase 9 v7)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import pytest

from Quantitative.calibration.train_unified_regime import (
    train_and_serialize_params,
    main,
)


def test_calibration_output_file_created(tmp_path):
    out_file = tmp_path / "calibrated_params.json"
    params = train_and_serialize_params(
        train_start="2010-01-01",
        train_end="2020-12-31",
        output_path=str(out_file),
    )
    assert out_file.exists()
    assert isinstance(params, dict)


def test_calibration_schema_version_and_keys(tmp_path):
    out_file = tmp_path / "calibrated_params.json"
    params = train_and_serialize_params(
        train_start="2010-01-01",
        train_end="2020-12-31",
        output_path=str(out_file),
    )
    assert params["version"] == "v7_unified_tuned"
    assert params["trained_on"] == "2010-01-01 to 2020-12-31"
    assert "bocpd" in params
    assert "io_hmm" in params
    assert "micro_states" in params
    assert "integration" in params
    assert "provenance" in params


def test_calibration_bocpd_params(tmp_path):
    out_file = tmp_path / "calibrated_params.json"
    params = train_and_serialize_params(output_path=str(out_file))
    bocpd = params["bocpd"]
    assert bocpd["hazard_rate"] == 0.015
    assert bocpd["threshold"] == 0.90
    assert bocpd["alarm_action"] == "FORCE_CRISIS"
    assert bocpd["observation"] == "credit_spread_velocity_21d"


def test_calibration_io_hmm_params(tmp_path):
    out_file = tmp_path / "calibrated_params.json"
    params = train_and_serialize_params(output_path=str(out_file))
    io_hmm = params["io_hmm"]
    assert io_hmm["n_states"] == 3
    assert io_hmm["state_names"] == ["EXPANSION", "SHOCK", "CRISIS"]
    assert len(io_hmm["input_features"]) == 6
    assert io_hmm["sticky_prior"] == 5.0


def test_calibration_micro_states_params(tmp_path):
    out_file = tmp_path / "calibrated_params.json"
    params = train_and_serialize_params(output_path=str(out_file))
    micro = params["micro_states"]
    assert micro["enabled"] is True
    assert len(micro["states"]) == 4
    state_names = [s["name"] for s in micro["states"]]
    assert "GOLDILOCKS" in state_names
    assert "LATE_CYCLE_DRAG" in state_names
    assert "LIQUIDITY_CRUNCH" in state_names
    assert "STAGFLATION" in state_names


def test_calibration_integration_params(tmp_path):
    out_file = tmp_path / "calibrated_params.json"
    params = train_and_serialize_params(output_path=str(out_file))
    integration = params["integration"]
    assert integration["bocpd_override_weight"] == 0.8
    assert integration["io_hmm_weight"] == 1.0
    assert integration["min_dwell_days"] == 5
    assert integration["max_transitions_per_year"] == 8


def test_calibration_sha256_provenance_verification(tmp_path):
    out_file = tmp_path / "calibrated_params.json"
    params = train_and_serialize_params(output_path=str(out_file))
    sha = params["provenance"]["sha256"]
    assert isinstance(sha, str)
    assert len(sha) == 64


def test_calibration_custom_date_range(tmp_path):
    out_file = tmp_path / "custom_params.json"
    params = train_and_serialize_params(
        train_start="2012-01-01",
        train_end="2018-12-31",
        output_path=str(out_file),
    )
    assert params["trained_on"] == "2012-01-01 to 2018-12-31"
    assert params["provenance"]["date_range"]["min"] == "2012-01-01"
    assert params["provenance"]["date_range"]["max"] == "2018-12-31"


def test_calibration_mkdir_parent(tmp_path):
    nested_dir = tmp_path / "sub" / "dir"
    out_file = nested_dir / "params.json"
    train_and_serialize_params(output_path=str(out_file))
    assert out_file.exists()


def test_calibration_main_cli_execution(tmp_path, monkeypatch):
    out_file = tmp_path / "cli_params.json"
    test_args = [
        "train_unified_regime.py",
        "--train-start", "2015-01-01",
        "--train-end", "2020-12-31",
        "--output", str(out_file),
    ]
    monkeypatch.setattr("sys.argv", test_args)

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert out_file.exists()
