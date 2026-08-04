"""Tests for the D-20260802-002 reinvestment-rate discovery signal."""

import numpy as np
import pandas as pd
import pytest

from valuation_alpha.reinvestment import (
    compute_reinvestment_metrics,
    reinvestment_screen,
    cohort_returns,
    cohort_summary,
    format_cohort_report,
    REINVEST_PLOWBACK_FLOOR,
)
from valuation_alpha.ratios import compute_lifecycle_metrics
from valuation_alpha.datastore.xbrl_financials import extract_quarterly_financials


def _quarterly(n=8, capex=30.0, ocf=100.0, rd=20.0, revenue=200.0,
               net_income=5.0, roic=0.15, assets=None):
    idx = pd.date_range("2022-01-01", periods=n, freq="QE")
    return pd.DataFrame(
        {
            "revenue": [revenue] * n,
            "capex": [-capex] * n,
            "rd": [rd] * n,
            "ocf": [ocf] * n,
            "net_income": [net_income] * n,
            "roic": [roic] * n,
            "assets": ([assets] * n if assets is not None else [200.0 * (1.05) ** i for i in range(n)]),
        },
        index=idx,
    )


class TestComputeReinvestmentMetrics:
    def test_high_reinvestment_profit_agnostic(self):
        # capex 30 + rd 20 = 50 plowback on OCF 100 => rate 0.5
        q = _quarterly()
        m = compute_reinvestment_metrics(q, "AAA")
        assert m.reinvestment_rate == pytest.approx(0.5)
        assert m.pass_signal is True
        assert m.reinvestment_intensity == pytest.approx(50.0 / 200.0)
        assert m.profitable is True

    def test_unprofitable_still_passes(self):
        # Negative net income must NOT gate the reinvestment signal.
        q = _quarterly(net_income=-10.0)
        m = compute_reinvestment_metrics(q, "BBB")
        assert m.profitable is False
        assert m.pass_signal is True

    def test_cash_burn_negative_ocf_surfaces(self):
        # Negative OCF (burning cash while reinvesting) yields a negative rate
        # = the gamble profile, never a crash or a false pass.
        q = _quarterly(ocf=-50.0, capex=40.0, rd=10.0)
        m = compute_reinvestment_metrics(q, "CCC")
        assert m.reinvestment_rate == pytest.approx(50.0 / -50.0)
        assert m.pass_signal is False

    def test_low_plowback_fails(self):
        q = _quarterly(capex=5.0, rd=0.0, ocf=100.0)
        m = compute_reinvestment_metrics(q, "DDD")
        assert m.reinvestment_rate == pytest.approx(0.05)
        assert m.pass_signal is False

    def test_missing_fundamentals(self):
        m = compute_reinvestment_metrics(pd.DataFrame(), "EEE")
        assert m.pass_signal is False
        assert m.reason == "no_fundamentals"


class TestReinvestmentScreen:
    def test_screen_ranks_and_tilts(self):
        qb = {
            "AAA": _quarterly(),                      # 0.5 rate, profitable
            "BBB": _quarterly(net_income=-10.0),      # 0.5 rate, unprofitable
            "DDD": _quarterly(capex=5.0, rd=0.0),     # 0.05 rate -> fails
        }
        screen = reinvestment_screen(qb, moat_scores={"BBB": 0.8})
        passed = screen[screen["pass_signal"]]
        assert set(passed["ticker"]) == {"AAA", "BBB"}
        assert "DDD" not in set(passed["ticker"])
        # BBB gets moat tilt on top of its expected growth.
        row_bbb = passed[passed["ticker"] == "BBB"].iloc[0]
        assert row_bbb["moat"] == pytest.approx(0.8)
        assert row_bbb["score"] > row_bbb["expected_growth"]

    def test_screen_missing_data(self):
        assert reinvestment_screen({}).empty


class TestCohortReturns:
    def test_cohort_tags_and_forward_returns(self):
        prices = pd.DataFrame({
            "AAA": np.linspace(10, 30, 800),   # rising
            "BBB": np.linspace(10, 25, 800),   # rising (unprofitable high-reinvest)
            "DDD": np.linspace(10, 9, 800),    # falling (low reinvest)
        }, index=pd.date_range("2021-01-01", periods=800, freq="B"))
        qb = {
            "AAA": _quarterly(net_income=5.0),
            "BBB": _quarterly(net_income=-5.0),
            "DDD": _quarterly(capex=5.0, rd=0.0, net_income=3.0),
        }
        df = cohort_returns(prices, qb, start=pd.Timestamp("2022-01-01"),
                            horizons={"fwd_3y": 500})
        assert set(df["ticker"]) == {"AAA", "BBB", "DDD"}
        tag = dict(zip(df["ticker"], df["cohort"]))
        assert tag["AAA"] == "BOTH"          # profitable AND high reinvestment
        assert tag["BBB"] == "HIGH_REINVEST" # unprofitable, high reinvestment
        assert tag["DDD"] == "PROFITABLE"    # profitable, low reinvestment
        assert df.loc[df["ticker"] == "DDD", "fwd_3y"].iloc[0] < 0

    def test_summary_and_report(self):
        prices = pd.DataFrame({
            "AAA": np.linspace(10, 30, 800),
            "BBB": np.linspace(10, 25, 800),
        }, index=pd.date_range("2021-01-01", periods=800, freq="B"))
        qb = {
            "AAA": _quarterly(net_income=5.0),
            "BBB": _quarterly(net_income=-5.0),
        }
        df = cohort_returns(prices, qb, start=pd.Timestamp("2022-01-01"),
                            horizons={"fwd_3y": 500})
        s = cohort_summary(df, "fwd_3y")
        assert {"HIGH_REINVEST", "BOTH"} <= set(s.index)
        rep = format_cohort_report(s, "fwd_3y")
        assert "HIGH_REINVEST" in rep


class TestLifecycleIntegration:
    def test_reinvestment_rate_flows_to_lifecycle_metrics(self):
        q = _quarterly(n=12)
        m = compute_lifecycle_metrics(q)
        assert m.reinvestment_rate == pytest.approx(0.5)

    def test_legacy_reinvestment_column_still_works(self):
        idx = pd.date_range("2022-01-01", periods=6, freq="QE")
        q = pd.DataFrame({"revenue": [100.0] * 6, "reinvestment": [0.25] * 6,
                          "roic": [0.15] * 6, "interest_coverage": [8.0] * 6,
                          "operating_margin": [0.2] * 6, "debt_to_capital": [0.3] * 6,
                          "cash_burn": [12.0] * 6}, index=idx)
        m = compute_lifecycle_metrics(q)
        assert m.reinvestment_rate == pytest.approx(0.25)


class TestXBRLFiledDate:
    def test_filed_date_surfaced(self):
        facts = {
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {
                            "USD": [
                                {"end": "2022-03-31", "val": 100.0, "form": "10-Q", "filed": "2022-05-02"},
                                {"end": "2022-06-30", "val": 110.0, "form": "10-Q", "filed": "2022-08-01"},
                            ]
                        }
                    },
                    "PaymentsToAcquirePropertyPlantAndEquipment": {
                        "units": {
                            "USD": [
                                {"end": "2022-03-31", "val": -30.0, "form": "10-Q", "filed": "2022-05-02"},
                                {"end": "2022-06-30", "val": -35.0, "form": "10-Q", "filed": "2022-08-01"},
                            ]
                        }
                    },
                }
            }
        }
        fields = {
            "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
            "capex": "PaymentsToAcquirePropertyPlantAndEquipment",
        }
        df = extract_quarterly_financials(facts, fields)
        assert "filed_date" in df.columns
        assert df.loc["2022-03-31", "filed_date"] == pd.Timestamp("2022-05-02")
        assert df.loc["2022-06-30", "capex"] == pytest.approx(-35.0)
