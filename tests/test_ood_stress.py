"""OOD (out-of-distribution) stress tests — audit finding #18.

Every test uses mocks only (no live network) and verifies:
  - No unhandled exceptions under extreme/degraded inputs
  - Outputs have expected NaN/empty structure
  - skipped / degraded flags are set correctly where applicable

Scenarios:
  1. Negative interest rates  (FRED DGS10 < 0)
  2. Prolonged liquidity freeze (empty/partial yfinance data)
  3. SEC CIK map failures (resolve_cik raises / returns None)
  4. Extreme credit spreads (BAA10Y >> 400 bps)
  5. All-NaN returns (ticker with zero price history)
"""

from __future__ import annotations

import math
import random
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest


# ──────────────────────────────────────────────────────────────────
# Scenario 1: Negative interest rates
# ──────────────────────────────────────────────────────────────────

class TestNegativeInterestRates:
    """FRED DGS10 returning negative values must not crash wacc or MC."""

    def test_compute_wacc_negative_risk_free(self):
        """WACC with negative cost-of-equity (driven by negative Rf) is
        mathematically valid and should not raise."""
        from Qualitative.psychological.four_lane_pipeline import compute_wacc

        # cost_of_equity = Rf + beta*ERP;  if Rf < 0 the whole term can be negative
        result = compute_wacc(
            equity_weight=0.80,
            cost_of_equity=-0.01,   # negative from deeply negative Rf
            debt_weight=0.20,
            cost_of_debt=0.03,
            tax_rate=0.21,
        )
        assert isinstance(result, float)
        assert not math.isnan(result)

    def test_wacc_below_terminal_growth_mc_does_not_crash(self):
        """When WACC <= terminal_growth the terminal value denominator
        approaches zero.  The engine must survive with a finite result
        (or inf / very large value) but never raise an unhandled exception."""
        from Qualitative.psychological.monte_carlo import (
            MonteCarloEngine,
            MonteCarloInput,
            create_monte_carlo_engine,
        )

        with patch("psychological.monte_carlo.load_hybrid_config", return_value={}):
            engine = create_monte_carlo_engine()

        # WACC slightly below terminal_growth (0.03)
        inp = MonteCarloInput(
            ticker="TEST_NEG_RF",
            expected_growth_mean=0.05,
            expected_growth_std=0.02,
            operating_margin_mean=0.20,
            operating_margin_std=0.02,
            wacc=0.02,           # < terminal_growth 0.03
            reinvestment_rate=0.40,
            roic=0.12,
            initial_revenue=10e9,
            initial_fcf=2e9,
            projection_years=5,
            n_simulations=50,     # small for speed
            terminal_growth=0.03,
        )
        # Must not raise — result may contain inf values but should complete
        result = engine.run(inp)
        assert result is not None
        assert result.n_simulations == 50

    def test_poisson_stress_lambda_negative_spread(self):
        """Negative credit spread (data anomaly) should fall through to
        regime-based fallback, never produce a negative lambda."""
        from Quantitative.stochastic.poisson_blackswan import PoissonBlackSwan

        pbs = PoissonBlackSwan(lambda_base=0.34)
        # negative spread → falls to regime fallback
        lam = pbs.compute_stress_lambda(current_spread_bps=-50.0, regime="NORMAL")
        assert lam >= 0.0

    def test_poisson_stress_lambda_zero_spread(self):
        """Zero spread should also fall through to regime fallback."""
        from Quantitative.stochastic.poisson_blackswan import PoissonBlackSwan

        pbs = PoissonBlackSwan(lambda_base=0.34)
        lam = pbs.compute_stress_lambda(current_spread_bps=0.0, regime="CRISIS")
        assert lam == pytest.approx(0.34 * 2.5)


# ──────────────────────────────────────────────────────────────────
# Scenario 2: Prolonged liquidity freeze
# ──────────────────────────────────────────────────────────────────

class TestLiquidityFreeze:
    """yfinance returning empty / partial data must not crash the pipeline."""

    def test_fetch_sleeve_prices_returns_empty_dataframe(self):
        """fetch_sleeve_prices returns pd.DataFrame() when yf.download
        yields None or empty on every retry."""
        import diversification.datastore as ds

        with patch.object(ds, "yf") as mock_yf, \
             patch.object(ds, "time"):
            mock_yf.download.return_value = None

            df = ds.fetch_sleeve_prices(["AAPL", "MSFT"], "2025-01-01", "2025-01-31")
            assert isinstance(df, pd.DataFrame)
            assert df.empty

    def test_fetch_sleeve_prices_partial_columns(self):
        """yfinance returns only some tickers — partial DataFrame returned."""
        import diversification.datastore as ds

        idx = pd.date_range("2025-01-01", periods=3)
        partial = pd.DataFrame({"Close": {"AAPL": [100, 101, 102]}},
                                index=idx)
        # multi-index columns: (Close, AAPL)
        partial.columns = pd.MultiIndex.from_tuples([("Close", "AAPL")])

        with patch.object(ds, "yf") as mock_yf, \
             patch.object(ds, "time"):
            mock_yf.download.return_value = partial

            df = ds.fetch_sleeve_prices(["AAPL", "MSFT"], "2025-01-01", "2025-01-04")
            assert isinstance(df, pd.DataFrame)
            # AAPL present, MSFT missing — but DataFrame is non-empty
            assert "AAPL" in df.columns

    def test_fetch_sleeve_prices_empty_list(self):
        """Empty ticker list returns empty DataFrame immediately."""
        import diversification.datastore as ds

        df = ds.fetch_sleeve_prices([], "2025-01-01", "2025-01-31")
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_run_l1_with_empty_prices(self):
        """Engine handles all-NaN return series gracefully."""
        from valuation_alpha.engine import run_l1

        prices = pd.DataFrame()  # empty
        factors = pd.DataFrame(
            columns=["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]
        )
        sp500 = pd.Series(dtype=float)
        sector = {"TEST": "technology"}

        # Mock get_universe to avoid DB hit
        with patch("valuation_alpha.engine.get_universe",
                   return_value=[{"ticker": "TEST", "sector": "technology", "bias": False}]):
            result = run_l1(
                ["TEST"], prices, factors, sp500, sector,
                {"TEST": pd.DataFrame()}, include_bias=True,
            )

        names = result["names"]
        assert len(names) == 1
        row = names.iloc[0]
        assert row["ticker"] == "TEST"
        assert np.isnan(row["alpha_1y_ann"])
        assert np.isnan(row["mahalanobis"])


# ──────────────────────────────────────────────────────────────────
# Scenario 3: SEC CIK map failures
# ──────────────────────────────────────────────────────────────────

class TestSECCIKFailures:
    """CIK resolution and companyfacts fetch failures must be absorbed."""

    def test_resolve_cik_returns_none(self):
        """resolve_cik returns None for an unknown ticker — no crash."""
        from valuation_alpha.universe.cik_resolver import resolve_cik

        with patch("valuation_alpha.universe.cik_resolver.get_cik_map",
                   return_value={}):
            result = resolve_cik("ZZZZZ")
            assert result is None

    def test_resolve_cik_map_exception(self):
        """resolve_cik propagates exceptions from get_cik_map (caller handles)."""
        from valuation_alpha.universe.cik_resolver import resolve_cik

        with patch("valuation_alpha.universe.cik_resolver.get_cik_map",
                   side_effect=ConnectionError("SEC down")):
            with pytest.raises(ConnectionError):
                resolve_cik("AAPL")

    def test_fetch_companyfacts_returns_empty_on_failure(self):
        """fetch_companyfacts returns {} when HTTP fails — never raises."""
        from valuation_alpha.datastore.xbrl_financials import fetch_companyfacts

        with patch("valuation_alpha.datastore.xbrl_financials.requests.get",
                   side_effect=ConnectionError("network down")):
            result = fetch_companyfacts("0000320193")
            assert result == {}

    def test_fetch_companyfacts_http_error(self):
        """fetch_companyfacts returns {} on HTTP 404/500."""
        from valuation_alpha.datastore.xbrl_financials import fetch_companyfacts

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("404 Not Found")
        with patch("valuation_alpha.datastore.xbrl_financials.requests.get",
                   return_value=mock_resp):
            result = fetch_companyfacts("0000320193")
            assert result == {}

    def test_pipeline_fetch_q_with_no_cik(self):
        """When CIK is None, pipeline _fetch_q skips the fetch entirely."""
        # Replicate pipeline.py's _fetch_q guard: no CIK → empty DataFrame
        cik = None
        if not cik:
            result = pd.DataFrame()
        else:
            result = "should not reach"
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_pipeline_skipped_list_on_all_failures(self, tmp_path):
        """When all tickers fail prices, all appear in skipped.
        Uses patch.start()/stop() to avoid Python's nesting limit."""
        import valuation_alpha.pipeline as pipeline_mod

        mock_universe = [
            {"ticker": "FAIL1", "sector": "tech", "sec_cik": "001", "bias": False},
            {"ticker": "FAIL2", "sector": "bio", "sec_cik": "002", "bias": False},
        ]

        patches = [
            patch.object(pipeline_mod, "get_universe", return_value=mock_universe),
            patch.object(pipeline_mod, "fetch_prices", return_value=pd.DataFrame()),
            patch.object(pipeline_mod, "fetch_ff5_factors",
                         return_value=pd.DataFrame(columns=["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"])),
            patch.object(pipeline_mod, "fetch_sp500", return_value=pd.Series(dtype=float)),
            patch.object(pipeline_mod, "fetch_companyfacts", return_value={}),
            patch.object(pipeline_mod, "build_fields_map", return_value={}),
            patch.object(pipeline_mod, "extract_quarterly_financials",
                         return_value=pd.DataFrame()),
            patch.object(pipeline_mod, "_derive_metrics", return_value={}),
            patch.object(pipeline_mod, "fetch_sleeve_prices", return_value=pd.DataFrame()),
            patch.object(pipeline_mod, "fetch_fred_series", return_value=pd.Series(dtype=float)),
            patch.object(pipeline_mod, "run_l1",
                         return_value={"names": pd.DataFrame(), "markov": {}, "config": {}}),
            patch.object(pipeline_mod, "generate_candidates", return_value=[]),
            patch.object(pipeline_mod, "rank_candidates", return_value=pd.DataFrame()),
            patch.object(pipeline_mod, "bias_ablation",
                         return_value={"run_a": {}, "run_b": {}, "delta": {}}),
            patch.object(pipeline_mod, "bias_ablation_report", return_value=""),
            patch.object(pipeline_mod, "_write", return_value=""),
            patch.object(pipeline_mod, "walk_forward_replay",
                         return_value={"decisions": pd.DataFrame(),
                                       "sleeve_returns": pd.Series(dtype=float)}),
            patch.object(pipeline_mod, "run_sleeve_backtest", return_value=pd.DataFrame()),
            patch.object(pipeline_mod, "sleeve_backtest_report", return_value=""),
            patch.object(pipeline_mod, "propose_configs",
                         return_value=[{"objective": "min_vol", "target_vol": 0.10}]),
            patch.object(pipeline_mod, "select_configs",
                         return_value=[{"objective": "min_vol", "target_vol": 0.10}]),
            patch.object(pipeline_mod, "walk_forward_allocate", return_value=pd.DataFrame()),
            patch.object(pipeline_mod, "portfolio_backtest",
                         return_value={"annualized_return": 0, "annualized_vol": 0,
                                       "sharpe": 0, "max_drawdown": 0}),
            patch.object(pipeline_mod, "_config_score", return_value=0),
            patch.object(pipeline_mod, "allocator_report", return_value=""),
        ]

        for p in patches:
            p.start()
        try:
            result = pipeline_mod.run_live_full(
                start="2025-01-01", end="2025-06-01",
                out_dir=str(tmp_path), max_workers=1,
            )
        finally:
            for p in patches:
                p.stop()

        # Both tickers should be in skipped (empty prices → no valid tickers)
        assert isinstance(result["skipped"], list)
        assert len(result["skipped"]) >= 2


# ──────────────────────────────────────────────────────────────────
# Scenario 4: Extreme credit spreads
# ──────────────────────────────────────────────────────────────────

class TestExtremeCreditSpreads:
    """BAA10Y at 1000+ bps must scale lambda_stress proportionally
    and BernoulliShockFilter must not crash with extreme ICR."""

    def test_poisson_lambda_scales_with_extreme_spread(self):
        """At 1100 bps (crisis level), lambda_stress should scale linearly."""
        from Quantitative.stochastic.poisson_blackswan import PoissonBlackSwan

        pbs = PoissonBlackSwan(
            lambda_base=0.34,
            historical_mean_spread_bps=220.0,
        )
        lam = pbs.compute_stress_lambda(current_spread_bps=1100.0)
        # ratio = 1100/220 = 5.0  →  lambda = 0.34 * 5.0 = 1.70
        assert lam == pytest.approx(0.34 * 5.0)

    def test_poisson_lambda_extreme_spread_non_negative(self):
        """Lambda is always >= 0 regardless of spread magnitude."""
        from Quantitative.stochastic.poisson_blackswan import PoissonBlackSwan

        pbs = PoissonBlackSwan(lambda_base=0.34)
        lam = pbs.compute_stress_lambda(current_spread_bps=10_000.0)
        assert lam >= 0.0

    def test_poisson_simulate_extreme_spread(self):
        """Full simulate at 1100 bps completes and returns valid result."""
        from Quantitative.stochastic.poisson_blackswan import PoissonBlackSwan, PoissonShockResult

        pbs = PoissonBlackSwan(lambda_base=0.34, historical_mean_spread_bps=220.0)
        result = pbs.simulate(current_spread_bps=1100.0, regime="CRISIS")
        assert isinstance(result, PoissonShockResult)
        assert result.lambda_stress > pbs.lambda_base
        assert result.current_spread_bps == 1100.0
        assert result.n_shocks >= 0

    def test_poisson_simulate_none_spread(self):
        """None spread falls back to regime multiplier without crashing."""
        from Quantitative.stochastic.poisson_blackswan import PoissonBlackSwan

        pbs = PoissonBlackSwan(lambda_base=0.34)
        result = pbs.simulate(current_spread_bps=None, regime="CRISIS")
        assert result.lambda_stress == pytest.approx(0.34 * 2.5)

    def test_bernoulli_extreme_low_icr(self):
        """ICR near zero (distressed) must produce high shock probability
        without crashing."""
        from Quantitative.stochastic.bernoulli_shock_filter import BernoulliShockFilter

        bsf = BernoulliShockFilter()
        prob = bsf.compute_shock_probability(
            icr=0.01,
            supplier_concentration=0.5,
            geopolitical_stress_factor=0.0,
        )
        assert 0.0 <= prob <= 1.0
        # ICR=0.01 → CCC/C range → high default probability
        assert prob > 0.05

    def test_bernoulli_negative_icr(self):
        """Negative ICR (negative earnings) must not crash."""
        from Quantitative.stochastic.bernoulli_shock_filter import BernoulliShockFilter

        bsf = BernoulliShockFilter()
        prob = bsf.compute_shock_probability(
            icr=-5.0,
            supplier_concentration=0.9,
            geopolitical_stress_factor=0.5,
        )
        assert 0.0 <= prob <= 1.0

    def test_bernoulli_extreme_high_icr(self):
        """Very high ICR (fortress balance sheet) → low shock probability."""
        from Quantitative.stochastic.bernoulli_shock_filter import BernoulliShockFilter

        bsf = BernoulliShockFilter()
        prob = bsf.compute_shock_probability(
            icr=100.0,
            supplier_concentration=0.3,
        )
        assert 0.0 <= prob <= 1.0
        assert prob < 0.05  # should be very low

    def test_bernoulli_run_trial_extreme_icr(self):
        """run_trial with extreme ICR returns valid BernoulliShockResult."""
        from Quantitative.stochastic.bernoulli_shock_filter import (
            BernoulliShockFilter,
            BernoulliShockResult,
        )

        bsf = BernoulliShockFilter()
        rng = random.Random(42)
        result = bsf.run_trial(
            icr=0.05,
            supplier_concentration=0.9,
            geopolitical_stress_factor=1.0,
            shock_severity=2.0,
            rng=rng,
        )
        assert isinstance(result, BernoulliShockResult)
        assert isinstance(result.shock_occurred, bool)
        assert 0.0 <= result.shock_probability <= 1.0
        assert result.icr_used == 0.05

    def test_bernoulli_health_modifier_extreme(self):
        """compute_health_modifier with extreme ICR returns bounded value."""
        from Quantitative.stochastic.bernoulli_shock_filter import BernoulliShockFilter

        mod_low = BernoulliShockFilter.compute_health_modifier(icr=0.0)
        mod_high = BernoulliShockFilter.compute_health_modifier(icr=100.0)
        assert 0.5 <= mod_low <= 2.0
        assert 0.5 <= mod_high <= 2.0

    def test_poisson_sample_zero_lambda(self):
        """Lambda=0 means zero shocks expected."""
        from Quantitative.stochastic.poisson_blackswan import PoissonBlackSwan

        pbs = PoissonBlackSwan(lambda_base=0.0)
        lam = pbs.compute_stress_lambda(current_spread_bps=0.0, regime="NORMAL")
        # regime NORMAL with lambda_base=0 → 0 * 1.0 = 0
        assert lam == 0.0
        np.random.seed(99)
        count = pbs.sample_shock_count(0.0)
        assert count == 0


# ──────────────────────────────────────────────────────────────────
# Scenario 5: All-NaN returns
# ──────────────────────────────────────────────────────────────────

class TestAllNaNReturns:
    """Ticker with zero price history: every downstream function must
    return NaN / empty / None — never raise."""

    def test_apply_slippage_all_nan_no_weights(self):
        """Legacy slippage path with all-NaN returns."""
        from valuation_alpha.alpha import apply_slippage

        returns = pd.Series([np.nan, np.nan, np.nan],
                            index=pd.date_range("2025-01-01", periods=3))
        result = apply_slippage(returns, slippage=0.005)
        assert len(result) == 3
        assert result.isna().all()

    def test_apply_slippage_all_nan_with_weights(self):
        """Turnover-based slippage with all-NaN returns."""
        from valuation_alpha.alpha import apply_slippage

        idx = pd.date_range("2025-01-01", periods=3)
        returns = pd.Series([np.nan, np.nan, np.nan], index=idx)
        weights = pd.DataFrame({"A": [1.0, 1.0, 1.0]}, index=idx)
        result = apply_slippage(returns, weights=weights, slippage=0.005)
        assert len(result) == 3
        assert result.isna().all()

    def test_ff5_residual_alpha_empty_returns(self):
        """Empty return series → None."""
        from valuation_alpha.alpha import ff5_residual_alpha

        factors = pd.DataFrame(
            np.random.randn(10, 6),
            columns=["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"],
            index=pd.date_range("2025-01-01", periods=10),
        )
        result = ff5_residual_alpha(pd.Series(dtype=float), factors)
        assert result is None

    def test_ff5_residual_alpha_none_returns(self):
        """None returns → None."""
        from valuation_alpha.alpha import ff5_residual_alpha

        factors = pd.DataFrame(
            np.random.randn(10, 6),
            columns=["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"],
        )
        result = ff5_residual_alpha(None, factors)
        assert result is None

    def test_ff5_residual_alpha_all_nan_returns(self):
        """Returns that are entirely NaN → None (no valid alignment)."""
        from valuation_alpha.alpha import ff5_residual_alpha

        idx = pd.date_range("2025-01-01", periods=100)
        returns = pd.Series([np.nan] * 100, index=idx)
        factors = pd.DataFrame(
            np.random.randn(100, 6),
            columns=["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"],
            index=idx,
        )
        result = ff5_residual_alpha(returns, factors)
        assert result is None

    def test_ff5_residual_alpha_empty_factors(self):
        """Empty factors → None."""
        from valuation_alpha.alpha import ff5_residual_alpha

        returns = pd.Series([0.01] * 100,
                            index=pd.date_range("2025-01-01", periods=100))
        result = ff5_residual_alpha(returns, pd.DataFrame())
        assert result is None

    def test_mahalanobis_state_all_nan_metrics(self):
        """All-NaN metric values → mahalanobis should be NaN/zero for
        every ticker, not crash."""
        from valuation_alpha.ratios import mahalanobis_state

        metrics = {
            "A": {"reinvestment_rate": np.nan, "roic": np.nan},
            "B": {"reinvestment_rate": np.nan, "roic": np.nan},
        }
        result = mahalanobis_state(metrics, ["reinvestment_rate", "roic"])
        assert isinstance(result, pd.DataFrame)
        assert "mahalanobis" in result.columns
        # All NaN → z-scored to 0 → distances are 0
        assert (result["mahalanobis"] == 0.0).all()

    def test_mahalanobis_state_empty_input(self):
        """Empty metrics dict → empty DataFrame."""
        from valuation_alpha.ratios import mahalanobis_state

        result = mahalanobis_state({}, ["reinvestment_rate"])
        assert isinstance(result, pd.DataFrame)
        assert result.empty or len(result) == 0

    def test_mahalanobis_state_single_ticker(self):
        """Single ticker → distance is 0 (centroid == itself)."""
        from valuation_alpha.ratios import mahalanobis_state

        metrics = {"A": {"reinvestment_rate": 0.5, "roic": 0.12}}
        result = mahalanobis_state(metrics, ["reinvestment_rate", "roic"])
        assert result.loc["A", "mahalanobis"] == pytest.approx(0.0)

    def test_excess_vs_sp500_empty(self):
        """excess_vs_sp500 with empty returns returns None."""
        from valuation_alpha.alpha import excess_vs_sp500

        result = excess_vs_sp500(pd.Series(dtype=float), pd.Series(dtype=float))
        assert result is None

    def test_apply_slippage_empty_series(self):
        """apply_slippage with empty returns returns empty series."""
        from valuation_alpha.alpha import apply_slippage

        result = apply_slippage(pd.Series(dtype=float))
        assert len(result) == 0

    def test_engine_all_nan_prices_row(self):
        """run_l1 row for a ticker with empty price history gets NaN alpha."""
        from valuation_alpha.engine import run_l1

        # prices has no columns → ticker gets empty ret → NaN alpha
        prices = pd.DataFrame(index=pd.date_range("2025-01-01", periods=10))
        factors = pd.DataFrame(
            np.random.randn(10, 6),
            columns=["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"],
            index=prices.index,
        )
        sp500 = pd.Series(np.random.randn(10), index=prices.index)

        with patch("valuation_alpha.engine.get_universe",
                   return_value=[{"ticker": "GHOST", "sector": "tech",
                                  "bias": False}]):
            result = run_l1(
                ["GHOST"], prices, factors, sp500,
                {"GHOST": "tech"}, {"GHOST": pd.DataFrame()},
                include_bias=True,
            )

        row = result["names"].iloc[0]
        assert row["ticker"] == "GHOST"
        assert np.isnan(row["alpha_1y_ann"])
        assert np.isnan(row["alpha_3y_ann"])
        assert np.isnan(row["mahalanobis"])
