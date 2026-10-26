"""
tests/test_regime_buffer.py

Unit tests for the Regime Hysteresis Dwell-Time Buffer & Statistical Regime Engine:
1. Verifies that noisy daily flips do not trigger transitions during the dwell lock.
2. Asserts that confirmed transitions occur only when probability >= threshold sustains K consecutive days.
3. Tests that emergency volatility shocks (e.g. daily return <= -3.5 sigma or 5d drop <= -8%)
   trigger immediate bypass into BEAR state.
4. Verifies PIT Bayesian posterior calculation and numerical stability.
"""

import pytest
import numpy as np
import pandas as pd

from Quantitative.regime.regime_buffer import (
    RegimeHysteresisBuffer,
    HysteresisBufferConfig,
)
from Quantitative.regime.statistical_regime_engine import StatisticalRegimeEngine


def test_hysteresis_buffer_dwell_lock():
    """Verifies that candidate signals within the minimum dwell period (15 days) are suppressed."""
    config = HysteresisBufferConfig(confirmation_days=5, min_dwell_days=15, enter_threshold=0.65)
    buffer = RegimeHysteresisBuffer(config)

    assert buffer.active_regime == "BULL"

    # Days 1 to 10: try feeding BEAR probability of 0.90
    dates = pd.date_range("2024-01-01", periods=10, freq="B")
    for d in dates:
        active = buffer.update(d, {"BULL": 0.10, "STAGNANT": 0.0, "BEAR": 0.90})
        # Dwell lock must prevent transition
        assert active == "BULL"
        assert buffer.days_in_active_regime <= 10


def test_hysteresis_buffer_confirmed_transition():
    """Verifies that an unhurried, sustained signal triggers a confirmed transition after K days."""
    config = HysteresisBufferConfig(confirmation_days=5, min_dwell_days=10, enter_threshold=0.65)
    buffer = RegimeHysteresisBuffer(config)

    dates = pd.date_range("2024-01-01", periods=20, freq="B")

    # Satisfy 10 days of minimum dwell in BULL
    for d in dates[:10]:
        buffer.update(d, {"BULL": 0.80, "STAGNANT": 0.10, "BEAR": 0.10})
    assert buffer.active_regime == "BULL"

    # Next 4 days: BEAR probability is high (counter 1 to 4) -> should not switch yet
    for d in dates[10:14]:
        active = buffer.update(d, {"BULL": 0.10, "STAGNANT": 0.10, "BEAR": 0.80})
        assert active == "BULL"

    # Day 5 of sustained signal (counter == 5) -> must execute transition to BEAR
    active_final = buffer.update(dates[14], {"BULL": 0.10, "STAGNANT": 0.10, "BEAR": 0.80})
    assert active_final == "BEAR"
    assert len(buffer.transition_history) == 1
    assert buffer.transition_history[0].trigger_type == "CONFIRMED_BUFFER"


def test_emergency_circuit_breaker_bypass():
    """Verifies that extreme market drops immediately bypass confirmation counters into BEAR."""
    config = HysteresisBufferConfig(
        confirmation_days=5,
        min_dwell_days=15,
        emergency_vol_sigma=-3.5,
        emergency_5d_drop=-0.08
    )
    buffer = RegimeHysteresisBuffer(config)

    d = pd.Timestamp("2020-02-27")
    # Feed severe 5-day drop of -10% on day 2 of dwell lock
    active = buffer.update(
        d,
        {"BULL": 0.70, "STAGNANT": 0.20, "BEAR": 0.10},  # Even if raw model still says BULL
        daily_return=-0.045,
        trailing_5d_return=-0.11
    )

    assert active == "BEAR", "Emergency drop must force immediate BEAR lock"
    assert len(buffer.transition_history) == 1
    assert buffer.transition_history[0].trigger_type == "EMERGENCY_BYPASS"


def test_statistical_regime_engine_posteriors():
    """Verifies Bayesian posterior probability normalization and feature extraction."""
    engine = StatisticalRegimeEngine()

    dates = pd.date_range("2023-01-01", "2024-06-01", freq="B")
    rng = np.random.default_rng(42)
    # Strong upward price series (Bullish)
    prices = pd.Series(100.0 * np.exp(np.cumsum(rng.normal(0.0008, 0.009, len(dates)))), index=dates)

    feat = engine.compute_daily_features(prices, dates[-1])
    assert feat is not None
    assert len(feat) == 4

    probs = engine.calculate_posteriors(feat)
    assert "BULL" in probs and "BEAR" in probs and "STAGNANT" in probs
    assert abs(sum(probs.values()) - 1.0) < 1e-6
    # Under strong positive momentum, Bull posterior must dominate
    assert probs["BULL"] > probs["BEAR"]
