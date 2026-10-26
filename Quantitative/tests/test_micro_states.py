"""
Unit Tests for Tier 2 Micro-State Classifiers (Economic Contextual Engines)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from Quantitative.micro_states import (
    MicroState,
    MicroStateClassifier,
    MicroStateConfig,
    build_micro_classifier,
)


def test_micro_state_enum_members():
    states = [s.value for s in MicroState]
    assert "GOLDILOCKS" in states
    assert "LATE_CYCLE_DRAG" in states
    assert "LIQUIDITY_CRUNCH" in states
    assert "STAGFLATION" in states
    assert len(MicroState) == 4


def test_micro_state_config_thresholds():
    cfg = MicroStateConfig()
    assert cfg.goldilocks_inflation_max == 2.5
    assert cfg.goldilocks_real_rate_max == 1.5
    assert cfg.goldilocks_spread_max == 150
    assert cfg.late_cycle_curve_max == 0
    assert cfg.crunch_real_rate_min == 2.0
    assert cfg.crunch_spread_min == 400
    assert cfg.stagflation_cpi_min == 3.5


def test_goldilocks_classification():
    classifier = build_micro_classifier()
    feats = {
        "cpi_yoy": 2.0, "real_rate": 1.0, "hy_oas": 120, "vix": 16, "curve_10y2y": 75,
        "stl_fsi": -0.2, "baa_aaa_spread": 0.5, "sahm_signal": 0.0
    }
    state, scores = classifier.classify(feats)
    assert state == MicroState.GOLDILOCKS
    assert scores[MicroState.GOLDILOCKS] > 0.5
    classifier._set_last_state(state)
    actions = classifier.get_pipeline_actions(state)
    assert actions["action"] == "LOWER_HURDLE_RATES"
    assert actions["wacc_adjustment"] == -0.5
    assert actions["growth_allocation"] == "MAXIMIZE"


def test_late_cycle_drag_classification():
    classifier = build_micro_classifier()
    feats = {
        "cpi_yoy": 3.0, "real_rate": 1.8, "hy_oas": 250, "vix": 22, "curve_10y2y": -15,
        "stl_fsi": 0.4, "baa_aaa_spread": 1.1, "sahm_signal": 0.1
    }
    state, scores = classifier.classify(feats)
    assert state == MicroState.LATE_CYCLE_DRAG
    classifier._set_last_state(state)
    actions = classifier.get_pipeline_actions(state)
    assert actions["action"] == "TIGHTEN_ENTRY_TRIM_CYCLICALS"
    assert actions["wacc_adjustment"] == 0.25
    assert actions["cyclical_trim"] is True


def test_liquidity_crunch_classification():
    classifier = build_micro_classifier()
    feats = {
        "cpi_yoy": 2.8, "real_rate": 2.5, "hy_oas": 500, "vix": 38, "curve_10y2y": 10,
        "stl_fsi": 2.1, "baa_aaa_spread": 2.5, "sahm_signal": 0.8
    }
    state, scores = classifier.classify(feats)
    assert state == MicroState.LIQUIDITY_CRUNCH
    classifier._set_last_state(state)
    actions = classifier.get_pipeline_actions(state)
    assert actions["action"] == "DOWN_FILTER_SHIFT_TO_BIL"
    assert actions["lambda_stress_mult"] == 2.0
    assert actions["bil_allocation"] == "MAXIMIZE"


def test_stagflation_classification():
    classifier = build_micro_classifier()
    feats = {
        "cpi_yoy": 5.2, "real_rate": 1.8, "hy_oas": 190, "vix": 24, "curve_10y2y": 45,
        "stl_fsi": 0.3, "baa_aaa_spread": 0.9, "sahm_signal": 0.3
    }
    state, scores = classifier.classify(feats)
    assert state == MicroState.STAGFLATION
    classifier._set_last_state(state)
    actions = classifier.get_pipeline_actions(state)
    assert actions["action"] == "ROUTE_CASH_TO_GOLD"
    assert actions["gold_allocation"] == "INCREASE"


def test_score_softmax_normalization():
    classifier = build_micro_classifier()
    feats = {
        "cpi_yoy": 2.0, "real_rate": 1.0, "hy_oas": 120, "vix": 16, "curve_10y2y": 75,
        "stl_fsi": -0.2, "baa_aaa_spread": 0.5, "sahm_signal": 0.0
    }
    _, scores = classifier.classify(feats)
    total_score = sum(scores.values())
    assert np.isclose(total_score, 1.0)
    assert all(0.0 <= v <= 1.0 for v in scores.values())


def test_classify_current_real_fred_data():
    classifier = build_micro_classifier()
    state, scores = classifier.classify_current()
    assert isinstance(state, MicroState)
    assert isinstance(scores, dict)
    assert len(scores) == 4


def test_classifier_history_audit_trail():
    classifier = build_micro_classifier()
    feats = {
        "cpi_yoy": 2.0, "real_rate": 1.0, "hy_oas": 120, "vix": 16, "curve_10y2y": 75,
        "stl_fsi": -0.2, "baa_aaa_spread": 0.5, "sahm_signal": 0.0
    }
    classifier.classify(feats)
    assert len(classifier.history) == 1
    last_hist = classifier.history[-1]
    assert "features" in last_hist
    assert "scores" in last_hist
    assert "state" in last_hist


def test_ambiguous_conditions_uniform_fallback():
    classifier = build_micro_classifier()
    # Features that match NO micro-state criteria (e.g. CPI high but real_rate low, curve flat, etc.)
    feats = {
        "cpi_yoy": 3.0, "real_rate": 0.5, "hy_oas": 180, "vix": 22, "curve_10y2y": 25,
        "stl_fsi": 0.5, "baa_aaa_spread": 1.0, "sahm_signal": 0.1
    }
    state, scores = classifier.classify(feats)
    assert isinstance(state, MicroState)
    assert np.isclose(sum(scores.values()), 1.0)
