"""Sentinel task queue — SQLite, WAL, idempotent, crash-safe.

The funnel runs off ``sentinel_queue``; every stage transition and every
gate verdict is persisted so a killed process resumes where it stopped.
Separate DB file (``data/sentinel.db``) keeps the shared ``reddit_quant.db``
schema untouched.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

# Stage enum for sentinel_queue.
STAGE_PENDING = "pending"
STAGE_PROCESSING = "processing"
STAGE_PASSED = "passed"
STAGE_FAILED = "failed"
STAGE_DROPPED = "dropped"

STAGES = {STAGE_PENDING, STAGE_PROCESSING, STAGE_PASSED, STAGE_FAILED, STAGE_DROPPED}

_DDL = """
CREATE TABLE IF NOT EXISTS sentinel_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    raw_json TEXT,
    created_utc INTEGER NOT NULL,
    updated_utc INTEGER NOT NULL,
    UNIQUE(source, source_key)
);
CREATE INDEX IF NOT EXISTS idx_sentinel_queue_stage ON sentinel_queue(stage, created_utc);

CREATE TABLE IF NOT EXISTS sentinel_fundamentals (
    ticker TEXT NOT NULL,
    fiscal_end TEXT NOT NULL,
    filed_date TEXT NOT NULL,
    form TEXT,
    qtrs INTEGER,
    ocf REAL,
    capex REAL,
    revenue REAL,
    gross_profit REAL,
    gross_margin REAL,
    cash REAL,
    current_assets REAL,
    current_liabilities REAL,
    total_assets REAL,
    total_liabilities REAL,
    equity REAL,
    retained_earnings REAL,
    ebit REAL,
    source TEXT,
    PRIMARY KEY (ticker, fiscal_end)
);
CREATE INDEX IF NOT EXISTS idx_sentinel_fund_ticker_filed ON sentinel_fundamentals(ticker, filed_date);

CREATE TABLE IF NOT EXISTS sentinel_funnel_results (
    ticker TEXT NOT NULL,
    source TEXT NOT NULL,
    gate TEXT NOT NULL,
    passed INTEGER NOT NULL,
    reason TEXT,
    metrics_json TEXT,
    evaluated_utc INTEGER NOT NULL,
    PRIMARY KEY (ticker, source, gate)
);

CREATE TABLE IF NOT EXISTS sentinel_telemetry (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lane TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    completed_at INTEGER,
    processed INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    status TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS sentinel_cooldowns (
    account_key TEXT PRIMARY KEY,
    next_allowed_utc INTEGER NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS sentinel_enrich (
    ticker TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT,
    fetched_at INTEGER NOT NULL,
    text_blob TEXT,
    score REAL,
    PRIMARY KEY (ticker, source)
);

CREATE TABLE IF NOT EXISTS sentinel_github_snapshots (
    ticker TEXT NOT NULL,
    repo_name TEXT NOT NULL,
    stars INTEGER NOT NULL,
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (ticker, repo_name, fetched_at)
);
"""


def _now() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())


def connect(db_path: str = "data/sentinel.db") -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()
    return conn


def enqueue(
    conn: sqlite3.Connection, ticker: str, source: str, source_key: str,
    raw_json: Optional[Dict] = None,
) -> bool:
    """Insert a queue item idempotently (unique by source+key). Returns True if new."""
    now = _now()
    cur = conn.execute(
        """INSERT OR IGNORE INTO sentinel_queue
           (ticker, source, source_key, stage, created_utc, updated_utc, raw_json)
           VALUES (?, ?, ?, 'pending', ?, ?, ?)""",
        (ticker, source, source_key, now, now, json.dumps(raw_json) if raw_json else None),
    )
    conn.commit()
    return cur.rowcount > 0


def claim_batch(conn: sqlite3.Connection, batch_size: int, max_attempts: int,
                stale_after_seconds: int = 900) -> List[sqlite3.Row]:
    """Atomically claim the next batch of pending items for processing.

    Crash-safe: rows stuck in 'processing' for longer than ``stale_after_seconds``
    (e.g. after a killed process) are reclaimed.
    """
    now = _now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            """SELECT id FROM sentinel_queue
               WHERE (stage = 'pending' OR (stage = 'processing' AND updated_utc < ?))
                 AND attempts < ?
               ORDER BY created_utc ASC LIMIT ?""",
            (now - stale_after_seconds, max_attempts, batch_size),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""UPDATE sentinel_queue SET stage = 'processing',
                        attempts = attempts + 1, updated_utc = ? WHERE id IN ({placeholders})""",
                (now, *ids),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if not ids:
        return []
    return conn.execute(
        f"SELECT * FROM sentinel_queue WHERE id IN ({','.join('?' for _ in ids)})",
        ids,
    ).fetchall()


def mark(conn: sqlite3.Connection, item_id: int, stage: str, error: Optional[str] = None) -> None:
    assert stage in STAGES, f"unknown stage {stage}"
    conn.execute(
        "UPDATE sentinel_queue SET stage = ?, last_error = ?, updated_utc = ? WHERE id = ?",
        (stage, error, _now(), item_id),
    )
    conn.commit()


def record_funnel(
    conn: sqlite3.Connection, ticker: str, source: str, gate: str,
    passed: bool, reason: str = "", metrics: Optional[Dict] = None,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO sentinel_funnel_results
           (ticker, source, gate, passed, reason, metrics_json, evaluated_utc)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (ticker, source, gate, int(passed), reason,
         json.dumps(metrics) if metrics else None, _now()),
    )
    conn.commit()


def upsert_fundamental(conn: sqlite3.Connection, row: Dict) -> None:
    cols = [
        "ticker", "fiscal_end", "filed_date", "form", "qtrs", "ocf", "capex",
        "revenue", "gross_profit", "gross_margin", "cash", "current_assets",
        "current_liabilities", "total_assets", "total_liabilities", "equity",
        "retained_earnings", "ebit", "source",
    ]
    placeholders = ",".join("?" for _ in cols)
    update_cols = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("ticker", "fiscal_end"))
    conn.execute(
        f"""INSERT INTO sentinel_fundamentals ({','.join(cols)})
            VALUES ({placeholders})
            ON CONFLICT(ticker, fiscal_end) DO UPDATE SET {update_cols}""",
        [row.get(c) for c in cols],
    )
    conn.commit()


def get_fundamentals(
    conn: sqlite3.Connection, ticker: str, as_of: Optional[str] = None,
) -> List[sqlite3.Row]:
    """Return fundamentals for a ticker, PIT-filtered to ``as_of`` when given."""
    if as_of:
        return conn.execute(
            """SELECT * FROM sentinel_fundamentals
               WHERE ticker = ? AND filed_date <= ?
               ORDER BY filed_date ASC""",
            (ticker, as_of),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM sentinel_fundamentals WHERE ticker = ? ORDER BY filed_date ASC",
        (ticker,),
    ).fetchall()


def start_run(conn: sqlite3.Connection, lane: str) -> int:
    cur = conn.execute(
        "INSERT INTO sentinel_telemetry (lane, started_at, status) VALUES (?, ?, 'running')",
        (lane, _now()),
    )
    conn.commit()
    return cur.lastrowid


def end_run(conn: sqlite3.Connection, run_id: int, processed: int, passed: int,
            failed: int, status: str, error: Optional[str] = None) -> None:
    conn.execute(
        """UPDATE sentinel_telemetry SET completed_at = ?, processed = ?, passed = ?,
           failed = ?, status = ?, error_message = ? WHERE run_id = ?""",
        (_now(), processed, passed, failed, status, error, run_id),
    )
    conn.commit()


def queue_status(conn: sqlite3.Connection) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT stage, COUNT(*) AS n FROM sentinel_queue GROUP BY stage"
    ).fetchall()
    return {r["stage"]: r["n"] for r in rows}


def enrich_store(
    conn: sqlite3.Connection, ticker: str, source: str, url: Optional[str],
    text: Optional[str], score: Optional[float],
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO sentinel_enrich (ticker, source, url, fetched_at, text_blob, score)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (ticker, source, url, _now(), text, score),
    )
    conn.commit()
