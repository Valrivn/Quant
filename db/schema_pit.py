"""PIT (point-in-time) sandbox schema — D-20260823-001, Stack A.

Every sandbox table carries `available_as_of`. Rows whose source timestamp
cannot be verified are quarantined into a sibling `<table>_excluded` partition
at ingest time by `ingest_pit_rows` — never silently dropped.

The clock-stepped reader in db/pit_reader.py is the only sanctioned read path.
"""
import json
import sqlite3


SANDBOX_TABLES = {
    "pit_transcripts": """
        CREATE TABLE IF NOT EXISTS pit_transcripts (
            row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            company_name TEXT,
            year INTEGER,
            quarter INTEGER,
            event_ts TEXT NOT NULL,
            available_as_of TEXT NOT NULL,
            content TEXT,
            metadata_json TEXT DEFAULT '{}'
        )
    """,
    "pit_scores": """
        CREATE TABLE IF NOT EXISTS pit_scores (
            score_id INTEGER PRIMARY KEY AUTOINCREMENT,
            row_id INTEGER NOT NULL REFERENCES pit_transcripts(row_id),
            instrument TEXT NOT NULL,
            score REAL NOT NULL,
            label TEXT,
            computed_as_of TEXT NOT NULL,
            UNIQUE(row_id, instrument)
        )
    """,
    "pit_market_labels": """
        CREATE TABLE IF NOT EXISTS pit_market_labels (
            label_id INTEGER PRIMARY KEY AUTOINCREMENT,
            row_id INTEGER NOT NULL REFERENCES pit_transcripts(row_id),
            horizon_days INTEGER NOT NULL,
            forward_return REAL,
            available_as_of TEXT NOT NULL,
            UNIQUE(row_id, horizon_days)
        )
    """,
}

import re as _re

QUARANTINE_TABLES = {
    name: _re.sub(r"\bNOT NULL\b", "", sql.replace(name, name + "_excluded"))
    for name, sql in SANDBOX_TABLES.items()
}

_PK_COLUMN = {
    "pit_transcripts": "row_id",
    "pit_scores": "score_id",
    "pit_market_labels": "label_id",
}


def create_pit_tables(conn: sqlite3.Connection) -> None:
    for name, sql in SANDBOX_TABLES.items():
        conn.execute(sql)
        conn.execute(QUARANTINE_TABLES[name].replace("INTEGER PRIMARY KEY", "INTEGER PRIMARY KEY"))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pit_audit_log (
               audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
               action TEXT NOT NULL,
               as_of TEXT,
               detail_json TEXT NOT NULL,
               ts_default TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )
    conn.commit()


_PIT_COLUMNS = {
    "pit_transcripts": ("event_ts",),
    "pit_market_labels": ("available_as_of",),
}


def ingest_pit_rows(conn: sqlite3.Connection, table: str, rows: list[dict]) -> dict:
    if table not in _PIT_COLUMNS:
        raise ValueError(f"unknown PIT table {table}")
    ts_col = _PIT_COLUMNS[table][0]
    accepted, quarantined = 0, 0
    cols = [d[1] for d in conn.execute(f"PRAGMA table_info({table})")]
    for row in rows:
        ts = row.get(ts_col)
        if not ts or not str(ts).strip():
            qcols = [d[1] for d in conn.execute(f"PRAGMA table_info({table}_excluded)")]
            payload = {c: row.get(c) for c in qcols if c != _PK_COLUMN[table]}
            names = [c for c in payload if payload[c] is not None]
            if names:
                conn.execute(
                    f"INSERT INTO {table}_excluded ({', '.join(names)}) "
                    f"VALUES ({', '.join('?' for _ in names)})",
                    tuple(payload[c] for c in names),
                )
            quarantined += 1
            continue
        names = [c for c in cols if c in row and c != _PK_COLUMN[table]]
        placeholders = ", ".join("?" for _ in names)
        conn.execute(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})",
            tuple(row[c] for c in names),
        )
        accepted += 1
    conn.execute(
        "INSERT INTO pit_audit_log (action, detail_json) VALUES (?, ?)",
        ("ingest", json.dumps({"table": table, "accepted": accepted, "quarantined": quarantined})),
    )
    conn.commit()
    return {"accepted": accepted, "quarantined": quarantined}
