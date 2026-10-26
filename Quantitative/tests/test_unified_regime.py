"""
Unit Tests for Unified Regime Engine Components & Integration (Phase 9 v7 Architecture)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import numpy as np
import pytest

from Quantitative.bocpd import BOCPD, BOCPDConfig
from Quantitative.io_hmm import IOHMM, IOHMMConfig
from Quantitative.micro_states import MicroState, MicroStateClassifier, MicroStateConfig
from Quantitative.unified_regime import UnifiedRegimeEngine, UnifiedRegimeConfig
from Quantitative.calibration.train_unified_regime import train_and_serialize_params


def test_bocpd_initialization_and_update():
    cfg = BOCPDConfig(hazard_rate=0.01, threshold=0.95)
    bocpd = BOCPD(cfg)
    
    # Normal update returns dict
    res = bocpd.update(0.01)
    assert isinstance(res, dict)
    assert "alarm_triggered" in res
    assert "p_r0" in res
    assert 0.0 <= res["p_r0"] <= 1.0


def test_io_hmm_belief_update():
    cfg = IOHMMConfig(n_states=3, state_names=("EXPANSION", "CHOPPY", "CRISIS"))
    io_hmm = IOHMM(cfg)
    
    inputs = np.array([0.0, 0.0, 0.15, 0.02, 0.01, 18.0])
    res = io_hmm.update(inputs)
    
    assert "belief" in res
    belief = res["belief"]
    assert len(belief) == 3
    assert np.isclose(np.sum(belief), 1.0)


def test_micro_state_classification():
    classifier = MicroStateClassifier()
    state, scores = classifier.classify_current()
    assert isinstance(scores, dict)


def test_unified_regime_integration():
    engine = UnifiedRegimeEngine()
    snapshot = engine.update()
    
    assert "macro_state" in snapshot
    assert "regime_name" in snapshot
    assert snapshot["macro_state"] in ("EXPANSION", "CHOPPY", "CRISIS")


def test_params_serialization_roundtrip(tmp_path):
    out_file = tmp_path / "test_params.json"
    params = train_and_serialize_params(train_start="2010-01-01", train_end="2020-12-31", output_path=str(out_file))
    
    assert out_file.exists()
    with open(out_file, "r") as f:
        loaded = json.load(f)
    
    assert loaded["version"] in ("v7_unified", "v7_unified_tuned")
    assert "bocpd" in loaded
    assert "io_hmm" in loaded
    assert "micro_states" in loaded


def test_bocpd_hazard_rate_parameter_effect():
    cfg_low = BOCPDConfig(hazard_rate=0.001)
    bocpd_low = BOCPD(cfg_low)
    bocpd_low.initialize()

    cfg_high = BOCPDConfig(hazard_rate=0.1)
    bocpd_high = BOCPD(cfg_high)
    bocpd_high.initialize()

    res_low = bocpd_low.update(0.5)
    res_high = bocpd_high.update(0.5)

    assert bocpd_high._log_hazard(0) > bocpd_low._log_hazard(0)
    assert res_high["p_r0"] > res_low["p_r0"]


def test_bocpd_threshold_alarm_trigger():
    cfg = BOCPDConfig(hazard_rate=0.05, threshold=0.10, min_run_length=5)
    bocpd = BOCPD(cfg)
    bocpd.initialize()
    # Multiple extreme shocks to elevate P(r_t=0)
    for _ in range(5):
        res = bocpd.update(50.0)
    assert res["p_r0"] >= 0.01
    assert "alarm_triggered" in res


def test_bocpd_force_crisis_override_logic():
    engine = UnifiedRegimeEngine()
    engine.initialize()
    engine._micro._set_last_state(MicroState.GOLDILOCKS)
    # Force BOCPD state into alarm_triggered
    engine._bocpd.state.alarm_triggered = True
    engine._bocpd.state.p_r0 = 0.98

    decision = engine._decide_unified_regime()
    assert decision["macro_map"] == 2
    assert "CRISIS" in decision["regime_name"]
    assert "BOCPD FORCED" in decision["regime_name"]


def test_io_hmm_three_states_config():
    cfg = IOHMMConfig(n_states=3, state_names=("EXPANSION", "CHOPPY", "CRISIS"))
    assert len(cfg.state_names) == 3
    assert cfg.state_names[0] == "EXPANSION"
    assert cfg.state_names[1] == "CHOPPY"
    assert cfg.state_names[2] == "CRISIS"


def test_io_hmm_sticky_prior():
    cfg = IOHMMConfig(sticky_prior=10.0)
    io_hmm = IOHMM(cfg)
    io_hmm.initialize()
    assert np.all(np.diag(io_hmm.state.trans_base) == 10.0)


def test_io_hmm_emission_probabilities():
    cfg = IOHMMConfig(n_states=3)
    io_hmm = IOHMM(cfg)
    io_hmm.initialize()
    u = np.array([0.1, 0.0, 0.15, 0.02, 0.01, 18.0])
    for s in range(3):
        log_prob = io_hmm._emission_log_prob(u, s)
        assert np.isfinite(log_prob)
        assert log_prob <= 0.0


def test_io_hmm_transition_matrix():
    cfg = IOHMMConfig(n_states=3)
    io_hmm = IOHMM(cfg)
    io_hmm.initialize()
    u = np.zeros(6)
    trans = io_hmm._compute_transition_probs(u)
    assert trans.shape == (3, 3)
    assert np.allclose(trans.sum(axis=1), 1.0)


def test_micro_states_goldilocks_conditions():
    classifier = MicroStateClassifier()
    feats = {
        "cpi_yoy": 2.0, "real_rate": 1.0, "hy_oas": 120, "vix": 15, "curve_10y2y": 80,
        "stl_fsi": 0.0, "baa_aaa_spread": 0.5, "sahm_signal": 0.0
    }
    state, scores = classifier.classify(feats)
    assert state == MicroState.GOLDILOCKS
    assert scores[MicroState.GOLDILOCKS] > 0.5


def test_micro_states_late_cycle_drag_conditions():
    classifier = MicroStateClassifier()
    feats = {
        "cpi_yoy": 2.8, "real_rate": 1.8, "hy_oas": 250, "vix": 22, "curve_10y2y": -10,
        "stl_fsi": 0.2, "baa_aaa_spread": 1.0, "sahm_signal": 0.1
    }
    state, scores = classifier.classify(feats)
    assert state == MicroState.LATE_CYCLE_DRAG


def test_micro_states_liquidity_crunch_conditions():
    classifier = MicroStateClassifier()
    feats = {
        "cpi_yoy": 2.5, "real_rate": 2.5, "hy_oas": 450, "vix": 35, "curve_10y2y": 20,
        "stl_fsi": 2.0, "baa_aaa_spread": 2.0, "sahm_signal": 0.6
    }
    state, scores = classifier.classify(feats)
    assert state == MicroState.LIQUIDITY_CRUNCH


def test_micro_states_stagflation_conditions():
    classifier = MicroStateClassifier()
    feats = {
        "cpi_yoy": 4.5, "real_rate": 1.5, "hy_oas": 180, "vix": 22, "curve_10y2y": 30,
        "stl_fsi": 0.5, "baa_aaa_spread": 0.8, "sahm_signal": 0.2
    }
    state, scores = classifier.classify(feats)
    assert state == MicroState.STAGFLATION


def test_micro_states_pipeline_actions_mapping():
    classifier = MicroStateClassifier()
    
    classifier._set_last_state(MicroState.GOLDILOCKS)
    actions_g = classifier.get_pipeline_actions(MicroState.GOLDILOCKS)
    assert actions_g["action"] == "LOWER_HURDLE_RATES"
    assert actions_g["wacc_adjustment"] == -0.5

    classifier._set_last_state(MicroState.LIQUIDITY_CRUNCH)
    actions_lc = classifier.get_pipeline_actions(MicroState.LIQUIDITY_CRUNCH)
    assert actions_lc["action"] == "DOWN_FILTER_SHIFT_TO_BIL"
    assert actions_lc["lambda_stress_mult"] == 2.0


def test_micro_states_wacc_adjustments():
    classifier = MicroStateClassifier()
    classifier._set_last_state(MicroState.GOLDILOCKS)
    g_act = classifier.get_pipeline_actions(MicroState.GOLDILOCKS)
    assert g_act["wacc_adjustment"] == -0.5

    classifier._set_last_state(MicroState.LATE_CYCLE_DRAG)
    l_act = classifier.get_pipeline_actions(MicroState.LATE_CYCLE_DRAG)
    assert l_act["wacc_adjustment"] == 0.25


def test_unified_regime_integration_weights_and_thresholds():
    cfg = UnifiedRegimeConfig(
        bocpd_override_weight=0.8,
        io_hmm_weight=1.0,
        micro_weight=1.0,
        crisis_threshold=0.7,
        choppy_threshold=0.4,
        min_dwell_days=10,
        max_transitions_per_year=6,
    )
    engine = UnifiedRegimeEngine(cfg)
    assert engine.config.crisis_threshold == 0.7
    assert engine.config.choppy_threshold == 0.4
    assert engine.config.min_dwell_days == 10


def test_unified_regime_dwell_constraint():
    engine = UnifiedRegimeEngine()
    engine.initialize()
    engine._micro._set_last_state(MicroState.GOLDILOCKS)
    engine._last_macro_state = 0  # EXPANSION
    engine._dwell_counter = 2      # Less than min_dwell_days (10)
    
    # Fake IO-HMM outputting state 1 (CHOPPY)
    engine._io_hmm.state.current_state_map = 1
    engine._io_hmm.state.current_belief = np.array([0.1, 0.8, 0.1])
    
    decision = engine._decide_unified_regime()
    # Dwell constraint should hold macro_map at previous state (0)
    assert decision["macro_map"] == 0


def test_unified_regime_frozen_params_json_tuned():
    path = Path("config/calibrated_unified_regime_params_tuned.json")
    assert path.exists()
    with open(path, "r") as f:
        data = json.load(f)
    assert data["version"] == "v7_unified_tuned"
    assert "bocpd" in data
    assert "io_hmm" in data
    assert "micro_states" in data
    assert "provenance" in data


def test_unified_regime_frozen_params_provenance():
    path = Path("config/calibrated_unified_regime_params_tuned.json")
    assert path.exists()
    with open(path, "r") as f:
        data = json.load(f)
    prov = data["provenance"]
    assert "sha256" in prov
    assert len(prov["sha256"]) == 64
    assert prov["date_range"]["min"] == "2010-01-01"
    assert prov["date_range"]["max"] == "2020-12-31"


def test_unified_regime_action_compilation_expansion():
    engine = UnifiedRegimeEngine()
    actions = engine._compile_actions(macro_map=0, micro_name="GOLDILOCKS")
    assert actions["macro_action"] == "HOLD"
    assert actions["micro_action"] == "LOWER_HURDLE_RATES"
    assert actions["wacc_adjustment_bps"] == -50


def test_unified_regime_action_compilation_choppy():
    engine = UnifiedRegimeEngine()
    actions = engine._compile_actions(macro_map=1, micro_name="LATE_CYCLE_DRAG")
    assert actions["macro_action"] == "DEFENSIVE"
    assert actions["micro_action"] == "TIGHTEN_ENTRY"
    assert actions["wacc_adjustment_bps"] == 25


def test_unified_regime_action_compilation_crisis():
    engine = UnifiedRegimeEngine()
    actions = engine._compile_actions(macro_map=2, micro_name="LIQUIDITY_CRUNCH")
    assert actions["macro_action"] == "CRISIS_MODE"
    assert actions["micro_action"] == "DOWN_FILTER_SHIFT_TO_BIL"
    assert actions["wacc_adjustment_bps"] == 100
    assert actions["allocation_shifts"]["equity"] == -0.3

