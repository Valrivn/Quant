"""Tests for the discovery Instagram source (D-20260807-002).

Covers: the InstagramSource wrapper with injected fixtures (no live network),
the live-gated default fetcher, the fail-closed cookie gate (no browser), and
the offline census row for the instagram source.
"""

import pytest

from discovery.deg_registry import DegradedRegistry, LIVE, DEGRADED
from discovery.structured_sources import (
    InstagramSource,
    LiveFetchDisabled,
    live_enabled,
)
from discovery.census import run_census


def _fake_fetcher(rows):
    def _fetch(limit=100):
        return rows
    return _fetch


class TestInstagramSourceWrapper:
    def test_success_marks_live_and_returns_mentions(self):
        reg = DegradedRegistry()
        src = InstagramSource(
            reg,
            _fake_fetcher([{
                "entity": "TSLA",
                "topic": "Stocks",
                "source_confidence": 0.6,
                "external_id": "https://www.instagram.com/p/ABC123/",
                "volume_or_rank": 100,
            }]),
        )
        result = src.fetch(limit=10, fetch_ts=1000)
        assert result.degraded is False
        assert len(result.mentions) == 1
        assert result.mentions[0].entity == "TSLA"
        assert result.mentions[0].source_id == "instagram"
        assert result.mentions[0].fetch_ts == 1000
        assert reg.is_live("instagram") is True

    def test_failure_degrades_and_zeroes(self):
        reg = DegradedRegistry()

        def boom(limit=100):
            raise RuntimeError("net down")

        src = InstagramSource(reg, boom)
        result = src.fetch(fetch_ts=2000)
        assert result.degraded is True
        assert result.reason == "net down"
        assert result.mentions == []
        assert reg.is_live("instagram") is False
        assert reg.status_of("instagram").status == DEGRADED
        assert len(reg.ledger_entries()) == 1
        assert reg.ledger_entries()[0].source_id == "instagram"

    def test_fetch_never_raises(self):
        reg = DegradedRegistry()

        def boom(limit=100):
            raise ValueError("boom")

        src = InstagramSource(reg, boom)
        result = src.fetch()
        assert result.degraded is True


class TestInstagramDefaultFetcher:
    def test_default_fetcher_is_live_gated(self, monkeypatch):
        """Without DISCOVERY_LIVE, the default fetcher raises LiveFetchDisabled."""
        monkeypatch.delenv("DISCOVERY_LIVE", raising=False)
        assert live_enabled() is False
        reg = DegradedRegistry()
        src = InstagramSource(reg)
        result = src.fetch()
        assert result.degraded is True
        assert "live fetch disabled" in (result.reason or "").lower()
        assert reg.status_of("instagram").status == DEGRADED

    def test_live_without_cookie_degrades_before_browser(self, monkeypatch, tmp_path):
        """With DISCOVERY_LIVE=1 but no cookie file, the source degrades with a
        cookie reason and NO browser is launched (fail-closed gate)."""
        monkeypatch.setenv("DISCOVERY_LIVE", "1")
        assert live_enabled() is True
        reg = DegradedRegistry()
        missing = str(tmp_path / "no_cookies.json")
        monkeypatch.setattr(
            "Qualitative.psychological.scrapers.instagram_primary.InstagramConfig",
            lambda config_dict=None: type(
                "FakeCfg",
                (),
                {"session_file": missing},
            )(),
        )
        src = InstagramSource(reg)
        result = src.fetch()
        assert result.degraded is True
        assert "cookie" in (result.reason or "").lower()
        assert reg.status_of("instagram").status == DEGRADED


class TestInstagramCensus:
    def test_offline_census_includes_instagram_row(self, monkeypatch):
        """Offline census (no DISCOVERY_LIVE) includes an instagram row that is
        DEGRADED with a live-fetch-disabled reason; no network is touched."""
        monkeypatch.delenv("DISCOVERY_LIVE", raising=False)
        census = run_census(limit=5)
        rows = {r.source_id: r for r in census["rows"]}
        assert "instagram" in rows
        row = rows["instagram"]
        assert row.status == DEGRADED
        assert "live fetch disabled" in (row.reason or "").lower()