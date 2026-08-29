"""
Gemini-based parallel labeling component for Reddit sentiment distillation.
Uses google/antigravity-gemini-3-flash to label Reddit submissions.
"""

import os
import sys
import json
import time
import sqlite3
import argparse
import logging
import threading
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Constants
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DISTILLED_DB = os.path.join(ROOT_DIR, "data", "reddit_distilled.db")
BATCH_DIR = os.path.join(ROOT_DIR, "data", "reddit_batches")
MODEL_NAME = "google/antigravity-gemini-3-flash"

PROMPT_TEMPLATE = """You are a financial sentiment analyzer for Reddit posts about stocks and markets.

Classify this Reddit post's sentiment. Consider:
- WSB slang: "moon"=bullish, "GUH"=bearish, "tendies"=bullish, "paper hands"=bearish, "diamond hands"=bullish, "bagholder"=bearish
- Emoji: 🚀📈=bullish, 📉💀=bearish
- Context: Is the author expressing bullish, bearish, or neutral sentiment about a stock/market?

Post text: {text}

Return ONLY valid JSON (no markdown):
{{"label": 0, "score": -0.5, "confidence": "high"}}

label: 0=bearish, 1=neutral, 2=bullish
score: -1.0 (most bearish) to +1.0 (most bullish)
confidence: high/medium/low"""

# Thread safety
db_lock = threading.Lock()

class RateLimiter:
    """Thread-safe rate limiter using a sliding window."""
    def __init__(self, max_calls: int = 10, period: float = 1.0):
        self.max_calls = max_calls
        self.period = period
        self.lock = threading.Lock()
        self.calls = []

    def wait(self):
        with self.lock:
            now = time.time()
            # filter out older calls
            self.calls = [t for t in self.calls if now - t < self.period]
            if len(self.calls) >= self.max_calls:
                sleep_time = self.period - (now - self.calls[0])
                if sleep_time > 0:
                    time.sleep(sleep_time)
                    now = time.time()
            self.calls.append(now)

rate_limiter = RateLimiter(max_calls=10, period=1.0)


def _parse_gemini_response(raw: str) -> Optional[Dict]:
    """Parse Gemini JSON response into label/score/confidence."""
    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = lines[1:]  # drop opening ```json or ```
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()

        data = json.loads(cleaned)
        label = int(data["label"])
        score = float(data["score"])
        confidence = str(data["confidence"]).lower()

        # Validate ranges
        if label not in (0, 1, 2):
            label = 1
        score = max(-1.0, min(1.0, score))
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"

        return {"label": label, "score": score, "confidence": confidence}
    except Exception as e:
        logger.warning(f"Failed to parse Gemini response: {raw[:300]}. Error: {e}")
        return None


def label_post(client, post: Dict) -> Optional[Dict]:
    """Label a single post with 3x retry on failure."""
    post_id = post["id"]
    post_text = post["text"]
    
    for attempt in range(3):
        try:
            rate_limiter.wait()
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "user", "content": PROMPT_TEMPLATE.format(text=post_text)}
                ],
                temperature=0.0,
                timeout=30.0,
            )
            raw = response.choices[0].message.content.strip()
            parsed = _parse_gemini_response(raw)
            if parsed:
                parsed["id"] = post_id
                return parsed
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed for post {post_id}: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return None


def write_results_to_db(results: List[Dict]) -> None:
    """Safely write batch results to the database."""
    if not results:
        return
    with db_lock:
        conn = sqlite3.connect(DISTILLED_DB)
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()
        try:
            cursor.executemany(
                """
                INSERT OR REPLACE INTO reddit_labels (
                    id, subreddit, category, title, selftext, combined_text,
                    score, upvote_ratio, num_comments, created_utc,
                    label, score_continuous, confidence, labeled_by, labeled_at, batch_id
                )
                SELECT 
                    id, subreddit, category, title, selftext, combined_text,
                    score, upvote_ratio, num_comments, created_utc,
                    ?, ?, ?, 'gemini', CURRENT_TIMESTAMP, batch_id
                FROM reddit_labels
                WHERE id = ? AND labeled_by = 'unlabeled'
                """,
                [
                    (r["label"], r["score"], r["confidence"], r["id"])
                    for r in results
                ]
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Database write error: {e}")
        finally:
            conn.close()


def process_batch_file(client, batch_path: str, batch_idx: int, total_batches: int) -> Dict:
    """Process a single JSONL batch file."""
    start_time = time.time()
    posts = []
    
    try:
        with open(batch_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    posts.append(json.loads(line))
    except Exception as e:
        logger.error(f"Failed to read batch file {batch_path}: {e}")
        return {"batch": batch_path, "labeled": 0, "errors": len(posts) or 1, "time": 0.0}

    results = []
    errors = 0
    
    # Process posts sequentially within a batch to respect the global rate limit cleanly
    for post in posts:
        res = label_post(client, post)
        if res:
            results.append(res)
        else:
            errors += 1

    write_results_to_db(results)
    
    duration = time.time() - start_time
    labeled_count = len(results)
    avg_time = duration / max(labeled_count, 1)
    
    logger.info(
        f"Progress: Batch {batch_idx}/{total_batches} ({os.path.basename(batch_path)}) | "
        f"labeled: {labeled_count}, errors: {errors}, Avg time: {avg_time:.3f}s/post"
    )
    
    return {
        "batch": batch_path,
        "labeled": labeled_count,
        "errors": errors,
        "time": duration
    }


def main():
    parser = argparse.ArgumentParser(description="Gemini-based Parallel Reddit Sentiment Labeler")
    parser.add_argument("--batch-file", type=str, default=None, help="Specific batch JSONL file to process")
    parser.add_argument("--concurrency", type=int, default=4, help="Number of parallel batch processing threads")
    parser.add_argument("--start-batch", type=int, default=None, help="Start batch number (inclusive)")
    parser.add_argument("--end-batch", type=int, default=None, help="End batch number (inclusive)")
    args = parser.parse_args()

    # Import OpenAI for Gemini API compatibility
    import openai
    api_key = os.environ.get("ANTIGRAVITY_API_KEY") or os.environ.get("OPENAI_API_KEY") or "not-needed"
    base_url = os.environ.get("ANTIGRAVITY_BASE_URL", "https://api.antigravity.dev/v1")
    client = openai.OpenAI(api_key=api_key, base_url=base_url)

    # Determine batch files to process
    batch_files = []
    if args.batch_file:
        if not os.path.exists(args.batch_file):
            logger.error(f"Specified batch file does not exist: {args.batch_file}")
            sys.exit(1)
        batch_files = [args.batch_file]
    else:
        if not os.path.exists(BATCH_DIR):
            logger.error(f"Batch directory does not exist: {BATCH_DIR}")
            sys.exit(1)
        
        all_batches = sorted([
            os.path.join(BATCH_DIR, f) 
            for f in os.listdir(BATCH_DIR) 
            if f.startswith("batch_") and f.endswith(".jsonl")
        ])
        
        for b_path in all_batches:
            fname = os.path.basename(b_path)
            # extract batch number
            try:
                b_num = int(fname.split("_")[1].split(".")[0])
            except (IndexError, ValueError):
                continue
            
            if args.start_batch is not None and b_num < args.start_batch:
                continue
            if args.end_batch is not None and b_num > args.end_batch:
                continue
            batch_files.append(b_path)

    if not batch_files:
        logger.info("No batches found to process.")
        return

    logger.info(f"Starting sentiment labeling on {len(batch_files)} batches with concurrency={args.concurrency}")
    
    total_labeled = 0
    total_errors = 0
    total_time = 0.0
    total_batches = len(batch_files)

    start_run_time = time.time()

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {
            executor.submit(process_batch_file, client, path, idx + 1, total_batches): path
            for idx, path in enumerate(batch_files)
        }
        
        for future in as_completed(futures):
            res = future.result()
            total_labeled += res["labeled"]
            total_errors += res["errors"]
            total_time += res["time"]

    total_duration = time.time() - start_run_time
    avg_per_post = total_duration / max(total_labeled, 1)
    logger.info(
        f"Labeling run completed. Total Labeled: {total_labeled}, Total Errors: {total_errors}, "
        f"Total Time: {total_duration:.2f}s, Overall Avg: {avg_per_post:.3f}s/post"
    )


if __name__ == "__main__":
    main()
