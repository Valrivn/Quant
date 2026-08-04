"""Tests for the B-20260803 P3 SellAlgorithm exit overlay."""

import numpy as np
import pandas as pd
import pytest

from valuation_alpha.exit import (
    SellAlgorithm,
    make_cashflow_gate,
    make_macro_gate,
    make_moat_gate,
    summarize_events,
)


def _prices(values, start="2020-01-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="B"))


class TestSellAlgorithm:
    def _run(self, values, regime, macro=None, cashflow=None, start="2020-01-01",
             watermark="frozen"):
        """Build a full_close series with 260 pre-days of history so the frozen
        entry reference high is stable at the pre-segment max."""
        n = len(values)
        idx = pd.date_range(start, periods=n, freq="B")
        pre = pd.date_range("2018-01-01", periods=260, freq="B")
        pre_vals = [100.0] * 260  # flat pre-history => reference high = 100
        full = pd.Series(pre_vals + values, index=pre.append(idx))
        rdates = pd.date_range(start, periods=n, freq="B")
        regime_series = pd.Series(regime, index=rdates)
        algo = SellAlgorithm(regime_by_date=regime_series,
                             macro_gate=macro, cashflow_gate=cashflow,
                             watermark=watermark)
        return algo, full, idx[0], idx[-1]

    def test_rolling_watermark_ratchets(self):
        # Spec-literal rolling watermark: base = max over trailing 252d ending
        # the prior day. A steady uptrend that makes new highs every day never
        # exceeds the band (base ratchets up), so no exit -> held.
        algo, full, s, e = self._run(
            [100, 110, 120, 130, 140, 150, 160, 170, 180],
            ["trending"] * 9,
            watermark="rolling",
        )
        events, pos = algo.simulate("AAA", full, s, e)
        assert pos == 1.0  # rolling base ratchets; band never breached

    def test_rolling_watermark_fires_on_excursion(self):
        # A single-day +40% excursion above the prior 252d ceiling exceeds the
        # widened band -> exit fires even with the rolling watermark.
        values = [100.0] * 260 + [100, 100, 100, 140, 100, 100, 100]
        idx = pd.date_range("2020-01-01", periods=7, freq="B")
        pre = pd.date_range("2018-01-01", periods=260, freq="B")
        full = pd.Series([100.0] * 260 + values[260:], index=pre.append(idx))
        rdates = pd.date_range("2020-01-01", periods=7, freq="B")
        algo = SellAlgorithm(regime_by_date=pd.Series(["trending"] * 7, index=rdates),
                             watermark="rolling")
        events, pos = algo.simulate("AAA", full, idx[0], idx[-1])
        assert pos < 1.0
        assert any(ev.pct > 0 for ev in events)

    def test_band_exit_trending(self):
        # +80% rise from a 100 reference in a trending regime exceeds the
        # widened band (28%) -> exit fires.
        algo, full, s, e = self._run(
            [100, 110, 120, 130, 140, 150, 160, 170, 180],
            ["trending"] * 9,
        )
        events, pos = algo.simulate("AAA", full, s, e)
        assert pos < 1.0
        assert any(ev.pct > 0 for ev in events)

    def test_choppy_requires_confirmation(self):
        # Choppy regime: band triggers but no macro/cashflow confirmation -> hold.
        algo, full, s, e = self._run(
            [100, 103, 106, 108, 110, 112, 114, 116],
            ["choppy"] * 8,
        )
        events, pos = algo.simulate("AAA", full, s, e)
        assert pos == 1.0  # no confirmation -> never exits

    def test_choppy_with_macro_confirms(self):
        algo, full, s, e = self._run(
            [100, 103, 106, 108, 110, 112, 114, 116, 120, 124],
            ["choppy"] * 10,
            macro=lambda date, ticker: True,
        )
        events, pos = algo.simulate("AAA", full, s, e)
        assert pos < 1.0

    def test_cashflow_accelerating_overrides(self):
        algo, full, s, e = self._run(
            [100, 110, 120, 130, 140, 150, 160, 170, 180],
            ["trending"] * 9,
            cashflow=lambda date, ticker: "accelerating",
        )
        events, pos = algo.simulate("AAA", full, s, e)
        assert pos == 1.0
        assert any(ev.cashflow_override for ev in events)


class TestMacroGate:
    def test_negative_above_200bps(self):
        spread = pd.Series([1.5, 2.5, 1.8],
                           index=pd.date_range("2020-01-01", periods=3, freq="B"))
        gate = make_macro_gate({"BAA10Y": spread})
        assert gate(pd.Timestamp("2020-01-03"), "AAA") is True
        assert gate(pd.Timestamp("2020-01-01"), "AAA") is False


class TestMoatCompromiseOnly:
    def _moat_run(self, values, moat_series, start="2020-01-01"):
        """Hold with moat_compromise_only=True and a PIT moat gate; prices that
        would trigger price-band exits under the default overlay are present."""
        n = len(values)
        idx = pd.date_range(start, periods=n, freq="B")
        pre = pd.date_range("2018-01-01", periods=260, freq="B")
        full = pd.Series([100.0] * 260 + values, index=pre.append(idx))
        rdates = idx
        regime = pd.Series(["trending"] * n, index=rdates)
        moat = pd.Series(moat_series, index=rdates)
        algo = SellAlgorithm(regime_by_date=regime,
                             moat_gate=make_moat_gate({"AAA": moat}),
                             moat_compromise_only=True)
        return algo, full, idx[0], idx[-1]

    def test_price_band_never_sells_without_compromise(self):
        # +80% rise would breach the trending band under the default overlay;
        # under moat-compromise-only with a stable moat it must be HELD.
        algo, full, s, e = self._moat_run(
            [100, 110, 120, 130, 140, 150, 160, 170, 180],
            [0.8] * 9,
        )
        events, pos = algo.simulate("AAA", full, s, e)
        assert pos == 1.0
        assert not any(ev.pct > 0 for ev in events)

    def test_moat_compromise_sells_full(self):
        # Moat drops from 0.8 peak to 0.4 (< 0.30 threshold below peak) -> full exit.
        algo, full, s, e = self._moat_run(
            [100, 110, 120, 130, 140, 150, 160, 170, 180],
            [0.8, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5, 0.4],
        )
        events, pos = algo.simulate("AAA", full, s, e)
        assert pos == 0.0
        assert any(ev.reason == "moat_compromise" and ev.pct == 1.0 for ev in events)

    def test_unknown_moat_never_sells(self):
        algo, full, s, e = self._moat_run(
            [100, 110, 120, 130, 140, 150, 160, 170, 180],
            [None] * 9,
        )
        events, pos = algo.simulate("AAA", full, s, e)
        assert pos == 1.0

    def test_small_drop_below_threshold_holds(self):
        # Drop of only 0.10 (< 0.30) below peak -> hold.
        algo, full, s, e = self._moat_run(
            [100] * 9,
            [0.8, 0.75, 0.75, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7],
        )
        events, pos = algo.simulate("AAA", full, s, e)
        assert pos == 1.0


class TestMoatGate:
    def test_returns_most_recent_on_or_before_date(self):
        moat = pd.Series([0.7, 0.8, 0.9],
                         index=pd.date_range("2020-01-01", periods=3, freq="B"))
        gate = make_moat_gate({"AAA": moat})
        assert gate(pd.Timestamp("2020-01-02"), "AAA") == 0.7
        assert gate(pd.Timestamp("2020-01-03"), "AAA") == 0.8
        assert gate(pd.Timestamp("2020-01-01"), "AAA") is None  # before first
        assert gate(pd.Timestamp("2020-01-05"), "AAA") == 0.9

    def test_no_data_returns_none(self):
        gate = make_moat_gate({})
        assert gate(pd.Timestamp("2020-01-01"), "AAA") is None
        gate2 = make_moat_gate({"BBB": pd.Series(dtype=float)})
        assert gate2(pd.Timestamp("2020-01-01"), "AAA") is None


class TestCashflowGate:
    def test_pit_filters_by_filed_date(self):
        q = pd.DataFrame({
            "fiscal_end": pd.to_datetime(["2020-03-31", "2020-06-30", "2020-09-30", "2020-12-31"]),
            "filed_date": pd.to_datetime(["2020-05-05", "2020-08-05", "2020-11-05", "2021-02-05"]),
            "operating_margin": [0.10, 0.12, 0.15, 0.18],
        })
        gate = make_cashflow_gate({"AAA": q})
        # Before the third filing, only 2 PIT rows -> None (insufficient).
        assert gate(pd.Timestamp("2020-10-01"), "AAA") is None
        # After 4 filings -> accelerating.
        assert gate(pd.Timestamp("2021-03-01"), "AAA") == "accelerating"


class TestSummarize:
    def test_counts(self):
        ev = []
        # Build one exit + one override via a tiny helper-free manual approach.
        from valuation_alpha.exit import ExitEvent
        ev.append(ExitEvent("A", pd.Timestamp("2020-01-01"), 100.0, "band_trending",
                            pct=0.5, regime="trending", macro_confirmed=False,
                            cashflow_override=False))
        ev.append(ExitEvent("A", pd.Timestamp("2020-01-10"), 110.0, "band_trending",
                            pct=1.0, regime="trending", macro_confirmed=False,
                            cashflow_override=False))
        st = summarize_events(ev, name_years=2.0)
        assert st.n_exits == 2
        assert st.n_phased == 1
