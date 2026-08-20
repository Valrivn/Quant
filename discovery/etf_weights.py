"""ETF holdings weight source (B-20260819-001, grading denominator).

The overlap frontier grades suppliers across the MAJOR-company set; QQQ index
weights are supplemental grading context ("we can use ETFs as a way"). Weights
are cached in ``etf_holdings`` and refreshed at most once per rebalance
(quarterly) — never per-run, so the SEC/ETF endpoint is not hammered.

Pattern mirrors ``discovery/structured_sources.py``: an injectable fetcher for
tests, a live-gated default fetcher, and DEGRADED-style handling — a missing
source yields zeroed contribution + a reason, never a fabricated weight.
"""

import csv
import os
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from .structured_sources import live_enabled

DEFAULT_ETF = "QQQ"
_DEFAULT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "etf_holdings", "QQQ.csv"
)
REBALANCE_FRESH_DAYS = 90


class EtfWeightSourceError(RuntimeError):
    """Raised when live ETF holdings cannot be fetched (fail closed)."""


def _default_fetcher(etf: str = DEFAULT_ETF, path: Optional[str] = None) -> List[dict]:
    """Live fetcher reading a local ``ticker,weight`` CSV (gated by live flag).

    Never fabricates: without ``DISCOVERY_LIVE=1`` or a readable CSV it raises,
    and the wrapper records the reason. Weight rows are (ticker, weight).
    """
    if not live_enabled():
        raise EtfWeightSourceError("ETF live fetch disabled (set DISCOVERY_LIVE=1)")
    csv_path = path or _DEFAULT_PATH
    if not os.path.exists(csv_path):
        raise EtfWeightSourceError(f"holdings file not found: {csv_path}")
    rows: List[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise EtfWeightSourceError(f"empty holdings file: {csv_path}")
        for row in reader:
            ticker = (row.get("ticker") or row.get("symbol") or "").strip().upper()
            weight = row.get("weight")
            if not ticker or weight is None:
                continue
            try:
                w = float(weight)
            except (TypeError, ValueError):
                continue
            rows.append({"ticker": ticker, "weight": w})
    if not rows:
        raise EtfWeightSourceError(f"no valid rows in holdings file: {csv_path}")
    return rows


def _as_of_default() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _table_exists(conn) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='etf_holdings'"
    ).fetchone()
    return row is not None


def ensure_table(conn) -> None:
    """Idempotent schema create for the etf_holdings cache table."""
    if _table_exists(conn):
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS etf_holdings (
            etf TEXT NOT NULL,
            ticker TEXT NOT NULL,
            weight REAL NOT NULL,
            as_of TEXT NOT NULL,
            fetched_at INTEGER NOT NULL,
            PRIMARY KEY (etf, ticker, as_of)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_etf_holdings_etf ON etf_holdings(etf, as_of)")
    conn.commit()


def get_weights(
    conn, etf: str = DEFAULT_ETF, as_of: Optional[str] = None
) -> Dict[str, float]:
    """Return {ticker: weight} from the cache for ``etf`` at ``as_of``.

    Falls back to the most recent cached as_of for the ETF when the requested
    date has no rows. Never hits the network.
    """
    if not _table_exists(conn):
        return {}
    if as_of:
        rows = conn.execute(
            "SELECT ticker, weight FROM etf_holdings WHERE etf = ? AND as_of = ?",
            (etf, as_of),
        ).fetchall()
        if rows:
            return {r["ticker"]: r["weight"] for r in rows}
    rows = conn.execute(
        """SELECT ticker, weight FROM etf_holdings
           WHERE etf = ? AND as_of = (SELECT MAX(as_of) FROM etf_holdings WHERE etf = ?)
           ORDER BY ticker""",
        (etf, etf),
    ).fetchall()
    return {r["ticker"]: r["weight"] for r in rows}


def _cache_fresh(conn, etf: str, days: int = REBALANCE_FRESH_DAYS) -> bool:
    if not _table_exists(conn):
        return False
    row = conn.execute(
        "SELECT fetched_at FROM etf_holdings WHERE etf = ? ORDER BY fetched_at DESC LIMIT 1",
        (etf,),
    ).fetchone()
    if not row:
        return False
    return (int(datetime.now(timezone.utc).timestamp()) - int(row["fetched_at"])) < days * 86400


def refresh_etf_weights(
    conn,
    etf: str = DEFAULT_ETF,
    as_of: Optional[str] = None,
    fetcher: Optional[Callable[[], List[dict]]] = None,
    force: bool = False,
) -> Dict[str, object]:
    """Fetch and cache weights for ``etf`` (at most once per rebalance window).

    Returns {etf, as_of, count, cached: bool, error: Optional[str]}. On any
    fetcher failure the cache is left untouched and ``error`` records why
    (fail closed, never a fabricated weight).
    """
    ensure_table(conn)
    as_of = as_of or _as_of_default()
    if not force and _cache_fresh(conn, etf):
        weights = get_weights(conn, etf, as_of)
        return {"etf": etf, "as_of": as_of, "count": len(weights), "cached": True, "error": None}
    try:
        rows = (fetcher or (lambda: _default_fetcher(etf)))()
    except Exception as exc:  # noqa: BLE001 - fail closed
        return {"etf": etf, "as_of": as_of, "count": 0, "cached": False, "error": str(exc)}

    now_ts = int(datetime.now(timezone.utc).timestamp())
    conn.executemany(
        """INSERT OR REPLACE INTO etf_holdings (etf, ticker, weight, as_of, fetched_at)
           VALUES (?, ?, ?, ?, ?)""",
        [(etf, r["ticker"], float(r["weight"]), as_of, now_ts) for r in rows],
    )
    conn.commit()
    return {"etf": etf, "as_of": as_of, "count": len(rows), "cached": False, "error": None}