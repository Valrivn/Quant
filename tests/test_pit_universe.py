"""Tests for the PIT (point-in-time) universe — survivorship-bias elimination.

Decision 2: Static PIT snapshot.  The PIT file maps quarter-end dates to lists
of tickers; ``pit_tickers_for_date`` filters any ticker list to only those
present in the universe as of a given date.

MVP: synthetic PIT file includes all ETF proxies for all dates — no actual
filtering happens yet.  These tests verify the *plumbing*: correct quarter-end
resolution, graceful degradation when data is missing, and filtering behavior
when real SP500 constituent data is eventually loaded.
"""

import json
from pathlib import Path

import pandas as pd
import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from diversification.datastore import (
    _quarter_end_date,
    pit_quarter_ends,
    pit_tickers_for_date,
    _load_pit_data,
    _pit_cache,
)
from diversification.sleeves import pit_tickers_for_date as pit_tickers_reexport


# ---------------------------------------------------------------------------
# Quarter-end resolution
# ---------------------------------------------------------------------------

class TestQuarterEndDate:
    def test_q1_end(self):
        assert _quarter_end_date("2020-01-15") == "2020-03-31"
        assert _quarter_end_date("2020-02-28") == "2020-03-31"
        assert _quarter_end_date("2020-03-31") == "2020-03-31"

    def test_q2_end(self):
        assert _quarter_end_date("2020-04-01") == "2020-06-30"
        assert _quarter_end_date("2020-06-15") == "2020-06-30"

    def test_q3_end(self):
        assert _quarter_end_date("2020-07-01") == "2020-09-30"
        assert _quarter_end_date("2020-09-30") == "2020-09-30"

    def test_q4_end(self):
        assert _quarter_end_date("2020-10-01") == "2020-12-31"
        assert _quarter_end_date("2020-12-31") == "2020-12-31"

    def test_timestamp_input(self):
        ts = pd.Timestamp("2021-05-15")
        assert _quarter_end_date(ts) == "2021-06-30"


# ---------------------------------------------------------------------------
# PIT data loading
# ---------------------------------------------------------------------------

class TestPitDataLoading:
    def test_loads_json(self):
        data = _load_pit_data()
        assert len(data) > 0
        # Should have quarter-ends from 2000 onwards
        assert "2000-03-31" in data
        assert "2025-06-30" in data

    def test_metadata_stripped(self):
        data = _load_pit_data()
        for key in data:
            assert not key.startswith("_"), f"Metadata key '{key}' should be stripped"

    def test_quarter_ends_sorted(self):
        ends = pit_quarter_ends()
        assert ends == sorted(ends)
        assert len(ends) >= 80  # ~25 years of quarterly data


# ---------------------------------------------------------------------------
# PIT ticker filtering
# ---------------------------------------------------------------------------

class TestPitTickerFiltering:
    def test_filters_to_pit_universe(self):
        """With synthetic data, all ETF tickers pass — but the plumbing works."""
        tickers = ["SPY", "VCSH", "VCIT", "BIL", "SHY", "SGOV", "GLD", "IAU"]
        result = pit_tickers_for_date(tickers, "2020-06-15")
        assert result == tickers  # all pass in MVP

    def test_drops_unknown_tickers(self):
        """Ticker not in PIT file is excluded."""
        tickers = ["SPY", "FAKE_TICKER", "GLD"]
        result = pit_tickers_for_date(tickers, "2020-06-15")
        assert "SPY" in result
        assert "GLD" in result
        assert "FAKE_TICKER" not in result

    def test_graceful_before_earliest_snapshot(self):
        """Date before earliest PIT snapshot returns original list (graceful)."""
        tickers = ["SPY", "GLD"]
        result = pit_tickers_for_date(tickers, "1995-01-01")
        assert result == tickers

    def test_uses_previous_quarter_end(self):
        """Mid-quarter date uses the most recent quarter-end snapshot."""
        # 2020-05-15 is in Q2; should use 2020-03-31 snapshot
        tickers = ["SPY", "VCSH"]
        result = pit_tickers_for_date(tickers, "2020-05-15")
        assert result == tickers  # MVP: all pass

    def test_reexport_matches_original(self):
        """The sleeves re-export is the same function."""
        assert pit_tickers_reexport is pit_tickers_for_date


# ---------------------------------------------------------------------------
# Realistic scenario: delisted ticker
# ---------------------------------------------------------------------------

class TestDelistedTicker:
    """Simulate what happens when a ticker is removed from the PIT universe.

    In the MVP, the synthetic file includes all ETF proxies for all dates,
    so this test manually constructs a scenario to verify the filtering logic.
    """

    def test_delisted_ticker_excluded(self):
        """If a ticker is NOT in the PIT snapshot for a date, it is excluded."""
        tickers = ["SPY", "VCSH", "DELISTED_ETF"]
        result = pit_tickers_for_date(tickers, "2020-06-15")
        # DELISTED_ETF is not in the synthetic file, so it should be excluded
        assert "DELISTED_ETF" not in result
        assert "SPY" in result
        assert "VCSH" in result


# ---------------------------------------------------------------------------
# Integration with sleeves import
# ---------------------------------------------------------------------------

class TestSleevesIntegration:
    def test_pit_function_accessible_from_sleeves(self):
        """pit_tickers_for_date can be imported from sleeves.py."""
        from diversification.sleeves import pit_tickers_for_date as fn
        assert callable(fn)
        result = fn(["SPY", "GLD"], "2020-01-01")
        assert "SPY" in result
        assert "GLD" in result
