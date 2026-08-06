"""Offline tests for Phase-1 modules (D-20260803-003): sleeves, macro_state,
risk_minimizer, and the fee_sim3 share-accounting engine."""

import numpy as np
import pandas as pd
import pytest

from diversification.macro_state import classify_state, macro_target_weights
from diversification.risk_minimizer import (
    _simplex_project,
    friction_bounded_rebalance,
    gradient_descent,
)
from diversification.sleeves import (
    ALL_TICKERS,
    DIVIDEND_YIELDS,
    SLEEVE_BOUNDS,
    MACRO_TARGETS,
    SLEEVES,
)


def _synthetic(idx_len=900, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2021-01-01", periods=idx_len, freq="B")
    prices = pd.DataFrame(index=idx)
    for t in ALL_TICKERS:
        vol = 0.012 if t in ("SPY", "GLD", "IAU") else 0.004
        rets = rng.normal(0.0002, vol, idx_len)
        prices[t] = 100.0 * np.exp(np.cumsum(rets))
    return prices


def _synthetic_spread(idx):
    spread = pd.Series(1.5, index=idx)
    crisis_start = idx[len(idx) // 3]
    spread.loc[crisis_start:] = 5.0
    return spread


class TestSleeves:

    def test_sleeve_config_shape(self):
        assert set(SLEEVES) == {"equity", "corporate_bonds", "short_bills", "gold"}
        assert SLEEVES["equity"] == ["SPY"]
        assert SLEEVES["gold"] == ["GLD", "IAU"]
        assert "SGOV" in SLEEVES["short_bills"]
        assert len(ALL_TICKERS) == len({t for ts in SLEEVES.values() for t in ts})

    def test_macro_targets_are_inside_bounds_and_sum_to_one(self):
        for state, targets in MACRO_TARGETS.items():
            assert abs(sum(targets.values()) - 1.0) < 1e-9
            for sleeve, w in targets.items():
                lo, hi = SLEEVE_BOUNDS[sleeve]
                assert lo <= w <= hi

    def test_bounds_feasible(self):
        assert sum(lo for lo, _ in SLEEVE_BOUNDS.values()) <= 1.0
        assert sum(hi for _, hi in SLEEVE_BOUNDS.values()) >= 1.0

    def test_dividend_yields_cover_all_tickers(self):
        from diversification.sleeves import P3_TICKERS

        # Pre-registered yields cover every tradeable ticker: sleeve ETFs plus
        # the Phase-2 stable-dividend candidates and the Phase-3 small/mid
        # proxies (coverage measurement only).
        assert set(DIVIDEND_YIELDS) == set(P3_TICKERS)
        assert DIVIDEND_YIELDS["GLD"] == 0.0
        assert DIVIDEND_YIELDS["IAU"] == 0.0


class TestMacroState:

    def test_bull_neutral_bear_classification(self):
        idx = pd.date_range("2021-01-01", periods=300, freq="B")
        spread = _synthetic_spread(idx)
        equity = pd.Series(np.linspace(100.0, 140.0, len(idx)), index=idx)
        assert classify_state(spread, equity, idx[100]) == "bull"
        assert classify_state(spread, equity, idx[200]) == "bear"
        spread_flat = pd.Series(2.5, index=idx)
        assert classify_state(spread_flat, equity, idx[100]) == "neutral"

    def test_missing_inputs_degrade_to_neutral(self):
        idx = pd.date_range("2021-01-01", periods=300, freq="B")
        spread = pd.Series(1.5, index=idx)
        assert classify_state(spread, pd.Series(dtype=float), idx[100]) == "neutral"
        assert classify_state(pd.Series(dtype=float), pd.Series(dtype=float), idx[100]) == "neutral"

    def test_no_lookahead(self):
        idx = pd.date_range("2021-01-01", periods=300, freq="B")
        spread = pd.Series(1.5, index=idx)
        spread.loc[idx[150]:] = 4.0
        equity = pd.Series(np.linspace(100.0, 140.0, len(idx)), index=idx)
        assert classify_state(spread, equity, idx[140]) == "bull"
        # Crisis onset is smoothed by the 90-day trailing median; once the
        # window is majority-widened the state flips to bear (no lookahead).
        assert classify_state(spread, equity, idx[200]) == "bear"

    def test_macro_target_weights_returns_valid_dict(self):
        w = macro_target_weights("bear")
        assert abs(sum(w.values()) - 1.0) < 1e-9
        assert macro_target_weights("unknown") == dict(MACRO_TARGETS["neutral"])


class TestRiskMinimizer:

    def test_simplex_projection_respects_bounds_and_sums_to_one(self):
        bounds = [(0.05, 0.3), (0.1, 0.5), (0.1, 0.5), (0.3, 0.7)]
        w = np.array([0.01, 0.2, 0.3, 0.9])
        p = _simplex_project(w, bounds)
        assert abs(p.sum() - 1.0) < 1e-6
        for (lo, hi), x in zip(bounds, p):
            assert lo - 1e-6 <= x <= hi + 1e-6

    def test_gradient_descent_minimizes_variance(self):
        rng = np.random.default_rng(0)
        n = 400
        idx = pd.date_range("2021-01-01", periods=n, freq="B")
        rets = pd.DataFrame(
            {
                "A": rng.normal(0.0, 0.02, n),
                "B": rng.normal(0.0, 0.02, n) + rng.normal(0.0, 0.02, n) * 0.1,
                "C": rng.normal(0.0, 0.03, n) + rng.normal(0.0, 0.02, n) * 0.5,
            },
            index=idx,
        )
        bounds = [(0.0, 1.0)] * 3
        w = gradient_descent(rets, bounds)
        assert w is not None
        assert abs(w.sum() - 1.0) < 1e-6
        cov = rets.cov().values
        eq = np.full(3, 1.0 / 3.0)
        assert w @ cov @ w <= eq @ cov @ eq + 1e-12

    def test_gradient_descent_returns_none_on_insufficient_data(self):
        small = pd.DataFrame({"A": [0.01, -0.01]})
        assert gradient_descent(small, [(0.0, 1.0), (0.0, 1.0)]) is None

    def test_friction_holds_when_no_improvement(self):
        rng = np.random.default_rng(1)
        n = 300
        idx = pd.date_range("2021-01-01", periods=n, freq="B")
        rets = pd.DataFrame({"A": rng.normal(0.0, 0.01, n)}, index=idx)
        bounds = [(0.0, 1.0)]
        current = np.array([1.0])
        w, traded = friction_bounded_rebalance(rets, bounds, current, fee_rate=0.005)
        assert not traded
        assert np.allclose(w, current)


class TestNasdaq:

    def _fake_session(self, payload):
        from diversification.datastore import requests

        class FakeResp:
            status_code = 200

            def json(self):
                return payload

        class FakeSession:
            def get(self, url, headers, timeout):
                return FakeResp()

        return FakeSession()

    def test_fetch_nasdaq_parses_chart(self, monkeypatch, tmp_path):
        from diversification.datastore import fetch_nasdaq

        payload = {
            "data": {
                "chart": [
                    {"z": {"dateTime": "1/3/2023", "value": "171.06"}},
                    {"z": {"dateTime": "1/4/2023", "value": "172.5"}},
                ]
            }
        }
        monkeypatch.setattr("diversification.datastore.requests.Session",
                            lambda: self._fake_session(payload))
        df = fetch_nasdaq(["GLD"], "2023-01-01", "2023-12-31", cache_dir=tmp_path)
        assert "GLD" in df.columns
        assert len(df) == 2
        assert float(df["GLD"].iloc[-1]) == 172.5

    def test_fetch_nasdaq_empty_on_failure(self, monkeypatch, tmp_path):
        from diversification.datastore import fetch_nasdaq

        def fake_get(url, headers, timeout):
            raise RuntimeError("network down")

        class FakeSession:
            def get(self, url, headers, timeout):
                raise RuntimeError("network down")

        monkeypatch.setattr("diversification.datastore.requests.Session",
                            lambda: FakeSession())
        df = fetch_nasdaq(["GLD"], "2023-01-01", "2023-12-31", cache_dir=tmp_path)
        assert df.empty


class TestFeeSim3Engine:

    def test_forced_deploy_then_hold_and_trade(self):
        from diversification.fee_sim3 import Portfolio

        idx = pd.date_range("2021-01-01", periods=900, freq="B")
        rng = np.random.default_rng(3)
        A = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, len(idx))))
        B = 100.0 * np.exp(np.cumsum(rng.normal(0.0001, 0.001, len(idx))))
        prices = pd.DataFrame({"A": A, "B": B}, index=idx)
        pf = Portfolio(prices, initial=10000.0)
        rebal = [idx[100], idx[200]]

        deployed = {"done": False}

        def target(d, w_cur, V):
            if not deployed["done"]:
                deployed["done"] = True
                return {"A": 1.0, "B": 0.0}, {}
            return {"A": 0.0, "B": 1.0}, {}

        vpath, info = pf.run(rebal, target)
        assert info["trades"] == 2
        assert info["fees"] > 0
        final = vpath.iloc[-1]
        assert final > 0
        assert info["dividends"] == 0.0  # no dividend yield in this fixture

    def test_small_turnover_is_skipped(self):
        from diversification.fee_sim3 import Portfolio

        idx = pd.date_range("2021-01-01", periods=900, freq="B")
        rng = np.random.default_rng(4)
        prices = pd.DataFrame(
            {
                "A": 100.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, len(idx)))),
                "B": 100.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, len(idx)))),
            },
            index=idx,
        )
        pf = Portfolio(prices, initial=10000.0)
        rebal = [idx[100], idx[200]]

        def target(d, w_cur, V):
            return {"A": 0.5, "B": 0.5}, {}

        vpath, info = pf.run(rebal, target)
        # First rebalance deploys; second has zero turnover and is skipped.
        assert info["trades"] == 1
        assert info["skipped"] >= 1

    def test_real_exdate_dividend_accrual(self):
        """Real ex-date dividend events credit exactly shares*dividend and are
        NOT replaced by the static DIVIDEND_YIELDS daily accrual (S4/S6)."""
        from diversification.fee_sim3 import Portfolio

        idx = pd.date_range("2021-01-01", periods=300, freq="B")
        prices = pd.DataFrame({"A": 100.0, "B": 100.0}, index=idx)
        ex_date = idx[150]
        div_hist = {"A": pd.Series([2.0], index=pd.DatetimeIndex([ex_date]))}
        pf = Portfolio(prices, initial=10000.0, div_hist=div_hist)

        deployed = {"done": False}

        def target(d, w_cur, V):
            if not deployed["done"]:
                deployed["done"] = True
                return {"A": 1.0, "B": 0.0}, {}
            return None, {}

        vpath, info = pf.run([idx[0]], target)
        # ~100 shares of A @ 100 (fees trim the buy slightly) -> one $2.00/share
        # dividend lump on the ex-date, not a daily static-yield drip.
        assert info["dividends"] == pytest.approx(200.0, rel=0.01)
        # Static daily-yield path is untouched: no div_hist -> zero dividends.
        pf2 = Portfolio(prices, initial=10000.0)
        vpath2, info2 = pf2.run([idx[0]], target)
        assert info2["dividends"] == 0.0

    def test_real_exdate_uses_event_not_static_yield(self):
        """A held asset with real events accrues the event total, not the
        static-yield accrual, even when DIVIDEND_YIELDS has a yield for it."""
        from diversification.fee_sim3 import Portfolio

        idx = pd.date_range("2021-01-01", periods=300, freq="B")
        prices = pd.DataFrame({"A": 100.0, "B": 100.0}, index=idx)
        ex_date = idx[150]
        div_hist = {"A": pd.Series([4.0], index=pd.DatetimeIndex([ex_date]))}
        pf = Portfolio(prices, initial=10000.0, div_hist=div_hist)

        deployed = {"done": False}

        def target(d, w_cur, V):
            if not deployed["done"]:
                deployed["done"] = True
                return {"A": 1.0, "B": 0.0}, {}
            return None, {}

        vpath, info = pf.run([idx[0]], target)
        assert info["dividends"] == pytest.approx(400.0, rel=0.01)
        # Real event total dominates any static-yield accrual.
        assert info["dividends"] > 300.0
