"""
Master orchestrator for the Reddit sentiment distillation pipeline.

Usage:
    python scripts/run_distillation.py --mode auto
    python scripts/run_distillation.py --mode gemini --workers 4
    python scripts/run_distillation.py --extract-only
    python scripts/run_distillation.py --batches-only
"""

import os
import sys
import json
import sqlite3
import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DISTILLED_DB = os.path.join(ROOT_DIR, "data", "reddit_distilled.db")
BATCH_DIR = os.path.join(ROOT_DIR, "data", "reddit_batches")

# Add sentiment_training to path
TRAINING_DIR = os.path.join(
    ROOT_DIR, "Qualitative", "psychological", "sentiment_training"
)
sys.path.insert(0, TRAINING_DIR)

from distill_reddit import extract, prepare_batches, status


def _count_labeled() -> Dict:
    """Quick count of labeled rows in distilled DB."""
    if not os.path.exists(DISTILLED_DB):
        return {"labeled": 0, "total": 0}
    conn = sqlite3.connect(DISTILLED_DB)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM reddit_labels WHERE label IS NOT NULL")
    labeled = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM reddit_labels")
    total = c.fetchone()[0]
    conn.close()
    return {"labeled": labeled, "total": total}


def process_batch(batch_file: str, mode: str, source: str) -> Dict:
    """Process a single batch file."""
    from label_batch import label_auto, label_gemini

    if mode == "gemini":
        return label_gemini(batch_file, source=source)
    else:
        return label_auto(batch_file, source=source)


def run_pipeline(
    mode: str = "auto",
    workers: int = 4,
    batch_size: int = 200,
    extract_only: bool = False,
    batches_only: bool = False,
) -> Dict:
    """
    Full pipeline:
    1. Extract submissions -> reddit_labels
    2. Prepare batch JSONL files
    3. Label all batches (parallel if workers > 1)
    4. Report summary
    """
    source = "gemini" if mode == "gemini" else "auto"
    start_time = time.time()

    # ── Step 1: Extract ──────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("STEP 1: Extracting submissions into labeling DB")
    logger.info("=" * 60)
    extract_result = extract()
    logger.info(f"Extract result: {extract_result}")

    if extract_only:
        elapsed = time.time() - start_time
        logger.info(f"Extract-only mode. Elapsed: {elapsed:.1f}s")
        return {"extract": extract_result, "elapsed_s": round(elapsed, 1)}

    # ── Step 2: Prepare batches ──────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("STEP 2: Preparing batch JSONL files")
    logger.info("=" * 60)
    batch_result = prepare_batches(batch_size=batch_size)
    logger.info(f"Batch result: {batch_result}")

    if batches_only:
        elapsed = time.time() - start_time
        logger.info(f"Batches-only mode. Elapsed: {elapsed:.1f}s")
        return {"extract": extract_result, "batches": batch_result, "elapsed_s": round(elapsed, 1)}

    # ── Step 3: Label batches ────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(f"STEP 3: Labeling batches (mode={mode}, workers={workers})")
    logger.info("=" * 60)

    # Collect batch files
    batch_files = sorted(
        [
            os.path.join(BATCH_DIR, f)
            for f in os.listdir(BATCH_DIR)
            if f.endswith(".jsonl")
        ]
    )

    if not batch_files:
        logger.warning("No batch files found to label.")
        return {
            "extract": extract_result,
            "batches": batch_result,
            "labeled_results": [],
            "elapsed_s": round(time.time() - start_time, 1),
        }

    logger.info(f"Found {len(batch_files)} batch files to process")

    labeled_results = []
    total_labeled = 0
    total_errors = 0

    # Process in parallel
    if workers > 1 and mode == "auto":
        # Parallel mode for auto-labeling (no API rate limits)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for bf in batch_files:
                future = executor.submit(process_batch, bf, mode, source)
                futures[future] = bf

            completed = 0
            for future in as_completed(futures):
                completed += 1
                bf = futures[future]
                try:
                    result = future.result()
                    labeled_results.append(result)
                    total_labeled += result.get("labeled", 0)
                    total_errors += result.get("errors", 0)
                except Exception as e:
                    logger.error(f"Error processing {bf}: {e}")
                    total_errors += 1

                # Progress report every 10 batches
                if completed % 10 == 0 or completed == len(batch_files):
                    counts = _count_labeled()
                    elapsed = time.time() - start_time
                    logger.info(
                        f"  Progress: {completed}/{len(batch_files)} batches | "
                        f"Total labeled: {counts['labeled']:,}/{counts['total']:,} | "
                        f"Elapsed: {elapsed:.0f}s"
                    )
    else:
        # Sequential mode
        for i, bf in enumerate(batch_files):
            try:
                result = process_batch(bf, mode, source)
                labeled_results.append(result)
                total_labeled += result.get("labeled", 0)
                total_errors += result.get("errors", 0)
            except Exception as e:
                logger.error(f"Error processing {bf}: {e}")
                total_errors += 1

            # Progress report every 10 batches
            if (i + 1) % 10 == 0 or (i + 1) == len(batch_files):
                counts = _count_labeled()
                elapsed = time.time() - start_time
                logger.info(
                    f"  Progress: {i + 1}/{len(batch_files)} batches | "
                    f"Total labeled: {counts['labeled']:,}/{counts['total']:,} | "
                    f"Elapsed: {elapsed:.0f}s"
                )

    # ── Step 4: Final summary ────────────────────────────────────────────
    elapsed = time.time() - start_time
    final_counts = _count_labeled()

    # Label distribution
    label_dist = {}
    by_source = {}
    if os.path.exists(DISTILLED_DB):
        conn = sqlite3.connect(DISTILLED_DB)
        c = conn.cursor()
        c.execute(
            "SELECT label, COUNT(*) FROM reddit_labels "
            "WHERE label IS NOT NULL GROUP BY label"
        )
        label_dist = dict(c.fetchall())
        c.execute(
            "SELECT labeled_by, COUNT(*) FROM reddit_labels "
            "WHERE label IS NOT NULL GROUP BY labeled_by"
        )
        by_source = dict(c.fetchall())
        conn.close()

    label_names = {0: "bearish", 1: "neutral", 2: "bullish"}
    summary = {
        "extract": extract_result,
        "batches": batch_result,
        "total_labeled": final_counts["labeled"],
        "total_rows": final_counts["total"],
        "errors": total_errors,
        "label_distribution": {label_names.get(k, k): v for k, v in label_dist.items()},
        "by_source": by_source,
        "mode": mode,
        "workers": workers,
        "elapsed_s": round(elapsed, 1),
    }

    logger.info("=" * 60)
    logger.info("DISTILLATION COMPLETE")
    logger.info("=" * 60)
    logger.info(f"  Total labeled:     {final_counts['labeled']:,}")
    logger.info(f"  Total rows:        {final_counts['total']:,}")
    logger.info(f"  Errors:            {total_errors}")
    logger.info(f"  Label dist:        {label_dist}")
    logger.info(f"  By source:         {by_source}")
    logger.info(f"  Elapsed:           {elapsed:.1f}s")
    logger.info("=" * 60)

    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Reddit sentiment distillation orchestrator"
    )
    parser.add_argument(
        "--mode", choices=["auto", "gemini"], default="auto",
        help="Labeling mode (default: auto)"
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Parallel workers for batch labeling (auto mode only)"
    )
    parser.add_argument(
        "--batch-size", type=int, default=200,
        help="Rows per batch file"
    )
    parser.add_argument(
        "--extract-only", action="store_true",
        help="Only run extract step"
    )
    parser.add_argument(
        "--batches-only", action="store_true",
        help="Only run extract + prepare_batches steps"
    )
    args = parser.parse_args()

    result = run_pipeline(
        mode=args.mode,
        workers=args.workers,
        batch_size=args.batch_size,
        extract_only=args.extract_only,
        batches_only=args.batches_only,
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
