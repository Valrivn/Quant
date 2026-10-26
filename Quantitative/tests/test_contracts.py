"""
Unit Tests for Feature Contracts and State Definitions (Phase 9 v7 Architecture)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from Quantitative.contracts.feature_contract import (
    CANONICAL_FEATURES,
    CANONICAL_STATES_2,
    CANONICAL_STATES_3,
    FEATURE_BOUNDS,
    FORBIDDEN_SERIES,
    F_INDEX_CREDIT_SPREAD_VELOCITY,
    F_INDEX_FINANCIAL_STRESS,
    F_INDEX_REALIZED_VOL,
    F_INDEX_REAL_RATE,
    F_INDEX_UNANTICIPATED_INFLATION,
    F_INDEX_YIELD_CURVE,
    MANDATORY_FRED_SERIES,
    N_CANONICAL_FEATURES,
    REQUIRED_FRED_SERIES,
    STATE_SORT_FEATURE_INDEX,
    VALIDATION_CONTRACT,
    assert_canonical_names,
    assert_no_forbidden_series,
    assert_no_synthetic_data,
    validate_feature_matrix_shape,
    validate_feature_names,
    validate_state_names,
)


def test_canonical_feature_count():
    assert len(CANONICAL_FEATURES) == 6
    assert N_CANONICAL_FEATURES == 6
    assert CANONICAL_FEATURES[0] == "CREDIT_SPREAD_VELOCITY_21D"


def test_validate_feature_matrix_shape():
    valid_matrix = np.zeros((100, 6))
    validate_feature_matrix_shape(valid_matrix, context="test")

    invalid_matrix = np.zeros((100, 5))
    with pytest.raises(AssertionError, match="Expected 6 features, got 5"):
        validate_feature_matrix_shape(invalid_matrix, context="test")


def test_validate_feature_names():
    names = list(CANONICAL_FEATURES)
    validate_feature_names(names, context="test")

    bad_names = names[:-1] + ["EXTRA"]
    with pytest.raises(AssertionError):
        validate_feature_names(bad_names, context="test")


def test_validate_state_names():
    validate_state_names(["EXPANSION", "SHOCK", "CRISIS"], n_states=3)
    validate_state_names(["NORMAL", "CRISIS"], n_states=2)

    with pytest.raises(AssertionError):
        validate_state_names(["EXPANSION", "CHOPPY", "CRISIS"], n_states=2)


def test_forbidden_series_denylist():
    assert_no_forbidden_series(["BAMLH0A0HYM2", "DGS10"])
    with pytest.raises(AssertionError, match="Forbidden series contract violation"):
        assert_no_forbidden_series(["BAMLH0A0HYM2", "CESI"])


def test_feature_indices_deterministic_bindings():
    assert F_INDEX_CREDIT_SPREAD_VELOCITY == 0
    assert F_INDEX_YIELD_CURVE == 1
    assert F_INDEX_REALIZED_VOL == 2
    assert F_INDEX_FINANCIAL_STRESS == 3
    assert F_INDEX_REAL_RATE == 4
    assert F_INDEX_UNANTICIPATED_INFLATION == 5
    assert CANONICAL_FEATURES[F_INDEX_CREDIT_SPREAD_VELOCITY] == "CREDIT_SPREAD_VELOCITY_21D"
    assert CANONICAL_FEATURES[F_INDEX_YIELD_CURVE] == "YIELD_CURVE_10Y2Y"
    assert CANONICAL_FEATURES[F_INDEX_REALIZED_VOL] == "REALIZED_VOLATILITY_21D"
    assert CANONICAL_FEATURES[F_INDEX_FINANCIAL_STRESS] == "FINANCIAL_STRESS_INDEX"
    assert CANONICAL_FEATURES[F_INDEX_REAL_RATE] == "REAL_RATE_10Y"
    assert CANONICAL_FEATURES[F_INDEX_UNANTICIPATED_INFLATION] == "UNANTICIPATED_INFLATION"


def test_cesi_eliminated_from_canonical():
    assert "CESI" not in CANONICAL_FEATURES
    assert "CESI_MACRO_SURPRISE" not in CANONICAL_FEATURES
    assert "CITI_ECONOMIC_SURPRISE" not in CANONICAL_FEATURES
    assert "CESI" in FORBIDDEN_SERIES
    assert "CITIE" in FORBIDDEN_SERIES


def test_state_sort_key_is_credit_spread_velocity():
    assert STATE_SORT_FEATURE_INDEX == F_INDEX_CREDIT_SPREAD_VELOCITY
    assert STATE_SORT_FEATURE_INDEX == 0


def test_assert_no_synthetic_data_pass():
    df = pd.DataFrame({
        "CREDIT_SPREAD_VELOCITY_21D": [0.1, 0.2, -0.05],
        "YIELD_CURVE_10Y2Y": [150.0, 140.0, 145.0],
        "REALIZED_VOLATILITY_21D": [0.12, 0.15, 0.14],
        "FINANCIAL_STRESS_INDEX": [-0.5, 0.1, 0.0],
        "REAL_RATE_10Y": [1.2, 1.3, 1.25],
        "UNANTICIPATED_INFLATION": [0.5, 0.4, 0.6],
    })
    assert_no_synthetic_data(df)


def test_assert_no_synthetic_data_all_zeros_fails():
    df = pd.DataFrame({
        "CREDIT_SPREAD_VELOCITY_21D": [0.0, 0.0, 0.0],
        "YIELD_CURVE_10Y2Y": [100.0, 100.0, 100.0],
    })
    with pytest.raises(RuntimeError, match="Synthetic/fallback data detected"):
        assert_no_synthetic_data(df)


def test_assert_no_synthetic_data_nan_fails():
    df = pd.DataFrame({
        "CREDIT_SPREAD_VELOCITY_21D": [0.1, np.nan, 0.2],
    })
    with pytest.raises(RuntimeError, match="Missing authentic PIT data"):
        assert_no_synthetic_data(df)


def test_feature_bounds_complete():
    for f in CANONICAL_FEATURES:
        assert f in FEATURE_BOUNDS
        bounds = FEATURE_BOUNDS[f]
        assert bounds.min_val < bounds.max_val
        assert bounds.transform in ("velocity_21d", "level", "zscore_tanh", "diff_yoy")


def test_validation_contract_defaults():
    vc = VALIDATION_CONTRACT
    assert vc.n_states == 3
    assert vc.folds_required == 3
    assert vc.ari_threshold == 0.45
    assert vc.max_state_occupancy == 0.60
    assert vc.crisis_occupancy_cap == 0.20
    assert vc.action_on_fail == "abort_pipeline"


def test_mandatory_fred_series_count():
    assert len(MANDATORY_FRED_SERIES) == 10
    expected_series = {"BAMLH0A0HYM2", "DGS10", "DGS2", "STLFSI2", "DFII10", "CPIAUCSL", "T10YIE", "BAA", "AAA", "DTB3"}
    assert set(MANDATORY_FRED_SERIES) == expected_series


def test_required_fred_series_mapping():
    assert len(REQUIRED_FRED_SERIES) == 6
    for f in CANONICAL_FEATURES:
        assert f in REQUIRED_FRED_SERIES
        assert len(REQUIRED_FRED_SERIES[f]) > 0


def test_assert_canonical_names_valid():
    assert_canonical_names(list(CANONICAL_FEATURES), context="valid_test")


def test_assert_canonical_names_forbidden_raises():
    bad_list = ["CREDIT_SPREAD_VELOCITY_21D", "CESI", "REALIZED_VOLATILITY_21D", "FINANCIAL_STRESS_INDEX", "REAL_RATE_10Y", "UNANTICIPATED_INFLATION"]
    with pytest.raises(AssertionError, match="Forbidden series contract violation"):
        assert_canonical_names(bad_list, context="forbidden_test")


def test_validate_feature_matrix_shape_1d_raises():
    X_1d = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    with pytest.raises(AssertionError, match="Expected 6 features, got 0"):
        validate_feature_matrix_shape(X_1d, context="1d_test")


def test_canonical_states_content():
    assert CANONICAL_STATES_2 == ["NORMAL", "CRISIS"]
    assert CANONICAL_STATES_3 == ["EXPANSION", "SHOCK", "CRISIS"]


def test_assert_no_synthetic_data_specific_cols():
    df = pd.DataFrame({
        "REAL_FEAT": [0.1, 0.2, 0.3],
        "DUMMY_FEAT": [0.0, 0.0, 0.0],
    })
    # Passing only REAL_FEAT should succeed
    assert_no_synthetic_data(df, feature_cols=["REAL_FEAT"])
    # Passing DUMMY_FEAT should raise
    with pytest.raises(RuntimeError):
        assert_no_synthetic_data(df, feature_cols=["DUMMY_FEAT"])

