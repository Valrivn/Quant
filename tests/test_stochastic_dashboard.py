"""
Tests for the stochastic risk dashboard:
  1. dashboard/stochastic_risk_service — plain dict/DataFrame builders
  2. dashboard/tab_stochastic_risk — import cleanliness + cached MC wrapper
"""

import inspect

import pytest

import dashboard.stochastic_risk_service as srv
import dashboard.tab_stochastic_risk as tab_mod

FAKE_METRICS = {
    "reinvestment_rate": 0.50,
    "roic": 0.30,
    "revenue_growth": 0.15,
    "margin_variance_10y": 0.04,
    "operating_margin": 0.35,
    "debt_to_capital": 0.25,
    "interest_coverage_ratio": 12.0,
    "cash_burn_months": 0.0,
}


class TestBuildLifecycleMetrics:

    def test_accepts_metrics_dict(self):
        lm = srv.build_lifecycle_metrics("TEST", FAKE_METRICS)
        assert lm.interest_coverage_ratio == 12.0
        assert lm.reinvestment_rate == 0.50

    def test_metrics_dict_overrides_defaults(self):
        lm = srv.build_lifecycle_metrics("TEST", {"interest_coverage_ratio": 3.0})
        assert lm.interest_coverage_ratio == 3.0
        assert lm.reinvestment_rate == srv.DEFAULT_METRICS["reinvestment_rate"]

    def test_sane_defaults_without_fundamentals(self):
        lm = srv.build_lifecycle_metrics("UNKNOWN", use_fundamentals=False)
        assert lm.interest_coverage_ratio == srv.DEFAULT_METRICS["interest_coverage_ratio"]
        assert lm.roic == srv.DEFAULT_METRICS["roic"]

    def test_returns_lifecycle_metrics(self):
        lm = srv.build_lifecycle_metrics("TEST", FAKE_METRICS)
        assert hasattr(lm, "cash_burn_months")
        assert hasattr(lm, "debt_to_capital")


class TestMarkovLifecycleSection:

    def test_run_markov_lifecycle_keys(self):
        res = srv.run_markov_lifecycle("TEST", FAKE_METRICS)
        expected = {
            "ticker", "current_state", "projected_state", "n_steps",
            "state_distribution", "projected_distribution",
            "transition_volatility", "convergence_step", "transition_matrix",
            "metrics_used",
        }
        assert expected.issubset(set(res))
        assert len(res["state_distribution"]) == 6
        assert res["n_steps"] == 5
        assert res["metrics_used"]["interest_coverage_ratio"] == 12.0

    def test_build_markov_projection_df(self):
        df, res = srv.build_markov_projection_df("TEST", FAKE_METRICS)
        assert list(df.columns)[0] == "step"
        assert len(df.columns) == 7
        assert len(df) == 6
        assert df["step"].tolist() == [0, 1, 2, 3, 4, 5]
        assert res["current_state"] in [s.value for s in srv.STATES]

    def test_projection_rows_are_distributions(self):
        df, _ = srv.build_markov_projection_df("TEST", FAKE_METRICS)
        probs = df[df.columns[1:]].sum(axis=1)
        assert probs.apply(lambda v: abs(v - 1.0) < 0.01).all()


class TestMcPortfolioImpactSection:

    def test_run_mc_portfolio_impact_keys_and_compute_ms(self):
        res = srv.run_mc_portfolio_impact(current_spread_bps=220.0, regime="NORMAL", n_portfolios=200)
        expected = {
            "n_shocks", "lambda_base", "lambda_stress", "spread_ratio",
            "regime", "portfolio_impact", "shock_magnitudes", "compute_ms",
        }
        assert expected.issubset(set(res))
        assert isinstance(res["compute_ms"], float)
        assert res["compute_ms"] > 0
        assert isinstance(res["shock_magnitudes"], list)

    def test_lambda_scales_with_spread(self):
        normal = srv.run_mc_portfolio_impact(current_spread_bps=220.0, regime="NORMAL", n_portfolios=50)
        crisis = srv.run_mc_portfolio_impact(current_spread_bps=550.0, regime="CRISIS", n_portfolios=50)
        assert crisis["lambda_stress"] > normal["lambda_stress"]


class TestBernoulliShockSection:

    def test_run_bernoulli_shock_keys(self):
        res = srv.run_bernoulli_shock(icr=8.0)
        expected = {
            "icr", "synthetic_rating", "p_default_1y", "p_default_5y",
            "shock_occurred", "shock_probability", "penalty_multiplier",
            "lgd", "recovery_rate",
        }
        assert expected.issubset(set(res))
        assert res["synthetic_rating"] == "A+"
        assert res["p_default_5y"] >= res["p_default_1y"]

    def test_run_bernoulli_shock_deterministic(self):
        a = srv.run_bernoulli_shock(icr=8.0)
        b = srv.run_bernoulli_shock(icr=8.0)
        assert a == b

    def test_rating_deteriorates_with_lower_icr(self):
        good = srv.run_bernoulli_shock(icr=20.0)
        bad = srv.run_bernoulli_shock(icr=1.0)
        assert good["p_default_1y"] < bad["p_default_1y"]


class TestSectorShockSection:

    def test_run_sector_shock_keys(self):
        res = srv.run_sector_shock(sector="semiconductor")
        expected = {
            "sector", "p_base", "margin_vol_10y", "current_margin_vol",
            "supplier_concentration", "geopolitical_stress_factor",
            "shock_probability",
        }
        assert expected.issubset(set(res))
        assert res["sector"] == "semiconductor"
        assert 0.0 <= res["shock_probability"] <= 1.0

    def test_build_sector_shock_curve(self):
        df = srv.build_sector_shock_curve(sector="semiconductor")
        assert list(df.columns) == ["current_margin_vol", "shock_probability"]
        assert len(df) == 8
        assert (df["shock_probability"] >= 0).all()
        assert (df["shock_probability"] <= 1.0).all()

    def test_shock_probability_rises_with_margin_vol(self):
        low = srv.run_sector_shock(sector="semiconductor", current_margin_vol=0.02)
        high = srv.run_sector_shock(sector="semiconductor", current_margin_vol=0.15)
        assert high["shock_probability"] > low["shock_probability"]


class TestBuildStochasticReport:

    def test_report_contains_all_sections(self):
        report = srv.build_stochastic_report(
            "TEST",
            metrics=FAKE_METRICS,
            sector="semiconductor",
            n_portfolios=100,
        )
        assert set(report) >= {"ticker", "metrics_used", "markov", "bernoulli", "sector_shock", "mc"}
        assert report["ticker"] == "TEST"
        assert "compute_ms" in report["mc"]
        assert report["mc"]["compute_ms"] > 0
        assert report["bernoulli"]["synthetic_rating"]
        assert report["sector_shock"]["sector"] == "semiconductor"


class TestStochasticRiskTabModule:

    def test_module_imports_cleanly(self):
        assert callable(tab_mod.render_stochastic_risk_tab)

    def test_render_function_signature(self):
        sig = inspect.signature(tab_mod.render_stochastic_risk_tab)
        assert "primary_ticker" in sig.parameters

    def test_cached_mc_wrapper_is_cached_and_stable(self):
        first = tab_mod.get_cached_mc_simulation(current_spread_bps=220.0, regime="NORMAL", n_portfolios=100)
        second = tab_mod.get_cached_mc_simulation(current_spread_bps=220.0, regime="NORMAL", n_portfolios=100)
        assert first == second
        assert "compute_ms" in first
        assert first["compute_ms"] > 0

    def test_cached_mc_wrapper_different_args(self):
        normal = tab_mod.get_cached_mc_simulation(current_spread_bps=220.0, regime="NORMAL", n_portfolios=100)
        crisis = tab_mod.get_cached_mc_simulation(current_spread_bps=550.0, regime="CRISIS", n_portfolios=100)
        assert crisis["lambda_stress"] > normal["lambda_stress"]
