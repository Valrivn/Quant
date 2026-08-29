"""Offline tests for the /backtest agent contract: audit gate, method dispatch,
and engine wiring. The heavy fee_sim3 engines are NOT executed here (they need
network); dispatch is validated via ENGINE_MAP + error paths."""

import numpy as np
import pandas as pd
import pytest

from backtesting.chi_square import audit_status, run_standard_backtest


def test_audit_status_clean():
    assert audit_status({"fred_source": "FRED"}, factors_ok=True) == "AUDITED CLEAN"


def test_audit_status_env_degraded_is_tagged_not_blocked():
    status = audit_status({"fred_source": "PRICE FALLBACK"}, factors_ok=False)
    assert status.startswith("AUDITED CLEAN (env-degraded:")
    assert "FF5" in status and "FRED" in status


def test_audit_status_data_degradation_reports_and_blocks():
    status = audit_status({"fred_source": "FRED", "div_partial": True}, True)
    assert status.startswith("DEGRADED-DATA:")
    with pytest.raises(RuntimeError, match="DEGRADED-DATA"):
        audit_status({"fred_source": "FRED", "div_partial": True}, True, hard_fail=True)


def test_placeholder_data_always_blocks():
    with pytest.raises(RuntimeError, match="DEGRADED-DATA.*placeholder"):
        audit_status({"fred_source": "FRED", "placeholder_data": True}, True, hard_fail=True)


def test_run_standard_backtest_rejects_invalid_method_before_engine():
    with pytest.raises(ValueError, match="unknown method"):
        run_standard_backtest("bogus")


def test_all_methods_dispatch_to_existing_fee_sim3_engines():
    """Every /backtest method must map to a real engine + real strategy label."""
    from backtesting.chi_square import ENGINE_MAP
    from diversification import fee_sim3 as fs

    for method, (engine_name, label) in ENGINE_MAP.items():
        assert hasattr(fs, engine_name), f"{method} -> missing engine {engine_name}"
        assert label, f"{method} -> empty strategy label"


def test_regime_returns_reports_bull_and_bear():
    from backtesting.chi_square import DEFAULT_REGIMES, regime_returns

    idx = pd.date_range("2019-01-02", "2025-03-01", freq="B")
    t = np.linspace(0, 4 * np.pi, len(idx))
    level = 10000.0 * np.exp(0.0006 * np.arange(len(idx)) + 0.05 * np.sin(t))
    vpath = pd.Series(level, index=idx)
    rr = regime_returns(vpath, DEFAULT_REGIMES)
    keys = set(rr.keys())
    assert any(k.startswith("bull:") for k in keys)
    assert any(k.startswith("bear:") for k in keys)
    assert all(v == v for v in rr.values())  # no NaN
