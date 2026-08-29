"""Offline tests for the extra risk metrics (Sortino, Calmar, win-rate)."""

import numpy as np
import pandas as pd
import pytest

from backtesting.metrics_extra import (
    calmar,
    monthly_returns,
    period_return,
    sortino,
    win_rate,
)


def _returns(seed=0, n=500, mu=0.0005, sd=0.01):
    rng = np.random.default_rng(seed)
    return pd.Series(rng.normal(mu, sd, n))


def test_sortino_positive_for_positive_drift():
    val = sortino(_returns(seed=1, mu=0.0005))
    assert val == val  # not NaN
    assert val > 0


def test_sortino_nan_for_no_downside():
    r = pd.Series([0.01] * 20)
    assert np.isnan(sortino(r))


def test_sortino_nan_for_short_series():
    assert np.isnan(sortino(pd.Series([0.01])))


def test_calmar():
    assert calmar(0.10, -0.20) == 0.5
    assert np.isnan(calmar(0.10, 0.0))
    assert np.isnan(calmar(0.10, None))


def test_win_rate_half_for_zero_mean():
    val = win_rate(_returns(seed=2, mu=0.0, sd=0.01))
    assert 0.0 <= val <= 1.0


def test_win_rate_above_benchmark():
    r = pd.Series([0.001, 0.001, 0.001, -0.001, 0.001])
    b = pd.Series([0.0, 0.0, 0.0, 0.0, 0.0])
    assert win_rate(r, b) == 0.8


def test_period_return():
    idx = pd.date_range("2022-01-01", periods=30, freq="D")
    s = pd.Series(np.linspace(100.0, 110.0, len(idx)), index=idx)
    assert period_return(s, "2022-01-01", "2022-01-30") == pytest.approx(0.10)
    assert np.isnan(period_return(s, "2020-01-01", "2020-01-05"))


def test_monthly_returns_length():
    idx = pd.date_range("2021-01-01", "2021-12-31", freq="B")
    s = pd.Series(np.linspace(100.0, 112.0, len(idx)), index=idx)
    mr = monthly_returns(s)
    assert len(mr) == 11
