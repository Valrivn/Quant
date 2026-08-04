"""Tests for the B-20260803 P1 discovery universe loader."""

import pandas as pd
import pytest

from valuation_alpha.universe import discovery


class TestNormalizeSector:
    def test_gics_map(self):
        assert discovery._normalize_sector("Information Technology") == "information_technology"
        assert discovery._normalize_sector("Health Care") == "healthcare"
        assert discovery._normalize_sector("Consumer Discretionary") == "consumer_cyclical"

    def test_unknown_falls_back_to_snake(self):
        assert discovery._normalize_sector("Something New") == "something_new"


class TestFetchConstituents:
    FAKE_HTML = """
    <html><body>
    <table>
      <tr><th>Symbol</th><th>Security</th><th>GICS Sector</th>
          <th>GICS Sub-Industry</th><th>CIK</th></tr>
      <tr><td>AAA</td><td>Alpha Corp</td><td>Materials</td><td>Aluminum</td><td>1234567</td></tr>
      <tr><td>BBB</td><td>Beta Corp</td><td>Health Care</td><td>Pharma</td><td>7654321</td></tr>
    </table>
    </body></html>
    """

    def test_parses_rows(self, monkeypatch):
        class FakeResp:
            def raise_for_status(self):
                pass

            @property
            def text(self):
                return TestFetchConstituents.FAKE_HTML

        class FakeRequests:
            @staticmethod
            def get(*args, **kwargs):
                return FakeResp()

        monkeypatch.setattr("requests.get", FakeRequests.get)
        df = discovery.fetch_constituents("MID")
        assert len(df) == 2
        assert set(df["ticker"]) == {"AAA", "BBB"}
        assert df.loc[0, "cik"] == "0001234567"

    def test_empty_on_failure(self, monkeypatch):
        class Boom:
            @staticmethod
            def get(*args, **kwargs):
                raise Exception("net down")

        monkeypatch.setattr("requests.get", Boom.get)
        assert discovery.fetch_constituents("MID").empty

    def test_unknown_index_empty(self):
        assert discovery.fetch_constituents("NOPE").empty


class TestDiscoveryUniverse:
    def test_baseline_shape(self):
        rows = discovery.discovery_universe_baseline()
        assert len(rows) == 20
        assert all(r["bias"] is False for r in rows)
        assert {r["group"] for r in rows} == {"MID", "SMALL"}
        assert all(r["sector"] for r in rows)
        assert all(len(r["sec_cik"]) == 10 for r in rows)

    def test_loaded_rows_roster_compatible(self, monkeypatch):
        def fake_enrich(rows):
            return [dict(r, sec_cik="0000000001") if not r["sec_cik"] else r for r in rows]

        class FakeResp:
            def raise_for_status(self):
                pass

            @property
            def text(self):
                return TestFetchConstituents.FAKE_HTML

        class FakeRequests:
            @staticmethod
            def get(*args, **kwargs):
                return FakeResp()

        monkeypatch.setattr("requests.get", FakeRequests.get)
        monkeypatch.setattr(
            "valuation_alpha.universe.discovery.enrich_universe", fake_enrich
        )
        rows = discovery.load_discovery_universe("MID", enrich_ciks=True)
        assert len(rows) == 2
        for r in rows:
            assert set(r.keys()) == {"ticker", "group", "bias", "sector", "sec_cik"}
        assert rows[0]["sec_cik"] == "0001234567"
