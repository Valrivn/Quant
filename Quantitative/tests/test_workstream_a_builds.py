"""
Tests for Workstream A builds & updates (RegimeState master clock, PositionState per-asset embargo,
high-bar override, modal EVA gate, Monte Carlo DCF, and emergency de-risking override).
"""

import pytest
import numpy as np
import pandas as pd

from regime.regime_state import RegimeState, RegimeStateStore
from portfolio.position_state import PositionState, PositionStateStore
from regime.market_events import MarketEventDetector
from regime.derisking import GradedDerisking
from regime.highbar import can_break_embargo
from valuation.eva_modal import eva_modal_gate
from valuation.monte_carlo_dcf import monte_carlo_valuation


def test_regime_state_master_clock():
    store = RegimeStateStore(storage_path="data/test_regime_state.json")
    store.reset()
    state = store.get()
    
    # 1. Global gate check
    state.market_event_triggered = True
    assert state.is_global_gate_open(is_expansion=False) is False
    assert state.is_global_gate_open(is_expansion=True) is True
    
    state.market_event_triggered = False
    state.embargo_days_remaining = 5
    assert state.is_global_gate_open(confirmation_unresolved=True) is False
    assert state.is_global_gate_open(confirmation_unresolved=False) is True
    
    # 2. Advance day with crisis freeze
    store.advance_day(is_crisis=True)
    assert store.get().embargo_days_remaining == 5
    
    store.advance_day(is_crisis=False)
    assert store.get().embargo_days_remaining == 4


def test_position_state_per_asset_embargo(tmp_path):
    parquet_path = tmp_path / "test_positions.parquet"
    store = PositionStateStore(storage_path=str(parquet_path))
    
    pos = PositionState(
        ticker="AAPL",
        weight=0.05,
        days_held=10,
        entry_date=pd.Timestamp("2024-01-01"),
        entry_composite_score=0.8,
        entry_price=150.0
    )
    store.set(pos)
    assert store.get("AAPL").embargo_days_remaining == 0
    assert store.is_embargoed("AAPL") is False
    
    store.set_embargo("AAPL", 5)
    assert store.get("AAPL").embargo_days_remaining == 5
    assert store.is_embargoed("AAPL") is True
    
    store.decrement_embargoes()
    assert store.get("AAPL").embargo_days_remaining == 4
    
    # Check persistence reload
    store_reloaded = PositionStateStore(storage_path=str(parquet_path))
    assert store_reloaded.get("AAPL").embargo_days_remaining == 4


def test_emergency_override_and_derisking():
    detector = MarketEventDetector()
    # Mock credit spread velocity trigger (3 consecutive days > 0.5 diff)
    res = detector.update({
        'credit_spread': 250.0,
        'credit_spread_history': [200.0, 200.6, 201.2, 201.8],
        'bocpd_posterior': 0.1,
        'spy_close': 500.0,
        'spy_sma_200': 450.0,
        'date': pd.Timestamp("2024-01-04")
    })
    assert res['emergency'] is True
    
    derisking = GradedDerisking()
    weights = {"AAPL": 0.6, "MSFT": 0.4}
    
    # Emergency AND (z_val > 0.0 OR icr < 3.0) -> aggressive de-risking immediately
    adj1 = derisking.apply_derisking(weights, days_since_confirmation=0, emergency=True, portfolio_valuation={'z_val': 0.5, 'icr': 4.0})
    assert round(adj1["AAPL"] + adj1["MSFT"], 4) == 0.40  # max step day 10 cap (40%)
    
    # Emergency AND (z_val <= -1.0 AND icr >= 5.0) -> SUPPRESS emergency exit
    adj2 = derisking.apply_derisking(weights, days_since_confirmation=0, emergency=True, portfolio_valuation={'z_val': -1.2, 'icr': 6.0})
    assert round(adj2["AAPL"] + adj2["MSFT"], 4) == 1.00  # unscaled target weights kept


def test_highbar_override_gate():
    asset_pass = {
        'z_val': -1.2,
        'dcf_p10': False,
        'roic': 0.15,
        'wacc': 0.08,
        'eva_modal_pass': True,
        'spread_velocity_21d': -0.1,
        'icr': 6.0,
    }
    assert can_break_embargo(asset_pass) is True
    
    asset_fail_icr = dict(asset_pass, icr=4.0)
    assert can_break_embargo(asset_fail_icr) is False

    asset_fail_spread = dict(asset_pass, spread_velocity_21d=0.2)
    assert can_break_embargo(asset_fail_spread) is False


def test_eva_modal_gate():
    rng = np.random.default_rng(42)
    paths_pass = rng.normal(loc=1.5, scale=0.1, size=1000)
    res_pass = eva_modal_gate(paths_pass, seed=42)
    assert res_pass['passed'] is True
    assert res_pass['p_positive'] >= 0.99
    assert res_pass['modal_eva'] > 0.0

    paths_fail = rng.normal(loc=-0.5, scale=0.5, size=1000)
    res_fail = eva_modal_gate(paths_fail, seed=42)
    assert res_fail['passed'] is False


def test_monte_carlo_dcf_valuation():
    res = monte_carlo_valuation(
        base_wacc=0.08,
        base_growth=0.05,
        base_term_growth=0.02,
        n=5000,
        seed=20261006
    )
    assert "p10" in res
    assert "z_score" in res
    assert res["seed"] == 20261006
    assert len(res["valuations"]) == 5000
    assert res["p10"] > 0.0
