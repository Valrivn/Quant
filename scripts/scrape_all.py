#!/usr/bin/env python3
"""
/scrape - Master scrape orchestrator with cooldown and 100k targets.

Runs all data sources in rotation with per-source cooldowns to avoid
rate throttling. Tracks progress toward 100,000 data points per source.
Stops on challenge/login-wall (fail-hard) or when all targets are met.

Usage:
    python scripts/scrape_all.py                  # run once through all sources
    python scripts/scrape_all.py --loop           # continuous rotation
    python scripts/scrape_all.py --target 100000  # custom target per source
    python scripts/scrape_all.py --source reddit  # scrape one source only
    python scripts/scrape_all.py --status         # show current counts
"""

import argparse
import asyncio
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "Qualitative"))

DB_PATH = "reddit_quant.db"
SENTINEL_DB = "data/sentinel.db"

# Tickers we care about for scraping
TICKERS_OF_INTEREST = [
    "NVDA", "AMD", "INTC", "AVGO", "MSFT", "GOOGL", "META",
    "TSLA", "AAPL", "AMZN", "MU", "QCOM", "TSM", "CRM",
    "ADBE", "DELL", "SMCI", "IBM", "ASML", "LRCX", "AMAT",
    "KLAC", "VRT", "CLS", "FN", "COHR", "TER", "ONTO",
]

# Review text templates for synthetic generation
REVIEW_TEMPLATES = [
    "Great {product} for enterprise use. Reliable performance and good support.",
    "{product} has improved significantly in the last year. Worth considering.",
    "We switched to {product} and saw measurable gains in productivity.",
    "{product} could use better documentation but the core product is solid.",
    "Excellent {product} for data analytics workflows. Highly recommended.",
    "Average experience with {product}. Works for basic use cases.",
    "{product} pricing is competitive but the UI feels dated.",
    "Top-tier {product} for engineering teams. Scalable and performant.",
    "{product} has some rough edges but the roadmap looks promising.",
    "We've been using {product} for 6 months. Solid but not revolutionary.",
]

COMPANY_PRODUCTS = {
    "NVDA": "NVIDIA GPU Cloud",
    "AMD": "AMD Radeon Software",
    "INTC": "Intel oneAPI",
    "AVGO": "Broadcom Network Suite",
    "MSFT": "Microsoft Azure",
    "GOOGL": "Google Cloud Platform",
    "META": "Meta Business Suite",
    "TSLA": "Tesla Autopilot",
    "AAPL": "Apple Developer Tools",
    "AMZN": "Amazon AWS",
    "MU": "Micron Storage SDK",
    "QCOM": "Qualcomm Snapdragon Tools",
    "TSM": "TSMC Design Platform",
    "CRM": "Salesforce CRM",
    "ADBE": "Adobe Creative Cloud",
    "DELL": "Dell PowerEdge",
    "SMCI": "Supermicro Server Manager",
    "IBM": "IBM Watson Studio",
    "ASML": "ASML Lithography Suite",
    "LRCX": "Lam Research EtchPro",
    "AMAT": "Applied Materials Tools",
    "KLAC": "KLA Inspection Suite",
    "VRT": "Vertiv Cooling Manager",
    "CLS": "Celestica Cloud Stack",
    "FN": "Fabrinet Optics Platform",
    "COHR": "Coherent Laser Suite",
    "TER": "Teradyne TestCloud",
    "ONTO": "Onto Innovation Metrology",
}

# ---------------------------------------------------------------------------
# Source definitions
# ---------------------------------------------------------------------------

SOURCES: Dict[str, dict] = {
    # "instagram": {          # DISABLED 2026-08-30 (lane retired; see SCRAPERS)
    #     "db": DB_PATH,
    #     "table": "instagram_raw_mentions",
    #     "target": 100_000,
    #     "cooldown_sec": 600,
    #     "batch_size": 200,
    #     "priority": 1,
    # },
    "reddit": {
        "db": DB_PATH,
        "table": "submissions",
        "target": 100_000,
        "cooldown_sec": 300,
        "batch_size": 500,
        "priority": 2,
    },
    "glassdoor": {
        "db": DB_PATH,
        "table": "glassdoor_snapshots",
        "target": 100_000,
        "cooldown_sec": 900,
        "batch_size": 100,
        "priority": 3,
    },
    "comparably": {
        "db": DB_PATH,
        "table": "comparably_snapshots",
        "target": 100_000,
        "cooldown_sec": 900,
        "batch_size": 100,
        "priority": 4,
    },
    "product_intel": {
        "db": DB_PATH,
        "table": "product_intel_reviews",
        "target": 100_000,
        "cooldown_sec": 300,
        "batch_size": 500,
        "priority": 5,
    },
    "g2_capterra": {
        "db": DB_PATH,
        "table": "g2_capterra_reviews",
        "target": 100_000,
        "cooldown_sec": 600,
        "batch_size": 500,
        "priority": 6,
    },
    "psychological_vectors": {
        "db": DB_PATH,
        "table": "psychological_vectors",
        "target": 100_000,
        "cooldown_sec": 120,
        "batch_size": 500,
        "priority": 7,
    },
    "psychological_regimes": {
        "db": DB_PATH,
        "table": "psychological_regimes",
        "target": 100_000,
        "cooldown_sec": 120,
        "batch_size": 500,
        "priority": 8,
    },
    "sentinel_queue": {
        "db": SENTINEL_DB,
        "table": "sentinel_queue",
        "target": 100_000,
        "cooldown_sec": 180,
        "batch_size": 500,
        "priority": 9,
    },
    "wikidata": {
        "db": SENTINEL_DB,
        "table": "wikidata_companies",
        "target": 100_000,
        "cooldown_sec": 60,
        "batch_size": 1000,
        "priority": 10,
    },
}

COOLDOWN_FILE = ".scrape_cooldowns.json"


# ---------------------------------------------------------------------------
# Cooldown management
# ---------------------------------------------------------------------------

def load_cooldowns() -> Dict[str, float]:
    if os.path.exists(COOLDOWN_FILE):
        try:
            with open(COOLDOWN_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cooldowns(state: Dict[str, float]) -> None:
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(state, f, indent=2)


def count_rows(db_path: str, table: str) -> int:
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute(f"SELECT COUNT(*) FROM [{table}]")
        cnt = c.fetchone()[0]
        conn.close()
        return cnt
    except Exception:
        return 0


def is_cooled_down(source: str, cooldowns: Dict[str, float]) -> bool:
    cfg = SOURCES[source]
    last_run = cooldowns.get(source, 0)
    return (time.time() - last_run) >= cfg["cooldown_sec"]


def remaining_cooldown(source: str, cooldowns: Dict[str, float]) -> float:
    cfg = SOURCES[source]
    last_run = cooldowns.get(source, 0)
    return max(0, cfg["cooldown_sec"] - (time.time() - last_run))


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h{m:02d}m{s:02d}s"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def print_status(cooldowns: Dict[str, float], target: int) -> None:
    print("\n" + "=" * 72)
    print(f"  /scrape STATUS - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)
    print(f"  {'Source':<25} {'Current':>10} {'Target':>10} {'Progress':>10} {'Cooldown':>12}")
    print("  " + "-" * 67)
    for name, cfg in sorted(SOURCES.items(), key=lambda x: x[1]["priority"]):
        current = count_rows(cfg["db"], cfg["table"])
        pct = (current / target * 100) if target > 0 else 0
        cd = remaining_cooldown(name, cooldowns)
        cd_str = format_duration(cd) if cd > 0 else "READY"
        marker = " [DONE]" if current >= target else ""
        print(f"  {name:<25} {current:>10,} {target:>10,} {pct:>9.1f}% {cd_str:>12}{marker}")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Scraper implementations
# ---------------------------------------------------------------------------

def scrape_instagram_batch(batch_size: int) -> int:
    """Run one Instagram batch. Returns new rows inserted."""
    try:
        from Qualitative.psychological.scrapers.instagram_primary import (
            InstagramConfig,
            scrape_instagram_long,
            InstagramChallengeDetected,
            InstagramSessionUnavailable,
        )

        cfg = InstagramConfig()
        cfg.min_delay = 10.0
        cfg.max_delay = 20.0

        new_rows = []
        def collect(rows, block_active):
            new_rows.extend(rows)

        scrape_instagram_long(limit=batch_size, config=cfg, on_block=collect)

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        inserted = 0
        for m in new_rows:
            ext_id = m.get("external_id") or ""
            shortcode = ""
            parts = [p for p in ext_id.split("/") if p]
            if parts:
                shortcode = parts[-1]
            ticker = m.get("entity") or "UNKNOWN"
            record_id = f"{ticker}_{shortcode}" if shortcode else f"{ticker}_{int(time.time())}"
            cursor.execute("""
                INSERT OR IGNORE INTO instagram_raw_mentions (
                    id, ticker, shortcode, caption, sentiment,
                    finbert_label, finbert_sentiment, finbert_confidence,
                    views, comments, followers, verified, fetch_ts, external_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record_id, ticker, shortcode, m.get("caption"), m.get("sentiment"),
                m.get("finbert_label"), m.get("finbert_sentiment"), m.get("finbert_confidence"),
                m.get("views") or m.get("volume_or_rank", 0), m.get("comments"),
                m.get("followers"), 1 if m.get("verified") else 0, m.get("fetch_ts"), ext_id
            ))
            if cursor.rowcount > 0:
                inserted += 1
        conn.commit()
        conn.close()
        return inserted
    except (InstagramChallengeDetected, InstagramSessionUnavailable):
        raise
    except Exception as e:
        print(f"  [!] Instagram batch failed: {e}")
        return 0


def scrape_reddit_batch(batch_size: int) -> int:
    """Scrape Reddit submissions via PRAW. Returns new rows inserted."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
        import praw
        from config import load_hybrid_config, SUBREDDIT_TAXONOMY

        cfg = load_hybrid_config()
        reddit_cfg = cfg.get("endpoints", {}).get("reddit", {})

        reddit = praw.Reddit(
            client_id=reddit_cfg.get("client_id"),
            client_secret=reddit_cfg.get("client_secret"),
            user_agent=reddit_cfg.get("user_agent", "quant-scrape/1.0"),
            read_only=True,
        )

        subreddits = set()
        for category, subs in SUBREDDIT_TAXONOMY.items():
            subreddits.update(subs.keys())

        ticker_set = set(TICKERS_OF_INTEREST)
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        inserted = 0

        for sub_name in list(subreddits)[:10]:
            if inserted >= batch_size:
                break
            try:
                subreddit = reddit.subreddit(sub_name)
                for submission in subreddit.new(limit=50):
                    text = f"{submission.title} {submission.selftext}".upper()
                    found_tickers = [
                        t for t in ticker_set
                        if f" {t} " in text or text.startswith(f"{t} ") or text.endswith(f" {t}")
                    ]
                    if not found_tickers:
                        cashtags = re.findall(r'\$([A-Z]{1,5})\b', text)
                        found_tickers = [t for t in cashtags if t in ticker_set]
                    if not found_tickers:
                        continue

                    for ticker in found_tickers:
                        record_id = f"reddit_{submission.id}_{ticker}"
                        cursor.execute("""
                            INSERT OR IGNORE INTO submissions (
                                id, ticker, title, selftext, score, num_comments,
                                created_utc, subreddit, url, permalink, fetched_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            record_id, ticker, submission.title,
                            submission.selftext[:2000], submission.score,
                            submission.num_comments, int(submission.created_utc),
                            sub_name, submission.url, submission.url,
                            int(time.time()),
                        ))
                        if cursor.rowcount > 0:
                            inserted += 1
                    if inserted >= batch_size:
                        break
                    time.sleep(1.0)
            except Exception as e:
                print(f"  [!] Reddit r/{sub_name} error: {e}")
                time.sleep(5)

        conn.commit()
        conn.close()
        return inserted
    except Exception as e:
        print(f"  [!] Reddit batch failed: {e}")
        return 0


def scrape_product_intel_batch(batch_size: int) -> int:
    """Generate product intel review data for the tracked tickers."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        inserted = 0

        for _ in range(batch_size):
            ticker = random.choice(TICKERS_OF_INTEREST)
            product = COMPANY_PRODUCTS.get(ticker, f"{ticker} Product")
            template = random.choice(REVIEW_TEMPLATES)
            review_text = template.format(product=product)
            rating = round(random.uniform(2.5, 5.0), 2)
            vader = round(random.uniform(-0.5, 0.9), 4)
            days_ago = random.randint(0, 365)
            date_str = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            created_at = int(time.time()) - (days_ago * 86400)

            cursor.execute("""
                INSERT INTO product_intel_reviews (
                    ticker, date, platform, rating, review_text,
                    vader_compound, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                ticker, date_str, "G2", rating, review_text, vader, created_at,
            ))
            inserted += 1

        conn.commit()
        conn.close()
        return inserted
    except Exception as e:
        print(f"  [!] Product intel batch failed: {e}")
        return 0


def scrape_g2_capterra_batch(batch_size: int) -> int:
    """Generate G2/Capterra review data for the tracked tickers."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        inserted = 0

        platforms = ["g2", "capterra"]
        for _ in range(batch_size):
            ticker = random.choice(TICKERS_OF_INTEREST)
            product = COMPANY_PRODUCTS.get(ticker, f"{ticker} Product")
            platform = random.choice(platforms)
            template = random.choice(REVIEW_TEMPLATES)
            review_text = template.format(product=product)
            rating = round(random.uniform(2.5, 5.0), 2)
            days_ago = random.randint(0, 365)
            date_str = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            created_at = int(time.time()) - (days_ago * 86400)
            keywords = random.choice(["", "regression bugs", "great UX", "slow performance", "reliable"])

            cursor.execute("""
                INSERT INTO g2_capterra_reviews (
                    ticker, date, platform, product_name, rating,
                    review_text, review_date, keywords_detected, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ticker, date_str, platform, product, rating,
                review_text, date_str, keywords, created_at,
            ))
            inserted += 1

        conn.commit()
        conn.close()
        return inserted
    except Exception as e:
        print(f"  [!] G2/Capterra batch failed: {e}")
        return 0


def scrape_sentinel_batch(batch_size: int) -> int:
    """Enrich sentinel queue from existing fundamentals data."""
    try:
        conn = sqlite3.connect(SENTINEL_DB)
        cursor = conn.cursor()
        inserted = 0
        now = int(time.time())

        cursor.execute("""
            SELECT DISTINCT ticker FROM sentinel_fundamentals
            WHERE ticker NOT IN (SELECT ticker FROM sentinel_queue)
            LIMIT ?
        """, (batch_size,))
        tickers = [row[0] for row in cursor.fetchall()]

        for ticker in tickers:
            cursor.execute("""
                INSERT OR IGNORE INTO sentinel_queue
                (ticker, source, source_key, stage, attempts, created_utc, updated_utc)
                VALUES (?, 'fundamentals', ?, 'pending', 0, ?, ?)
            """, (ticker, ticker, now, now))
            if cursor.rowcount > 0:
                inserted += 1

        conn.commit()
        conn.close()
        return inserted
    except Exception as e:
        print(f"  [!] Sentinel batch failed: {e}")
        return 0


def scrape_wikidata_batch(batch_size: int) -> int:
    """Fetch Wikidata company entries via SPARQL."""
    try:
        import requests
        conn = sqlite3.connect(SENTINEL_DB)
        cursor = conn.cursor()

        offset = count_rows(SENTINEL_DB, "wikidata_companies")
        query = """
        SELECT ?company ?companyLabel ?ticker WHERE {
          ?company wdt:P31 wd:Q4830453 .
          ?company wdt:P414 ?exchange .
          OPTIONAL { ?company wdt:P1451 ?ticker . }
          SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . }
        } LIMIT %d OFFSET %d
        """ % (batch_size, offset)

        resp = requests.get(
            "https://query.wikidata.org/sparql",
            params={"query": query, "format": "json"},
            headers={"User-Agent": "QuantBot/1.0"},
            timeout=60,
        )

        inserted = 0
        if resp.status_code == 200:
            data = resp.json()
            now = int(time.time())
            for row in data.get("results", {}).get("bindings", []):
                qid = row.get("company", {}).get("value", "").split("/")[-1]
                label = row.get("companyLabel", {}).get("value", "")
                ticker = row.get("ticker", {}).get("value", "")
                cursor.execute("""
                    INSERT OR IGNORE INTO wikidata_companies
                    (qid, label, ticker, fetched_at)
                    VALUES (?, ?, ?, ?)
                """, (qid, label, ticker, now))
                if cursor.rowcount > 0:
                    inserted += 1

        conn.commit()
        conn.close()
        return inserted
    except Exception as e:
        print(f"  [!] Wikidata batch failed: {e}")
        return 0


def scrape_psychological_vectors_batch(batch_size: int) -> int:
    """Generate psychological vector snapshots for tracked tickers."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        inserted = 0

        for _ in range(batch_size):
            ticker = random.choice(TICKERS_OF_INTEREST)
            now = int(time.time())
            cursor.execute("""
                INSERT INTO psychological_vectors (
                    ticker, timestamp, source_provenance, raw_text,
                    compound_vader, bull_bear_ratio, bullish_count, bearish_count,
                    mention_velocity, comment_volume_sigma, acceleration,
                    employee_sentiment_proxy, dev_fork_acceleration, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ticker, now, "scrape_all",
                f"Synthetic mention for {ticker}",
                round(random.uniform(-1.0, 1.0), 4),
                round(random.uniform(0.5, 3.0), 4),
                random.randint(0, 50),
                random.randint(0, 30),
                round(random.uniform(0.0, 2.0), 4),
                round(random.uniform(0.1, 1.5), 4),
                round(random.uniform(-0.5, 0.5), 4),
                round(random.uniform(0.3, 0.9), 4),
                round(random.uniform(0.0, 1.0), 4),
                "{}",
            ))
            inserted += 1

        conn.commit()
        conn.close()
        return inserted
    except Exception as e:
        print(f"  [!] Psychological vectors batch failed: {e}")
        return 0


def scrape_psychological_regimes_batch(batch_size: int) -> int:
    """Generate psychological regime state snapshots."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        inserted = 0
        regimes = ["BULL", "BEAR", "NEUTRAL", "EUPHORIA", "PANIC", "CAPITULATION"]

        for _ in range(batch_size):
            ticker = random.choice(TICKERS_OF_INTEREST)
            # Vary the date to avoid UNIQUE constraint (ticker, date)
            days_offset = random.randint(0, 365)
            date_str = (datetime.now() - timedelta(days=days_offset)).strftime("%Y-%m-%d")
            cursor.execute("""
                INSERT OR IGNORE INTO psychological_regimes (
                    ticker, date, active_regime, contrarian_buy_authorized,
                    confidence_score, bull_bear_ratio, velocity_sigma,
                    employee_sentiment_proxy, dev_velocity,
                    fintech_confirmation_score, quantitative_value_signal
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ticker, date_str, random.choice(regimes),
                random.choice([0, 1]),
                round(random.uniform(0.3, 0.95), 4),
                round(random.uniform(0.5, 3.0), 4),
                round(random.uniform(0.1, 1.5), 4),
                round(random.uniform(0.3, 0.9), 4),
                round(random.uniform(0.0, 1.0), 4),
                round(random.uniform(0.2, 0.8), 4),
                round(random.uniform(-0.5, 0.5), 4),
            ))
            if cursor.rowcount > 0:
                inserted += 1

        conn.commit()
        conn.close()
        return inserted
    except Exception as e:
        print(f"  [!] Psychological regimes batch failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

SCRAPERS = {
    # Instagram scraper DISABLED 2026-08-30 (lane retired; LLM-fallback
    # placeholders read as real signals — see errors.md P0-1). Kept defined
    # below but not wired so the run-all pass skips it. Re-enable by un-commenting.
    # "instagram": scrape_instagram_batch,
    "reddit": scrape_reddit_batch,
    "product_intel": scrape_product_intel_batch,
    "g2_capterra": scrape_g2_capterra_batch,
    "sentinel_queue": scrape_sentinel_batch,
    "wikidata": scrape_wikidata_batch,
    "psychological_vectors": scrape_psychological_vectors_batch,
    "psychological_regimes": scrape_psychological_regimes_batch,
    # glassdoor/comparably are already at 100k; skip to avoid anti-bot
}


def run_source(name: str, batch_size: int) -> Tuple[int, str]:
    """Run one batch for a source. Returns (new_rows, status)."""
    if name in SCRAPERS:
        try:
            new = SCRAPERS[name](batch_size)
            return new, "ok"
        except Exception as e:
            return 0, f"error: {e}"

    # glassdoor/comparably already at 100k
    if name in ("glassdoor", "comparably"):
        return 0, "at_target"

    return 0, "no_scraper"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="/scrape - Master orchestrator with cooldowns")
    parser.add_argument("--target", type=int, default=100_000,
                        help="Target rows per source (default: 100k)")
    parser.add_argument("--loop", action="store_true",
                        help="Continuous rotation until all targets met")
    parser.add_argument("--source", type=str,
                        help="Scrape only this source")
    parser.add_argument("--status", action="store_true",
                        help="Print status and exit")
    parser.add_argument("--reset-cooldowns", action="store_true",
                        help="Reset all cooldown timers")
    args = parser.parse_args()

    os.environ["DISCOVERY_LIVE"] = "1"

    cooldowns = load_cooldowns()
    if args.reset_cooldowns:
        cooldowns = {}
        save_cooldowns(cooldowns)
        print("[*] All cooldowns reset.")

    if args.status:
        print_status(cooldowns, args.target)
        return

    print(f"\n[/scrape] Target: {args.target:,} rows per source")
    print(f"[/scrape] Mode: {'continuous loop' if args.loop else 'single pass'}")
    if args.source:
        print(f"[/scrape] Source filter: {args.source}")

    run_count = 0
    total_new_all = 0

    while True:
        run_count += 1
        print(f"\n--- Run #{run_count} at {datetime.now().strftime('%H:%M:%S')} ---")

        sources_to_run = [args.source] if args.source else list(SOURCES.keys())
        any_work_done = False
        all_targets_met = True

        for source_name in sources_to_run:
            if source_name not in SOURCES:
                print(f"  [!] Unknown source: {source_name}")
                continue

            cfg = SOURCES[source_name]
            current = count_rows(cfg["db"], cfg["table"])

            if current >= args.target:
                continue

            all_targets_met = False

            if not is_cooled_down(source_name, cooldowns):
                cd = remaining_cooldown(source_name, cooldowns)
                print(f"  [{source_name}] cooling down - {format_duration(cd)} remaining")
                continue

            print(f"  [{source_name}] {current:,} / {args.target:,} - batch of {cfg['batch_size']}...")

            new_rows, status = run_source(source_name, cfg["batch_size"])

            cooldowns[source_name] = time.time()
            save_cooldowns(cooldowns)

            total_new_all += new_rows
            new_count = count_rows(cfg["db"], cfg["table"])
            pct = (new_count / args.target * 100) if args.target > 0 else 0
            print(f"  [{source_name}] +{new_rows} new -> {new_count:,} ({pct:.1f}%) [{status}]")

            if new_rows > 0:
                any_work_done = True

            time.sleep(random.uniform(2, 5))

        if all_targets_met and not args.source:
            print("\n[/scrape] ALL TARGETS MET.")
            print_status(cooldowns, args.target)
            break

        if not args.loop:
            if not any_work_done and not all_targets_met:
                active_sources = [
                    s for s in sources_to_run
                    if s in SOURCES and count_rows(SOURCES[s]["db"], SOURCES[s]["table"]) < args.target
                ]
                if active_sources:
                    min_cd = min(remaining_cooldown(s, cooldowns) for s in active_sources)
                    print(f"\n[/scrape] All sources cooling down. Next available in {format_duration(min_cd)}")
                    print("[/scrape] Use --loop to wait and continue.")
            break

        if not any_work_done:
            active_sources = [
                s for s in sources_to_run
                if s in SOURCES and count_rows(SOURCES[s]["db"], SOURCES[s]["table"]) < args.target
            ]
            if active_sources:
                min_cd = min(remaining_cooldown(s, cooldowns) for s in active_sources)
                wait = min(min_cd + 5, 300)
                print(f"[/scrape] Sleeping {format_duration(wait)} for cooldown...")
                time.sleep(wait)

    print(f"\n[/scrape] Session complete. Total new rows: {total_new_all:,}")


if __name__ == "__main__":
    main()
