"""ADDITIVE SQLite schema for the anti-bias alt-data consensus gate (D-20260816-001 P1).

Mirrors ``db/schema_discovery.py``: a standalone ``create_*_tables`` function
that only ever creates new tables (``CREATE TABLE IF NOT EXISTS``) and never
modifies existing tables. The connection layer (WAL, thread-local) is the
repo's ``db/connection.py``.

Tables (research-only):
  consensus_company_rows   per-company sorted rows (composite, blocks, flags)
  consensus_review_evidence  per-source review evidence (n, star, flags)
  consensus_flags          per-company flag ledger (attack/polarization/set-aside)
"""

import sqlite3


def create_consensus_tables(conn: sqlite3.Connection) -> None:
    """Create the consensus gate sandbox tables (additive only)."""
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS consensus_company_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ts INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            sector TEXT NOT NULL,
            composite_score REAL NOT NULL,
            block_scores TEXT NOT NULL,
            factor_scores TEXT NOT NULL,
            flags TEXT NOT NULL,
            converged BOOLEAN NOT NULL,
            normalized BOOLEAN NOT NULL,
            usable_sources INTEGER NOT NULL,
            total_reviews INTEGER NOT NULL,
            UNIQUE(run_ts, ticker)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_consensus_rows_ts ON consensus_company_rows(run_ts)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_consensus_rows_ticker ON consensus_company_rows(ticker)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS consensus_review_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ts INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            source TEXT NOT NULL,
            n INTEGER NOT NULL,
            star_level REAL,
            skewness REAL,
            iqr REAL,
            usability TEXT NOT NULL,
            flags TEXT,
            UNIQUE(run_ts, ticker, source)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_consensus_reviews_ts ON consensus_review_evidence(run_ts)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_consensus_reviews_ticker ON consensus_review_evidence(ticker)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS consensus_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ts INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            flag TEXT NOT NULL,
            source TEXT,
            reason TEXT,
            UNIQUE(run_ts, ticker, flag, source)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_consensus_flags_ts ON consensus_flags(run_ts)")

    conn.commit()