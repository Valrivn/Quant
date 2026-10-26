"""
test_swap_barrier_v8.py — Unit tests for ExponentialSwapBarrier (Phase 8 Redesign)
"""

import math
import pandas as pd
import pytest

from Quantitative.swap_barrier import ExponentialSwapBarrier, evaluate_swap_candidates
from ingestion.trade_audit import TradeAuditLedger


def test_swap_barrier_decay():
    sb = ExponentialSwapBarrier()
    
    # Month 0: Barrier should be ~150 bps (non-moat, EXPANSION)
    bar_0 = sb.calculate_barrier_bps(holding_months=0.0, is_moat_protected=False, regime="EXPANSION")
    assert abs(bar_0 - 150.0) < 1e-3

    # Month 24 (t_target): Barrier should be 150 * exp(-0.5) ~ 90.98 bps
    bar_24 = sb.calculate_barrier_bps(holding_months=24.0, is_moat_protected=False, regime="EXPANSION")
    expected_24 = 150.0 * math.exp(-0.5)
    assert abs(bar_24 - expected_24) < 1e-3


def test_swap_barrier_moat_penalty():
    sb = ExponentialSwapBarrier()
    
    # OMR = 0.70 (+20bps for 0.2 OMR above 0.5 threshold)
    pen_omr_07 = sb.calculate_moat_penalty_bps(omr=0.70)
    assert abs(pen_omr_07 - 20.0) < 1e-3

    # OMR = 0.50 (0bps)
    pen_omr_05 = sb.calculate_moat_penalty_bps(omr=0.50)
    assert abs(pen_omr_05 - 0.0) < 1e-3

    # Fallback is_moat_protected=True -> 100bps
    pen_fallback = sb.calculate_moat_penalty_bps(is_moat_protected=True)
    assert abs(pen_fallback - 100.0) < 1e-3


def test_swap_barrier_regime_conditioning():
    sb = ExponentialSwapBarrier()
    
    bar_exp = sb.calculate_barrier_bps(holding_months=12.0, omr=0.60, regime="EXPANSION")
    bar_shk = sb.calculate_barrier_bps(holding_months=12.0, omr=0.60, regime="SHOCK")
    bar_cri = sb.calculate_barrier_bps(holding_months=12.0, omr=0.60, regime="CRISIS")

    assert bar_exp < bar_shk < bar_cri
    assert abs(bar_shk - 1.25 * bar_exp) < 1e-3
    assert abs(bar_cri - 1.50 * bar_exp) < 1e-3


def test_should_swap_logic():
    sb = ExponentialSwapBarrier()
    
    # Upside edge = (0.25 - 0.10) * 10000 = 1500 bps vs barrier ~150 bps -> Swap
    should, edge, bar = sb.should_swap(upside_i=0.10, upside_j=0.25, holding_months=1.0, regime="EXPANSION")
    assert should is True
    assert abs(edge - 1500.0) < 1e-3

    # Small edge (0.11 vs 0.10 -> 100 bps) vs tight CRISIS barrier (~240 bps) -> No Swap
    should_cri, edge_cri, bar_cri = sb.should_swap(upside_i=0.10, upside_j=0.11, holding_months=1.0, regime="CRISIS")
    assert should_cri is False
