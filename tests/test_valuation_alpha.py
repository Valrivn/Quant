"""Tests for the valuation_alpha P0 foundation (roster + datastore)."""

import io
import zipfile

import pandas as pd
import pytest

from valuation_alpha.universe.roster import (
    UNIVERSE,
    bias_names,
    get_cik,
    get_group,
    get_universe,
)
from valuation_alpha.datastore import xbrl_financials, prices, factors


class TestRoster:

    def test_total_universe_size(self):
        assert len(UNIVERSE) == 50

    def test_bias_count(self):
        assert len(bias_names()) == 10
        assert sum(1 for r in UNIVERSE if r["bias"]) == 10

    def test_non_bias_universe_size(self):
        assert len(get_universe(include_bias=False)) == 40

    def test_group_sizes(self):
        assert len(get_group("A")) == 10
        assert len(get_group("B")) == 30
        assert len(get_group("C")) == 10

    def test_all_tickers_unique_uppercase(self):
        tickers = [r["ticker"] for r in UNIVERSE]
        assert len(tickers) == len(set(tickers))
        assert all(t == t.upper() for t in tickers)

    def test_megacap_ciks_present(self):
        for t in bias_names():
            assert get_cik(t) is not None

    def test_bias_flag_only_in_group_a(self):
        for r in UNIVERSE:
            assert r["bias"] == (r["group"] == "A")


class TestXbrlExtraction:
    def _fake_companyfacts(self):
        def entry(form, end, val, frame=None):
            e = {"form": form, "end": end, "val": val}
            if frame:
                e["frame"] = frame
            return e

        return {
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                entry("10-Q", "2026-03-31", 1000.0, "CY2026Q1"),
                                entry("10-Q", "2025-12-31", 900.0, "CY2025Q4"),
                                entry("10-K", "2025-09-30", 4000.0),
                            ]
                        }
                    },
                    "OperatingIncomeLoss": {
                        "units": {
                            "USD": [
                                entry("10-Q", "2026-03-31", 200.0, "CY2026Q1"),
                                entry("10-Q", "2025-12-31", 150.0, "CY2025Q4"),
                            ]
                        }
                    },
                }
            }
        }

    def test_extracts_quarterly_rows_and_columns(self):
        df = xbrl_financials.extract_quarterly_financials(
            self._fake_companyfacts(), {"revenue": "Revenues", "roic": "OperatingIncomeLoss"}
        )
        assert list(df.columns) == ["revenue", "roic"]
        assert len(df) == 2
        assert df.loc["2026-03-31", "revenue"] == 1000.0
        assert df.loc["2025-12-31", "roic"] == 150.0
        assert df.index.name == "fiscal_end"

    def test_missing_tag_yields_nan(self):
        df = xbrl_financials.extract_quarterly_financials(
            self._fake_companyfacts(), {"revenue": "Revenues", "missing": "DoesNotExist"}
        )
        assert "missing" not in df.columns or df["missing"].isna().all()

    def test_empty_input_returns_empty(self):
        df = xbrl_financials.extract_quarterly_financials({}, {"revenue": "Revenues"})
        assert df.empty


class TestFetchCompanyfacts:
    def test_returns_empty_on_network_failure(self, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("network down")

        monkeypatch.setattr(xbrl_financials.requests, "get", boom)
        assert xbrl_financials.fetch_companyfacts("0001045810") == {}


class TestFactors:
    def test_ff5_decimals_conversion(self, monkeypatch):
        csv_text = (
            "This is the header line\n"
            "Annual Factors: January-December\n"
            ", Mkt-RF, SMB, HML, RMW, CMA, RF\n"
            "194601,5.00,2.00,1.00,0.50,0.25,0.10\n"
            "194602,3.00,1.00,0.50,0.25,0.10,0.05\n"
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("F-F_Research_Data_5_Factors_2x3.CSV", csv_text)
        buf.seek(0)

        def fake_get(url, timeout=None):
            class R:
                content = buf.getvalue()

                def raise_for_status(self):
                    pass

            return R()

        monkeypatch.setattr(factors.requests, "get", fake_get)
        df = factors.fetch_ff5_factors()
        assert list(df.columns) == ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF"]
        assert df.index.name == "date"
        assert df.iloc[0]["Mkt-RF"] == pytest.approx(0.05)
        assert df.iloc[1]["SMB"] == pytest.approx(0.01)

    def test_ff5_skips_annual_block_with_bare_year_dates(self, monkeypatch):
        csv_text = (
            "This is the header line\n"
            ", Mkt-RF, SMB, HML, RMW, CMA, RF\n"
            "194601,5.00,2.00,1.00,0.50,0.25,0.10\n"
            "194602,3.00,1.00,0.50,0.25,0.10,0.05\n"
            "Annual Factors: January-December\n"
            ", Mkt-RF, SMB, HML, RMW, CMA, RF\n"
            "1946,35.00,12.00,8.00,3.00,2.00,1.00\n"
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("F-F_Research_Data_5_Factors_2x3.CSV", csv_text)
        buf.seek(0)

        def fake_get(url, timeout=None):
            class R:
                content = buf.getvalue()

                def raise_for_status(self):
                    pass

            return R()

        monkeypatch.setattr(factors.requests, "get", fake_get)
        df = factors.fetch_ff5_factors()
        assert len(df) == 2
        assert df.index.strftime("%Y%m").tolist() == ["194601", "194602"]

    def test_ff5_daily_8_digit_dates_parsed(self, monkeypatch):
        csv_text = (
            "This is the header line\n"
            ", Mkt-RF, SMB, HML, RMW, CMA, RF\n"
            "19460701,0.50,0.20,0.10,0.05,0.02,0.01\n"
            "19460702,0.30,0.10,0.05,0.02,0.01,0.005\n"
            "Annual Factors: January-December\n"
            ", Mkt-RF, SMB, HML, RMW, CMA, RF\n"
            "1946,35.00,12.00,8.00,3.00,2.00,1.00\n"
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("F-F_Research_Data_5_Factors_2x3_daily.CSV", csv_text)
        buf.seek(0)

        def fake_get(url, timeout=None):
            class R:
                content = buf.getvalue()

                def raise_for_status(self):
                    pass

            return R()

        monkeypatch.setattr(factors.requests, "get", fake_get)
        df = factors.fetch_ff5_factors()
        assert len(df) == 2
        assert df.index.strftime("%Y%m%d").tolist() == ["19460701", "19460702"]
        assert df.index.freq is None or df.index.freq in ("D", "B")


class TestPrices:
    def test_fetch_prices_uses_canned_frame(self, monkeypatch):
        canned = pd.DataFrame(
            {"AAPL": [1.0, 2.0], "MSFT": [3.0, 4.0]},
            index=pd.to_datetime(["2026-01-01", "2026-01-02"]),
        )

        class FakeYf:
            @staticmethod
            def download(tickers, start=None, end=None, progress=None, auto_adjust=None):
                return canned

        monkeypatch.setattr(prices, "yf", FakeYf())
        df = prices.fetch_prices(["AAPL", "MSFT"], "2026-01-01", "2026-01-03")
        assert list(df.columns) == ["AAPL", "MSFT"]
        assert len(df) == 2
        assert df.loc["2026-01-02", "AAPL"] == 2.0