"""
test_har_abs_vol.py — Unit tests for Quantitative/har_abs_vol.py (Phase 8 Redesign)
"""

import numpy as np
import pandas as pd
import pytest
from Quantitative.har_abs_vol import HARAbsVol


def test_har_abs_vol_fit_and_predict():
    np.random.seed(42)
    dates = pd.date_range("2018-01-01", periods=300, freq="B")
    returns = pd.Series(np.random.normal(0.0002, 0.015, 300), index=dates)

    har = HARAbsVol()
    har.fit(returns)

    assert har.is_fitted
    v1d, v5d, v22d = har.predict_vol_chain(returns)

    assert len(v1d) == 300
    assert len(v5d) == 300
    assert len(v22d) == 300
    assert (v1d > 0).all()
    assert (v5d > 0).all()
    assert (v22d > 0).all()


def test_nested_cv_sector_calibrate():
    np.random.seed(42)
    dates = pd.date_range("2018-01-01", periods=200, freq="B")
    returns_df = pd.DataFrame({
        "AAPL": np.random.normal(0.0005, 0.018, 200),
        "MSFT": np.random.normal(0.0004, 0.015, 200),
        "JPM": np.random.normal(0.0002, 0.012, 200),
    }, index=dates)

    sectors = pd.Series({"AAPL": "Technology", "MSFT": "Technology", "JPM": "Financials"})

    har = HARAbsVol()
    scales = har.nested_cv_sector_calibrate(returns_df, sectors)

    assert "Technology" in scales
    assert "Financials" in scales
    assert len(scales["Technology"]) == 3
