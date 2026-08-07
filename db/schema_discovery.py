"""ADDITIVE SQLite schema for the discovery trend feed (D-20260806-001 P1).

Mirrors the pattern in ``db/schema_fintech.py``: a standalone ``create_*_tables``
function that only ever creates new tables (``CREATE TABLE IF NOT EXISTS``) and
never modifies existing tables. The connection layer (WAL, thread-local) is the
repo's ``db/connection.py``.

Tables (sandbox, research-only):
  discovery_mentions              raw mentions per source
  discovery_candidates            mentions promoted to candidates
  discovery_gate_passes           full-pipeline gate results
  discovery_integration_decisions final decisions (research-only)
  discovery_source_status         degraded registry (per-source status)
  discovery_concepts              research backlog (concept-vs-ticker separation)
"""

import sqlite3


def create_discovery_tables(conn: sqlite3.Connection) -> None:
    """Create the discovery sandbox tables (additive only)."""
    cursor = conn.cursor()

    # Raw mentions (SEC 5 provenance: source_id, fetch_ts).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS discovery_mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            entity TEXT NOT NULL,
            topic TEXT NOT NULL,
            fetch_ts INTEGER NOT NULL,
            source_confidence REAL NOT NULL,
            volume_or_rank REAL,
            sentiment REAL,
            external_id TEXT,
            UNIQUE(source_id, external_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_disc_mentions_source_ts ON discovery_mentions(source_id, fetch_ts)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_disc_mentions_entity ON discovery_mentions(entity)")

    # Candidates: a mention promoted after ticker validation.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS discovery_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mention_id INTEGER NOT NULL,
            ticker TEXT,
            decision_ts INTEGER NOT NULL,
            reason_codes TEXT,
            FOREIGN KEY (mention_id) REFERENCES discovery_mentions(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_disc_candidates_mention ON discovery_candidates(mention_id)")

    # Gate passes: full-pipeline result for a candidate.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS discovery_gate_passes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            passed BOOLEAN NOT NULL,
            decision_ts INTEGER NOT NULL,
            reason_codes TEXT,
            FOREIGN KEY (candidate_id) REFERENCES discovery_candidates(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_disc_gate_candidate ON discovery_gate_passes(candidate_id)")

    # Integration decisions: research-only in P1.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS discovery_integration_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            gate_pass_id INTEGER NOT NULL,
            decision TEXT NOT NULL,
            decision_ts INTEGER NOT NULL,
            reason_codes TEXT,
            FOREIGN KEY (gate_pass_id) REFERENCES discovery_gate_passes(id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_disc_decisions_gate ON discovery_integration_decisions(gate_pass_id)")

    # Degraded registry (D-20260804-002 pattern): per-source status.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS discovery_source_status (
            source_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            last_checked_at INTEGER,
            reason TEXT
        )
    """)

    # Research backlog (SEC 3.5): concepts can only prime hypotheses, never
    # allocation.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS discovery_concepts (
            concept_id INTEGER PRIMARY KEY AUTOINCREMENT,
            concept_name TEXT NOT NULL,
            topic TEXT NOT NULL,
            first_seen INTEGER NOT NULL,
            sources TEXT,
            linked_tickers TEXT,
            hypothesis TEXT,
            status TEXT NOT NULL
        )
    """)

    conn.commit()