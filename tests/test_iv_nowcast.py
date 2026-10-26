"""
test_iv_nowcast.py — Unit tests for Quantitative/iv_nowcast.py (Phase 8 Redesign)
"""

import numpy as np
import pandas as pd
import pytest
from Quantitative.iv_nowcast import GARCHIVNowcast, analytical_duration_adjustment


def test_analytical_duration_adjustment():
    iv_base = 100.0
    d_wacc = 8.0
    delta_wacc = 0.005  # +50 bps shift in WACC

    # IV_nowcast = 100 * (1 - 8 * 0.005) = 100 * 0.96 = 96.0
    adj_iv = analytical_duration_adjustment(iv_base, d_wacc, delta_wacc)
    assert np.isclose(adj_iv, 96.0)


def test_garch_iv_nowcast_interpolation():
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=150, freq="B")
    returns = pd.Series(np.random.normal(0.0002, 0.015, 150), index=dates)

    garch = GARCHIVNowcast()
    garch.fit(returns)

    weekly_iv = 120.0
    test_date = dates[100]

    daily_iv = garch.interpolate_iv(
        weekly_iv=weekly_iv,
        date=test_date,
        daily_returns=returns,
        d_wacc=6.5,
        delta_wacc=0.003,
    )

    assert daily_iv > 0.0
    assert abs(daily_iv - weekly_iv) < 50.0
