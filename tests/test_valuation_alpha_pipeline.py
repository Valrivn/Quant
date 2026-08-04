"""Offline tests for the P5 pipeline orchestration and dashboard tab."""

import json

import pandas as pd
import pytest

import valuation_alpha.pipeline as pipeline
import dashboard.tab_valuation_alpha as tab_mod

_FACTOR_COLS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]
_SLEEVES = ["corporate_bonds", "short_bills", "gold", "equity_income"]


def _dates(n=100):
    return pd.date_range("2020-01-01", periods=n, freq="B")


def _names_df():
    return pd.DataFrame(
        {
            "ticker": ["A", "B", "C"],
            "sector": ["tech", "fin", "energy"],
            "bias": [True, False, False],
            "lifecycle": ["FAST_GROWER", "STALWART", "CYCLICAL"],
            "mahalanobis": [1.0, 2.0, 3.0],
            "alpha_1y_ann": [0.05, 0.04, 0.03],
            "alpha_1y_ci_lower": [0.0, 0.0, 0.0],
            "alpha_1y_ci_upper": [0.1, 0.1, 0.1],
            "alpha_3y_ann": [0.08, 0.07, 0.06],
            "alpha_3y_ci_lower": [0.0, 0.0, 0.0],
            "alpha_3y_ci_upper": [0.15, 0.15, 0.15],
        }
    )


def _sleeve_returns_df():
    return pd.DataFrame({s: 0.0 for s in _SLEEVES}, index=_dates())


def _sleeve_results_df():
    return pd.DataFrame(
        {
            "sleeve": _SLEEVES + ["portfolio"],
            "alpha_annualized": [0.01, 0.02, 0.03, 0.04, 0.05],
            "alpha_ci_lower": [-0.01] * 5,
            "alpha_ci_upper": [0.03, 0.05, 0.07, 0.09, 0.11],
            "sharpe": [0.5, 0.6, 0.7, 0.8, 0.9],
            "deflated_sharpe": [0.5, 0.6, 0.7, 0.8, 0.9],
        }
    )


def _backtest_dict():
    return {
        "annualized_return": 0.10,
        "annualized_vol": 0.15,
        "sharpe": 0.8,
        "max_drawdown": -0.2,
        "alpha": {"alpha_annualized": 0.05, "ci_lower": 0.01, "ci_upper": 0.09},
        "deflated_sharpe": {"dsr": 0.9},
        "excess_vs_sp500": {"excess_annualized": 0.03},
        "returns": pd.Series(0.001, index=_dates()),
    }


def _install_pipeline_stubs(monkeypatch):
    def fake_fetch_prices(tickers, start, end):
        return pd.DataFrame({t: 100.0 for t in tickers}, index=_dates())

    def fake_fetch_ff5_factors():
        return pd.DataFrame({c: 0.0 for c in _FACTOR_COLS}, index=_dates())

    def fake_fetch_sp500(start, end):
        return pd.Series(0.0, index=_dates())

    def fake_fetch_companyfacts(cik):
        return {}

    def fake_fetch_sleeve_prices(tickers, start, end):
        return pd.DataFrame({t: 100.0 for t in tickers}, index=_dates())

    def fake_fetch_fred_series(sid, start, end):
        return pd.Series(0.0, index=_dates())

    def fake_run_l1(*args, **kwargs):
        return {"names": _names_df(), "markov": {}, "config": {}}

    def fake_generate_candidates(names, **kwargs):
        return [
            {
                "name": "k5_blended",
                "k": 5,
                "scoring": "blended",
                "tickers": ["A", "B"],
                "weights": {"A": 0.5, "B": 0.5},
            }
        ]

    def fake_rank_candidates(names, factors, sp500, prices, candidates, **kwargs):
        return pd.DataFrame(
            {
                "candidate_name": ["k5_blended"],
                "tickers": ["A,B"],
                "alpha_annualized": [0.06],
                "ci_lower": [0.0],
                "ci_upper": [0.12],
                "sharpe": [0.7],
                "deflated_sharpe": [0.8],
                "excess_sp500": [0.02],
                "n_obs": [100],
            }
        )

    def fake_bias_ablation(names_all, names_no_bias):
        return {
            "run_a": {"mean_alpha_3y": 0.05, "share_positive": 0.6, "best": "A", "worst": "B"},
            "run_b": {"mean_alpha_3y": 0.04, "share_positive": 0.5, "best": "C", "worst": "D"},
            "verdict": "EDGE_REAL",
        }

    def fake_walk_forward_replay(historical):
        return {
            "sleeve_returns": _sleeve_returns_df(),
            "decisions": pd.DataFrame(
                {"date": [_dates()[0], _dates()[50]], "spread_regime": ["NORMAL", "WIDENING"]}
            ),
            "portfolio_returns": pd.Series(0.0, index=_dates()),
            "config": {},
        }

    def fake_run_sleeve_backtest(historical):
        return _sleeve_results_df()

    def fake_walk_forward_allocate(sleeve_returns, **kwargs):
        return pd.DataFrame({c: 0.0 for c in sleeve_returns.columns}, index=sleeve_returns.index)

    def fake_portfolio_backtest(weights, sleeve_returns, **kwargs):
        return _backtest_dict()

    monkeypatch.setattr(pipeline, "fetch_prices", fake_fetch_prices)
    monkeypatch.setattr(pipeline, "fetch_ff5_factors", fake_fetch_ff5_factors)
    monkeypatch.setattr(pipeline, "fetch_sp500", fake_fetch_sp500)
    monkeypatch.setattr(pipeline, "fetch_companyfacts", fake_fetch_companyfacts)
    monkeypatch.setattr(pipeline, "fetch_sleeve_prices", fake_fetch_sleeve_prices)
    monkeypatch.setattr(pipeline, "fetch_fred_series", fake_fetch_fred_series)
    monkeypatch.setattr(pipeline, "run_l1", fake_run_l1)
    monkeypatch.setattr(pipeline, "generate_candidates", fake_generate_candidates)
    monkeypatch.setattr(pipeline, "rank_candidates", fake_rank_candidates)
    monkeypatch.setattr(pipeline, "bias_ablation", fake_bias_ablation)
    monkeypatch.setattr(pipeline, "walk_forward_replay", fake_walk_forward_replay)
    monkeypatch.setattr(pipeline, "run_sleeve_backtest", fake_run_sleeve_backtest)
    monkeypatch.setattr(pipeline, "walk_forward_allocate", fake_walk_forward_allocate)
    monkeypatch.setattr(pipeline, "portfolio_backtest", fake_portfolio_backtest)


class TestBuildFieldsMap:
    def test_returns_expected_friendly_keys(self):
        fields = pipeline.build_fields_map()
        expected = {
            "revenue", "operating_income", "equity", "interest_expense",
            "long_term_debt", "cash", "operating_expenses",
        }
        assert expected.issubset(set(fields))


class TestRunLiveFull:
    def test_full_pipeline_returns_bundle_and_writes_reports(self, monkeypatch, tmp_path):
        _install_pipeline_stubs(monkeypatch)
        result = pipeline.run_live_full(out_dir=str(tmp_path))
        expected_keys = {
            "run_a", "run_b", "ranking", "bias_ablation", "sleeve_results",
            "decisions", "allocator", "reports_paths", "skipped", "config",
        }
        assert expected_keys.issubset(set(result))
        assert isinstance(result["skipped"], list)
        assert isinstance(result["ranking"], pd.DataFrame)
        assert isinstance(result["sleeve_results"], pd.DataFrame)
        assert isinstance(result["decisions"], pd.DataFrame)
        assert isinstance(result["allocator"], dict)

        for name in ("bias_ablation_report.md", "sleeve_backtest_report.md",
                     "allocator_report.md", "results.json"):
            assert (tmp_path / name).exists()

        with open(tmp_path / "results.json", "r", encoding="utf-8") as f:
            bundle = json.load(f)
        assert "run_a" in bundle
        assert "ranking" in bundle
        assert "skipped" in bundle


class FakeSt:
    def __init__(self):
        self.calls = []

    def title(self, *a, **k):
        self.calls.append(("title", a, k))

    def caption(self, *a, **k):
        self.calls.append(("caption", a, k))

    def markdown(self, *a, **k):
        self.calls.append(("markdown", a, k))

    def dataframe(self, *a, **k):
        self.calls.append(("dataframe", a, k))

    def plotly_chart(self, *a, **k):
        self.calls.append(("plotly_chart", a, k))

    def info(self, *a, **k):
        self.calls.append(("info", a, k))

    def warning(self, *a, **k):
        self.calls.append(("warning", a, k))

    def error(self, *a, **k):
        self.calls.append(("error", a, k))


class TestRenderValuationAlphaTab:
    def test_renders_with_fake_cached_result(self, monkeypatch):
        fake_st = FakeSt()
        monkeypatch.setattr(tab_mod, "st", fake_st)

        fake_result = {
            "run_a": {"names": _names_df()},
            "run_b": {"names": _names_df()},
            "ranking": pd.DataFrame(
                {
                    "candidate_name": ["k5_blended"],
                    "tickers": ["A,B"],
                    "alpha_annualized": [0.06],
                    "ci_lower": [0.0],
                    "ci_upper": [0.12],
                    "sharpe": [0.7],
                    "deflated_sharpe": [0.8],
                    "excess_sp500": [0.02],
                }
            ),
            "sleeve_results": _sleeve_results_df(),
            "allocator": {"backtest": _backtest_dict()},
            "reports_paths": {
                "bias_ablation": "",
                "sleeve_backtest": "",
                "allocator": "",
            },
        }
        monkeypatch.setattr(tab_mod, "get_cached_pipeline", lambda: fake_result)

        tab_mod.render_valuation_alpha_tab()
        assert any(c[0] == "dataframe" for c in fake_st.calls)
        assert any(c[0] == "plotly_chart" for c in fake_st.calls)