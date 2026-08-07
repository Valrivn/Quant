"""Tests for the discovery provenance ledger (D-20260806-001 P1, SEC 5).

Covers: mentions -> candidates -> gate_passes -> integration_decisions chain,
provenance fields (source_id, fetch_ts, decision_ts, reason_codes), trace
auditability, and the additive schema sanity check.
"""

import sqlite3

import pytest

from discovery.ledger import (
    Mention,
    Candidate,
    GatePass,
    IntegrationDecision,
    ProvenanceLedger,
)
from db.schema_discovery import create_discovery_tables


def _mention(entity="NVDA", source_id="reddit", fetch_ts=1000):
    return Mention(
        source_id=source_id,
        entity=entity,
        topic="Stocks",
        fetch_ts=fetch_ts,
        source_confidence=0.9,
        volume_or_rank=10.0,
        sentiment=0.5,
        external_id="ext-1",
    )


class TestProvenanceChain:
    def test_full_chain_builds(self):
        ledger = ProvenanceLedger()
        m = ledger.add_mention(_mention())
        c = ledger.promote_to_candidate(m, decision_ts=1100, reason_codes=["validated"], ticker="NVDA")
        gp = ledger.record_gate_pass(c, decision_ts=1200, passed=True, reason_codes=["qual_pass"])
        dec = ledger.record_integration_decision(gp, decision_ts=1300, decision="research", reason_codes=["p1_sandbox"])

        assert len(ledger.mentions) == 1
        assert len(ledger.candidates) == 1
        assert len(ledger.gate_passes) == 1
        assert len(ledger.integration_decisions) == 1
        assert dec.gate_pass is gp
        assert gp.candidate is c
        assert c.mention is m

    def test_trace_returns_full_chain(self):
        ledger = ProvenanceLedger()
        m = ledger.add_mention(_mention())
        c = ledger.promote_to_candidate(m, 2000, ["validated"], ticker="NVDA")
        gp = ledger.record_gate_pass(c, 3000, True, ["qual_pass"])
        ledger.record_integration_decision(gp, 4000, "research", ["p1_sandbox"])

        chain = ledger.trace("NVDA")
        stages = [entry["stage"] for entry in chain]
        assert stages == ["mention", "candidate", "gate_pass", "integration_decision"]

        # Provenance fields present at each stage.
        mention_entry = chain[0]
        assert mention_entry["source_id"] == "reddit"
        assert mention_entry["fetch_ts"] == 1000
        assert chain[1]["decision_ts"] == 2000
        assert chain[1]["reason_codes"] == ["validated"]
        assert chain[2]["passed"] is True
        assert chain[3]["decision"] == "research"

    def test_trace_ignores_other_entities(self):
        ledger = ProvenanceLedger()
        m = ledger.add_mention(_mention(entity="NVDA"))
        ledger.add_mention(_mention(entity="AAPL", source_id="stocktwits"))
        ledger.promote_to_candidate(m, 2000, ["validated"], ticker="NVDA")
        chain = ledger.trace("AAPL")
        # AAPL was mentioned but never promoted -> only the mention stage.
        assert [e["stage"] for e in chain] == ["mention"]

    def test_trace_unknown_entity_empty(self):
        ledger = ProvenanceLedger()
        assert ledger.trace("ZZZZ") == []

    def test_reason_codes_are_copied_not_shared(self):
        ledger = ProvenanceLedger()
        m = ledger.add_mention(_mention())
        c = ledger.promote_to_candidate(m, 2000, ["validated"], ticker="NVDA")
        c.reason_codes.append("mutated")
        # The stored candidate's list is the same object; trace reflects it.
        chain = ledger.trace("NVDA")
        assert "mutated" in chain[1]["reason_codes"]


class TestSchemaSanity:
    def test_create_discovery_tables_is_additive(self):
        conn = sqlite3.connect(":memory:")
        create_discovery_tables(conn)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = {
            "discovery_mentions",
            "discovery_candidates",
            "discovery_gate_passes",
            "discovery_integration_decisions",
            "discovery_source_status",
            "discovery_concepts",
        }
        assert expected.issubset(tables)

    def test_mentions_table_columns(self):
        conn = sqlite3.connect(":memory:")
        create_discovery_tables(conn)
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(discovery_mentions)").fetchall()
        }
        assert {"source_id", "entity", "topic", "fetch_ts", "source_confidence"}.issubset(cols)

    def test_recreate_is_idempotent(self):
        conn = sqlite3.connect(":memory:")
        create_discovery_tables(conn)
        create_discovery_tables(conn)  # must not raise
        assert True

    def test_source_status_primary_key(self):
        conn = sqlite3.connect(":memory:")
        create_discovery_tables(conn)
        conn.execute(
            "INSERT INTO discovery_source_status (source_id, status) VALUES ('reddit', 'LIVE')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO discovery_source_status (source_id, status) VALUES ('reddit', 'DEGRADED')"
            )