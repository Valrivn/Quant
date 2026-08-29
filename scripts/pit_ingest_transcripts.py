"""Phase 1 — ingest S&P 500 earnings transcripts into the PIT sandbox.

D-20260823-001 / Stack A. Source: kurry/sp500_earnings_transcripts (HF, MIT,
33,362 transcripts 2005-2025, populated `date` field verified 2026-08-23).

Isolation: dedicated data/pit_sandbox.db — never touches reddit_quant.db or
any frozen core. Rows outside the pre-registered window are counted and
skipped; undated rows go through ingest_pit_rows' quarantine partition
(DA-1 counter-test runs on the real corpus here).

Usage: python scripts/pit_ingest_transcripts.py [--fresh]
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datasets import load_dataset

from db.schema_pit import SANDBOX_TABLES, create_pit_tables, ingest_pit_rows
from scripts.pit_phase0_audit import _DATE_RE, _bars

SANDBOX_DB = Path("data/pit_sandbox.db")
SOURCE_NAME = "kurry/sp500_earnings_transcripts"
BATCH = 400


def fresh_sandbox() -> sqlite3.Connection:
    if SANDBOX_DB.exists():
        SANDBOX_DB.unlink()
    SANDBOX_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SANDBOX_DB))
    conn.execute("PRAGMA journal_mode=WAL;")
    create_pit_tables(conn)
    return conn


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh", action="store_true", help="recreate the sandbox DB")
    args = parser.parse_args()

    window_start = _bars()["window"]["start"]
    min_date = f"{window_start[:4]}-01-01" if False else str(window_start)

    if args.fresh or not SANDBOX_DB.exists():
        conn = fresh_sandbox()
    else:
        conn = sqlite3.connect(str(SANDBOX_DB))
        conn.execute("PRAGMA journal_mode=WAL;")
        existing = conn.execute("SELECT COUNT(*) FROM pit_transcripts").fetchone()[0]
        if existing:
            print(f"sandbox already holds {existing} transcripts; pass --fresh to rebuild")
            return 0

    print(f"loading {SOURCE_NAME} ...")
    t0 = time.time()
    ds = load_dataset(SOURCE_NAME, split="train")
    print(f"loaded {len(ds)} rows in {time.time() - t0:.0f}s")

    stats = {"in_window": 0, "out_of_window": 0, "accepted": 0, "quarantined": 0}
    batch: list[dict] = []
    t0 = time.time()
    for i, rec in enumerate(ds):
        call_date = (rec.get("date") or "").strip()
        if call_date[:10] < min_date:
            stats["out_of_window"] += 1
            continue
        stats["in_window"] += 1
        batch.append({
            "symbol": rec.get("symbol") or "",
            "company_name": rec.get("company_name"),
            "year": rec.get("year"),
            "quarter": rec.get("quarter"),
            "event_ts": call_date,
            "available_as_of": call_date,
            "content": rec.get("content") or "",
            "metadata_json": json.dumps({
                "company_id": rec.get("company_id"),
                "n_segments": len(rec.get("structured_content") or []),
                "source": SOURCE_NAME,
            }),
        })
        if len(batch) >= BATCH:
            r = ingest_pit_rows(conn, "pit_transcripts", batch)
            stats["accepted"] += r["accepted"]
            stats["quarantined"] += r["quarantined"]
            batch = []
            if (i // BATCH) % 20 == 0:
                print(f"  {i + 1}/{len(ds)} rows ({time.time() - t0:.0f}s)")
    if batch:
        r = ingest_pit_rows(conn, "pit_transcripts", batch)
        stats["accepted"] += r["accepted"]
        stats["quarantined"] += r["quarantined"]

    total_source_rows = sum(stats[k] for k in ("in_window", "out_of_window")) + stats["quarantined"]
    dated_seen = stats["in_window"]
    coverage_pct = 100.0 * dated_seen / max(total_source_rows, 1)
    bar = _bars()["phase0"]["timestamp_coverage_min_pct"]

    n_syms = conn.execute(
        "SELECT COUNT(DISTINCT symbol), MIN(event_ts), MAX(event_ts) FROM pit_transcripts"
    ).fetchone()
    quar = conn.execute("SELECT COUNT(*) FROM pit_transcripts_excluded").fetchone()[0]

    print("\n=== PHASE 1 INGEST REPORT ===")
    print(f"source rows scanned : {total_source_rows}")
    print(f"in window (>= {min_date}): {stats['in_window']}")
    print(f"out of window       : {stats['out_of_window']}")
    print(f"accepted            : {stats['accepted']}")
    print(f"quarantined         : {stats['quarantined']} (excluded partition holds {quar})")
    print(f"DA-1 coverage       : {coverage_pct:.2f}% (bar >= {bar}%) -> "
          f"{'PASS' if coverage_pct >= bar else 'FAIL'}")
    print(f"distinct symbols    : {n_syms[0]} | span {n_syms[1]} .. {n_syms[2]}")
    print(f"sandbox db          : {SANDBOX_DB}")
    return 0 if coverage_pct >= bar else 1


if __name__ == "__main__":
    raise SystemExit(main())
