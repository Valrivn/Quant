"""
Verification tests for Workstream A components.
"""

import pytest
import pandas as pd
import numpy as np

from portfolio.allocator import compute_realized_vol, compute_vol_scale_factor, apply_vol_targeting
from harness.three_arm_runner import RebalanceScheduler, HysteresisGate, SwapGapGate, HoldingDwellTracker
from regime.market_events import MarketEventDetector
from regime.derisking import GradedDerisking


def test_vol_targeting():
    returns = pd.Series(np.random.normal(0.0005, 0.01, 100))
    vol = compute_realized_vol(returns, lookback=63)
    assert vol > 0.0

    scale = compute_vol_scale_factor(realized_vol=0.15, target_vol=0.11)
    assert round(scale, 4) == round(0.11 / 0.15, 4)

    scale_capped = compute_vol_scale_factor(realized_vol=0.08, target_vol=0.11)
    assert scale_capped == 1.0

    weights = {"AAPL": 0.5, "MSFT": 0.5}
    res = apply_vol_targeting(weights, realized_vol=0.22, cash_instruments=["BIL", "SHY"])
    assert len(res) == 4
    assert round(sum(res.values()), 6) == 1.0
    assert res["AAPL"] < 0.5


def test_execution_controls():
    scheduler = RebalanceScheduler()
    assert scheduler.should_rebalance(pd.Timestamp("2024-04-01")) is True
    assert scheduler.should_rebalance(pd.Timestamp("2024-04-02")) is False
    assert scheduler.should_rebalance(pd.Timestamp("2024-05-01")) is False

    next_date = scheduler.next_rebalance_date(pd.Timestamp("2024-04-01"))
    assert next_date == pd.Timestamp("2024-07-01")

    hgate = HysteresisGate()
    assert hgate.should_trade(0.10, 0.105) is False  # diff 0.005 / 0.105 = 4.7% <= 15%
    assert hgate.should_trade(0.10, 0.20) is True    # diff 0.10 / 0.20 = 50% > 15%

    sgate = SwapGapGate()
    assert sgate.should_swap(1.0, 1.9) is True   # gap 0.9 >= 0.8
    assert sgate.should_swap(1.0, 1.5) is False  # gap 0.5 < 0.8

    tracker = HoldingDwellTracker()
    assert tracker.can_exit("AAPL", days_held=30, icr=2.5, credit_spread_bps=150) is False
    assert tracker.can_exit("AAPL", days_held=30, icr=0.8, credit_spread_bps=150) is True
    assert tracker.can_exit("AAPL", days_held=30, icr=2.5, credit_spread_bps=350) is True
    assert tracker.can_exit("AAPL", days_held=65, icr=2.5, credit_spread_bps=150) is True


def test_regime_components():
    detector = MarketEventDetector()
    res = detector.update({
        'credit_spread': 250.0,
        'credit_spread_history': [200.0, 200.6, 201.2, 201.8], # 3 consecutive +0.6 diffs (>0.5)
        'bocpd_posterior': 0.1,
        'spy_close': 500.0,
        'spy_sma_200': 450.0,
        'date': pd.Timestamp("2024-01-04")
    })
    assert res['triggered'] is True
    assert res['trigger_type'] == 'spread_velocity'

    derisking = GradedDerisking()
    assert derisking.get_equity_cap(0) == 1.0  # day 1
    assert derisking.get_equity_cap(2) == 0.7  # day 3
    assert derisking.get_equity_cap(4) == 0.5  # day 5
    assert derisking.get_equity_cap(9) == 0.4  # day 10

    adjusted = derisking.apply_derisking({"AAPL": 0.6, "MSFT": 0.4}, days_since_confirmation=2)
    assert round(adjusted["AAPL"] + adjusted["MSFT"], 4) == 0.70
    assert round(adjusted["BIL"] + adjusted["SHY"], 4) == 0.30
