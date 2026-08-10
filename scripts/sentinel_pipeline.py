#!/usr/bin/env python3
"""Sentinel pipeline CLI (B-20260809-003).

Commands:
  init                 create the sentinel DB (idempotent)
  enqueue --tickers X  enqueue tickers into the funnel queue
  pass                 run one funnel pass over pending queue items
  sec-sync [--years N] bulk SEC quarterly sync for the roster
  ig-harvest [--limit] single-account IG harvest -> enqueue (fail-closed)
  github-sync           GitHub star snapshots for the roster (G3 source)
  status               queue + funnel summary
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Qualitative"))

from discovery.sentinel import governor, queue as q
from discovery.sentinel.config import load_sentinel_config
from discovery.sentinel.orchestrator import (
    run_github_sync, run_ig_harvest, run_pass, run_sec_sync,
)
from valuation_alpha.universe.roster import get_cik, get_universe


def _conn(cfg):
    return q.connect(cfg["sentinel"]["db_path"])


def _reddit_conn(cfg):
    import sqlite3
    path = _scfg(cfg)["lanes"]["altdata"]["reddit_db_path"]
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _scfg(cfg):
    return cfg["sentinel"]


def cmd_init(cfg):
    conn = _conn(cfg)
    status = q.queue_status(conn)
    print(json.dumps({"db": cfg["sentinel"]["db_path"], "queue": status}, indent=2))
    conn.close()


def cmd_enqueue(cfg, tickers):
    conn = _conn(cfg)
    new = 0
    for t in tickers:
        if q.enqueue(conn, t.strip().upper(), "cli", f"cli:{t.strip().upper()}"):
            new += 1
    print(f"enqueued {new} new")
    conn.close()


def cmd_pass(cfg):
    conn = _conn(cfg)
    rconn = _reddit_conn(cfg)
    res = run_pass(conn, rconn, _scfg(cfg), get_cik)
    print(json.dumps(res, indent=2))
    conn.close()
    rconn.close()


def cmd_sec_sync(cfg, years):
    conn = _conn(cfg)
    tickers = [r["ticker"] for r in get_universe()]
    res = run_sec_sync(conn, _scfg(cfg), tickers, get_cik, years_back=years)
    print(json.dumps(res, indent=2))
    conn.close()


def cmd_ig_harvest(cfg, limit):
    conn = _conn(cfg)
    rconn = _reddit_conn(cfg)
    res = run_ig_harvest(conn, rconn, _scfg(cfg), limit=limit)
    print(json.dumps(res, indent=2))
    conn.close()
    rconn.close()


def cmd_github_sync(cfg):
    conn = _conn(cfg)
    tickers = [r["ticker"] for r in get_universe()]
    res = run_github_sync(conn, _scfg(cfg), tickers)
    print(json.dumps(res, indent=2))
    conn.close()


def cmd_status(cfg):
    conn = _conn(cfg)
    print("queue:", json.dumps(q.queue_status(conn), indent=2))
    rows = conn.execute(
        """SELECT ticker, gate, passed, reason FROM sentinel_funnel_results
           ORDER BY evaluated_utc DESC LIMIT 30"""
    ).fetchall()
    print("recent funnel results:")
    for r in rows:
        print(f"  {r['ticker']} {r['gate']} pass={r['passed']} {r['reason'] or ''}")
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="Sentinel discovery pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    pe = sub.add_parser("enqueue")
    pe.add_argument("--tickers", required=True, help="comma-separated tickers")
    sub.add_parser("pass")
    ps = sub.add_parser("sec-sync")
    ps.add_argument("--years", type=int, default=3)
    ph = sub.add_parser("ig-harvest")
    ph.add_argument("--limit", type=int, default=30)
    pg = sub.add_parser("github-sync")
    sub.add_parser("status")

    args = ap.parse_args()
    cfg = load_sentinel_config()

    if args.cmd == "init":
        cmd_init(cfg)
    elif args.cmd == "enqueue":
        cmd_enqueue(cfg, args.tickers.split(","))
    elif args.cmd == "pass":
        cmd_pass(cfg)
    elif args.cmd == "sec-sync":
        cmd_sec_sync(cfg, args.years)
    elif args.cmd == "ig-harvest":
        cmd_ig_harvest(cfg, args.limit)
    elif args.cmd == "github-sync":
        cmd_github_sync(cfg)
    elif args.cmd == "status":
        cmd_status(cfg)


if __name__ == "__main__":
    main()
