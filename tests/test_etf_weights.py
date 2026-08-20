"""Tests for the ETF holdings weight source (B-20260819-001).

Covers: caching (quarterly freshness), get_weights fallback, refresh with an
injected fetcher (never hits the network), and fail-closed on a bad fetcher.
"""

import sqlite3

import pytest

from discovery.etf_weights import (
    DEFAULT_ETF,
    REBALANCE_FRESH_DAYS,
    EtfWeightSourceError,
    ensure_table,
    get_weights,
    refresh_etf_weights,
    _default_fetcher,
)


@pytest.fixture
def conn(tmp_path):
    db = sqlite3.connect(str(tmp_path / "etf_test.db"))
    db.row_factory = sqlite3.Row
    yield db
    db.close()


def _fetcher(rows):
    def _f():
        return [{"ticker": t, "weight": w} for t, w in rows]
    return _f


class TestRefresh:
    def test_refresh_populates_cache(self, conn):
        res = refresh_etf_weights(conn, as_of="2026-06-30", fetcher=_fetcher(
            [("NVDA", 9.0), ("MSFT", 8.0)]
        ))
        assert res["cached"] is False
        assert res["count"] == 2
        assert res["error"] is None
        weights = get_weights(conn, as_of="2026-06-30")
        assert weights == {"NVDA": 9.0, "MSFT": 8.0}

    def test_fresh_cache_is_not_refetched(self, conn):
        refresh_etf_weights(conn, as_of="2026-06-30", fetcher=_fetcher([("NVDA", 9.0)]))
        calls = []

        def spy():
            calls.append(1)
            return [{"ticker": "AMD", "weight": 5.0}]

        res = refresh_etf_weights(conn, as_of="2026-06-30", fetcher=spy)
        assert res["cached"] is True
        assert calls == []
        assert "AMD" not in get_weights(conn)

    def test_force_refresh_overrides_cache(self, conn):
        refresh_etf_weights(conn, as_of="2026-06-30", fetcher=_fetcher([("NVDA", 9.0)]))
        res = refresh_etf_weights(
            conn, as_of="2026-09-30", fetcher=_fetcher([("AMD", 5.0)]), force=True
        )
        assert res["cached"] is False
        assert get_weights(conn, as_of="2026-09-30") == {"AMD": 5.0}

    def test_fail_closed_on_fetcher_error(self, conn):
        def boom():
            raise EtfWeightSourceError("holdings file not found")

        res = refresh_etf_weights(conn, fetcher=boom)
        assert res["count"] == 0
        assert res["cached"] is False
        assert "holdings file not found" in res["error"]
        assert get_weights(conn) == {}

    def test_default_fetcher_requires_live_flag(self, monkeypatch):
        monkeypatch.delenv("DISCOVERY_LIVE", raising=False)
        with pytest.raises(EtfWeightSourceError):
            _default_fetcher()

    def test_rebalance_window_constant(self):
        assert REBALANCE_FRESH_DAYS == 90


class TestGetWeights:
    def test_empty_db_returns_empty(self, conn):
        assert get_weights(conn) == {}

    def test_returns_most_recent_as_of_when_exact_missing(self, conn):
        refresh_etf_weights(conn, as_of="2026-03-31", fetcher=_fetcher([("NVDA", 9.0)]))
        refresh_etf_weights(conn, as_of="2026-06-30", fetcher=_fetcher([("NVDA", 9.5)]), force=True)
        # Asking for a date with no rows falls back to the most recent cache.
        assert get_weights(conn, as_of="2026-04-15") == {"NVDA": 9.5}

    def test_ensure_table_idempotent(self, conn):
        ensure_table(conn)
        ensure_table(conn)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='etf_holdings'"
        ).fetchone() is not None