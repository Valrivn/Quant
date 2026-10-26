"""
test_swap_barrier_v3.py — Unit tests for Quantitative/swap_barrier_v3.py
"""

import math
import pytest
from Quantitative.swap_barrier_v3 import exponential_decay, SwapBarrierV3


def test_exponential_decay_initial():
    # At t_held=0, omr=1.0: barrier = C * exp(0) + 0 = C
    C = 0.0250  # 250 bps
    barrier = exponential_decay(C=C, gamma=0.25, t_target=30.0, t_held=0.0, omr_normalized=1.0)
    assert pytest.approx(barrier, abs=1e-6) == C


def test_exponential_decay_decay_over_time():
    C = 0.0250
    b0 = exponential_decay(C=C, gamma=0.25, t_target=30.0, t_held=0.0, omr_normalized=1.0)
    b15 = exponential_decay(C=C, gamma=0.25, t_target=30.0, t_held=15.0, omr_normalized=1.0)
    b30 = exponential_decay(C=C, gamma=0.25, t_target=30.0, t_held=30.0, omr_normalized=1.0)

    assert b0 > b15 > b30


def test_moat_penalty():
    C = 0.0250
    # Low OMR should increase barrier due to moat penalty
    b_high_moat = exponential_decay(C=C, gamma=0.25, t_target=30.0, t_held=10.0, omr_normalized=1.0)
    b_low_moat = exponential_decay(C=C, gamma=0.25, t_target=30.0, t_held=10.0, omr_normalized=0.0)

    assert b_low_moat > b_high_moat


def test_swap_barrier_v3_churn_rate():
    sb = SwapBarrierV3(C=0.0250, gamma=0.25, t_target=30.0)
    annual_churn = sb.simulate_churn(n_periods_months=120) / 10.0
    assert annual_churn < 5.0


def test_should_swap_logic():
    sb = SwapBarrierV3(C=0.0250, gamma=0.25, t_target=30.0)
    # Barrier at t=0 is 250 bps = 0.0250
    assert not sb.should_swap(current_utility=0.10, candidate_utility=0.12, t_held=0.0)  # delta = 0.0200 < 0.0250
    assert sb.should_swap(current_utility=0.10, candidate_utility=0.13, t_held=0.0)      # delta = 0.0300 > 0.0250
