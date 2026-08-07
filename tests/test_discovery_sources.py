"""Tests for the discovery structured/video sources (D-20260806-001 P1).

Covers: DEGRADED-registry fail-closed rule (>=2 LIVE sources), per-source
zeroed-contribution + ledger entry on failure, structured-source wrappers with
injected fixtures (no live network), and the sandbox-gated video stub.
"""

import pytest

from discovery.deg_registry import (
    DegradedRegistry,
    MIN_LIVE_SOURCES_FOR_AGREEMENT,
    LIVE,
    DEGRADED,
)
from discovery.structured_sources import (
    StructuredSource,
    SecEdgarNewFilersSource,
    RedditSource,
    StockTwitsSource,
    ApeWisdomSource,
    LiveFetchDisabled,
    live_enabled,
)
from discovery.video_sources import VideoSourceStub, VideoSourceLockedError


def _fake_fetcher(rows):
    def _fetch(limit=100):
        return rows
    return _fetch


class TestDegradedRegistry:
    def test_default_min_live_sources_is_two(self):
        assert MIN_LIVE_SOURCES_FOR_AGREEMENT == 2

    def test_agreement_fails_closed_with_fewer_than_two_live(self):
        reg = DegradedRegistry()
        reg.mark_live("reddit")
        assert reg.agreement_ok() is False

    def test_agreement_ok_with_two_live(self):
        reg = DegradedRegistry()
        reg.mark_live("reddit")
        reg.mark_live("stocktwits")
        assert reg.agreement_ok() is True

    def test_degraded_source_cannot_be_second_agreement(self):
        reg = DegradedRegistry()
        reg.mark_live("reddit")
        reg.mark_degraded("stocktwits", "api down")
        # Only 1 live source -> fail closed even though stocktwits is present.
        assert reg.agreement_ok() is False

    def test_mark_degraded_zeroes_contribution_and_logs(self):
        reg = DegradedRegistry()
        reg.mark_degraded("apewisdom", "rate limited", cycle_ts=100)
        assert reg.is_live("apewisdom") is False
        assert reg.status_of("apewisdom").status == DEGRADED
        entries = reg.ledger_entries()
        assert len(entries) == 1
        assert entries[0].source_id == "apewisdom"
        assert entries[0].contribution_zeroed is True
        assert entries[0].reason == "rate limited"

    def test_mark_live_clears_degraded(self):
        reg = DegradedRegistry()
        reg.mark_degraded("reddit", "down")
        reg.mark_live("reddit")
        assert reg.is_live("reddit") is True
        assert reg.status_of("reddit").status == LIVE


class TestStructuredSourceWrapper:
    def test_success_marks_live_and_returns_mentions(self):
        reg = DegradedRegistry()
        src = StructuredSource(
            "reddit",
            _fake_fetcher([{"entity": "NVDA", "topic": "Stocks", "external_id": "1"}]),
            reg,
        )
        result = src.fetch(limit=10, fetch_ts=1000)
        assert result.degraded is False
        assert len(result.mentions) == 1
        assert result.mentions[0].entity == "NVDA"
        assert result.mentions[0].source_id == "reddit"
        assert result.mentions[0].fetch_ts == 1000
        assert reg.is_live("reddit") is True

    def test_failure_degrades_and_zeroes(self):
        reg = DegradedRegistry()

        def boom(limit=100):
            raise RuntimeError("net down")

        src = StructuredSource("stocktwits", boom, reg)
        result = src.fetch(fetch_ts=2000)
        assert result.degraded is True
        assert result.reason == "net down"
        assert result.mentions == []
        assert reg.is_live("stocktwits") is False
        assert reg.status_of("stocktwits").status == DEGRADED
        assert len(reg.ledger_entries()) == 1

    def test_fetch_never_raises(self):
        reg = DegradedRegistry()

        def boom(limit=100):
            raise ValueError("boom")

        src = StructuredSource("apewisdom", boom, reg)
        # Should not raise; returns a degraded FetchResult.
        result = src.fetch()
        assert result.degraded is True


class TestConcreteSources:
    def test_sec_edgar_source_id(self):
        reg = DegradedRegistry()
        src = SecEdgarNewFilersSource(reg, _fake_fetcher([]))
        assert src.source_id == "sec_edgar_new_filers"

    def test_reddit_source_id(self):
        reg = DegradedRegistry()
        assert RedditSource(reg, _fake_fetcher([])).source_id == "reddit"

    def test_stocktwits_source_id(self):
        reg = DegradedRegistry()
        assert StockTwitsSource(reg, _fake_fetcher([])).source_id == "stocktwits"

    def test_apewisdom_source_id(self):
        reg = DegradedRegistry()
        assert ApeWisdomSource(reg, _fake_fetcher([])).source_id == "apewisdom"

    def test_default_fetchers_are_live_gated(self, monkeypatch):
        """Without DISCOVERY_LIVE, default fetchers raise LiveFetchDisabled."""
        monkeypatch.delenv("DISCOVERY_LIVE", raising=False)
        assert live_enabled() is False
        reg = DegradedRegistry()
        src = SecEdgarNewFilersSource(reg)
        result = src.fetch()
        assert result.degraded is True
        assert "live fetch disabled" in (result.reason or "").lower()
        assert reg.status_of("sec_edgar_new_filers").status == DEGRADED

    def test_live_enabled_flag(self, monkeypatch):
        monkeypatch.setenv("DISCOVERY_LIVE", "1")
        assert live_enabled() is True


class TestVideoSourceStub:
    def test_status_is_gated(self):
        stub = VideoSourceStub("instagram")
        st = stub.status()
        assert st.status == "gated"
        assert st.locked is True

    def test_produce_candidates_raises_locked(self):
        stub = VideoSourceStub("tiktok")
        with pytest.raises(VideoSourceLockedError):
            stub.produce_candidates()

    def test_invalid_source_id_rejected(self):
        with pytest.raises(ValueError):
            VideoSourceStub("facebook")

    def test_video_stub_has_no_scraping(self):
        import inspect
        import discovery.video_sources as mod

        src = inspect.getsource(mod)
        assert "requests" not in src
        assert "http" not in src