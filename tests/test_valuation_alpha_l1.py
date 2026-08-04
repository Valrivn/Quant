"""Tests for the valuation_alpha L1 core engine (ratios, alpha, engine)."""

import numpy as np
import pandas as pd
import pytest

from Quantitative.stochastic.markov_lifecycle import LifecycleMetrics
from valuation_alpha.ratios import (
    compute_lifecycle_metrics,
    peer_percentiles,
    mahalanobis_state,
)
from valuation_alpha.alpha import (
    ff5_residual_alpha,
    apply_slippage,
    excess_vs_sp500,
    portfolio_returns,
)
from valuation_alpha.engine import run_l1


def _synthetic_frame(rows=10, base_revenue=100.0, growth=0.02, margin=0.20):
    idx = pd.date_range("2023-01-01", periods=rows, freq="QE")
    revenue = [base_revenue * (1 + growth) ** i for i in range(rows)]
    return pd.DataFrame(
        {
            "revenue": revenue,
            "roic": [0.15] * rows,
            "reinvestment": [0.25] * rows,
            "interest_coverage": [8.0] * rows,
            "operating_margin": [margin] * rows,
            "debt_to_capital": [0.3] * rows,
            "cash_burn": [12.0] * rows,
        },
        index=idx,
    )
    idx = pd.date_range("2023-01-01", periods=quarters, freq="QE")
    revenue = [base_revenue * (1 + growth) ** i for i in range(quarters)]
    return pd.DataFrame(
        {
            "revenue": revenue,
            "roic": [0.15] * quarters,
            "reinvestment": [0.25] * quarters,
            "interest_coverage": [8.0] * quarters,
            "operating_margin": [margin] * quarters,
            "debt_to_capital": [0.3] * quarters,
            "cash_burn": [12.0] * quarters,
        },
        index=idx,
    )


class TestComputeLifecycleMetrics:

    def test_fields_populated_and_growth_computed(self):
        q = _synthetic_frame(rows=12, base_revenue=100.0, growth=0.02)
        m = compute_lifecycle_metrics(q)
        assert isinstance(m, LifecycleMetrics)
        assert m.roic == pytest.approx(0.15)
        assert m.operating_margin == pytest.approx(0.20)
        assert m.interest_coverage_ratio == pytest.approx(8.0)
        assert m.reinvestment_rate == pytest.approx(0.25)
        last4 = sum(100.0 * (1.02) ** i for i in range(8, 12))
        prev4 = sum(100.0 * (1.02) ** i for i in range(4, 8))
        assert m.revenue_growth == pytest.approx(last4 / prev4 - 1.0)
        assert m.margin_variance_10y == pytest.approx(0.0)

    def test_short_history_is_nan_safe(self):
        q = pd.DataFrame(
            {"revenue": [10.0, 11.0]},
            index=pd.date_range("2025-01-01", periods=2, freq="QE"),
        )
        m = compute_lifecycle_metrics(q)
        assert m.revenue_growth == pytest.approx(11.0 / 10.0 - 1.0)
        assert np.isnan(m.roic)
        assert np.isnan(m.margin_variance_10y)

    def test_empty_frame_returns_nan_metrics(self):
        m = compute_lifecycle_metrics(pd.DataFrame())
        assert np.isnan(m.roic)
        assert np.isnan(m.revenue_growth)


class TestPeerPercentiles:
    def test_extreme_ticker_gets_extreme_percentiles(self):
        metrics = {
            "A": LifecycleMetrics(0.1, 0.1, 0.01, 0.01, 0.05, 0.1, 1.0, 1.0),
            "B": LifecycleMetrics(0.2, 0.2, 0.05, 0.02, 0.10, 0.2, 3.0, 2.0),
            "C": LifecycleMetrics(0.9, 0.9, 0.40, 0.03, 0.30, 0.3, 9.0, 3.0),
        }
        sector = {"A": "s", "B": "s", "C": "s"}
        df = peer_percentiles(metrics, sector, ["roic", "revenue_growth"])
        assert df.loc["A", "roic_pct"] == pytest.approx(0.0)
        assert df.loc["C", "roic_pct"] == pytest.approx(100.0)
        assert df.loc["B", "roic_pct"] == pytest.approx(50.0)


class TestMahalanobisState:
    def test_finite_and_outlier_larger(self):
        metrics = {
            "A": LifecycleMetrics(0.1, 0.1, 0.01, 0.01, 0.05, 0.1, 1.0, 1.0),
            "B": LifecycleMetrics(0.2, 0.2, 0.02, 0.02, 0.10, 0.2, 2.0, 2.0),
            "C": LifecycleMetrics(0.2, 0.2, 0.02, 0.02, 0.10, 0.2, 2.0, 2.0),
            "OUT": LifecycleMetrics(0.9, 0.9, 0.50, 0.05, 0.40, 0.5, 9.0, 5.0),
        }
        df = mahalanobis_state(metrics, ["roic", "revenue_growth", "operating_margin"])
        assert not df.empty
        assert df["mahalanobis"].notna().all()
        assert df.loc["OUT", "mahalanobis"] > df.loc["A", "mahalanobis"]

    def test_empty_on_failure(self):
        df = mahalanobis_state({}, ["roic"])
        assert df.empty


class TestFf5ResidualAlpha:
    def _synthetic(self, n=300, seed=0):
        rng = np.random.default_rng(seed)
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        mkt = rng.normal(0.0005, 0.01, n)
        smb = rng.normal(0.0002, 0.008, n)
        hml = rng.normal(0.0001, 0.008, n)
        rmw = rng.normal(0.0001, 0.008, n)
        cma = rng.normal(0.0001, 0.008, n)
        rf = np.full(n, 0.0001)
        alpha = 0.0005
        beta = np.array([1.0, 0.5, -0.3, 0.2, 0.1])
        excess = alpha + mkt * beta[0] + smb * beta[1] + hml * beta[2] + rmw * beta[3] + cma * beta[4]
        factors = pd.DataFrame(
            {"Mkt-RF": mkt, "SMB": smb, "HML": hml, "RMW": rmw, "CMA": cma, "RF": rf},
            index=dates,
        )
        returns = pd.Series(excess + rf, index=dates)
        return returns, factors

    def test_recovers_alpha(self):
        returns, factors = self._synthetic()
        res = ff5_residual_alpha(returns, factors, horizon_days=300)
        assert res is not None
        assert res["alpha_annualized"] == pytest.approx(0.0005 * 252, rel=0.05)
        assert res["ci_lower"] <= res["alpha_annualized"] <= res["ci_upper"]
        assert res["n_obs"] == 300

    def test_none_on_insufficient_data(self):
        returns, factors = self._synthetic(n=30)
        assert ff5_residual_alpha(returns, factors, horizon_days=30) is None
        assert ff5_residual_alpha(pd.Series(dtype=float), factors) is None

    def test_rescales_monthly_factors_to_daily(self):
        """Monthly FF5 factors forward-filled to daily must be rescaled to the
        per-day equivalent or the regression alpha is inflated by ~21x.
        Uses piecewise-constant daily factors (constant within each month), the
        case where the monthly rescale is exact."""
        rng = np.random.default_rng(1)
        r_dates = pd.date_range("2022-01-03", periods=756, freq="B")
        periods = r_dates.to_period("M")
        months = sorted(periods.unique())
        n_days = periods.value_counts()
        monthly_f = pd.DataFrame(
            {
                col: rng.normal(0.0003, 0.01, len(months))
                for col in ["Mkt-RF", "SMB", "HML", "RMW", "CMA"]
            },
            index=months,
        )
        f_daily = monthly_f.reindex(periods).reset_index(drop=True)
        f_daily.index = r_dates
        rf_daily = np.full(len(r_dates), 0.0001)
        alpha = 0.0005
        beta = np.array([1.0, 0.5, -0.3, 0.2, 0.1])
        excess = alpha + f_daily[["Mkt-RF", "SMB", "HML", "RMW", "CMA"]].values @ beta
        returns = pd.Series(excess + rf_daily, index=r_dates)
        monthly = (
            f_daily * n_days.reindex(periods).values[:, None]
        ).groupby(periods).first()
        monthly.index = pd.PeriodIndex(months).to_timestamp("M")
        monthly["RF"] = rf_daily[0] * n_days.reindex(months).values
        res = ff5_residual_alpha(returns, monthly, horizon_days=756)
        assert res is not None
        assert res["alpha_annualized"] == pytest.approx(alpha * 252, rel=0.05)
        assert res["n_obs"] >= 700


class TestApplySlippage:
    def test_reduces_cumulative_return(self):
        returns = pd.Series([0.01, 0.01, 0.01, 0.01], index=range(4))
        out = apply_slippage(returns, slippage=0.005)
        assert (out <= returns).all()
        assert (1 + out).prod() < (1 + returns).prod()


class TestExcessVsSp500:
    def test_returns_dict(self):
        idx = pd.date_range("2024-01-01", periods=100, freq="B")
        returns = pd.Series(np.full(100, 0.001), index=idx)
        sp500 = pd.Series(np.full(100, 0.0005), index=idx)
        res = excess_vs_sp500(returns, sp500)
        assert res is not None
        assert res["excess_annualized"] == pytest.approx(0.0005 * 252)

    def test_none_on_empty(self):
        assert excess_vs_sp500(pd.Series(dtype=float), pd.Series(dtype=float)) is None


class TestPortfolioReturns:
    def test_weighted_combination(self):
        idx = pd.date_range("2024-01-01", periods=3, freq="D")
        df = pd.DataFrame({"A": [0.01, 0.02, 0.03], "B": [0.02, 0.01, 0.00]}, index=idx)
        port = portfolio_returns(df, {"A": 0.5, "B": 0.5})
        assert port.iloc[0] == pytest.approx(0.015)
        assert port.iloc[1] == pytest.approx(0.015)


class TestRunL1:
    def test_end_to_end(self):
        rng = np.random.default_rng(1)
        n = 300
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        prices = pd.DataFrame(
            {
                "AAA": np.cumprod(1 + rng.normal(0.0005, 0.01, n)),
                "BBB": np.cumprod(1 + rng.normal(0.0003, 0.01, n)),
                "CCC": np.cumprod(1 + rng.normal(0.0001, 0.01, n)),
            },
            index=dates,
        )
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
        sp500 = pd.Series(np.cumprod(1 + rng.normal(0.0003, 0.008, n)), index=dates)
        sector = {"AAA": "tech", "BBB": "tech", "CCC": "tech"}
        quarterly = {
            "AAA": _synthetic_frame(rows=12, base_revenue=100.0, growth=0.03),
            "BBB": _synthetic_frame(rows=12, base_revenue=50.0, growth=0.01),
            "CCC": _synthetic_frame(rows=12, base_revenue=20.0, growth=0.0),
        }
        result = run_l1(
            ["AAA", "BBB", "CCC"],
            prices,
            factors,
            sp500,
            sector,
            quarterly,
            horizons_days={252: "1y", 756: "3y"},
        )
        names = result["names"]
        expected = {
            "ticker", "sector", "bias", "lifecycle", "mahalanobis",
            "alpha_1y_ann", "alpha_1y_ci_lower", "alpha_1y_ci_upper",
            "alpha_3y_ann", "alpha_3y_ci_lower", "alpha_3y_ci_upper",
            "excess_1y", "excess_3y",
        }
        assert expected.issubset(set(names.columns))
        assert len(names) == 3
        assert set(result["markov"].keys()) == {"AAA", "BBB", "CCC"}
        assert result["config"]["time_horizon"] == 5

    def test_skips_missing_data_with_nan(self):
        n = 300
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        prices = pd.DataFrame(
            {"AAA": np.cumprod(1 + np.random.default_rng(0).normal(0.0005, 0.01, n))},
            index=dates,
        )
        factors = pd.DataFrame(
            {
                "Mkt-RF": np.zeros(n), "SMB": np.zeros(n), "HML": np.zeros(n),
                "RMW": np.zeros(n), "CMA": np.zeros(n), "RF": np.zeros(n),
            },
            index=dates,
        )
        sp500 = pd.Series(np.ones(n), index=dates)
        sector = {"AAA": "tech", "BBB": "tech"}
        quarterly = {"AAA": _synthetic_frame(rows=12)}
        out = run_l1(
            ["AAA", "BBB"],
            prices,
            factors,
            sp500,
            sector,
            quarterly,
        )
        names = out["names"]
        assert len(names) == 2
        bbb = names[names["ticker"] == "BBB"].iloc[0]
        assert np.isnan(bbb["mahalanobis"])
        assert np.isnan(bbb["alpha_1y_ann"])