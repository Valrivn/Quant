"""Tests for the B-20260803 P1 CIK resolver (valuation_alpha/universe/cik_resolver.py)."""

import pytest

from valuation_alpha.universe.cik_resolver import (
    fetch_cik_map,
    get_cik_map,
    refresh_cik_map,
    resolve_cik,
    resolve_ciks,
    enrich_universe,
)

FAKE_TICKERS_JSON = {
    "0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORPORATION"},
    "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "2": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
}


class TestFetchCikMap:
    def test_parses_and_pads(self, monkeypatch):
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return FAKE_TICKERS_JSON

        class FakeRequests:
            @staticmethod
            def get(*args, **kwargs):
                return FakeResp()

        monkeypatch.setattr("requests.get", FakeRequests.get)
        mapping = fetch_cik_map()
        assert mapping["NVDA"]["cik"] == "0001045810"
        assert mapping["AAPL"]["cik"] == "0000320193"
        assert mapping["MSFT"]["cik"] == "0000789019"
        assert mapping["NVDA"]["title"] == "NVIDIA CORPORATION"

    def test_empty_on_failure(self, monkeypatch):
        class Boom:
            @staticmethod
            def get(*args, **kwargs):
                raise Exception("net down")

        monkeypatch.setattr("requests.get", Boom.get)
        assert fetch_cik_map() == {}


class TestResolveCik:
    def test_resolve_known(self, monkeypatch):
        monkeypatch.setattr("valuation_alpha.universe.cik_resolver.get_cik_map",
                            lambda *a, **k: {
                                "NVDA": {"cik": "0001045810", "title": "NVIDIA"},
                                "AAPL": {"cik": "0000320193", "title": "Apple"},
                            })
        assert resolve_cik("NVDA") == "0001045810"
        assert resolve_cik("nvda") == "0001045810"

    def test_resolve_unknown(self, monkeypatch):
        monkeypatch.setattr("valuation_alpha.universe.cik_resolver.get_cik_map",
                            lambda *a, **k: {})
        assert resolve_cik("ZZZZ") is None

    def test_resolve_ciks_bulk(self, monkeypatch):
        monkeypatch.setattr("valuation_alpha.universe.cik_resolver.get_cik_map",
                            lambda *a, **k: {
                                "A": {"cik": "0000000001", "title": ""},
                                "B": {"cik": "0000000002", "title": ""},
                            })
        out = resolve_ciks(["A", "B", "C"])
        assert out == {"A": "0000000001", "B": "0000000002", "C": None}


class TestEnrichUniverse:
    def test_fills_missing_cik(self, monkeypatch):
        monkeypatch.setattr("valuation_alpha.universe.cik_resolver.resolve_ciks",
                            lambda ts, *a, **k: {t: "0000000001" for t in ts})
        rows = [{"ticker": "ZZZ", "sec_cik": None}]
        out = enrich_universe(rows)
        assert out[0]["sec_cik"] == "0000000001"

    def test_leaves_existing_untouched(self, monkeypatch):
        called = []
        monkeypatch.setattr("valuation_alpha.universe.cik_resolver.resolve_ciks",
                            lambda ts, *a, **k: called.append(ts) or {})
        rows = [{"ticker": "NVDA", "sec_cik": "0001045810"}]
        out = enrich_universe(rows)
        assert out[0]["sec_cik"] == "0001045810"
        assert called == []
