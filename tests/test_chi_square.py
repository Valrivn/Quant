"""Offline tests for the chi-square house backtest gate (v2)."""

import numpy as np
import pandas as pd
import pytest

from backtesting.chi_square import (
    DEFAULT_REGIMES,
    ENGINE_MAP,
    audit_status,
    chi_square_independence,
    metric_bundle,
    regime_winloss,
    run_standard_backtest,
)


def test_chi2_systematic_table():
    res = chi_square_independence([[10, 2], [2, 10]])
    assert res["method"] == "chi2"
    assert res["df"] == 1
    assert res["p_value"] < 0.05
    assert res["verdict"] == "SYSTEMATIC (p<alpha)"
    assert res["expected"] == [[6.0, 6.0], [6.0, 6.0]]


def test_chi2_chance_table():
    res = chi_square_independence([[8, 8], [8, 8]])
    assert res["p_value"] > 0.05
    assert res["verdict"] == "CHANCE (p>=alpha)"


def test_fisher_fallback_on_sparse_cells():
    res = chi_square_independence([[3, 0], [0, 3]])
    assert res["method"] == "fisher"
    # p=0.1 for two-sided fisher on this table


def test_chi2_rejects_empty_table():
    with pytest.raises(ValueError):
        chi_square_independence([[0, 0], [0, 0]])


def test_engine_map_covers_approved_methods():
    expected = {
        "spy", "macro", "minvar", "dividend",
        "opportunistic", "static-ml", "adaptive", "rm-final",
        "ig-llm",
    }
    assert set(ENGINE_MAP) == expected


def test_unknown_method_rejected_before_engine_run():
    with pytest.raises(ValueError, match="unknown method"):
        run_standard_backtest("not-a-method")


def test_metric_bundle_full_row():
    idx = pd.date_range("2018-02-01", periods=2000, freq="B")
    rng = np.random.default_rng(3)
    vpath = 10000.0 * np.exp(np.cumsum(rng.normal(0.0006, 0.008, len(idx))))
    spy = 100.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.008, len(idx))))
    info = {"fees": 123.45, "trades": 9}
    row = metric_bundle("TEST", pd.Series(vpath, index=idx), info,
                        pd.Series(spy, index=idx), "2018-02-01", idx[-1])
    assert row is not None
    assert row["end_value"] > 10000.0
    assert row["fees"] == 123.45
    assert row["trades"] == 9
    assert row["maxdd"] <= 0.0
    assert np.isfinite(row["sharpe"])
    assert np.isfinite(row["sortino"]) or np.isnan(row["sortino"])
    assert np.isfinite(row["win_rate"])
    assert row["excess_sp500"] == row["excess_sp500"]  # not NaN


def test_regime_winloss_contingency_shape():
    idx = pd.date_range("2020-01-01", "2024-12-31", freq="B")
    rng = np.random.default_rng(7)
    strat = pd.Series(np.cumprod(1 + rng.normal(0.0008, 0.01, len(idx))), index=idx)
    spy = pd.Series(np.cumprod(1 + rng.normal(0.0005, 0.01, len(idx))), index=idx)
    cont, meta = regime_winloss(strat, spy, DEFAULT_REGIMES)
    assert cont.shape == (2, 2)  # bull x bear, win x loss
    assert set(meta) == {"bull", "bear"}
    assert meta["bull"]["n_months"] > 0
    assert meta["bear"]["n_months"] > 0


def test_audit_status_clean():
    assert audit_status({"fred_source": "FRED"}, True) == "AUDITED CLEAN"


def test_audit_status_env_degraded_not_blocking():
    status = audit_status({"fred_source": "PRICE FALLBACK"}, False)
    assert status.startswith("AUDITED CLEAN (env-degraded")


def test_audit_status_data_blocking_hard_fail():
    status = audit_status({"fred_source": "FRED", "div_partial": True}, True)
    assert status.startswith("DEGRADED-DATA")
    with pytest.raises(RuntimeError, match="DEGRADED-DATA"):
        audit_status({"fred_source": "FRED", "div_partial": True}, True, hard_fail=True)
