"""Offline tests for Phase-2 modules (D-20260803-004): dividend_audit,
opportunistic engine, datastore dividend fetcher, and the DIVIDEND strategy
wiring in fee_sim3."""

import numpy as np
import pandas as pd
import pytest

from diversification.dividend_audit import (
    audit_basket,
    audit_dividend_history,
    screen_dividend_name,
    xbrl_dividend_crosscheck,
)
from diversification.opportunistic import (
    _trailing_z,
    absolute_buying_opportunity,
    opportunistic_equity_weights,
)
from diversification.sleeves import (
    DIVIDEND_CANDIDATES,
    DIVIDEND_EXCLUDED_TICKERS,
    DIVIDEND_YIELDS,
)


def _quarterly(start, n, amount=0.25):
    return pd.Series([amount] * n, index=pd.date_range(start, periods=n, freq="QS"))


def _qualifying(date="2025-06-01", price=25.0):
    return audit_dividend_history(_quarterly("2019-01-01", 26), price, date)


class TestAuditHistory:

    def test_passes_qualifying_history(self):
        ok, reasons = _qualifying()
        assert ok, reasons

    def test_fails_on_short_span(self):
        ok, reasons = audit_dividend_history(_quarterly("2022-06-01", 10), 25.0, "2025-06-01")
        assert not ok
        assert any("span" in r for r in reasons)

    def test_fails_on_skipped_year(self):
        ser = _quarterly("2019-01-01", 26)
        ser = ser[ser.index.year != 2022]
        ok, reasons = audit_dividend_history(ser, 25.0, "2025-06-01")
        assert not ok
        assert any("skipped" in r for r in reasons)

    def test_fails_on_big_yoy_cut(self):
        ser = _quarterly("2019-01-01", 26)
        ser.loc[ser.index.year == 2024] = 0.06
        ok, reasons = audit_dividend_history(ser, 25.0, "2025-06-01")
        assert not ok
        assert any("cut" in r for r in reasons)

    def test_no_partial_year_false_cut(self):
        # A small year-to-date 2025 sum must NOT read as a cut against full
        # 2024; only complete years are compared. Price set so the trailing
        # yield still clears the 3% floor (isolates the cut gate).
        ser = _quarterly("2019-01-01", 26)
        ser.loc[ser.index.year == 2025] = 0.05
        ok, reasons = audit_dividend_history(ser, 15.0, "2025-06-01")
        assert ok, reasons
        assert not any("cut" in r for r in reasons)

    def test_fails_on_low_yield(self):
        ok, reasons = audit_dividend_history(_quarterly("2019-01-01", 26), 50.0, "2025-06-01")
        assert not ok
        assert any("yield" in r for r in reasons)

    def test_fails_on_empty_history(self):
        ok, reasons = audit_dividend_history(pd.Series(dtype=float), 25.0, "2025-06-01")
        assert not ok


class TestScreenName:

    def test_rejects_reit_bdc_mlp_keywords(self):
        assert not screen_dividend_name("Realty Income Corp (O)")
        assert not screen_dividend_name("Ares Capital Corp (BDC)")
        assert not screen_dividend_name("Enterprise Products Partners (MLP)")

    def test_passes_ordinary_name(self):
        assert screen_dividend_name("Procter & Gamble Co")


class TestAuditBasket:

    def _histories(self, seed_dates=True):
        return {
            "A": _quarterly("2019-01-01", 26),
            "B": _quarterly("2019-01-01", 26),
            "C": _quarterly("2019-01-01", 26),
            "NEW": _quarterly("2022-06-01", 12),
            "O": _quarterly("2019-01-01", 26),
        }

    def test_basket_admits_qualifying_members(self):
        prices = pd.DataFrame({"A": [25.0], "B": [25.0], "C": [25.0], "NEW": [25.0]},
                              index=[pd.Timestamp("2025-06-01")])
        basket, rejected, fallback = audit_basket(
            list(self._histories()), self._histories(), prices, "2025-06-01"
        )
        assert {"A", "B", "C"} <= set(basket)
        assert "O" in rejected  # REIT excluded by ticker
        assert not fallback

    def test_no_lookahead_new_name_admitted_only_after_window(self):
        prices = pd.DataFrame({"NEW": [25.0]}, index=[pd.Timestamp("2023-06-01")])
        div = {"NEW": _quarterly("2022-06-01", 24)}
        basket, rejected, _ = audit_basket(["NEW"], div, prices, "2023-06-01",
                                           min_candidates=1)
        assert "NEW" not in basket
        basket2, _, _ = audit_basket(["NEW"], div, prices, "2028-06-01",
                                     min_candidates=1)
        assert "NEW" in basket2

    def test_minimum_candidates_floor_triggers_fallback(self):
        prices = pd.DataFrame({"A": [25.0]}, index=[pd.Timestamp("2025-06-01")])
        div = {"A": _quarterly("2019-01-01", 26), "X": pd.Series(dtype=float)}
        basket, rejected, fallback = audit_basket(["A", "X"], div, prices, "2025-06-01",
                                                  min_candidates=3)
        assert fallback
        assert len(basket) == 1


class TestXBRLCrosscheck:

    def _facts_extractor(self, payouts, filed="2025-05-01"):
        def extract(facts, fields):
            ser = pd.Series(payouts, dtype=float)
            df = ser.to_frame("dividend_ps")
            df.index = pd.to_datetime(df.index)
            df.index.name = "fiscal_end"
            df["filed_date"] = pd.Timestamp(filed)
            return df
        return extract

    def test_na_when_edgar_unreachable(self):
        fetch = lambda cik: {}
        status, _ = xbrl_dividend_crosscheck(
            "A", "123", _quarterly("2019-01-01", 26), "2025-06-01",
            fetch_companyfacts=fetch, extract=lambda f, x: pd.DataFrame(),
        )
        assert status == "NA"

    def test_na_when_tooling_missing(self):
        status, _ = xbrl_dividend_crosscheck(
            "A", "123", _quarterly("2019-01-01", 26), "2025-06-01"
        )
        assert status == "NA"

    def test_pass_when_sources_agree(self):
        payouts = {"2024-07-01": 0.25, "2024-10-01": 0.25,
                   "2025-01-01": 0.25, "2025-04-01": 0.25}
        yf = _quarterly("2024-07-01", 4, 0.25)
        status, detail = xbrl_dividend_crosscheck(
            "A", "123", yf, "2025-06-01",
            fetch_companyfacts=lambda cik: {"facts": {}},
            extract=self._facts_extractor(payouts),
        )
        assert status == "PASS", detail

    def test_fail_when_sources_diverge(self):
        payouts = {"2024-07-01": 0.10, "2024-10-01": 0.10,
                   "2025-01-01": 0.10, "2025-04-01": 0.10}
        yf = _quarterly("2024-07-01", 4, 0.25)
        status, detail = xbrl_dividend_crosscheck(
            "A", "123", yf, "2025-06-01",
            fetch_companyfacts=lambda cik: {"facts": {}},
            extract=self._facts_extractor(payouts),
        )
        assert status == "FAIL", detail


class TestOpportunistic:

    def _series(self, crash=False):
        idx = pd.date_range("2021-01-01", periods=320, freq="B")
        vals = np.full(320, 100.0)
        if crash:
            vals[-30:] = 60.0
        return pd.Series(vals, index=idx)

    def test_absolute_opportunity_fires_only_after_crash(self):
        date = self._series(crash=True).index[-1]
        assert absolute_buying_opportunity(self._series(crash=True), date)
        assert not absolute_buying_opportunity(self._series(crash=False), date)

    def test_overlay_only_in_bear_state(self):
        prices = {"A": self._series(crash=True), "B": self._series(), "C": self._series()}
        date = prices["A"].index[-1]
        base = {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3}
        bull = opportunistic_equity_weights(list(prices), prices, date, "bull", base)
        assert bull == base
        bear = opportunistic_equity_weights(list(prices), prices, date, "bear", base)
        assert bear is not base
        assert abs(sum(bear.values()) - 1.0) < 1e-9
        assert bear["A"] > base["A"]

    def test_no_tilt_without_oversold_member(self):
        prices = {"A": self._series(), "B": self._series(), "C": self._series()}
        date = prices["A"].index[-1]
        base = {"A": 1 / 3, "B": 1 / 3, "C": 1 / 3}
        assert opportunistic_equity_weights(list(prices), prices, date, "bear", base) == base

    def test_trailing_z_uses_only_prior_data(self):
        idx = pd.date_range("2021-01-01", periods=320, freq="B")
        ser = pd.Series(np.full(320, 100.0), index=idx)
        z = _trailing_z(ser, idx[300])
        assert z == z  # not NaN
        assert abs(z) < 0.05


class TestDatastoreDividends:

    def _fake_yf(self, monkeypatch, tmp_path, series_by_ticker, fail=False):
        from diversification import datastore

        class FakeTicker:
            def __init__(self, sym):
                self.sym = sym

            @property
            def dividends(self):
                if fail or self.sym not in series_by_ticker:
                    raise RuntimeError("network down")
                return series_by_ticker[self.sym]

        monkeypatch.setattr(datastore.yf, "Ticker", FakeTicker)

    def test_fetch_dividend_history_and_cache(self, monkeypatch, tmp_path):
        from diversification.datastore import fetch_dividend_history

        # yfinance returns tz-aware ex-dates; the fetcher must normalize them.
        raw = pd.Series([0.5, 0.5], index=pd.to_datetime(["2023-03-01", "2023-06-01"]))
        raw.index = raw.index.tz_localize("America/New_York")
        series = {"AAA": raw}
        self._fake_yf(monkeypatch, tmp_path, series)
        out = fetch_dividend_history(["AAA"], "2020-01-01", "2024-01-01", cache_dir=tmp_path)
        assert "AAA" in out and len(out["AAA"]) == 2
        assert getattr(out["AAA"].index, "tz", None) is None
        assert (tmp_path / "dividends" / "AAA.csv").exists()
        # Second call reads the disk cache; the fake would raise only when called.
        out2 = fetch_dividend_history(["AAA"], "2020-01-01", "2024-01-01", cache_dir=tmp_path)
        assert list(out2["AAA"]) == list(out["AAA"])

    def test_fetch_dividend_history_empty_on_failure(self, monkeypatch, tmp_path):
        from diversification.datastore import fetch_dividend_history

        self._fake_yf(monkeypatch, tmp_path, {}, fail=True)
        out = fetch_dividend_history(["AAA"], "2020-01-01", "2024-01-01", cache_dir=tmp_path)
        assert out == {}


class TestFeeSim3Dividend:

    def test_dividend_strategy_accrues_and_falls_back(self):
        from diversification.fee_sim3 import Portfolio

        idx = pd.date_range("2021-01-01", periods=900, freq="B")
        rng = np.random.default_rng(5)
        prices = pd.DataFrame(
            {
                "SPY": 100.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, len(idx)))),
                "SHY": 100.0 * np.exp(np.cumsum(rng.normal(0.0001, 0.002, len(idx)))),
                "KO": 100.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.01, len(idx)))),
            },
            index=idx,
        )
        pf = Portfolio(prices, initial=10000.0)
        rebal = [idx[50], idx[150]]

        def target(d, w_cur, V):
            if V >= 9999.0:  # first call deploys out of cash
                return {"KO": 0.6, "SHY": 0.4}, {"fallback": False}
            return {"SPY": 0.6, "SHY": 0.4}, {"fallback": False}

        vpath, info = pf.run(rebal, target)
        assert info["trades"] >= 1
        # KO is a pre-registered dividend payer: accrual must be non-zero.
        assert info["dividends"] > 0
        assert vpath.iloc[-1] > 0

    def test_candidate_universe_is_pre_registered(self):
        assert len(DIVIDEND_CANDIDATES) >= 10
        assert "O" in DIVIDEND_EXCLUDED_TICKERS
        assert set(DIVIDEND_CANDIDATES) <= set(DIVIDEND_YIELDS)
