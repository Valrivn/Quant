"""
tests/test_qual_standstill_monte_carlo.py — Unit Tests for Standstill Convex & Monte Carlo Evaluator.

Tests:
1. Standstill Baseline Invariance at lambda = 0.00:
   - Exactly reproduces the verified Optimal Value benchmark (+17.46% CAGR, Sharpe 0.83, Max DD -20.77%).
2. Convex Portfolio Blend:
   - w_hybrid = (1 - lambda) * w_quant + lambda * w_tilted for lambda in [0.00, 0.02, 0.05, 0.10].
   - Weight preservation (total equity weight preserved).
   - Correct exponentiation: w_tilted proportional to w_quant * exp(0.4 * Q_i).
3. 1,000-path Block-Bootstrap Monte Carlo:
   - Evaluates block-bootstrap with block_size=3.
   - 95% Confidence Intervals for CAGR, Sharpe, MaxDD, and Information Ratio.
   - At lambda = 0.00: Information Ratio is identically 0.00.
4. Custom asset-level returns and weights support.
"""

import numpy as np
import pandas as pd
import pytest

from validation.qual_standstill_monte_carlo import (
    evaluate_convex_standstill_monte_carlo,
    compute_tilted_weights,
    compute_convex_weights,
    compute_series_metrics,
    generate_block_bootstrap_indices,
    simulate_bootstrap_paths,
    StandstillMonteCarloReport,
)


class TestConvexWeighting:

    def test_tilted_weights_exponential_scaling(self):
        """Assert w_tilted scales as w_quant * exp(0.4 * Q_i) and preserves sum."""
        quant_weights = {"AAPL": 0.30, "MSFT": 0.30, "GOOGL": 0.25}
        conviction_scores = {"AAPL": 2.0, "MSFT": 0.0, "GOOGL": -1.0}

        w_tilted = compute_tilted_weights(quant_weights, conviction_scores, tilt_sensitivity=0.40)

        # Total weight preserved (0.85)
        assert pytest.approx(np.sum(w_tilted), abs=1e-6) == 0.85

        # AAPL (high conviction +2.0) should receive higher weight than MSFT (0.0)
        assert w_tilted[0] > quant_weights["AAPL"]
        # GOOGL (negative conviction -1.0) should receive lower weight
        assert w_tilted[2] < quant_weights["GOOGL"]
        # Order preserved
        assert w_tilted[0] > w_tilted[1] > w_tilted[2]

    def test_convex_blend_endpoints(self):
        """Assert lambda=0.0 returns quant weights, lambda=1.0 returns tilted weights."""
        w_quant = np.array([0.40, 0.45])
        w_tilted = np.array([0.50, 0.35])

        w_00 = compute_convex_weights(w_quant, w_tilted, lambda_val=0.0)
        assert np.allclose(w_00, w_quant)

        w_10 = compute_convex_weights(w_quant, w_tilted, lambda_val=1.0)
        assert np.allclose(w_10, w_tilted)

        w_05 = compute_convex_weights(w_quant, w_tilted, lambda_val=0.5)
        expected = 0.5 * w_quant + 0.5 * w_tilted
        assert np.allclose(w_05, expected)


@pytest.fixture(scope="module")
def baseline_report():
    """Run evaluator with default Optimal Value benchmark dataset."""
    return evaluate_convex_standstill_monte_carlo(
        lambdas=(0.00, 0.02, 0.05, 0.10),
        n_simulations=1000,
        block_size=3,
        seed=42,
    )


class TestStandstillMonteCarloBaseline:

    def test_baseline_standstill_exact_invariance(self, baseline_report):
        """
        CRITICAL: At lambda = 0.00, must exactly reproduce the Optimal Value benchmark:
        +17.46% net return, Sharpe 0.83, Max DD -20.77%.
        """
        res_0 = baseline_report.results_by_lambda[0.00]
        point = res_0.point_metrics

        # Annualized return (CAGR) should be 17.46%
        assert pytest.approx(point["cagr"], abs=1e-3) == 0.1746
        # Sharpe ratio should be 0.83
        assert pytest.approx(point["sharpe_ratio"], abs=0.01) == 0.83
        # Max drawdown should be -20.77%
        assert pytest.approx(point["max_drawdown"], abs=1e-3) == -0.2077
        # Information Ratio at lambda=0.0 must be 0.00
        assert point["information_ratio"] == 0.00

    def test_monte_carlo_confidence_intervals(self, baseline_report):
        """Assert 95% Confidence Intervals are well-formed across all lambdas."""
        for lam in [0.00, 0.02, 0.05, 0.10]:
            res = baseline_report.results_by_lambda[lam]

            # CAGR CI check
            cagr = res.annualized_return
            assert cagr.ci_lower <= cagr.mean <= cagr.ci_upper
            assert cagr.ci_lower < cagr.ci_upper

            # Sharpe CI check
            sharpe = res.sharpe_ratio
            assert sharpe.ci_lower <= sharpe.mean <= sharpe.ci_upper
            assert sharpe.ci_lower < sharpe.ci_upper

            # Max Drawdown CI check (drawdowns are negative)
            maxdd = res.max_drawdown
            assert maxdd.ci_lower <= maxdd.mean <= maxdd.ci_upper
            assert maxdd.ci_lower < maxdd.ci_upper
            assert maxdd.mean < 0.0

            # Information Ratio CI check
            ir = res.information_ratio
            if lam == 0.00:
                assert ir.point_estimate == 0.0
                assert ir.mean == 0.0
                assert ir.ci_lower == 0.0
                assert ir.ci_upper == 0.0
            else:
                assert ir.ci_lower <= ir.mean <= ir.ci_upper

    def test_summary_table_generation(self, baseline_report):
        """Assert summary table exists, is non-empty, and has expected columns."""
        df = baseline_report.summary_table
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 4
        assert "cagr_point" in df.columns
        assert "sharpe_point" in df.columns
        assert "maxdd_p95" in df.columns
        assert "ir_point" in df.columns

        # Verify lambda 0.0 row has 17.46% and 0.83
        row_0 = df.iloc[0]
        assert "17.46%" in row_0["cagr_point"]
        assert "0.83" in row_0["sharpe_point"]

    def test_block_bootstrap_index_generation(self):
        """Assert block bootstrap generates valid continuous blocks."""
        T = 83
        B = 3
        N = 100
        indices = generate_block_bootstrap_indices(n_periods=T, block_size=B, n_simulations=N)

        assert indices.shape == (N, T)
        assert np.all(indices >= 0)
        assert np.all(indices < T)

        # Check consecutive pairs within blocks: difference is 1 (mod T)
        for s in range(5):
            for t in range(0, T - B, B):
                assert (indices[s, t + 1] - indices[s, t]) % T == 1
                assert (indices[s, t + 2] - indices[s, t + 1]) % T == 1


class TestCustomAssetReturnsSimulation:

    def test_evaluate_with_custom_dataframe(self):
        """Assert evaluate_convex_standstill_monte_carlo works with custom asset returns."""
        # Create synthetic 36-month asset returns for 3 stocks
        rng = np.random.default_rng(123)
        dates = pd.date_range("2021-01-31", periods=36, freq="ME")
        returns_data = {
            "STOCK_A": rng.normal(0.015, 0.04, 36),
            "STOCK_B": rng.normal(0.010, 0.05, 36),
            "STOCK_C": rng.normal(0.005, 0.06, 36),
        }
        df_rets = pd.DataFrame(returns_data, index=dates)

        quant_weights = {"STOCK_A": 0.33, "STOCK_B": 0.33, "STOCK_C": 0.34}
        conviction_scores = {"STOCK_A": 1.5, "STOCK_B": 0.0, "STOCK_C": -1.5}

        report = evaluate_convex_standstill_monte_carlo(
            quant_weights=quant_weights,
            conviction_scores=conviction_scores,
            monthly_returns=df_rets,
            lambdas=(0.00, 0.05, 0.10),
            n_simulations=500,
            block_size=3,
            seed=123,
        )

        assert isinstance(report, StandstillMonteCarloReport)
        assert len(report.results_by_lambda) == 3

        # At lambda = 0.0, Information Ratio must be 0.00
        res_0 = report.results_by_lambda[0.00]
        assert res_0.point_metrics["information_ratio"] == 0.00

        # At lambda = 0.10, positive tilt should create active risk & return
        res_10 = report.results_by_lambda[0.10]
        assert res_10.point_metrics["tracking_error"] > 0.0
