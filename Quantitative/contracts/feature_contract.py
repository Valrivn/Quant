"""
Canonical Feature Contract — Single Source of Truth for All Regime Features

This module defines the IMMUTABLE feature contract across the entire codebase.
Any change to feature names, indices, or bounds requires a new pre-registration
version and SHA-256 re-pin.

Pre-Registration: config/pre_registration_v6.yaml (or v7+ when amended)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, List, Dict, Tuple

# ═══════════════════════════════════════════════════════════════════════════
# CANONICAL FEATURE DEFINITIONS (6 features, fixed order)
# ═══════════════════════════════════════════════════════════════════════════

# The 6 canonical macro features for HMM regime detection.
# Order is FROZEN — index 0 is the credit-spread sort key for state labeling.
CANONICAL_FEATURES: Final[List[str]] = [
    "CREDIT_SPREAD_VELOCITY_21D",   # Index 0: Δ(HY_OAS or BAA10Y)_21d — PRIMARY SORT KEY
    "YIELD_CURVE_10Y2Y",            # Index 1: 10Y minus 2Y slope (bps)
    "REALIZED_VOLATILITY_21D",      # Index 2: 21-day rolling realized vol (annualized)
    "FINANCIAL_STRESS_INDEX",       # Index 3: STLFSI2 level
    "REAL_RATE_10Y",                # Index 4: DFII10 (10-Year TIPS real yield)
    "UNANTICIPATED_INFLATION",      # Index 5: CPI YoY minus 10Y Breakeven (T10YIE)
]

# Deterministic index bindings (NEVER change without pre-reg version bump)
F_INDEX_CREDIT_SPREAD_VELOCITY: Final[int] = 0
F_INDEX_YIELD_CURVE: Final[int] = 1
F_INDEX_REALIZED_VOL: Final[int] = 2
F_INDEX_FINANCIAL_STRESS: Final[int] = 3
F_INDEX_REAL_RATE: Final[int] = 4
F_INDEX_UNANTICIPATED_INFLATION: Final[int] = 5

N_CANONICAL_FEATURES: Final[int] = len(CANONICAL_FEATURES)  # == 6


# ═══════════════════════════════════════════════════════════════════════════
# FEATURE BOUNDS & STATIONARITY TRANSFORMS
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FeatureBounds:
    """Hard bounds for each canonical feature after stationarity transform."""
    min_val: float
    max_val: float
    transform: str  # "velocity_21d", "level", "zscore_tanh", "diff_yoy"


FEATURE_BOUNDS: Final[Dict[str, FeatureBounds]] = {
    "CREDIT_SPREAD_VELOCITY_21D": FeatureBounds(
        min_val=-5.0, max_val=5.0, transform="velocity_21d"
    ),
    "YIELD_CURVE_10Y2Y": FeatureBounds(
        min_val=-300.0, max_val=300.0, transform="level"
    ),
    "REALIZED_VOLATILITY_21D": FeatureBounds(
        min_val=0.0, max_val=1.0, transform="level"  # annualized vol fraction
    ),
    "FINANCIAL_STRESS_INDEX": FeatureBounds(
        min_val=-3.0, max_val=5.0, transform="level"  # STLFSI2 standardized
    ),
    "REAL_RATE_10Y": FeatureBounds(
        min_val=-2.0, max_val=5.0, transform="level"  # DFII10 in %
    ),
    "UNANTICIPATED_INFLATION": FeatureBounds(
        min_val=-5.0, max_val=5.0, transform="diff_yoy"
    ),
}


# ═══════════════════════════════════════════════════════════════════════════
# STATE CONTRACT
# ═══════════════════════════════════════════════════════════════════════════

# Canonical regime state names (2-state for v7+; 3-state legacy)
CANONICAL_STATES_2: Final[List[str]] = ["NORMAL", "CRISIS"]
CANONICAL_STATES_3: Final[List[str]] = ["EXPANSION", "SHOCK", "CRISIS"]

# State sort key: CREDIT_SPREAD_VELOCITY_21D (index 0) mean
# State 0 (NORMAL/EXPANSION) MUST have lower mean than State 1 (CRISIS/SHOCK)
STATE_SORT_FEATURE_INDEX: Final[int] = F_INDEX_CREDIT_SPREAD_VELOCITY


# ═══════════════════════════════════════════════════════════════════════════
# VALIDATION CONTRACT
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ValidationContract:
    """Pre-registered validation parameters (from pre_registration_v6.yaml hmm section)."""
    n_states: int = 3
    folds_required: int = 3
    ari_threshold: float = 0.45
    action_on_fail: str = "abort_pipeline"
    state_ordering: str = "credit_spread_mean"
    label_definition: str = "NBER recession (6m lag) + Sahm Rule (1m lag) -> 3-state: EXP/SHOCK/CRISIS"
    
    # Occupancy caps (v6)
    max_state_occupancy: float = 0.60
    crisis_occupancy_cap: float = 0.20
    
    # Diagnostic targets (not gates)
    precision_crisis_target: float = 0.50
    recall_crisis_target: float = 0.40
    f1_crisis_target: float = 0.44
    false_alarm_rate_max: float = 0.15
    mean_dwell_days_min: float = 63.0
    transitions_per_year_max: float = 6.0


VALIDATION_CONTRACT: Final[ValidationContract] = ValidationContract()


# ═══════════════════════════════════════════════════════════════════════════
# PROVENANCE CONTRACT
# ═══════════════════════════════════════════════════════════════════════════

REQUIRED_FRED_SERIES: Final[Dict[str, str]] = {
    "CREDIT_SPREAD_VELOCITY_21D": "BAMLH0A0HYM2",  # HY OAS for velocity
    "YIELD_CURVE_10Y2Y": "DGS10,DGS2",              # 10Y-2Y slope
    "REALIZED_VOLATILITY_21D": "FF5_Mkt_RF,FF5_RF", # Market returns for realized vol
    "FINANCIAL_STRESS_INDEX": "STLFSI2",
    "REAL_RATE_10Y": "DFII10",
    "UNANTICIPATED_INFLATION": "CPIAUCSL,T10YIE",   # CPI YoY - 10Y Breakeven
}

# Zero-synthetic assertion: these series MUST exist in FRED cache
MANDATORY_FRED_SERIES: Final[List[str]] = [
    "BAMLH0A0HYM2",  # HY OAS
    "DGS10",         # 10Y Treasury
    "DGS2",          # 2Y Treasury
    "STLFSI2",       # St. Louis Financial Stress Index
    "DFII10",        # 10Y TIPS real yield
    "CPIAUCSL",      # CPI All Urban
    "T10YIE",        # 10Y Breakeven Inflation
    "BAA",           # Baa Corporate Yield
    "AAA",           # Aaa Corporate Yield
    "DTB3",          # 3-Month T-Bill
]

# ───────────────────────────────────────────────────────────────────────────
# RETIRED SERIES DENYLIST
# ───────────────────────────────────────────────────────────────────────────
# Series that MUST NEVER appear in a feature matrix or a FRED manifest, because
# no authentic public series exists for them under that name and every prior
# attempt was silently filled with synthetic noise or a fabricated proxy.
#
# CESI (Citi Economic Surprise Index) is the canonical case: it is a proprietary,
# licensed index that is NOT published on FRED. `regime_shield_v3.py` filled it
# with `np.random.normal(0, 20, n)` (seed 45) and `hmm_regime.py` filled it with
# 21-day market momentum while labelling it "CESI". Both were pre-registered
# inputs to a 6-feature HMM.
#
# Its economic slot is now UNANTICIPATED_INFLATION (CPIAUCSL YoY - T10YIE),
# index 5 of the canonical contract.
FORBIDDEN_SERIES: Final[frozenset] = frozenset({
    "CESI",
    "CESI_MACRO_SURPRISE",
    "CITI_ECONOMIC_SURPRISE",
    "CITI_ECONOMIC_SURPRISE_INDEX",
    "CITIE",
})


def assert_no_forbidden_series(series_ids: "List[str]", context: str = "") -> None:
    """Assert none of ``series_ids`` is a retired/unavailable series.

    Guards the *name* layer of the zero-synthetic policy. The value layer
    (all-zero / constant / NaN columns) is enforced by
    :func:`assert_no_synthetic_data` and by
    :func:`Quantitative.data.fred_feed.assert_authentic_pit`.
    """
    hits = sorted(FORBIDDEN_SERIES.intersection(s.strip() for s in series_ids if s))
    if hits:
        raise AssertionError(
            f"Forbidden series contract violation {context}: {hits}. "
            f"These have no authentic public series and were previously filled "
            f"synthetically. Use UNANTICIPATED_INFLATION "
            f"(CPIAUCSL YoY - T10YIE) for the macro-surprise slot."
        )


def assert_canonical_names(names: "List[str]", context: str = "") -> None:
    """Assert a feature-name list is exactly the canonical contract, CESI-free."""
    assert_no_forbidden_series(list(names), context)
    validate_feature_names(list(names), context)


# ═══════════════════════════════════════════════════════════════════════════
# CONTRACT VALIDATION FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def validate_feature_matrix_shape(X: "np.ndarray", context: str = "") -> None:
    """
    Assert that feature matrix has exactly N_CANONICAL_FEATURES columns.
    Raises AssertionError with descriptive message if violated.
    """
    import numpy as np
    n_features = X.shape[1] if X.ndim == 2 else 0
    if n_features != N_CANONICAL_FEATURES:
        raise AssertionError(
            f"Feature contract violation {context}: "
            f"Expected {N_CANONICAL_FEATURES} features, got {n_features}. "
            f"Canonical: {CANONICAL_FEATURES}"
        )


def validate_feature_names(names: List[str], context: str = "") -> None:
    """Assert that feature names match canonical order exactly."""
    if names != CANONICAL_FEATURES:
        raise AssertionError(
            f"Feature name contract violation {context}: "
            f"Expected {CANONICAL_FEATURES}, got {names}"
        )


def validate_state_names(states: List[str], n_states: int = 3) -> None:
    """Assert state names match canonical for given n_states."""
    expected = CANONICAL_STATES_3 if n_states == 3 else CANONICAL_STATES_2
    if states != expected:
        raise AssertionError(
            f"State name contract violation: Expected {expected}, got {states}"
        )


def assert_no_synthetic_data(df: "pd.DataFrame", feature_cols: List[str] = None) -> None:
    """
    Hard guard: raise if any canonical feature column contains synthetic/fallback data.
    Checks for: all zeros, constant values, or known synthetic markers.
    """
    import numpy as np
    cols = feature_cols or CANONICAL_FEATURES
    for col in cols:
        if col in df.columns:
            s = df[col]
            # All zeros or all same value = synthetic
            if (s == 0).all() or s.nunique() == 1:
                raise RuntimeError(
                    f"FATAL: Synthetic/fallback data detected in '{col}' — "
                    f"all zeros or constant. Aborting per zero-synthetic policy."
                )
            # NaN after alignment = missing real data
            if s.isna().any():
                raise RuntimeError(
                    f"FATAL: Missing authentic PIT data for '{col}' — "
                    f"NaN detected after alignment. Aborting per zero-synthetic policy."
                )


# ═══════════════════════════════════════════════════════════════════════════
# EXPORTS
# ═══════════════════════════════════════════════════════════════════════════

__all__ = [
    # Feature definitions
    "CANONICAL_FEATURES",
    "F_INDEX_CREDIT_SPREAD_VELOCITY",
    "F_INDEX_YIELD_CURVE",
    "F_INDEX_REALIZED_VOL",
    "F_INDEX_FINANCIAL_STRESS",
    "F_INDEX_REAL_RATE",
    "F_INDEX_UNANTICIPATED_INFLATION",
    "N_CANONICAL_FEATURES",
    # Bounds
    "FEATURE_BOUNDS",
    "FeatureBounds",
    # State contract
    "CANONICAL_STATES_2",
    "CANONICAL_STATES_3",
    "STATE_SORT_FEATURE_INDEX",
    # Validation
    "VALIDATION_CONTRACT",
    "ValidationContract",
    # Provenance
    "REQUIRED_FRED_SERIES",
    "MANDATORY_FRED_SERIES",
    "FORBIDDEN_SERIES",
    # Validators
    "validate_feature_matrix_shape",
    "validate_feature_names",
    "validate_state_names",
    "assert_no_synthetic_data",
    "assert_no_forbidden_series",
    "assert_canonical_names",
]