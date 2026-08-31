"""Offline tests for the Discovery modules (B-20260804-001): the lightweight
Markov momentum chain and the return-max within-equity optimizer, plus the
pre-registered config block (invariant 4)."""

import numpy as np
import pandas as pd
import pytest

from diversification.allocator import load_config
from diversification.markov_momentum import momentum_overweight, momentum_score, transition_counts
from diversification.return_max import (
    ORDER as RM_ORDER,
    complex_return_series,
    fit_static_return_weights,
    objective_return_max,
    optimize_return_weights,
)

CFG = load_config()
RM = CFG["return_max"]


def _rng_returns(n=400, ncols=3, seed=7, drift=0.0004):
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(rng.normal(drift, 0.008, size=(n, ncols)),
                      columns=["spy", "small_mid", "dividend"],
                      index=pd.date_range("2020-01-01", periods=n, freq="B"))
    return df


def _ticker_prices(n=600):
    idx = pd.date_range("2018-01-01", periods=n, freq="B")
    data = {}
    for t in ["SPY", "MDY", "IWM", "VCSH", "VCIT", "BIL", "SHY", "SGOV"]:
        data[t] = 100.0
    return pd.DataFrame(data, index=idx)


class TestConfig:
    def test_dd_bound_is_40pct(self):
        assert RM["max_drawdown_bound"] == 0.40

    def test_bear_is_buy_more_state(self):
        e = RM["state_equity"]
        assert e["bear"] >= e["bull"] >= e["neutral"]

    def test_bear_buy_more_shares_sum_to_one(self):
        bb = RM["bear_buy_more"]
        assert abs(bb["basket_share"] + bb["spy_share"] + bb["small_mid_share"] - 1.0) < 1e-9

    def test_within_equity_bounds_feasible(self):
        for key in ("within_equity_bounds_high", "within_equity_bounds_low"):
            b = RM[key]
            assert sum(v[0] for v in b.values()) <= 1.0
            assert sum(v[1] for v in b.values()) >= 1.0

    def test_momentum_params_pre_registered(self):
        m = RM["momentum"]
        assert m["tilt"] > 1.0
        assert m["max_tilted"] >= 1
        assert m["window_days"] > 0

    def test_downside_engine_bars(self):
        de = RM["downside_engine"]
        assert de["arm"] < de["disarm"] < 0.0


class TestTransitionCounts:
    def test_all_up_series(self):
        c = transition_counts(np.ones(100))
        assert c[1, 1] == 99.0
        assert c[1, 0] == 0.0

    def test_matrix_shape_and_nonneg(self):
        c = transition_counts(np.random.default_rng(0).normal(0, 0.01, 500))
        assert c.shape == (2, 2)
        assert (c >= 0).all()


class TestMomentumScore:
    def test_up_trend_scores_high(self):
        idx = pd.date_range("2023-01-01", periods=300, freq="B")
        up = pd.Series(np.linspace(100.0, 160.0, 300), index=idx)
        s = momentum_score(up, idx[-1], window_days=252, min_obs=120)
        assert s is not None and s >= 0.5

    def test_down_trend_scores_low(self):
        idx = pd.date_range("2023-01-01", periods=300, freq="B")
        dn = pd.Series(np.linspace(100.0, 60.0, 300), index=idx)
        s = momentum_score(dn, idx[-1], window_days=252, min_obs=120)
        assert s is None or s <= 0.5

    def test_insufficient_history_returns_none(self):
        idx = pd.date_range("2023-01-01", periods=20, freq="B")
        s = momentum_score(pd.Series(np.ones(20), index=idx), idx[-1], min_obs=120)
        assert s is None

    def test_trailing_only(self):
        idx = pd.date_range("2023-01-01", periods=400, freq="B")
        up = pd.Series(np.linspace(100.0, 200.0, 400), index=idx)
        mid = idx[250]
        s = momentum_score(up, mid, window_days=252, min_obs=120)
        assert s is not None


class TestMomentumOverweight:
    def test_tilts_top_member_and_renormalizes(self):
        idx = pd.date_range("2023-01-01", periods=300, freq="B")
        prices = pd.DataFrame({
            "SPY": np.linspace(100, 160, 300),
            "MDY": np.linspace(100, 150, 300),
            "IWM": np.linspace(100, 90, 300),
            "JNJ": np.linspace(100, 95, 300),
        }, index=idx)
        base = {"SPY": 0.4, "MDY": 0.3, "IWM": 0.2, "JNJ": 0.1}
        out = momentum_overweight(["SPY", "MDY", "IWM", "JNJ"], prices, idx[-1], CFG, base)
        assert abs(sum(out.values()) - 1.0) < 1e-9
        assert out["SPY"] > base["SPY"] + 1e-9
        assert out["IWM"] < base["IWM"]

    def test_noop_when_disabled(self):
        cfg = dict(CFG)
        cfg["return_max"] = dict(CFG["return_max"])
        cfg["return_max"]["momentum"] = dict(CFG["return_max"]["momentum"])
        cfg["return_max"]["momentum"]["enabled"] = False
        idx = pd.date_range("2023-01-01", periods=300, freq="B")
        prices = pd.DataFrame({"SPY": np.linspace(100, 160, 300)}, index=idx)
        base = {"SPY": 1.0}
        assert momentum_overweight(["SPY"], prices, idx[-1], cfg, base) == base


class TestReturnMaxObjective:
    def test_high_mean_beats_low_mean_even_at_high_vol(self):
        rng = np.random.default_rng(0)
        lo = pd.DataFrame(rng.normal(0.0004, 0.004, 300), columns=["a"])
        hi = pd.DataFrame(rng.normal(0.0012, 0.02, 300), columns=["b"])
        rets = pd.concat([lo, hi], axis=1)
        w_hi = np.array([0.0, 1.0])
        w_lo = np.array([1.0, 0.0])
        assert objective_return_max(w_hi, rets, CFG) > objective_return_max(w_lo, rets, CFG)

    def test_dd_breach_punished(self):
        rng = np.random.default_rng(1)
        crash = pd.DataFrame(rng.normal(-0.002, 0.02, 500), columns=["a"])
        assert objective_return_max(np.array([1.0]), crash, CFG) < -5.0


class TestOptimizeReturnWeights:
    def test_sum_to_one_within_bounds(self):
        w = optimize_return_weights(_rng_returns(), CFG)
        assert w is not None
        assert abs(sum(w) - 1.0) < 1e-6
        bounds = RM["within_equity_bounds_high"]
        for i, s in enumerate(RM_ORDER):
            lo, hi = bounds[s]
            assert lo - 1e-9 <= w[i] <= hi + 1e-9

    def test_low_div_prefers_equity(self):
        rets = _rng_returns(drift=0.001)
        w_high = optimize_return_weights(rets, CFG, low_div=False)
        w_low = optimize_return_weights(rets, CFG, low_div=True)
        assert w_low is not None and w_high is not None
        assert w_low[0] >= w_high[0] - 1e-9

    def test_deterministic(self):
        rets = _rng_returns()
        assert np.allclose(optimize_return_weights(rets, CFG),
                           optimize_return_weights(rets, CFG))

    def test_insufficient_data_none(self):
        assert optimize_return_weights(_rng_returns(n=20), CFG) is None


def _ticker_rets(n=1000, seed=7):
    rng = np.random.default_rng(seed)
    cols = ["SPY", "MDY", "IWM", "VCSH", "VCIT", "BIL", "SHY", "SGOV"]
    return pd.DataFrame(rng.normal(0.0003, 0.006, size=(n, len(cols))),
                        columns=cols,
                        index=pd.date_range("2019-01-01", periods=n, freq="B"))


class TestComplexReturnSeries:
    def test_full_history_when_window_none(self):
        rets = _ticker_rets(n=1000)
        sr = complex_return_series(rets, rets.index[-1], CFG, lambda d: [], window_days=None)
        assert sr is not None
        assert list(sr.columns) == RM_ORDER
        assert len(sr) > 500

    def test_trailing_window_respected(self):
        rets = _ticker_rets(n=1000)
        sr = complex_return_series(rets, rets.index[-1], CFG, lambda d: [],
                                   window_days=252)
        assert sr is not None
        assert len(sr) <= 252 + 30


class TestStaticReturnFit:
    def test_returns_valid_weight_dict(self):
        rets = _ticker_rets(n=1200)
        w = fit_static_return_weights(rets, CFG, lambda d: [], RM["train_end"])
        assert w is not None
        assert abs(sum(w.values()) - 1.0) < 1e-6
        assert set(w.keys()) == set(RM_ORDER)


class TestIgLlmRetired:
    """D-20260815-001 gate-first path is RETIRED and LOCKED (2026-08-30).

    Instagram (instagram_raw_mentions / instagram_qual_proxies) was archived
    out of the live DB and the code path is fail-closed: the IG-LLM gate and
    the qualitative proxy reader must REFUSE loudly, never silently degrade or
    inject Instagram candidates into a backtest."""

    def test_ig_llm_passed_candidates_is_locked(self):
        from diversification import fee_sim3
        with pytest.raises(RuntimeError, match="RETIRED and LOCKED"):
            fee_sim3._ig_llm_passed_candidates()

    def test_ig_llm_backtest_engine_is_locked(self):
        from diversification import fee_sim3
        with pytest.raises(RuntimeError, match="DISABLED"):
            fee_sim3.run_sim_discovery_ig_llm()

    def test_qualitative_signals_refuses_ig_llm_ticker(self):
        from discovery import gate_data
        with pytest.raises(RuntimeError, match="RETIRED and LOCKED"):
            gate_data.qualitative_signals("IG_LLM_AAA")
