"""Offline tests for the L3 whole-portfolio allocator."""

import numpy as np
import pandas as pd
import pytest

from portfolio.allocator import (
    portfolio_weights,
    apply_vol_target,
    walk_forward_allocate,
    portfolio_backtest,
)
from portfolio.llm_guide import propose_configs, select_configs
from portfolio.report import allocator_report

SLEEVES = ["corporate_bonds", "short_bills", "gold", "equity_income"]


def _synthetic_returns(days=756, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2021-01-01", periods=days, freq="B")
    drift = {"corporate_bonds": 0.0004, "short_bills": 0.0001, "gold": 0.0001, "equity_income": 0.0006}
    data = {}
    for s in SLEEVES:
        data[s] = rng.normal(drift[s], 0.01, days)
    return pd.DataFrame(data, index=idx)


class TestPortfolioWeights:

    def test_simplex_and_drift(self):
        r = _synthetic_returns()
        for obj in ("sharpe", "dual"):
            res = portfolio_weights(r, objective=obj, seed=1)
            w = res["weights"]
            assert abs(sum(w.values()) - 1.0) < 1e-6
            assert all(v >= 0 for v in w.values())
            assert w["equity_income"] > w["short_bills"]
            assert w["corporate_bonds"] > w["short_bills"]

    def test_alpha_objective(self):
        r = _synthetic_returns()
        res = portfolio_weights(r, objective="alpha", seed=2)
        assert "objective_value" in res
        assert abs(sum(res["weights"].values()) - 1.0) < 1e-6


class TestApplyVolTarget:

    def test_target_vol(self):
        r = _synthetic_returns()
        w = portfolio_weights(r, objective="dual", seed=3)["weights"]
        scaled = apply_vol_target(w, r, target_vol=0.10)
        assert all(v >= 0 for v in scaled.values())
        port = sum(r[s] * scaled[s] for s in SLEEVES)
        vol = port.std(ddof=1) * np.sqrt(252)
        assert abs(vol - 0.10) < 0.02


class TestWalkForwardAllocate:

    def test_step_function(self):
        r = _synthetic_returns(days=756, seed=4)
        w = walk_forward_allocate(r, rebalance_days=63, train_days=252, target_vol=0.10)
        assert isinstance(w, pd.DataFrame)
        assert list(w.columns) == SLEEVES
        assert abs(w.sum(axis=1) - 1.0).max() < 1e-6
        assert w.index[0] >= r.index[252]
        diffs = w.diff().abs().sum(axis=1)
        change_days = diffs[diffs > 1e-6].index
        assert len(change_days) == 0 or (change_days[0] >= w.index[0])


class TestPortfolioBacktest:

    def test_no_lookahead_and_metrics(self):
        r = _synthetic_returns(days=400, seed=5)
        w = pd.DataFrame(0.25, index=r.index, columns=SLEEVES)
        w.iloc[5] = [0.1, 0.2, 0.3, 0.4]
        bt = portfolio_backtest(w, r)
        assert "annualized_return" in bt
        assert bt["max_drawdown"] < 0
        assert len(bt["returns"]) == len(r)
        shifted = w.shift(1)
        assert bt["returns"].iloc[6] == pytest.approx(
            (shifted.iloc[6] * r.iloc[6]).sum(), abs=1e-9
        )


class TestLLMGuide:

    def test_propose_configs(self):
        r = _synthetic_returns()
        configs = propose_configs(r)
        assert 5 <= len(configs) <= 7
        for c in configs:
            for key in ("name", "objective", "target_vol", "max_drawdown_cap", "rationale", "regime"):
                assert key in c
            assert isinstance(c["rationale"], str) and c["rationale"]
        assert any(c["name"] == "equal_weight" for c in configs)

    def test_select_configs_deterministic(self):
        r = _synthetic_returns()
        proposed = propose_configs(r)
        a = select_configs(proposed, k=3)
        b = select_configs(proposed, k=3)
        assert len(a) == 3
        assert [c["name"] for c in a] == [c["name"] for c in b]


class TestReport:

    def test_report_string(self):
        r = _synthetic_returns()
        w = walk_forward_allocate(r, rebalance_days=63, train_days=252, target_vol=0.10)
        bt = portfolio_backtest(w, r)
        configs = select_configs(propose_configs(r), k=3)
        rep = allocator_report(w, bt, configs)
        assert isinstance(rep, str) and len(rep) > 0
        assert "objective" in rep or "sleeve" in rep