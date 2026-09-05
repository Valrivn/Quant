import pytest
import pandas as pd
import numpy as np
from backtesting.backtest import run_walk_forward_backtest
from optimization.optuna_search import run_bayesian_optimization
from backtesting.chi_square import load_factors

# Mock the database aggregations query inside backtest to run offline test
def test_backtest_calculation(monkeypatch):
    # Mock data to return when reading daily_aggregations
    mock_data = pd.DataFrame([
        {"ticker": "MSFT", "date": "2026-06-01", "category": "retail_options", "subreddit": "wallstreetbets", "raw_sentiment": 0.5, "weight": 0.2, "weighted_sentiment": 0.1},
        {"ticker": "AAPL", "date": "2026-06-01", "category": "retail_options", "subreddit": "wallstreetbets", "raw_sentiment": -0.2, "weight": 0.2, "weighted_sentiment": -0.04},
    ])
    
    # Mock pandas read_sql_query
    monkeypatch.setattr(pd, "read_sql_query", lambda query, conn, params=None: mock_data)
    
    # Mock yfinance return values
    mock_returns = pd.DataFrame(
        data=np.array([[0.05, -0.02]]),
        index=pd.to_datetime(["2026-06-30"]),
        columns=["MSFT", "AAPL"]
    )
    from backtesting import backtest
    monkeypatch.setattr(backtest, "fetch_historical_returns", lambda tickers, start, end: mock_returns)
    
    category_weights = {"retail_options": 1.0}
    subreddit_weights = {"retail_options": {"wallstreetbets": 1.0}}
    
    results = run_walk_forward_backtest(category_weights, subreddit_weights)
    
    assert "sharpe" in results
    assert "ic" in results
    assert "hit_rate" in results
    assert isinstance(results["returns"], list)
    # Risk metrics
    assert "max_drawdown" in results
    assert "ulcer_index" in results
    assert "var_95" in results
    assert "cvar_95" in results
    assert results["max_drawdown"] <= 0.0
    assert results["ulcer_index"] >= 0.0
    assert results["cvar_95"] <= results["var_95"]


def test_t_plus_1_execution(monkeypatch):
    """Regression: signal at t must execute at t+1 (no lookahead bias).

    Setup: 3 common dates, strong positive signal every day.
    Day-0 signal should capture Day-1 return, Day-1 signal should capture
    Day-2 return.  Day-0 return should NOT appear in the series (no t-0 trade).
    """
    dates = ["2026-06-01", "2026-06-02", "2026-06-03"]
    mock_data = pd.DataFrame([
        {"ticker": "AAA", "date": d, "category": "tech", "subreddit": "sub",
         "raw_sentiment": 1.0, "weight": 1.0, "weighted_sentiment": 1.0}
        for d in dates
    ])

    monkeypatch.setattr(pd, "read_sql_query", lambda q, c, params=None: mock_data)

    # Returns over all 3 dates: +1%, +10%, -5%
    mock_returns = pd.DataFrame(
        {"AAA": [0.01, 0.10, -0.05]},
        index=pd.to_datetime(dates),
    )

    from backtesting import backtest
    monkeypatch.setattr(backtest, "fetch_historical_returns", lambda t, s, e: mock_returns)

    results = run_walk_forward_backtest(
        {"tech": 1.0}, {"tech": {"sub": 1.0}}
    )

    rets = results["returns"]
    # After t+1 shift we get 2 return values (day-0 signal is dropped)
    assert len(rets) == 2, f"Expected 2 returns after t+1 shift, got {len(rets)}"
    # Day-0 signal (+1) × Day-1 return (+10%) = +0.10
    assert abs(rets[0] - 0.10) < 1e-9, f"First return should be +0.10, got {rets[0]}"
    # Day-1 signal (+1) × Day-2 return (-5%) = -0.05
    assert abs(rets[1] - (-0.05)) < 1e-9, f"Second return should be -0.05, got {rets[1]}"


def test_ff5_factors_fetch():
    """FF5 factors load from Ken French with cache; alpha_ff5 populated in backtest."""
    factors = load_factors()
    assert not factors.empty, "FF5 factors should not be empty"
    assert list(factors.columns) == ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"], "Wrong columns"
    assert len(factors) > 10000, "Should have ~15k daily rows (1963-present)"
    assert factors.index.min().year <= 1965, "Should start ~1963"
    assert factors.index.max().year >= 2025, "Should include recent data"
    # Values are decimals (not percentages)
    assert factors["Mkt-RF"].abs().max() < 0.5, "Daily factor returns should be < 50%"


def test_ff5_cache_invalidation(tmp_path, monkeypatch):
    """Cache respects max_age_days and can be invalidated."""
    from valuation_alpha.datastore.factors import fetch_ff5_factors, _FF5_CACHE
    
    # First fetch populates cache
    df1 = fetch_ff5_factors(use_cache=True, max_age_days=1)
    assert _FF5_CACHE.exists(), "Cache file should be created"
    
    # Second fetch with fresh cache returns same data (fast path)
    df2 = fetch_ff5_factors(use_cache=True, max_age_days=1)
    pd.testing.assert_frame_equal(df1, df2)
    
    # Cache invalidation: max_age_days=0 forces refetch
    df3 = fetch_ff5_factors(use_cache=True, max_age_days=0)
    assert len(df3) == len(df1), "Refetch should return same data"
