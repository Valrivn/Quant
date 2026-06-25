import pytest
import pandas as pd
import numpy as np
from backtesting.backtest import run_walk_forward_backtest
from optimization.optuna_search import run_bayesian_optimization

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
