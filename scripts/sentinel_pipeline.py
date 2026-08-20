#!/usr/bin/env python3
"""Sentinel pipeline CLI (B-20260809-003).

Commands:
  init                 create the sentinel DB (idempotent)
  enqueue --tickers X  enqueue tickers into the funnel queue
  pass                 run one funnel pass over pending queue items
  sec-sync [--years N] bulk SEC quarterly sync for the roster
  ig-harvest [--limit] single-account IG harvest -> enqueue (fail-closed)
  github-sync           GitHub star snapshots for the roster (G3 source)
  frontier-expand       overlap-graded frontier expansion (B-20260819-001)
  run-all [--with-ig]  run the entire pipeline end-to-end (highly cached)
  status               queue + funnel summary
"""

# Monkeypatch nodriver to use Brave browser path on Windows
try:
    import nodriver.core.config
    nodriver.core.config.find_chrome_executable = lambda *args, **kwargs: r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
except ImportError:
    pass

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Qualitative"))

from discovery.sentinel import governor, queue as q
from discovery.sentinel.config import load_sentinel_config
from discovery.sentinel.orchestrator import (
    run_frontier_expansion, run_github_sync, run_ig_harvest, run_pass, run_sec_sync,
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


def cmd_frontier_expand(cfg):
    conn = _conn(cfg)
    res = run_frontier_expansion(conn, _scfg(cfg), get_cik)
    print(json.dumps(res, indent=2))
    conn.close()


def cmd_run_all(cfg, with_ig=False):
    """Run the entire pipeline end-to-end continuously: enqueuing, syncing SEC,
    syncing GitHub, running the overlap-graded frontier expansion, and entering
    a continuous Funnel Pass loop. Instagram is DEMOTED: it only runs when
    --with-ig is set AND we are outside market hours (hygiene fallback, never
    a discovery driver).
    """
    conn = _conn(cfg)
    tickers = [r["ticker"] for r in get_universe()]

    print("Step 1/4: Enqueuing missing roster tickers...")
    enqueued = 0
    for t in tickers:
        if q.enqueue(conn, t.upper(), "cli", f"cli:{t.upper()}"):
            enqueued += 1
    print(f"  Enqueued {enqueued} new tickers.")

    print("\nStep 2/4: Running SEC CIK sync (7-day caching)...")
    res_sec = run_sec_sync(conn, _scfg(cfg), tickers, get_cik, years_back=10)
    print(f"  SEC sync complete: {json.dumps(res_sec)}")

    print("\nStep 3/4: Running GitHub snapshots (daily caching)...")
    res_gh = run_github_sync(conn, _scfg(cfg), tickers)
    print(f"  GitHub sync complete: {json.dumps(res_gh)}")

    print("\nStep 3.5/4: Running overlap-graded frontier expansion...")
    try:
        res_fr = run_frontier_expansion(conn, _scfg(cfg), get_cik)
        print(f"  Frontier expansion complete: {json.dumps(res_fr)}")
    except Exception as exc:
        print(f"  Frontier expansion skipped or failed: {exc}")

    print("\nStep 4/4: Starting continuous Funnel Pass Loop...")
    print("[*] Press Ctrl+C at any time to stop the pipeline loop.")

    loop_count = 0
    try:
        while True:
            loop_count += 1
            print(f"\n{'='*20} Loop iteration #{loop_count} {'='*20}")

            print("Resetting queue failed/processing items to pending...")
            conn.execute(
                "UPDATE sentinel_queue SET stage='pending', attempts=0 WHERE stage IN ('failed', 'processing')"
            )
            conn.commit()

            print("Executing Sentinel Funnel Pass...")
            rconn = _reddit_conn(cfg)
            res_pass = run_pass(conn, rconn, _scfg(cfg), get_cik)
            rconn.close()

            # Instagram is DEMOTED: hygiene fallback only, fired last and only
            # when explicitly enabled AND outside market hours (09:30-16:00 ET).
            ig_utc_hour = datetime.now().hour
            et_hour = (ig_utc_hour - 5) % 24
            if with_ig and not (9 <= et_hour <= 16):
                print("Running Instagram harvest (hygiene fallback, off-hours)...")
                try:
                    rconn = _reddit_conn(cfg)
                    res_ig = run_ig_harvest(conn, rconn, _scfg(cfg), limit=30)
                    print(f"  Instagram harvest complete: {json.dumps(res_ig)}")
                    rconn.close()
                except Exception as exc:
                    print(f"  Instagram harvest skipped or failed: {exc}")
            else:
                ig_note = "disabled (--with-ig not set)" if not with_ig else "skipped (market hours)"
                print(f"Instagram harvest {ig_note}.")

            # Calculate Instagram Reels progress (target 100,000 reels)
            ig_count = 0
            try:
                rconn = _reddit_conn(cfg)
                row = rconn.execute("SELECT COUNT(*) FROM instagram_raw_mentions").fetchone()
                if row:
                    ig_count = row[0]
                rconn.close()
            except Exception:
                pass

            ig_target = 100000
            ig_pct = min(100.0, (ig_count / ig_target) * 100.0)

            # Calculate Funnel Evaluation progress (completed roster tickers)
            queue_status = q.queue_status(conn)
            total_tickers = sum(queue_status.values())
            passed_cnt = queue_status.get("passed", 0)
            failed_cnt = queue_status.get("failed", 0)
            completed_tickers = passed_cnt + failed_cnt
            funnel_pct = (completed_tickers / total_tickers * 100) if total_tickers > 0 else 0

            # Unified Overall Progress (50% IG Reels dataset target, 50% Funnel Evaluations)
            overall_pct = (ig_pct * 0.5) + (funnel_pct * 0.5)

            print("\n" + "-" * 50)
            print("Sentinel Pipeline Execution Summary:")
            print(f"  Processed queue items: {res_pass.get('processed', 0)}")
            print(f"  Passed this run: {res_pass.get('passed', 0)}")
            print(f"  Failed this run: {res_pass.get('failed', 0)}")
            print("-" * 50)
            print("Pipeline Progress Targets:")
            print(f"  1. Instagram Reels Data:  {ig_count:,} / {ig_target:,} reels ({ig_pct:.2f}% complete)")
            print(f"  2. Ticker Funnel Gating: {completed_tickers} / {total_tickers} tickers ({funnel_pct:.2f}% complete)")
            print("-" * 50)
            print(f"  OVERALL GOAL PROGRESS: {overall_pct:.2f}%")
            print("-" * 50)

            passed_rows = conn.execute(
                "SELECT ticker FROM sentinel_queue WHERE stage = 'passed'"
            ).fetchall()
            passed_tickers = [r["ticker"] for r in passed_rows]
            print(f"  Current Passed Cohort: {sorted(passed_tickers)}")
            print("=" * 50)

            # Human-like session break after the 100 reels batch
            sleep_time = random.uniform(30, 120)
            print(f"[*] Sleeping for {int(sleep_time)}s (30s - 2m session break)...")
            sleep_start = time.time()
            while time.time() - sleep_start < sleep_time:
                remaining = int(sleep_time - (time.time() - sleep_start))
                sys.stdout.write(f"\rNext batch/pass in: {remaining}s...   ")
                sys.stdout.flush()
                time.sleep(1)
            sys.stdout.write("\n")

    except KeyboardInterrupt:
        print("\n[!] Continuous pipeline loop interrupted by user. Exiting cleanly.")
    finally:
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
    pf = sub.add_parser("frontier-expand")
    pra = sub.add_parser("run-all")
    pra.add_argument("--with-ig", action="store_true",
                     help="enable Instagram harvest as hygiene fallback (off-hours only)")
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
    elif args.cmd == "frontier-expand":
        cmd_frontier_expand(cfg)
    elif args.cmd == "run-all":
        cmd_run_all(cfg, with_ig=getattr(args, "with_ig", False))
    elif args.cmd == "status":
        cmd_status(cfg)


if __name__ == "__main__":
    main()
