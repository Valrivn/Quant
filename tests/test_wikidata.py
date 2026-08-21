"""Tests for Wikidata client and database tables (ruling D-20260820-001)."""

import sqlite3
import pytest
import urllib.request

# Ensure import has no network calls at import time
def test_import_performs_no_network():
    # Attempting to mock urllib request to ensure nothing is triggered
    called = []
    original_urlopen = urllib.request.urlopen

    def fake_urlopen(*args, **kwargs):
        called.append(args)
        raise RuntimeError("Network hit on import!")

    urllib.request.urlopen = fake_urlopen
    try:
        import discovery.wikidata as wd
        assert wd.WDQS_ENDPOINT == "https://query.wikidata.org/sparql"
    finally:
        urllib.request.urlopen = original_urlopen
    assert not called


def test_fetch_companies(monkeypatch):
    import discovery.wikidata as wd

    # Canned SPARQL JSON response bindings
    mock_bindings = [
        {
            "company": "http://www.wikidata.org/entity/Q2283",
            "ticker": " MSFT ",
            "label": "Microsoft",
        },
        {
            "company": "http://www.wikidata.org/entity/Q11463",
            "ticker": "ADBE",
            "label": "Adobe",
        },
        {
            "company": "http://www.wikidata.org/entity/Q35848",
            "ticker": "AAPL",
            # label missing to test OPTIONAL
        },
    ]

    monkeypatch.setattr(wd, "_sparql_query", lambda q, timeout_s=90: mock_bindings)

    res = wd.fetch_companies()
    # Check deterministic sort by qid (Q11463, Q2283, Q35848)
    assert len(res) == 3
    assert res[0] == {"qid": "Q11463", "label": "Adobe", "ticker": "ADBE"}
    assert res[1] == {"qid": "Q2283", "label": "Microsoft", "ticker": "MSFT"}
    assert res[2] == {"qid": "Q35848", "label": "", "ticker": "AAPL"}


def test_fetch_typed_edges(monkeypatch):
    import discovery.wikidata as wd

    mock_bindings = [
        {
            "source": "http://www.wikidata.org/entity/Q2283",
            "target": "http://www.wikidata.org/entity/Q11463",
            "prop": "subsidiary",
            "from": "2019-01-01T00:00:00Z",
            "to": "2021-12-31T23:59:59Z",
        },
        {
            "source": "http://www.wikidata.org/entity/Q35848",
            "target": "http://www.wikidata.org/entity/Q2283",
            "prop": "owner",
            # from and to are missing
        },
        {
            "source": "http://www.wikidata.org/entity/Q11463",
            "target": "http://www.wikidata.org/entity/Q35848",
            "prop": "parent",
            "from": "2020-05-15",
        },
    ]

    monkeypatch.setattr(wd, "_sparql_query", lambda q, timeout_s=90: mock_bindings)

    res = wd.fetch_typed_edges()
    assert len(res) == 3

    # Check relation mapping, date truncation, missing date fields to empty string
    # Sorting order should be: source_qid, target_qid, relation, valid_from, valid_to
    # Q11463 -> Q35848 (parent, 2020-05-15, "")
    # Q2283 -> Q11463 (subsidiary, 2019-01-01, 2021-12-31)
    # Q35848 -> Q2283 (owner, "", "")
    assert res[0] == {
        "source_qid": "Q11463",
        "target_qid": "Q35848",
        "relation": "parent",
        "valid_from": "2020-05-15",
        "valid_to": "",
    }
    assert res[1] == {
        "source_qid": "Q2283",
        "target_qid": "Q11463",
        "relation": "subsidiary",
        "valid_from": "2019-01-01",
        "valid_to": "2021-12-31",
    }
    assert res[2] == {
        "source_qid": "Q35848",
        "target_qid": "Q2283",
        "relation": "owner",
        "valid_from": "",
        "valid_to": "",
    }


def test_temporal_coverage_probe(monkeypatch):
    import discovery.wikidata as wd

    # Test case 1: normal counts
    monkeypatch.setattr(wd, "_sparql_query", lambda q, timeout_s=120: [{"total": "100", "dated": "40"}])
    probe1 = wd.temporal_coverage_probe()
    assert probe1 == {
        "total_edges": 100,
        "dated_edges": 40,
        "pct_dated": 40.0,
    }

    # Test case 2: zero total
    monkeypatch.setattr(wd, "_sparql_query", lambda q, timeout_s=120: [{"total": "0", "dated": "0"}])
    probe2 = wd.temporal_coverage_probe()
    assert probe2 == {
        "total_edges": 0,
        "dated_edges": 0,
        "pct_dated": 0.0,
    }

    # Test case 3: invalid/malformed response
    monkeypatch.setattr(wd, "_sparql_query", lambda q, timeout_s=120: [])
    probe3 = wd.temporal_coverage_probe()
    assert probe3 == {
        "total_edges": 0,
        "dated_edges": 0,
        "pct_dated": 0.0,
    }


def test_schema_creation_and_roundtrip():
    from db.schema_discovery import create_discovery_tables

    conn = sqlite3.connect(":memory:")
    create_discovery_tables(conn)

    # Verify tables exist
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"wikidata_companies", "wiki_edges", "wiki_runs"}.issubset(tables)

    # Verify column structures and insert/select round-trip
    # 1. wikidata_companies
    conn.execute(
        "INSERT INTO wikidata_companies (qid, label, ticker, fetched_at) VALUES (?, ?, ?, ?)",
        ("Q11463", "Adobe", "ADBE", 1600000000),
    )
    row = conn.execute("SELECT qid, label, ticker, fetched_at FROM wikidata_companies").fetchone()
    assert row == ("Q11463", "Adobe", "ADBE", 1600000000)

    # 2. wiki_edges
    conn.execute(
        "INSERT INTO wiki_edges (source_qid, target_qid, relation, valid_from, valid_to, provenance, discovered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("Q2283", "Q11463", "subsidiary", "2019-01-01", "2021-12-31", "wikidata_import", 1600000000),
    )
    row_edge = conn.execute(
        "SELECT source_qid, target_qid, relation, valid_from, valid_to, provenance, discovered_at FROM wiki_edges"
    ).fetchone()
    assert row_edge == ("Q2283", "Q11463", "subsidiary", "2019-01-01", "2021-12-31", "wikidata_import", 1600000000)

    # 3. wiki_runs
    conn.execute(
        "INSERT INTO wiki_runs (kind, started, finished, stats_json) VALUES (?, ?, ?, ?)",
        ("company_sync", 1600000000, 1600000100, '{"fetched": 100}'),
    )
    row_run = conn.execute("SELECT run_id, kind, started, finished, stats_json FROM wiki_runs").fetchone()
    assert row_run == (1, "company_sync", 1600000000, 1600000100, '{"fetched": 100}')
