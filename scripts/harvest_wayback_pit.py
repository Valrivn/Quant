"""CLI for harvesting point-in-time ratings from the Wayback Machine.

Examples:
  python scripts/harvest_wayback_pit.py harvest --tickers NVDA,INTC --since 2008-06 --until 2026-08
  python scripts/harvest_wayback_pit.py harvest --tickers MSFT --months 24
  python scripts/harvest_wayback_pit.py harvest --all --dry-run
  python scripts/harvest_wayback_pit.py report
"""

import argparse
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from discovery import wayback_pit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harvest_wayback_pit")
    sub = parser.add_subparsers(dest="command", required=True)

    h = sub.add_parser("harvest", help="Harvest PIT Glassdoor ratings into the DB.")
    h.add_argument("--tickers", type=str, default=None,
                   help="Comma-separated tickers (default: all 18).")
    h.add_argument("--since", type=str, default=None,
                   help="Earliest month (YYYYMM).")
    h.add_argument("--until", type=str, default=None,
                   help="Latest month (YYYYMM).")
    h.add_argument("--months", type=int, default=None,
                   help="Only the last N months per ticker.")
    h.add_argument("--all", action="store_true",
                   help="Harvest every ticker (default when --tickers omitted).")
    h.add_argument("--dry-run", action="store_true",
                   help="Resolve and parse snapshots without writing to disk.")
    h.add_argument("--sleep", type=float, default=1.1,
                   help="Seconds between archive fetches (default 1.1).")
    h.add_argument("--db", type=str, default=None,
                   help="SQLite database path (default QUANT_DB_PATH or reddit_quant.db).")

    r = sub.add_parser("report", help="Show per-ticker coverage and harvested rows.")
    r.add_argument("--db", type=str, default=None,
                   help="SQLite database path.")
    r.add_argument("--online", action="store_true",
                   help="Also query the Wayback CDX for archive coverage.")
    return parser


def _resolve_windows(since, until, months):
    if months is None:
        return since, until
    now = wayback_pit.datetime.now(tz=wayback_pit.timezone.utc)
    ym = f"{now:%Y%m}"
    until = ym
    year, mon = int(ym[:4]), int(ym[4:6])
    for _ in range(months):
        mon -= 1
        if mon == 0:
            mon = 12
            year -= 1
    return f"{year}{mon:02d}", until


def cmd_harvest(args) -> int:
    tickers = None
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    since, until = _resolve_windows(args.since, args.until, args.months)

    harvester = wayback_pit.WaybackPitHarvester(
        db_path=args.db, dry_run=args.dry_run, fetch_sleep=args.sleep,
    )
    summary = harvester.harvest(tickers=tickers, since=since, until=until)
    print(json.dumps(summary, indent=2, default=str))
    sys.stdout.flush()
    return 0


def cmd_report(args) -> int:
    import sqlite3 as _sqlite3

    from db.schema import create_pit_rating_tables

    db_path = args.db or wayback_pit._default_db_path()
    conn = _sqlite3.connect(db_path)
    conn.row_factory = _sqlite3.Row
    create_pit_rating_tables(conn)
    rows = conn.execute(
        "SELECT ticker, source, COUNT(*) AS n, MIN(valid_date) AS lo, MAX(valid_date) AS hi, "
        "SUM(rating IS NOT NULL) AS parsed "
        "FROM pit_rating_snapshots GROUP BY ticker, source ORDER BY ticker"
    ).fetchall()
    print(f"db: {db_path}")
    print(f"{'ticker':6s} {'source':10s} {'rows':>6s} {'parsed':>6s}  span")
    for r in rows:
        print(f"{r['ticker']:6s} {r['source']:10s} {r['n']:6d} {r['parsed']:6d}  {r['lo']} -> {r['hi']}")
    conn.close()
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "harvest":
        return cmd_harvest(args)
    if args.command == "report":
        return cmd_report(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())