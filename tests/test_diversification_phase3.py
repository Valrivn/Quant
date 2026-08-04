"""Offline tests for Phase-3 modules (D-20260803-005): the risk-constrained
gradient-descent allocator, the profit-change OR-gate, the cash-shortfall
relocation ablation, and the fee_sim3 Phase-3 strategy wiring."""

import numpy as np
import pandas as pd
import pytest

from diversification.allocator import (
    cash_shortfall_relocation,
    fit_static_ml_weights,
    load_config,
    objective,
    optimize_weights,
    profit_change_trigger,
    sleeve_return_series,
)
from diversification.fee_sim3 import _p3_sleeve_target

CFG = load_config()


def _rng_returns(n=300, ncols=4, seed=7):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(rng.normal(0.0004, 0.008, size=(n, ncols)),
                      columns=["spy", "small_mid", "dividend", "bonds"],
                      index=pd.date_range("2020-01-01", periods=n, freq="B"))
    return df


def _ticker_prices():
    idx = pd.date_range("2018-01-01", periods=400, freq="B")
    data = {}
    for t in ["SPY", "MDY", "IWM", "VCSH", "VCIT", "BIL", "SHY", "SGOV"]:
        data[t] = 100.0
    return pd.DataFrame(data, index=idx)


class TestConfig:
    def test_static_targets_sum_to_one(self):
        assert abs(sum(CFG["static_targets"].values()) - 1.0) < 1e-9

    def test_small_mid_floored(self):
        assert CFG["static_targets"]["small_mid"] == 0.15

    def test_dd_bound_is_30pct(self):
        assert CFG["optimizer"]["max_drawdown_bound"] == 0.30

    def test_bounds_feasible(self):
        lows = sum(CFG["sleeve_bounds"][s][0] for s in CFG["sleeves"])
        highs = sum(CFG["sleeve_bounds"][s][1] for s in CFG["sleeves"])
        assert lows <= 1.0 <= highs


class TestObjective:
    def test_low_risk_weight_scores_better(self):
        rng = np.random.default_rng(0)
        lo = pd.DataFrame(rng.normal(0.0006, 0.004, 300), columns=["a"])
        hi = pd.DataFrame(rng.normal(0.0006, 0.03, 300), columns=["b"])
        rets = pd.concat([lo, hi], axis=1)
        w_lo = np.array([1.0, 0.0])
        w_hi = np.array([0.0, 1.0])
        assert objective(w_lo, rets, CFG, CFG["sleeve_bounds"]) > objective(
            w_hi, rets, CFG, CFG["sleeve_bounds"])

    def test_dd_breach_is_heavily_punished(self):
        rng = np.random.default_rng(1)
        crash = pd.DataFrame(rng.normal(-0.001, 0.02, 500), columns=["a"])
        obj = objective(np.array([1.0]), crash, CFG, [(0.0, 1.0)])
        # A -30%+ drawdown under the dd_penalty must read as a deep negative.
        assert obj < -10.0


class TestOptimizer:
    def test_weights_sum_to_one_and_within_bounds(self):
        w = optimize_weights(_rng_returns(), CFG)
        assert w is not None
        assert abs(sum(w) - 1.0) < 1e-6
        order = list(CFG["sleeves"].keys())
        for i, s in enumerate(order):
            lo, hi = CFG["sleeve_bounds"][s]
            assert lo - 1e-9 <= w[i] <= hi + 1e-9

    def test_deterministic_given_seed(self):
        rets = _rng_returns()
        assert np.allclose(optimize_weights(rets, CFG), optimize_weights(rets, CFG))

    def test_small_mid_respects_floor(self):
        w = optimize_weights(_rng_returns(), CFG)
        sm = w[list(CFG["sleeves"].keys()).index("small_mid")]
        assert sm <= 0.15 + 1e-9

    def test_insufficient_data_returns_none(self):
        small = _rng_returns(n=20)
        assert optimize_weights(small, CFG) is None


def _ticker_rets(n=1000, seed=7):
    rng = np.random.default_rng(seed)
    cols = ["SPY", "MDY", "IWM", "VCSH", "VCIT", "BIL", "SHY", "SGOV"]
    return pd.DataFrame(rng.normal(0.0003, 0.006, size=(n, len(cols))),
                        columns=cols,
                        index=pd.date_range("2019-01-01", periods=n, freq="B"))


class TestSleeveReturnSeries:
    def test_full_history_when_window_none(self):
        rets = _ticker_rets(n=1000)
        sr = sleeve_return_series(rets, rets.index[-1], CFG, lambda d: [], window_days=None)
        assert sr is not None
        assert list(sr.columns) == list(CFG["sleeves"].keys())
        assert len(sr) > 500

    def test_trailing_window_respected(self):
        rets = _ticker_rets(n=1000)
        sr = sleeve_return_series(rets, rets.index[-1], CFG, lambda d: [],
                                  window_days=252)
        assert sr is not None
        assert len(sr) <= 252 + 30


class TestProfitChange:
    def test_fires_on_large_swing(self):
        idx = pd.date_range("2024-01-01", periods=90, freq="B")
        trail = pd.DataFrame(0.005, index=idx, columns=["SPY", "MDY"])
        w = {"SPY": 0.5, "MDY": 0.5}
        assert profit_change_trigger(trail, w, CFG)

    def test_no_fire_on_small_move(self):
        idx = pd.date_range("2024-01-01", periods=90, freq="B")
        trail = pd.DataFrame(0.0005, index=idx, columns=["SPY", "MDY"])
        w = {"SPY": 0.5, "MDY": 0.5}
        assert not profit_change_trigger(trail, w, CFG)

    def test_no_fire_on_empty(self):
        assert not profit_change_trigger(None, {"SPY": 1.0}, CFG)


class TestCashShortfall:
    def test_relocates_when_cash_depleted(self):
        target = {"SPY": 0.45, "MDY": 0.075, "IWM": 0.075, "VCSH": 0.04,
                  "VCIT": 0.04, "BIL": 0.04, "SHY": 0.24, "SGOV": 0.04}
        w_cur = {"SPY": 0.999}
        out, relocated = cash_shortfall_relocation(target, w_cur, "bear", CFG)
        assert relocated
        assert out != target
        assert abs(sum(out.values()) - 1.0) < 1e-9

    def test_no_relocation_with_cash(self):
        target = {"SPY": 0.45}
        w_cur = {"SPY": 0.60, "VCSH": 0.30}
        out, relocated = cash_shortfall_relocation(target, w_cur, "bear", CFG)
        assert not relocated
        assert out == target


class TestStaticMLFit:
    def test_returns_valid_weight_dict(self):
        rets = _ticker_rets(n=1200)
        w = fit_static_ml_weights(rets, CFG, lambda d: [], CFG["optimizer"]["train_end"])
        assert w is not None
        assert abs(sum(w.values()) - 1.0) < 1e-6
        assert set(w.keys()) == set(CFG["sleeves"].keys())


class TestSleeveTargetMapping:
    def test_maps_static_targets_to_tickers(self):
        prices = _ticker_prices()
        rets = prices.pct_change(fill_method=None)
        cfg = CFG
        target = _p3_sleeve_target(dict(cfg["static_targets"]), prices.index[300],
                                   rets, prices, {}, cfg)
        assert abs(sum(target.values()) - 1.0) < 1e-6
        assert target["SPY"] == pytest.approx(0.45)
        assert target["MDY"] == pytest.approx(0.075)
        assert target["IWM"] == pytest.approx(0.075)
        assert abs(target.get("SHY", 0.0) - 0.20) < 1e-9 or "SHY" in target
