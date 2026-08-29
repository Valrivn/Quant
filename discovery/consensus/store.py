"""Persistence for consensus-gate rows (D-20260816-001, P1, research-only).

Writes per-company rows + review evidence + flags into the additive consensus
tables. Uses the repo's ``db/connection.py`` (WAL, thread-local). All writes
carry a ``run_ts`` so every run is auditable and idempotent per (run_ts, ticker).
"""

import json
import logging
import sqlite3
import time
from typing import List, Optional

logger = logging.getLogger(__name__)


def persist_consensus_run(
    rows: List,
    review_map: Optional[dict] = None,
    conn: Optional[sqlite3.Connection] = None,
    run_ts: Optional[int] = None,
) -> int:
    """Persist one consensus run. Returns the run_ts used.

    ``rows``: sorted CompanyVerdict-like objects (or any object with the same
    attribute surface). ``review_map``: {ticker: [(source, n, star, skew, iqr,
    usability, flags)]}.
    """
    from db.connection import connection_context

    run_ts = run_ts or int(time.time())
    review_map = review_map or {}

    ctx = connection_context() if conn is None else _null_ctx(conn)
    with ctx as c:
        for row in rows:
            c.execute(
                """
                INSERT OR REPLACE INTO consensus_company_rows (
                    run_ts, ticker, sector, composite_score, block_scores,
                    factor_scores, flags, converged, normalized,
                    usable_sources, total_reviews
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_ts,
                    row.ticker,
                    getattr(row, "sector", ""),
                    row.composite_score,
                    json.dumps(getattr(row, "block_scores", {})),
                    json.dumps(getattr(row, "factor_scores", {})),
                    json.dumps(getattr(row, "flags", [])),
                    bool(getattr(row, "converged", True)),
                    bool(getattr(row, "normalized", False)),
                    int(getattr(row, "usable_sources", 0)),
                    int(getattr(row, "total_reviews", 0)),
                ),
            )
            for flag in getattr(row, "flags", []):
                c.execute(
                    """
                    INSERT OR REPLACE INTO consensus_flags (
                        run_ts, ticker, flag, source, reason
                    ) VALUES (?,?,?,?,?)
                    """,
                    (run_ts, row.ticker, flag, None, ""),
                )
        for ticker, revs in review_map.items():
            for (source, n, star, skew, iqr, usability, flags) in revs:
                c.execute(
                    """
                    INSERT OR REPLACE INTO consensus_review_evidence (
                        run_ts, ticker, source, n, star_level, skewness, iqr,
                        usability, flags
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (run_ts, ticker, source, n, star, skew, iqr, usability,
                     json.dumps(flags or [])),
                )
    return run_ts


class _null_ctx:
    """Minimal no-op context manager when a caller-provided conn is used."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        if exc[0] is not None:
            self.conn.rollback()
            raise
        self.conn.commit()
        return False