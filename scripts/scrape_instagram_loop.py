#!/usr/bin/env python3
"""
Continuous Instagram scraping loop with irregular delay intervals,
data persistence, progress tracking, and dynamic time estimation.
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

from Qualitative.psychological.scrapers.instagram_primary import fetch_instagram_mentions, InstagramConfig
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
                id, ticker, shortcode, caption, sentiment, views, comments, followers, verified, fetch_ts, external_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record_id,
            ticker,
            shortcode,
            m.get("caption"),
            m.get("sentiment"),
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
    parser = argparse.ArgumentParser(description="Instagram Scrape Loop")
    parser.add_argument("--target", type=int, default=100000, help="Target total scraped records")
    parser.add_argument("--batch-size", type=int, default=100, help="Number of records to scrape per batch")
    parser.add_argument("--min-delay", type=int, default=60, help="Minimum delay between batches in seconds")
    parser.add_argument("--max-delay", type=int, default=120, help="Maximum delay between batches in seconds")
    args = parser.parse_args()

    os.environ["DISCOVERY_LIVE"] = "1"
    
    conn = get_connection()
    setup_db(conn)
    
    initial_count = get_total_count(conn)
    print(f"[*] Database initialized. Currently stored instagram mentions: {initial_count}")
    print(f"[*] Scraping target: {args.target} records.")
    
    if initial_count >= args.target:
        print("[!] Target already reached or exceeded!")
        return

    scraped_this_run = 0
    start_time = time.time()
    
    try:
        while True:
            current_total = get_total_count(conn)
            if current_total >= args.target:
                print(f"\n[+] Success! Target of {args.target} reached. Total stored: {current_total}")
                break
                
            batch_start = time.time()
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Starting next batch of {args.batch_size}...")
            
            try:
                cfg = InstagramConfig()
                mentions = fetch_instagram_mentions(limit=args.batch_size, config=cfg)
                new_inserted = save_mentions(conn, mentions)
                scraped_this_run += new_inserted
                current_total += new_inserted
            except Exception as e:
                print(f"[!] Error fetching/saving batch: {e}. Sleeping before retry.")
                # If hit a rate limit/challenge, do a longer cooldown
                cooldown = random.randint(600, 1200)
                print(f"[*] Cooling down for {format_duration(cooldown)}...")
                time.sleep(cooldown)
                continue
                
            # Progress calculation
            progress_pct = (current_total / args.target) * 100
            remaining_records = args.target - current_total
            
            # Dynamic time estimation based on records scraped during this run
            elapsed_run_time = time.time() - start_time
            if scraped_this_run > 0:
                avg_time_per_record = elapsed_run_time / scraped_this_run
                est_remaining_seconds = remaining_records * avg_time_per_record
                est_time_str = format_duration(est_remaining_seconds)
            else:
                est_time_str = "Calculating..."
                
            print(f"Status: {progress_pct:.3f}% | Total Scraped: {current_total}/{args.target} (+{new_inserted} new)")
            print(f"Run Stats: Scraped {scraped_this_run} items in {format_duration(elapsed_run_time)}")
            print(f"Estimated Remaining Time to Target: {est_time_str}")
            
            if current_total >= args.target:
                print(f"\n[+] Target reached!")
                break
                
            # Sleep with irregular interval
            sleep_time = random.uniform(args.min_delay, args.max_delay)
            print(f"[*] Sleeping for {format_duration(sleep_time)}...")
            
            # Countdown print
            sleep_start = time.time()
            while time.time() - sleep_start < sleep_time:
                remaining = int(sleep_time - (time.time() - sleep_start))
                sys.stdout.write(f"\rNext batch in: {remaining}s...   ")
                sys.stdout.flush()
                time.sleep(1)
            sys.stdout.write("\n")
            
    except KeyboardInterrupt:
        print("\n[!] Scraping loop interrupted by user. Saving and exiting.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
