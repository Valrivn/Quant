#!/usr/bin/env python3
"""
Human-paced Instagram scraping loop (D-20260809-003 pattern, hardened).

Runs binge blocks through the pacing supervisor in
``instagram_primary.scrape_instagram_long``: each block is one real-session
browser pass, blocks are separated by inter-block gaps, and the run HARD
STOPS after ``--max-hours`` (default: config ``max_active_hours`` ≈ 5-6h) of
ACTIVE scraping. Challenges / login walls / attach failures FAIL HARD: the
loop stops and reports instead of blind-retrying.
"""

import argparse
import sys
import os
import time
import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Qualitative"))

from Qualitative.psychological.scrapers.instagram_primary import (
    fetch_instagram_mentions,
    InstagramConfig,
    scrape_instagram_long,
    InstagramChallengeDetected,
    InstagramSessionUnavailable,
)
from db.connection import get_connection

def setup_db(conn: sqlite3.Connection):
    """Create the raw instagram mentions table if it does not exist."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS instagram_raw_mentions (
            id TEXT PRIMARY KEY,
            ticker TEXT,
            shortcode TEXT,
            caption TEXT,
            sentiment REAL,
            finbert_label TEXT,
            finbert_sentiment REAL,
            finbert_confidence REAL,
            views INTEGER,
            comments INTEGER,
            followers INTEGER,
            verified INTEGER,
            fetch_ts INTEGER,
            external_id TEXT
        )
    """)
    conn.commit()

def save_mentions(conn: sqlite3.Connection, mentions: list) -> int:
    """Save mentions to database, return number of new unique mentions inserted."""
    cursor = conn.cursor()
    new_count = 0
    for m in mentions:
        ext_id = m.get("external_id") or ""
        # Extract shortcode from url
        # e.g., https://www.instagram.com/p/ABC123/ -> ABC123
        shortcode = ""
        parts = [p for p in ext_id.split("/") if p]
        if parts:
            shortcode = parts[-1]
            
        ticker = m.get("entity") or "UNKNOWN"
        record_id = f"{ticker}_{shortcode}" if shortcode else f"{ticker}_{int(time.time())}_{random.randint(1000, 9999)}"
        
        # We use INSERT OR IGNORE to prevent duplicate records
        cursor.execute("""
            INSERT OR IGNORE INTO instagram_raw_mentions (
                id, ticker, shortcode, caption, sentiment,
                finbert_label, finbert_sentiment, finbert_confidence,
                views, comments, followers, verified, fetch_ts, external_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record_id,
            ticker,
            shortcode,
            m.get("caption"),
            m.get("sentiment"),
            m.get("finbert_label"),
            m.get("finbert_sentiment"),
            m.get("finbert_confidence"),
            m.get("views") or m.get("volume_or_rank", 0),
            m.get("comments"),
            m.get("followers"),
            1 if m.get("verified") else 0,
            m.get("fetch_ts"),
            ext_id
        ))
        if cursor.rowcount > 0:
            new_count += 1
            
    conn.commit()
    return new_count

def format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration."""
    if seconds <= 0:
        return "0s"
    td = timedelta(seconds=int(seconds))
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)

def get_total_count(conn: sqlite3.Connection) -> int:
    """Return the total number of records in the database."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(1) FROM instagram_raw_mentions")
    return cursor.fetchone()[0]

def main():
    parser = argparse.ArgumentParser(description="Human-paced Instagram scrape loop (hard stop after N active hours)")
    parser.add_argument("--max-hours", type=float, default=None,
                        help="Hard stop after N hours of ACTIVE scraping (default: config max_active_hours)")
    parser.add_argument("--target", type=int, default=0,
                        help="Optional total-record target; stop early once reached")
    parser.add_argument("--batch-size", type=int, default=100,
                        help="Number of records to scrape per binge block")
    args = parser.parse_args()

    os.environ["DISCOVERY_LIVE"] = "1"

    cfg = InstagramConfig()
    if args.max_hours:
        cfg.max_active_hours = args.max_hours

    conn = get_connection()
    setup_db(conn)

    initial_count = get_total_count(conn)
    print(f"[*] Database initialized. Currently stored instagram mentions: {initial_count}")
    print(f"[*] Hard stop after {cfg.max_active_hours}h of ACTIVE scraping "
          f"(binge blocks, inter-block gaps {int(cfg.inter_block_gap_seconds[0])}-{int(cfg.inter_block_gap_seconds[1])}s).")

    start_time = time.time()
    total_new = 0

    def persist(rows, block_active):
        nonlocal total_new
        new_inserted = save_mentions(conn, rows)
        total_new += new_inserted
        current_total = get_total_count(conn)
        elapsed = time.time() - start_time
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Block: +{new_inserted} new "
              f"(total {current_total}) in {format_duration(block_active)} | "
              f"run +{total_new} new in {format_duration(elapsed)}")

    try:
        summary = scrape_instagram_long(limit=args.batch_size, config=cfg, on_block=persist)
        current_total = get_total_count(conn)
        print(f"\n[+] Run complete. New rows this run: {total_new} | DB total: {current_total}")
        print(f"    Blocks: {summary['blocks']} | Active scraping: {format_duration(summary['active_seconds'])}")
        if args.target and current_total >= args.target:
            print(f"    Target of {args.target} reached.")
    except InstagramChallengeDetected as e:
        print(f"\n[!] FAIL-HARD: Instagram challenge detected: {e}")
        print("    Pipeline stopped. Do NOT auto-resume. Report to CEO and wait for explicit go.")
    except InstagramSessionUnavailable as e:
        print(f"\n[!] FAIL-HARD: Instagram session unavailable: {e}")
        print("    Pipeline stopped. Do NOT auto-resume. Report to CEO and wait for explicit go.")
    except KeyboardInterrupt:
        print("\n[!] Scraping loop interrupted by user. Saving and exiting.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
