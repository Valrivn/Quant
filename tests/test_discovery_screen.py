"""Tests for the B-20260803 P2 discovery pilot screener."""

import numpy as np
import pandas as pd
import pytest

from valuation_alpha.discovery_screen import (
    quant_baseline_flags,
    liquidity_gate,
    glassdoor_tilt,
    run_discovery_screen,
)


def _names_frame():
    return pd.DataFrame({
        "ticker": ["AAA", "BBB", "CCC", "DDD"],
        "alpha_3y_ann": [0.20, 0.10, 0.05, -0.30],
        "cash_burn_months_pct": [60.0, 8.0, 40.0, np.nan],
        "interest_coverage_ratio_pct": [5.0, 2.0, 0.5, np.nan],
        "mahalanobis": [0.4, 0.5, 0.6, 0.7],
    })


class TestQuantBaseline:
    def test_flags_distress_and_alpha_floor(self):
        out = quant_baseline_flags(_names_frame())
        # BBB: low cash burn but otherwise OK -> passes quant (cash burn pct is
        # a percentile; the row reason only fires on extreme values)
        pass_set = set(out.loc[out["pass_quant"], "ticker"])
        # DDD has alpha z below floor -> excluded
        assert "DDD" not in pass_set
        # Every row got a decision
        assert out["pass_quant"].notna().all()

    def test_empty(self):
        assert quant_baseline_flags(pd.DataFrame()).empty


class TestLiquidityGate:
    def test_filters_by_price_and_history(self):
        dates = pd.date_range("2020-01-01", periods=120, freq="B")
        px = pd.DataFrame({
            "AAA": np.linspace(10, 20, 120),
            "BBB": np.full(120, 1.0),          # below min price
            "CCC": np.linspace(30, 40, 120),
        }, index=dates)
        fails = liquidity_gate(px, ["AAA", "BBB", "CCC", "ZZZ"])
        assert "BBB" in fails and "price<2" in fails["BBB"]
        assert "ZZZ" in fails
        assert "AAA" not in fails


class TestGlassdoorTilt:
    def test_tilt_continuous_not_gate(self):
        scores = pd.DataFrame({
            "ticker": ["AAA", "BBB", "CCC"],
            "score": [0.5, 0.5, 0.5],
        })
        gd = {"AAA": 0.8, "BBB": 0.4, "CCC": None}
        out = glassdoor_tilt(scores, gd)
        # above median -> positive tilt; below -> negative; None -> 0
        assert out.loc[out["ticker"] == "AAA", "glassdoor_z_tilt"].iloc[0] > 0
        assert out.loc[out["ticker"] == "BBB", "glassdoor_z_tilt"].iloc[0] < 0
        assert out.loc[out["ticker"] == "CCC", "glassdoor_z_tilt"].iloc[0] == 0.0
        # still ranked (not excluded)
        assert set(out["ticker"]) == {"AAA", "BBB", "CCC"}


class TestRunDiscoveryScreen:
    def test_end_to_end(self):
        rows = [
            {"ticker": "AAA", "group": "MID", "bias": False, "sector": "industrials", "sec_cik": "1"},
            {"ticker": "BBB", "group": "MID", "bias": False, "sector": "financials", "sec_cik": "2"},
            {"ticker": "CCC", "group": "SMALL", "bias": False, "sector": "healthcare", "sec_cik": "3"},
        ]
        names = pd.DataFrame({
            "ticker": ["AAA", "BBB", "CCC"],
            "alpha_3y_ann": [0.30, 0.10, 0.05],
            "cash_burn_months_pct": [60.0, 50.0, 40.0],
            "interest_coverage_ratio_pct": [5.0, 5.0, 5.0],
            "mahalanobis": [0.4, 0.4, 0.4],
        })
        dates = pd.date_range("2020-01-01", periods=100, freq="B")
        px = pd.DataFrame({t: np.linspace(20, 25, 100) for t in ["AAA", "BBB", "CCC"]}, index=dates)
        res = run_discovery_screen(rows, names, px, glassdoor_by_ticker={"AAA": 0.9, "CCC": 0.4})
        assert len(res["pass"]) == 3
        assert res["fail"] == {}
        assert set(res["screen"]["ticker"]) == {"AAA", "BBB", "CCC"}
        # tilt: AAA above median (+), BBB median (0), CCC below median (-)
        assert res["screen"].iloc[0]["ticker"] == "AAA"

    def test_reports_failures(self):
        rows = [
            {"ticker": "AAA", "group": "MID", "bias": False, "sector": "industrials", "sec_cik": "1"},
            {"ticker": "BBB", "group": "MID", "bias": False, "sector": "financials", "sec_cik": "2"},
        ]
        names = pd.DataFrame({
            "ticker": ["AAA", "BBB"],
            "alpha_3y_ann": [0.30, -0.50],
            "cash_burn_months_pct": [60.0, 5.0],
            "interest_coverage_ratio_pct": [5.0, 0.4],
            "mahalanobis": [0.4, 0.9],
        })
        dates = pd.date_range("2020-01-01", periods=100, freq="B")
        px = pd.DataFrame({t: np.linspace(20, 25, 100) for t in ["AAA", "BBB"]}, index=dates)
        res = run_discovery_screen(rows, names, px)
        assert res["pass"] == ["AAA"]
        assert "BBB" in res["fail"]
        assert "quant:" in res["fail"]["BBB"]
