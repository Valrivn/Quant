"""Label all Reddit batches with the RedditSentimentEngine under labeled_by='bigpickle'.

Idempotent: rows already carrying a 'bigpickle' label are skipped (INSERT OR IGNORE).
"""

import os
import sys
import json
import time
import sqlite3
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TRAINING_DIR = os.path.join(ROOT_DIR, "Qualitative", "psychological", "sentiment_training")
BATCH_DIR = os.path.join(ROOT_DIR, "data", "reddit_batches")
DISTILLED_DB = os.path.join(ROOT_DIR, "data", "reddit_distilled.db")

sys.path.insert(0, TRAINING_DIR)

_thread_local = threading.local()


def _count_labeled() -> int:
    conn = sqlite3.connect(DISTILLED_DB)
    n = conn.execute("SELECT COUNT(*) FROM reddit_labels WHERE labeled_by='bigpickle'").fetchone()[0]
    conn.close()
    return n


def process_batch(batch_file: str) -> None:
    from label_batch import label_auto
    label_auto(batch_file, source="bigpickle")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    batch_files = sorted(
        f for f in os.listdir(BATCH_DIR) if f.startswith("batch_") and f.endswith(".jsonl")
    )
    total = len(batch_files)
    logger.info(f"Labeling {total} batch files with workers={args.workers}")
    start = time.time()

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process_batch, os.path.join(BATCH_DIR, f)): f for f in batch_files}
        for future in as_completed(futures):
            future.result()
            done += 1
            if done % 25 == 0 or done == total:
                elapsed = time.time() - start
                logger.info(
                    f"Progress: {done}/{total} batches | bigpickle-labeled: {_count_labeled():,} | "
                    f"elapsed {elapsed:.0f}s"
                )

    logger.info(f"DONE in {time.time() - start:.1f}s | bigpickle-labeled: {_count_labeled():,}")


if __name__ == "__main__":
    main()