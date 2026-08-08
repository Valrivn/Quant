"""Offline tests for discovery.gate_data (B-20260807-002, lane 1)."""

import math
import sqlite3

import numpy as np
import pandas as pd
import pytest

from discovery import gate_data

_MOAT_KEYS = [
    "product_breadth",
    "developer_momentum",
    "employee_sentiment",
    "revenue_concentration",
    "network_effect_proxy",
    "regulatory_barrier",
]


def _prices(tickers, n=800):
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    rng = np.random.default_rng(42)
    return pd.DataFrame(
        {t: 100.0 * np.cumprod(1.0 + rng.normal(0, 0.01, n)) for t in tickers},
        index=dates,
    )


def _factors(n=800):
    dates = pd.date_range("2023-01-01", periods=n, freq="B")
    rng = np.random.default_rng(7)
    return pd.DataFrame(
        {
            "Mkt-RF": rng.normal(0, 0.01, n),
            "SMB": rng.normal(0, 0.01, n),
            "HML": rng.normal(0, 0.01, n),
            "RMW": rng.normal(0, 0.01, n),
            "CMA": rng.normal(0, 0.01, n),
            "RF": 0.0,
        },
        index=dates,
    )


def _empty_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE glassdoor_comparably_audit ("
        " ticker TEXT, date TEXT, glassdoor_normalized REAL, comparably_normalized REAL)"
    )
    conn.execute(
        "CREATE TABLE github_org_metrics ("
        " ticker TEXT, repo_name TEXT, stars INTEGER, forks INTEGER)"
    )
    conn.execute(
        "CREATE TABLE product_intel_reviews ("
        " ticker TEXT, platform TEXT, rating REAL)"
    )
    conn.commit()
    return conn


class TestBuildNamesFrame:
    def test_columns_and_live_alpha(self, monkeypatch):
        monkeypatch.setattr(gate_data, "fetch_prices", lambda *a, **k: _prices(["AAA", "BBB"]))
        monkeypatch.setattr(gate_data, "fetch_ff5_factors", lambda *a, **k: _factors())
        monkeypatch.setattr(gate_data, "_cached_results", lambda: {})
        df = gate_data.build_names_frame(["AAA", "BBB"])
        assert list(df.columns) == [
            "ticker",
            "alpha_3y_ann",
            "cash_burn_months_pct",
            "interest_coverage_ratio_pct",
            "mahalanobis",
        ]
        assert df["ticker"].tolist() == ["AAA", "BBB"]
        assert df["alpha_3y_ann"].notna().all()
        prov = df.attrs["provenance"]
        assert set(prov.keys()) == {"AAA", "BBB"}
        for t in ["AAA", "BBB"]:
            assert set(prov[t].keys()) == {
                "alpha_3y_ann",
                "cash_burn_months_pct",
                "interest_coverage_ratio_pct",
                "mahalanobis",
            }
            assert prov[t]["alpha_3y_ann"] == "live_ff5"
            assert prov[t]["cash_burn_months_pct"] == "NaN"
            assert prov[t]["interest_coverage_ratio_pct"] == "NaN"
            assert prov[t]["mahalanobis"] == "NaN"

    def test_live_failure_falls_back_to_cached(self, monkeypatch):
        monkeypatch.setattr(gate_data, "fetch_prices", lambda *a, **k: pd.DataFrame())
        monkeypatch.setattr(gate_data, "fetch_ff5_factors", lambda *a, **k: pd.DataFrame())
        monkeypatch.setattr(
            gate_data,
            "_cached_results",
            lambda: {"AAA": {"alpha_3y_ann": 0.25, "mahalanobis": 2.0}},
        )
        df = gate_data.build_names_frame(["AAA"])
        assert df.loc[0, "alpha_3y_ann"] == pytest.approx(0.25)
        assert df.loc[0, "mahalanobis"] == pytest.approx(2.0)
        prov = df.attrs["provenance"]["AAA"]
        assert prov["alpha_3y_ann"] == "results_runa_cached"
        assert prov["mahalanobis"] == "results_runa_cached"

    def test_all_nan_when_both_fail(self, monkeypatch):
        monkeypatch.setattr(gate_data, "fetch_prices", lambda *a, **k: pd.DataFrame())
        monkeypatch.setattr(gate_data, "fetch_ff5_factors", lambda *a, **k: pd.DataFrame())
        monkeypatch.setattr(gate_data, "_cached_results", lambda: {})
        df = gate_data.build_names_frame(["ZZZ"])
        assert pd.isna(df.loc[0, "alpha_3y_ann"])
        assert pd.isna(df.loc[0, "cash_burn_months_pct"])
        assert pd.isna(df.loc[0, "interest_coverage_ratio_pct"])
        assert pd.isna(df.loc[0, "mahalanobis"])
        prov = df.attrs["provenance"]["ZZZ"]
        assert prov["alpha_3y_ann"] == "NaN"
        assert prov["cash_burn_months_pct"] == "NaN"
        assert prov["interest_coverage_ratio_pct"] == "NaN"
        assert prov["mahalanobis"] == "NaN"


class TestXbrlParse:
    def test_parse_xbrl_metrics(self):
        facts = {
            "facts": {
                "us-gaap": {
                    "OperatingIncomeLoss": {
                        "units": {
                            "USD": [
                                {"end": "2025-03-31", "val": 1000.0},
                                {"end": "2025-06-30", "val": 1200.0},
                            ]
                        }
                    },
                    "InterestExpense": {
                        "units": {"USD": [{"end": "2025-06-30", "val": 100.0}]}
                    },
                    "CashAndCashEquivalentsAtCarryingValue": {
                        "units": {"USD": [{"end": "2025-06-30", "val": 6000.0}]}
                    },
                    "OperatingExpenses": {
                        "units": {"USD": [{"end": "2025-06-30", "val": 3000.0}]}
                    },
                }
            }
        }
        m = gate_data.parse_xbrl_metrics(facts)
        assert m["interest_coverage_ratio_pct"] == pytest.approx(12.0)
        assert m["cash_burn_months_pct"] == pytest.approx(6.0)

    def test_parse_xbrl_missing_tags(self):
        assert gate_data.parse_xbrl_metrics({}) == {}


class TestQualitativeSignals:
    def test_all_default_neutral_when_db_empty(self, monkeypatch):
        conn = _empty_conn()
        monkeypatch.setattr(gate_data, "get_connection", lambda: conn)
        signals, prov = gate_data.qualitative_signals("AAA")
        assert set(signals.keys()) == set(_MOAT_KEYS)
        assert all(v == 0.5 for v in signals.values())
        assert all(v == "default_neutral" for v in prov.values())

    def test_cached_signals_override(self, monkeypatch):
        conn = _empty_conn()
        conn.execute(
            "INSERT INTO glassdoor_comparably_audit"
            " (ticker, date, glassdoor_normalized, comparably_normalized)"
            " VALUES ('AAA', '2025-01-01', 0.8, 0.7)"
        )
        conn.execute(
            "INSERT INTO glassdoor_comparably_audit"
            " (ticker, date, glassdoor_normalized, comparably_normalized)"
            " VALUES ('AAA', '2024-01-01', 0.5, 0.5)"
        )
        conn.execute(
            "INSERT INTO github_org_metrics (ticker, repo_name, stars, forks)"
            " VALUES ('AAA', 'r1', 100, 20)"
        )
        conn.execute(
            "INSERT INTO github_org_metrics (ticker, repo_name, stars, forks)"
            " VALUES ('AAA', 'r2', 50, 10)"
        )
        conn.execute(
            "INSERT INTO product_intel_reviews (ticker, platform, rating)"
            " VALUES ('AAA', 'G2', 4.0)"
        )
        conn.execute(
            "INSERT INTO product_intel_reviews (ticker, platform, rating)"
            " VALUES ('AAA', 'G2', 5.0)"
        )
        conn.commit()
        monkeypatch.setattr(gate_data, "get_connection", lambda: conn)
        signals, prov = gate_data.qualitative_signals("AAA")
        assert signals["employee_sentiment"] == pytest.approx(0.8)
        assert prov["employee_sentiment"] == "cached:glassdoor_comparably_audit"
        expected_dev = min(
            1.0,
            (
                math.log1p(100)
                + math.log1p(20)
                + math.log1p(50)
                + math.log1p(10)
            )
            / 15.0,
        )
        assert signals["developer_momentum"] == pytest.approx(expected_dev)
        assert prov["developer_momentum"] == "cached:github_org_metrics"
        assert signals["product_breadth"] == pytest.approx(0.9)
        assert prov["product_breadth"] == "cached:product_intel_reviews"
        assert signals["revenue_concentration"] == 0.5
        assert signals["network_effect_proxy"] == 0.5
        assert signals["regulatory_barrier"] == 0.5
        assert prov["revenue_concentration"] == "default_neutral"

    def test_db_error_all_default_neutral(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(gate_data, "get_connection", boom)
        signals, prov = gate_data.qualitative_signals("AAA")
        assert all(v == 0.5 for v in signals.values())
        assert all(v == "default_neutral" for v in prov.values())


class TestNormalizeMahalanobis:
    def test_percentile_rank_and_nan_preserved(self):
        df = pd.DataFrame(
            {
                "ticker": ["A", "B", "C", "D", "E"],
                "mahalanobis": [1.0, 2.0, 3.0, 4.0, np.nan],
            }
        )
        out = gate_data.normalize_mahalanobis(df)
        assert out["mahalanobis"].iloc[:4].tolist() == pytest.approx(
            [0.25, 0.5, 0.75, 1.0]
        )
        assert pd.isna(out["mahalanobis"].iloc[4])
        assert df["mahalanobis"].iloc[0] == 1.0
        assert pd.isna(df["mahalanobis"].iloc[4])


class TestCoverageSummary:
    def test_counts_match_provenance(self, monkeypatch):
        df = pd.DataFrame(
            {
                "ticker": ["AAA", "BBB", "CCC", "DDD"],
                "alpha_3y_ann": [0.1, 0.2, np.nan, 0.3],
                "cash_burn_months_pct": [np.nan] * 4,
                "interest_coverage_ratio_pct": [np.nan] * 4,
                "mahalanobis": [np.nan] * 4,
            }
        )
        df.attrs["provenance"] = {
            "AAA": {
                "alpha_3y_ann": "live_ff5",
                "cash_burn_months_pct": "NaN",
                "interest_coverage_ratio_pct": "NaN",
                "mahalanobis": "NaN",
            },
            "BBB": {
                "alpha_3y_ann": "results_runa_cached",
                "cash_burn_months_pct": "NaN",
                "interest_coverage_ratio_pct": "NaN",
                "mahalanobis": "results_runa_cached",
            },
            "CCC": {
                "alpha_3y_ann": "NaN",
                "cash_burn_months_pct": "NaN",
                "interest_coverage_ratio_pct": "NaN",
                "mahalanobis": "NaN",
            },
            "DDD": {
                "alpha_3y_ann": "live_ff5",
                "cash_burn_months_pct": "live_sec",
                "interest_coverage_ratio_pct": "live_sec",
                "mahalanobis": "NaN",
            },
        }
        monkeypatch.setattr(gate_data, "build_names_frame", lambda tickers: df)
        counts = gate_data.coverage_summary(["AAA", "BBB", "CCC", "DDD"])
        assert counts == {
            "live_ff5": 2,
            "results_runa_cached": 1,
            "live_sec": 0,
            "default_neutral": 1,
        }