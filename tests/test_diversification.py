"""Offline tests for the diversification sleeve (datastore, backtest, report)."""

import numpy as np
import pandas as pd
import pytest

from diversification.datastore import (
    SLEEVES,
    fetch_sleeve_prices,
    fetch_fred_series,
    assemble_historical,
)
from diversification.backtest import walk_forward_replay, run_sleeve_backtest
from diversification.report import sleeve_backtest_report


def _synthetic_prices(days=756, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2021-01-01", periods=days, freq="B")
    tickers = sorted({t for ts in SLEEVES.values() for t in ts})
    prices = pd.DataFrame(index=idx)
    for t in tickers:
        drift = 0.0002 if t in ("VTI", "VB") else 0.00005
        rets = rng.normal(drift, 0.01, days)
        prices[t] = 100.0 * np.exp(np.cumsum(rets))
    return prices


def _synthetic_fred(idx):
    spread = pd.Series(1.5, index=idx)
    crisis_start = idx[len(idx) // 3]
    crisis_end = idx[2 * len(idx) // 3]
    spread.loc[crisis_start:crisis_end] = 5.0
    real_rate = pd.Series(1.0, index=idx)
    m2 = pd.Series(np.linspace(1000.0, 1100.0, len(idx)), index=idx)
    return {
        "BAA10Y": spread,
        "DFII10": real_rate,
        "M2SL": m2,
        "DGS10": pd.Series(4.0, index=idx),
    }


class TestDatastore:

    def test_sleeves_shape(self):
        assert set(SLEEVES) == {"corporate_bonds", "short_bills", "gold", "equity_income"}
        assert SLEEVES["corporate_bonds"] == ["VCSH", "VCIT"]
        assert SLEEVES["short_bills"] == ["BIL", "SHY"]
        assert SLEEVES["gold"] == ["GLD", "IAU"]
        assert SLEEVES["equity_income"] == ["VTI", "VB", "BND"]

    def test_fetch_sleeve_prices_monkeypatched(self, monkeypatch):
        idx = pd.date_range("2023-01-01", periods=5, freq="B")
        fake = pd.DataFrame(
            {"Close": np.arange(5.0)},
            index=idx,
        )
        fake.columns = pd.MultiIndex.from_tuples([("Close", "VCSH")])

        def fake_download(tickers, start, end, progress, auto_adjust):
            return fake

        monkeypatch.setattr("diversification.datastore.yf.download", fake_download)
        df = fetch_sleeve_prices(["VCSH"], "2023-01-01", "2023-01-10")
        assert list(df.columns) == ["VCSH"]
        assert len(df) == 5

    def test_fetch_sleeve_prices_empty_on_failure(self, monkeypatch):
        def fake_download(*args, **kwargs):
            raise RuntimeError("network down")

        monkeypatch.setattr("diversification.datastore.yf.download", fake_download)
        df = fetch_sleeve_prices(["VCSH"], "2023-01-01", "2023-01-10")
        assert df.empty

    def test_fetch_fred_series_empty_on_failure(self, monkeypatch):
        class FakeScraper:
            def fetch_series(self, series_id):
                raise RuntimeError("network down")

        monkeypatch.setattr("diversification.datastore.FREDScraper", FakeScraper)
        s = fetch_fred_series("BAMLC0A0CM", "2023-01-01", "2023-12-31")
        assert isinstance(s, pd.Series)
        assert s.empty

    def test_assemble_historical_shape(self):
        prices = pd.DataFrame({"VCSH": [1.0, 2.0]})
        fred = {"BAMLC0A0CM": pd.Series([150.0])}
        hist = assemble_historical(prices, fred)
        assert set(hist) == {"prices", "fred"}
        assert hist["prices"] is prices
        assert hist["fred"] is fred


class TestBacktest:

    def test_walk_forward_replay_shape_and_crisis_switch(self):
        prices = _synthetic_prices()
        fred = _synthetic_fred(prices.index)
        hist = assemble_historical(prices, fred)
        result = walk_forward_replay(hist, rebalance_months=3)

        sleeve_returns = result["sleeve_returns"]
        assert set(sleeve_returns.columns) == set(SLEEVES)
        assert not sleeve_returns.empty

        decisions = result["decisions"]
        assert "date" in decisions.columns
        assert len(decisions) > 0

        # Portfolio is equal-weight 25% per sleeve.
        expected = sum(sleeve_returns[s] * 0.25 for s in SLEEVES)
        pd.testing.assert_series_equal(
            result["portfolio_returns"].dropna(),
            expected.dropna(),
            check_names=False,
        )

        # During the crisis window the bond leg switches to short bills.
        crisis_start = prices.index[len(prices) // 3]
        crisis_end = prices.index[2 * len(prices) // 3]
        crisis_decisions = decisions[
            (decisions["date"] >= crisis_start) & (decisions["date"] <= crisis_end)
        ]
        assert not crisis_decisions.empty
        assert (crisis_decisions["bond_choice"] == "short_bills").any()

        # No lookahead: a decision's regime matches the trailing spread at that date.
        for _, row in decisions.iterrows():
            trailing = fred["BAA10Y"][fred["BAA10Y"].index <= row["date"]].tail(90)
            median = trailing.median() * 100.0
            if median > 300:
                assert row["spread_regime"] == "CRISIS"
            elif median > 200:
                assert row["spread_regime"] == "WIDENING"
            else:
                assert row["spread_regime"] == "NORMAL"

    def test_run_sleeve_backtest_columns_and_portfolio_row(self):
        prices = _synthetic_prices()
        fred = _synthetic_fred(prices.index)
        hist = assemble_historical(prices, fred)
        results = run_sleeve_backtest(hist)
        expected_cols = {
            "sleeve",
            "annualized_return",
            "annualized_vol",
            "sharpe",
            "deflated_sharpe",
            "alpha_annualized",
            "alpha_ci_lower",
            "alpha_ci_upper",
            "excess_sp500",
            "boot_ci_lower",
            "boot_ci_upper",
        }
        assert set(results.columns) == expected_cols
        assert (results["sleeve"] == "portfolio").any()
        assert len(results) == len(SLEEVES) + 1


class TestReport:

    def test_report_returns_string_with_sleeve_name(self):
        results = pd.DataFrame(
            {
                "sleeve": ["corporate_bonds", "portfolio"],
                "alpha_annualized": [0.05, 0.03],
                "alpha_ci_lower": [0.01, -0.01],
                "alpha_ci_upper": [0.09, 0.07],
                "sharpe": [0.8, 0.6],
                "deflated_sharpe": [0.7, 0.5],
            }
        )
        decisions = pd.DataFrame(
            {
                "date": pd.to_datetime(["2023-01-01", "2023-04-01"]),
                "spread_regime": ["NORMAL", "CRISIS"],
            }
        )
        text = sleeve_backtest_report(results, decisions)
        assert isinstance(text, str)
        assert len(text) > 0
        assert "corporate_bonds" in text