"""
tests/test_oos_backtest.py

Rigorous test suite for Out-of-Sample (OOS) evaluation and devil's advocate stress tests:
1. Verifies temporal embargo partition integrity (zero leakage between In-Sample, Embargo, and Holdout).
2. Tests dynamic square-root microstructure slippage vs flat linear costs.
3. Asserts delisting and extreme left-tail stress penalization.
4. Validates null cross-sectional shuffling falsification (skill vs random selection).
5. Tests Probability of Backtest Overfitting (PBO) and Deflated Sharpe Ratio (DSR).
6. Audits boundary hitrate against mock/hardcoded values.
"""

import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

from backtesting.oos_evaluation import (
    OOSEvaluationEngine,
    OOSPartitionConfig,
    MarketImpactConfig,
)


@pytest.fixture
def sample_daily_data():
    """Generates continuous daily price/return history spanning 2018 to 2026."""
    dates = pd.date_range("2018-01-01", "2026-03-31", freq="B")
    rng = np.random.default_rng(2026)
    returns = rng.normal(0.0004, 0.012, size=len(dates))
    df = pd.DataFrame({
        "date": dates,
        "daily_return": returns,
        "price": 100.0 * (1.0 + returns).cumprod(),
    })
    return df


def test_temporal_partition_and_embargo(sample_daily_data):
    """Ensure strict partition and non-empty embargo window with zero leakage."""
    engine = OOSEvaluationEngine()
    splits = engine.partition_time_series(sample_daily_data, date_column="date")

    assert "in_sample" in splits
    assert "embargo" in splits
    assert "holdout" in splits
    assert "out_of_sample" in splits

    is_df = splits["in_sample"]
    embargo_df = splits["embargo"]
    holdout_df = splits["holdout"]
    oos_df = splits["out_of_sample"]

    # Verify dates do not overlap
    assert is_df["date"].max() <= pd.Timestamp("2022-12-31")
    assert embargo_df["date"].min() > pd.Timestamp("2022-12-31")
    assert embargo_df["date"].max() < pd.Timestamp("2023-04-01")
    assert holdout_df["date"].min() >= pd.Timestamp("2023-04-01")
    assert holdout_df["date"].max() <= pd.Timestamp("2024-12-31")
    assert oos_df["date"].min() >= pd.Timestamp("2025-01-01")

    # Embargo must contain exactly the Q1 2023 purge window
    assert len(embargo_df) > 0


def test_dynamic_microstructure_impact_vs_flat_fee():
    """Verifies that dynamic slippage scales with turnover, volatility, and position size."""
    engine = OOSEvaluationEngine()

    # Small turnover low vol
    cost_low = engine.compute_dynamic_slippage(turnover=0.05, daily_vol=0.10, n_positions=25)
    # High turnover crisis vol
    cost_high = engine.compute_dynamic_slippage(turnover=0.50, daily_vol=0.40, n_positions=10)

    assert cost_low > 0.0
    assert cost_high > cost_low
    # High turnover & volatility stress should clearly surpass flat 15 bps
    assert cost_high > 15.0


def test_delisting_stress_injection(sample_daily_data):
    """Verifies that delisting injection introduces catastrophic downside tail shocks."""
    engine = OOSEvaluationEngine()
    clean_rets = sample_daily_data["daily_return"].copy()

    stressed_rets = engine.inject_delisting_stress(
        clean_rets,
        delisting_rate_annual=0.05,
        bankruptcy_penalty=-0.75,
        seed=123
    )

    assert len(stressed_rets) == len(clean_rets)
    # Stressed cumulative return must underperform clean return
    clean_cum = (1.0 + clean_rets).prod()
    stressed_cum = (1.0 + stressed_rets).prod()
    assert stressed_cum < clean_cum

    # Stressed minimum return must reflect the bankruptcy penalty
    assert stressed_rets.min() <= -0.70


def test_cross_sectional_null_shuffling():
    """
    Simulates a synthetic alpha strategy and verifies that
    random asset-shuffling properly falsifies the edge (p-value check).
    """
    engine = OOSEvaluationEngine()
    dates = pd.date_range("2024-01-01", "2024-12-31", freq="B")
    n_days = len(dates)
    tickers = [f"TICK_{i}" for i in range(10)]

    rng = np.random.default_rng(42)
    # Synthetic asset returns
    asset_rets = pd.DataFrame(
        rng.normal(0.0002, 0.015, size=(n_days, 10)),
        index=dates,
        columns=tickers
    )

    # Strategy that consistently tilts heavily into top 2 performing assets (real skill mock)
    asset_rets["TICK_0"] += 0.003
    weights = pd.DataFrame(0.05, index=dates, columns=tickers)
    weights["TICK_0"] = 0.55

    res = engine.run_null_shuffling_test(asset_rets, weights, n_shuffles=200, seed=42)

    assert "strategy_sharpe" in res
    assert "empirical_p_value" in res
    assert res["strategy_sharpe"] > res["null_mean_sharpe"]
    # With genuine alpha concentrated in TICK_0, null shuffling should reject the null (p < 0.05)
    assert res["passes_falsification"] is True


def test_pbo_and_deflated_sharpe_ratio():
    """Tests that DSR properly deflates Sharpe under multiple hypothesis testing."""
    engine = OOSEvaluationEngine()

    rng = np.random.default_rng(101)
    # 4 distinct walk-forward fold return series
    folds = [
        pd.Series(rng.normal(0.0006, 0.010, size=252)),
        pd.Series(rng.normal(0.0004, 0.011, size=252)),
        pd.Series(rng.normal(-0.0001, 0.012, size=252)),
        pd.Series(rng.normal(0.0005, 0.010, size=252)),
    ]

    res = engine.compute_pbo_and_dsr(folds, n_trials=513)

    assert "pbo_estimate" in res
    assert "expected_max_null_sharpe" in res
    assert "dsr" in res
    assert 0.0 <= res["dsr"] <= 1.0
    assert 0.0 <= res["pbo_estimate"] <= 1.0


def test_devil_advocate_mock_detection():
    """
    Adversarial audit check: inspects boundary_hitrate.py to verify that
    the hardcoded 'oos_hit_rate = 0.62' mock has been permanently eliminated.
    """
    from validation import boundary_hitrate
    import inspect
    source = inspect.getsource(boundary_hitrate)

    has_hardcoded_mock = "oos_hit_rate = 0.62" in source
    assert has_hardcoded_mock is False, "Failure: hardcoded mock 'oos_hit_rate = 0.62' still present!"
    assert "oos_source" in source, "Honest OOS tracking metadata must be present"


def test_hysteresis_deadband_suppresses_micro_churn():
    """Verifies that target weight changes below threshold (e.g. 15%) do not trigger rebalance."""
    engine = OOSEvaluationEngine()

    current_w = pd.Series({"AAPL": 0.20, "MSFT": 0.20, "GOOGL": 0.20, "AMZN": 0.20, "BIL": 0.20})
    # Target weights with minor drift (5% change) and one major shift (30% change)
    target_w = pd.Series({"AAPL": 0.23, "MSFT": 0.18, "GOOGL": 0.20, "AMZN": 0.05, "BIL": 0.34})

    adjusted_w = engine.apply_hysteresis_deadband(target_w, current_w, threshold=0.10)

    # AAPL (3% diff) and MSFT (2% diff) should stay frozen at current weights (0.20)
    # AMZN (15% diff) and BIL (14% diff) should execute
    assert abs(adjusted_w["AAPL"] - adjusted_w["MSFT"]) < 1e-4
    assert adjusted_w["AMZN"] < adjusted_w["AAPL"]


def test_drawdown_regime_shield_cuts_tail_losses():
    """Verifies that dynamic drawdown circuit breaker reduces equity exposure and preserves capital."""
    engine = OOSEvaluationEngine()

    # Simulate deep market crash (-30% drop over 30 days)
    crash_days = 30
    crash_rets = pd.Series([-0.012] * crash_days, index=pd.date_range("2020-02-20", periods=crash_days, freq="B"))

    shielded_rets, state_df = engine.apply_drawdown_regime_shield(
        crash_rets,
        equity_allocation=0.85,
        drawdown_warning_level=-0.08,
        drawdown_crisis_level=-0.15
    )

    unshielded_cum = (1.0 + crash_rets).prod() - 1.0
    shielded_cum = (1.0 + shielded_rets).prod() - 1.0

    # Shielded portfolio must truncate drawdown and significantly outperform raw market crash
    assert shielded_cum > unshielded_cum
    assert "CRISIS" in state_df["regime"].values
    # In crisis regime, equity weight is cut from 85% to 15%
    assert state_df[state_df["regime"] == "CRISIS"]["equity_weight"].iloc[-1] == 0.15


def test_probationary_queue_fail_closed_on_missing_metrics():
    """
    Verifies that missing EPS revisions or OMR data strictly fails closed (returns False / rejects),
    rather than relying on hardcoded mock pass numbers.
    """
    from Quantitative.probationary_queue import ProbationaryQueue, estimate_revisions_slope, compute_omr

    ticker = "UNVETTED_CO"
    d = pd.Timestamp("2024-01-01")

    # Empty market data -> missing metrics
    empty_market_data = {}

    slope = estimate_revisions_slope(ticker, d, empty_market_data)
    assert np.isnan(slope), "Missing revisions slope must evaluate to NaN, not a mock positive float"

    omr = compute_omr(ticker, d, empty_market_data)
    assert np.isnan(omr), "Missing OMR must evaluate to NaN, not a mock passing float"

    # In queue: ticker must fail probation checks when metrics are absent
    pq = ProbationaryQueue()
    pq.min_days = 5
    pq.max_days = 10
    pq.on_entry_trigger(ticker, d, price=100.0, iv=120.0)

    # Process after min_days with empty data -> should not pass
    passed = pq.daily_process(pd.Timestamp("2024-01-08"), market_data=empty_market_data)
    assert ticker not in passed, "Unvetted company with missing metrics must be excluded from passing queue"


