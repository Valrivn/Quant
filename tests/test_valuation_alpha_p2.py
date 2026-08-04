"""Offline tests for the P2 selection layer (selection, stats, report)."""

import numpy as np
import pandas as pd
import pytest

from valuation_alpha.selection import (
    generate_candidates,
    rank_candidates,
    composite_score,
)
from valuation_alpha.stats import (
    block_bootstrap_alpha,
    deflated_sharpe,
    reality_check_mc,
    bias_ablation,
)
from valuation_alpha.report import bias_ablation_report

_FACTOR_COLS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA"]


def _synthetic_names():
    return pd.DataFrame(
        {
            "ticker": ["A", "B", "C", "D", "E", "F", "G", "H"],
            "sector": ["tech", "fin", "energy", "health", "tech", "fin", "energy", "health"],
            "alpha_3y_ann": [0.10, 0.09, 0.08, 0.07, 0.06, 0.05, 0.04, 0.03],
            "alpha_1y_ann": [0.05, 0.04, 0.03, 0.02, 0.01, 0.00, -0.01, -0.02],
            "lifecycle": [
                "FAST_GROWER", "STALWART", "FAST_GROWER", "SLOW_GROWER",
                "STALWART", "CYCLICAL", "TURNAROUND", "ASSET_PLAY",
            ],
            "debt_to_capital_pct": [10, 20, 30, 40, 50, 60, 70, 80],
            "mahalanobis": [1, 2, 3, 4, 5, 6, 7, 8],
        }
    )


def _synthetic_market(n=300, seed=0, tickers=("A", "B", "C", "D", "E", "F", "G", "H")):
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    mkt = rng.normal(0.0004, 0.01, n)
    factors = pd.DataFrame(
        {
            "Mkt-RF": mkt,
            "SMB": rng.normal(0.0, 0.008, n),
            "HML": rng.normal(0.0, 0.008, n),
            "RMW": rng.normal(0.0, 0.008, n),
            "CMA": rng.normal(0.0, 0.008, n),
            "RF": np.full(n, 0.0001),
        },
        index=dates,
    )
    data = {}
    for i, t in enumerate(tickers):
        alpha = 0.0003 + 0.0001 * i
        data[t] = alpha + mkt * 1.0 + rng.normal(0.0, 0.01, n)
    returns = pd.DataFrame(data, index=dates)
    sp500 = pd.Series(rng.normal(0.0003, 0.008, n), index=dates)
    return returns, factors, sp500


class TestGenerateCandidates:
    def test_candidates_created_and_weights_valid(self):
        names = _synthetic_names()
        cands = generate_candidates(names, top_n=8, k_values=[5, 10, 15])
        assert len(cands) == 3
        for c in cands:
            assert c["tickers"]
            assert set(c["tickers"]).issubset(set(names["ticker"]))
            assert sum(c["weights"].values()) == pytest.approx(1.0)
            sector_w = {}
            for t in c["tickers"]:
                s = names.loc[names["ticker"] == t, "sector"].iloc[0]
                sector_w[s] = sector_w.get(s, 0.0) + c["weights"][t]
            for s, w in sector_w.items():
                assert w <= 0.30 + 1e-9

    def test_composite_score_series_and_alpha_only(self):
        names = _synthetic_names()
        s = composite_score(names, "blended")
        assert isinstance(s, pd.Series)
        assert s.index.tolist() == names["ticker"].tolist()
        top = composite_score(names, "alpha_only").nlargest(1).index[0]
        assert top == "A"


class TestRankCandidates:
    def test_returns_sorted_ranking(self):
        names = _synthetic_names()
        returns, factors, sp500 = _synthetic_market()
        cands = generate_candidates(names, top_n=8, k_values=[5, 10])
        df = rank_candidates(names, factors, sp500, returns, cands)
        expected = {
            "candidate_name", "tickers", "alpha_annualized", "ci_lower",
            "ci_upper", "sharpe", "deflated_sharpe", "excess_sp500", "n_obs",
        }
        assert expected.issubset(set(df.columns))
        assert df["alpha_annualized"].is_monotonic_decreasing


class TestBlockBootstrapAlpha:
    def test_ci_ordering_and_count(self):
        returns, factors, _ = _synthetic_market(n=300)
        res = block_bootstrap_alpha(returns["A"], factors, n_boot=50)
        assert res["boot_ci_lower"] <= res["boot_ci_upper"]
        assert res["n_boot"] == 50


class TestDeflatedSharpe:
    def test_dsr_bounds_and_penalty(self):
        d1 = deflated_sharpe(1.0, 300, 5)
        d2 = deflated_sharpe(1.0, 300, 100)
        assert 0.0 <= d1["dsr"] <= 1.0
        assert d2["dsr"] < d1["dsr"]
        assert d1["p_value"] == pytest.approx(1.0 - d1["dsr"])


class TestRealityCheckMc:
    def _candidates(self, strong=False):
        if strong:
            return [
                {"name": "strong", "tickers": ["S"], "weights": {"S": 1.0}},
                {"name": "weak", "tickers": ["W"], "weights": {"W": 1.0}},
            ]
        return [
            {"name": "w1", "tickers": ["X"], "weights": {"X": 1.0}},
            {"name": "w2", "tickers": ["Y"], "weights": {"Y": 1.0}},
        ]

    def test_pvalue_range_and_weak_case(self):
        n = 300
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        rng = np.random.default_rng(3)
        mkt = rng.normal(0.0004, 0.01, n)
        factors = pd.DataFrame(
            {
                "Mkt-RF": mkt,
                "SMB": rng.normal(0.0, 0.008, n),
                "HML": rng.normal(0.0, 0.008, n),
                "RMW": rng.normal(0.0, 0.008, n),
                "CMA": rng.normal(0.0, 0.008, n),
                "RF": np.full(n, 0.0001),
            },
            index=dates,
        )
        returns = pd.DataFrame(
            {
                "X": rng.normal(0.0, 0.01, n),
                "Y": rng.normal(0.0, 0.01, n),
            },
            index=dates,
        )
        sp500 = pd.Series(rng.normal(0.0003, 0.008, n), index=dates)
        cands = self._candidates(strong=False)
        res = reality_check_mc(cands, returns, factors, sp500, n_sims=500)
        assert 0.0 <= res["p_value"] <= 1.0
        assert res["best_observed_alpha"] is not None
        assert res["p_value"] > 0.05

    def test_strong_candidate_low_pvalue(self):
        n = 1000
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        rng = np.random.default_rng(5)
        mkt = rng.normal(0.0004, 0.01, n)
        factors = pd.DataFrame(
            {
                "Mkt-RF": mkt,
                "SMB": rng.normal(0.0, 0.008, n),
                "HML": rng.normal(0.0, 0.008, n),
                "RMW": rng.normal(0.0, 0.008, n),
                "CMA": rng.normal(0.0, 0.008, n),
                "RF": np.full(n, 0.0001),
            },
            index=dates,
        )
        strong = np.zeros(n)
        strong[-20:] = 0.05
        returns = pd.DataFrame(
            {"S": strong + rng.normal(0.0, 0.005, n), "W": rng.normal(0.0, 0.005, n)},
            index=dates,
        )
        sp500 = pd.Series(rng.normal(0.0003, 0.008, n), index=dates)
        cands = self._candidates(strong=True)
        res = reality_check_mc(cands, returns, factors, sp500, n_sims=500)
        assert res["p_value"] < 0.05


class TestBiasAblation:
    def test_riding_bias(self):
        run_a = pd.DataFrame(
            {"ticker": ["M1", "M2", "M3"], "alpha_3y_ann": [0.20, 0.01, -0.01]}
        )
        run_b = pd.DataFrame(
            {"ticker": ["N1", "N2", "N3"], "alpha_3y_ann": [0.001, -0.001, 0.0]}
        )
        assert bias_ablation(run_a, run_b)["verdict"] == "RIDING_BIAS"

    def test_edge_real(self):
        run_a = pd.DataFrame(
            {"ticker": ["A", "B", "C"], "alpha_3y_ann": [0.10, 0.08, 0.06]}
        )
        run_b = pd.DataFrame(
            {"ticker": ["D", "E", "F"], "alpha_3y_ann": [0.07, 0.06, 0.05]}
        )
        assert bias_ablation(run_a, run_b)["verdict"] == "EDGE_REAL"


class TestBiasAblationReport:
    def test_returns_markdown_with_verdict(self):
        run_a = pd.DataFrame(
            {"ticker": ["A", "B", "C"], "alpha_3y_ann": [0.10, 0.08, 0.06]}
        )
        run_b = pd.DataFrame(
            {"ticker": ["D", "E", "F"], "alpha_3y_ann": [0.07, 0.06, 0.05]}
        )
        stats = bias_ablation(run_a, run_b)
        report = bias_ablation_report(stats["run_a"], stats["run_b"], stats)
        assert isinstance(report, str)
        assert len(report) > 0
        assert stats["verdict"] in report