"""
Knowledge distillation pipeline: extract Reddit submissions into a labeling DB,
then prepare batch JSONL files for sentiment labeling.

Usage:
    python distill_reddit.py extract           -- populate reddit_labels (unlabeled)
    python distill_reddit.py prepare_batches   -- create batch JSONL files
    python distill_reddit.py status            -- show progress summary
"""

import os
import sys
import json
import sqlite3
import argparse
import logging
from typing import List, Dict, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
REDDIT_DB = os.path.join(ROOT_DIR, "reddit_quant.db")
DISTILLED_DB = os.path.join(ROOT_DIR, "data", "reddit_distilled.db")
BATCH_DIR = os.path.join(ROOT_DIR, "data", "reddit_batches")

BATCH_SIZE = 200
MIN_TEXT_LEN = 10

# ── Schema ───────────────────────────────────────────────────────────────
CREATE_LABELS_TABLE = """
CREATE TABLE IF NOT EXISTS reddit_labels (
    id TEXT,
    subreddit TEXT,
    category TEXT,
    title TEXT,
    selftext TEXT,
    combined_text TEXT,
    score INT,
    upvote_ratio REAL,
    num_comments INT,
    created_utc INT,
    -- Labels to be filled by labelers
    label INT,
    score_continuous REAL,
    confidence TEXT,
    labeled_by TEXT DEFAULT 'unlabeled',
    labeled_at TIMESTAMP,
    batch_id INT,
    PRIMARY KEY (id, labeled_by)
);
"""


def _connect_distilled() -> sqlite3.Connection:
    """Open the distilled DB with WAL mode."""
    os.makedirs(os.path.dirname(DISTILLED_DB), exist_ok=True)
    conn = sqlite3.connect(DISTILLED_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(CREATE_LABELS_TABLE)
    return conn


def extract(source_db: str = REDDIT_DB, min_len: int = MIN_TEXT_LEN) -> Dict:
    """
    Read ALL submissions from reddit_quant.db, combine title + selftext,
    filter short texts, and insert into reddit_labels (unlabeled).

    Returns summary dict.
    """
    logger.info(f"Extracting submissions from {source_db}")

    src = sqlite3.connect(source_db)
    src.row_factory = sqlite3.Row
    cursor = src.cursor()
    cursor.execute(
        "SELECT id, subreddit, category, title, selftext, "
        "score, upvote_ratio, num_comments, created_utc "
        "FROM submissions"
    )
    all_rows = cursor.fetchall()
    src.close()

    logger.info(f"Read {len(all_rows)} submissions from source DB")

    dest = _connect_distilled()
    dest_cursor = dest.cursor()

    # Track existing IDs so we can skip already-loaded rows
    existing = set()
    try:
        dest_cursor.execute("SELECT id FROM reddit_labels")
        existing = {row[0] for row in dest_cursor.fetchall()}
    except sqlite3.OperationalError:
        pass

    inserted = 0
    skipped_short = 0
    skipped_existing = 0
    batch: List[Dict] = []

    for row in all_rows:
        sub_id = row["id"]
        if sub_id in existing:
            skipped_existing += 1
            continue

        title = (row["title"] or "").strip()
        selftext = (row["selftext"] or "").strip()
        combined = f"{title}\n{selftext}".strip()

        if len(combined) < min_len:
            skipped_short += 1
            continue

        batch.append({
            "id": sub_id,
            "subreddit": row["subreddit"],
            "category": row["category"],
            "title": title,
            "selftext": selftext,
            "combined_text": combined,
            "score": row["score"],
            "upvote_ratio": row["upvote_ratio"],
            "num_comments": row["num_comments"],
            "created_utc": row["created_utc"],
        })

        if len(batch) >= 1000:
            _insert_batch(dest_cursor, batch)
            inserted += len(batch)
            batch = []

    # Flush remaining
    if batch:
        _insert_batch(dest_cursor, batch)
        inserted += len(batch)

    dest.commit()
    dest.close()

    summary = {
        "source_rows": len(all_rows),
        "inserted": inserted,
        "skipped_short": skipped_short,
        "skipped_existing": skipped_existing,
    }
    logger.info(f"Extract complete: {summary}")
    return summary


def _insert_batch(cursor: sqlite3.Cursor, rows: List[Dict]) -> None:
    """Insert a batch of rows into reddit_labels (unlabeled)."""
    cursor.executemany(
        """
        INSERT OR IGNORE INTO reddit_labels
            (id, subreddit, category, title, selftext, combined_text,
             score, upvote_ratio, num_comments, created_utc)
        VALUES
            (:id, :subreddit, :category, :title, :selftext, :combined_text,
             :score, :upvote_ratio, :num_comments, :created_utc)
        """,
        rows,
    )


def prepare_batches(batch_size: int = BATCH_SIZE) -> Dict:
    """
    Split unlabeled rows into batch JSONL files.

    Creates: data/reddit_batches/batch_001.jsonl, batch_002.jsonl, ...
    Each line: {"id": "...", "text": "...", "subreddit": "...", "category": "..."}

    Returns summary dict.
    """
    os.makedirs(BATCH_DIR, exist_ok=True)

    conn = _connect_distilled()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, subreddit, category, combined_text FROM reddit_labels "
        "WHERE label IS NULL ORDER BY rowid"
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        logger.info("No unlabeled rows to batch.")
        return {"unlabeled": 0, "batches_created": 0}

    # Clean old batches
    for f in os.listdir(BATCH_DIR):
        if f.startswith("batch_") and f.endswith(".jsonl"):
            os.remove(os.path.join(BATCH_DIR, f))

    total_batches = 0
    for i in range(0, len(rows), batch_size):
        batch_num = (i // batch_size) + 1
        batch_rows = rows[i : i + batch_size]
        batch_file = os.path.join(BATCH_DIR, f"batch_{batch_num:03d}.jsonl")

        with open(batch_file, "w", encoding="utf-8") as f:
            for row in batch_rows:
                record = {
                    "id": row[0],
                    "subreddit": row[1],
                    "category": row[2],
                    "text": row[3],
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        total_batches += 1

    # Tag batch_id in DB
    conn = _connect_distilled()
    cursor = conn.cursor()
    for i in range(0, len(rows), batch_size):
        batch_num = (i // batch_size) + 1
        batch_ids = [rows[j][0] for j in range(i, min(i + batch_size, len(rows)))]
        placeholders = ",".join("?" * len(batch_ids))
        cursor.execute(
            f"UPDATE reddit_labels SET batch_id = ? WHERE id IN ({placeholders})",
            [batch_num] + batch_ids,
        )
    conn.commit()
    conn.close()

    summary = {
        "unlabeled": len(rows),
        "batches_created": total_batches,
        "batch_size": batch_size,
    }
    logger.info(f"Batches created: {summary}")
    return summary


def status() -> Dict:
    """Show labeling progress summary."""
    conn = _connect_distilled()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM reddit_labels")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM reddit_labels WHERE label IS NOT NULL")
    labeled = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM reddit_labels WHERE label IS NULL")
    unlabeled = cursor.fetchone()[0]

    cursor.execute(
        "SELECT labeled_by, COUNT(*) FROM reddit_labels "
        "WHERE label IS NOT NULL GROUP BY labeled_by"
    )
    by_source = dict(cursor.fetchall())

    cursor.execute(
        "SELECT label, COUNT(*) FROM reddit_labels "
        "WHERE label IS NOT NULL GROUP BY label"
    )
    label_dist = dict(cursor.fetchall())

    conn.close()

    # Count batch files on disk
    batch_files = 0
    if os.path.exists(BATCH_DIR):
        batch_files = len([f for f in os.listdir(BATCH_DIR) if f.endswith(".jsonl")])

    summary = {
        "total_rows": total,
        "labeled": labeled,
        "unlabeled": unlabeled,
        "progress_pct": round(100 * labeled / max(total, 1), 2),
        "by_source": by_source,
        "label_distribution": label_dist,
        "batch_files_on_disk": batch_files,
    }

    print(f"\n{'='*60}")
    print(f"  Reddit Distillation Status")
    print(f"{'='*60}")
    print(f"  Total rows:    {total:>8,}")
    print(f"  Labeled:       {labeled:>8,}  ({summary['progress_pct']}%)")
    print(f"  Unlabeled:     {unlabeled:>8,}")
    print(f"  Batch files:   {batch_files:>8}")
    if by_source:
        print(f"  By source:     {by_source}")
    if label_dist:
        label_names = {0: "bearish", 1: "neutral", 2: "bullish"}
        dist_str = ", ".join(
            f"{label_names.get(k, k)}: {v:,}" for k, v in label_dist.items()
        )
        print(f"  Distribution:  {dist_str}")
    print(f"{'='*60}\n")

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Reddit sentiment distillation pipeline"
    )
    sub = parser.add_subparsers(dest="command")

    # extract
    extract_p = sub.add_parser("extract", help="Extract submissions into labeling DB")
    extract_p.add_argument(
        "--source-db", default=REDDIT_DB, help="Path to source reddit DB"
    )
    extract_p.add_argument(
        "--min-len", type=int, default=MIN_TEXT_LEN,
        help="Min combined text length (chars)"
    )

    # prepare_batches
    batches_p = sub.add_parser("prepare_batches", help="Create batch JSONL files")
    batches_p.add_argument(
        "--batch-size", type=int, default=BATCH_SIZE, help="Rows per batch"
    )

    # status
    sub.add_parser("status", help="Show labeling progress")

    args = parser.parse_args()

    if args.command == "extract":
        result = extract(source_db=args.source_db, min_len=args.min_len)
        print(json.dumps(result, indent=2))

    elif args.command == "prepare_batches":
        result = prepare_batches(batch_size=args.batch_size)
        print(json.dumps(result, indent=2))

    elif args.command == "status":
        status()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
