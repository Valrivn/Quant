"""Sentinel rate governor — SQLite token bucket + circuit breakers.

Cross-process safe (WAL + BEGIN IMMEDIATE): parallel lane processes share one
budget, so "run N workers" can never exceed the configured rate. Fail-closed:
an OPEN circuit refuses work until it has cooled down and a probe succeeds.
"""

import sqlite3
import time
from typing import Optional

_GOV_DDL = """
CREATE TABLE IF NOT EXISTS sentinel_rate_limits (
    bucket_key TEXT PRIMARY KEY,
    rate REAL NOT NULL,
    burst REAL NOT NULL,
    tokens REAL NOT NULL,
    last_refill INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sentinel_circuits (
    circuit_key TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'CLOSED',
    failure_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    opened_utc INTEGER,
    next_probe_utc INTEGER,
    failure_threshold INTEGER NOT NULL DEFAULT 5,
    success_threshold INTEGER NOT NULL DEFAULT 2,
    timeout_seconds INTEGER NOT NULL DEFAULT 60
);
"""


def _ensure_gov(conn: sqlite3.Connection) -> None:
    conn.executescript(_GOV_DDL)
    conn.commit()


def throttle(
    conn: sqlite3.Connection,
    bucket_key: str,
    rate: float,
    burst: float,
    cost: float = 1.0,
    max_wait_seconds: float = 60.0,
    poll_seconds: float = 0.25,
) -> bool:
    """Block until a token is available or the wait budget runs out.

    Returns True if the caller may proceed, False if the budget was exhausted
    (the caller should fail closed).
    """
    _ensure_gov(conn)
    deadline = time.time() + max_wait_seconds
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM sentinel_rate_limits WHERE bucket_key = ?", (bucket_key,)
            ).fetchone()
            now = time.time()
            if row is None:
                conn.execute(
                    """INSERT INTO sentinel_rate_limits (bucket_key, rate, burst, tokens, last_refill)
                       VALUES (?, ?, ?, ?, ?)""",
                    (bucket_key, rate, burst, burst, int(now)),
                )
                conn.commit()
                return True
            elapsed = max(0.0, now - row["last_refill"])
            tokens = min(row["burst"], row["tokens"] + elapsed * row["rate"])
            if tokens >= cost:
                conn.execute(
                    """UPDATE sentinel_rate_limits SET tokens = ?, last_refill = ?
                       WHERE bucket_key = ?""",
                    (tokens - cost, int(now), bucket_key),
                )
                conn.commit()
                return True
            conn.execute(
                "UPDATE sentinel_rate_limits SET last_refill = ? WHERE bucket_key = ?",
                (int(now), bucket_key),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if time.time() >= deadline:
            return False
        time.sleep(poll_seconds)


def circuit_state(conn: sqlite3.Connection, key: str, failure_threshold: int = 5,
                  success_threshold: int = 2, timeout_seconds: int = 60) -> str:
    _ensure_gov(conn)
    row = conn.execute(
        "SELECT * FROM sentinel_circuits WHERE circuit_key = ?", (key,)
    ).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO sentinel_circuits (circuit_key, failure_threshold, success_threshold, timeout_seconds)
               VALUES (?, ?, ?, ?)""",
            (key, failure_threshold, success_threshold, timeout_seconds),
        )
        conn.commit()
        return "CLOSED"
    if row["state"] == "OPEN":
        if row["next_probe_utc"] and time.time() >= row["next_probe_utc"]:
            conn.execute(
                """UPDATE sentinel_circuits SET state = 'HALF_OPEN',
                   success_count = 0 WHERE circuit_key = ?""", (key,),
            )
            conn.commit()
            return "HALF_OPEN"
        return "OPEN"
    return row["state"]


def circuit_allow(conn: sqlite3.Connection, key: str, failure_threshold: int = 5,
                  success_threshold: int = 2, timeout_seconds: int = 60) -> bool:
    """Fail-closed gate: allow a request only when the circuit is not OPEN."""
    state = circuit_state(conn, key, failure_threshold, success_threshold, timeout_seconds)
    return state != "OPEN"


def record_success(conn: sqlite3.Connection, key: str, success_threshold: int = 2) -> None:
    _ensure_gov(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM sentinel_circuits WHERE circuit_key = ?", (key,)
        ).fetchone()
        if row is None:
            conn.commit()
            return
        if row["state"] == "HALF_OPEN":
            sc = row["success_count"] + 1
            if sc >= (row["success_threshold"] or success_threshold):
                conn.execute(
                    """UPDATE sentinel_circuits SET state = 'CLOSED', failure_count = 0,
                       success_count = 0, opened_utc = NULL, next_probe_utc = NULL
                       WHERE circuit_key = ?""", (key,),
                )
            else:
                conn.execute(
                    "UPDATE sentinel_circuits SET success_count = ? WHERE circuit_key = ?",
                    (sc, key),
                )
        else:
            conn.execute(
                "UPDATE sentinel_circuits SET failure_count = 0 WHERE circuit_key = ?", (key,),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def record_failure(conn: sqlite3.Connection, key: str, timeout_seconds: int = 60) -> None:
    _ensure_gov(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM sentinel_circuits WHERE circuit_key = ?", (key,)
        ).fetchone()
        if row is None:
            conn.commit()
            return
        now = time.time()
        if row["state"] == "HALF_OPEN":
            conn.execute(
                """UPDATE sentinel_circuits SET state = 'OPEN', failure_count = 0,
                   success_count = 0, opened_utc = ?, next_probe_utc = ? WHERE circuit_key = ?""",
                (int(now), int(now + (row["timeout_seconds"] or timeout_seconds)), key),
            )
        else:
            fc = row["failure_count"] + 1
            if fc >= (row["failure_threshold"] or 5):
                conn.execute(
                    """UPDATE sentinel_circuits SET state = 'OPEN', failure_count = ?,
                       opened_utc = ?, next_probe_utc = ? WHERE circuit_key = ?""",
                    (fc, int(now), int(now + (row["timeout_seconds"] or timeout_seconds)), key),
                )
            else:
                conn.execute(
                    "UPDATE sentinel_circuits SET failure_count = ? WHERE circuit_key = ?",
                    (fc, key),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def cooldown_set(conn: sqlite3.Connection, account_key: str, until_utc: int, reason: str = "") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO sentinel_cooldowns (account_key, next_allowed_utc, reason)
           VALUES (?, ?, ?)""",
        (account_key, until_utc, reason),
    )
    conn.commit()


def cooldown_until(conn: sqlite3.Connection, account_key: str) -> Optional[int]:
    row = conn.execute(
        "SELECT next_allowed_utc FROM sentinel_cooldowns WHERE account_key = ?", (account_key,)
    ).fetchone()
    return row["next_allowed_utc"] if row else None


def cooldown_blocked(conn: sqlite3.Connection, account_key: str, now_utc: Optional[int] = None) -> bool:
    until = cooldown_until(conn, account_key)
    if until is None:
        return False
    return (now_utc or int(time.time())) < until
