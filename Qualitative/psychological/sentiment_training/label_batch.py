"""
Label a single batch of Reddit posts for sentiment.

Usage:
    python label_batch.py --batch data/reddit_batches/batch_001.jsonl --mode auto
    python label_batch.py --batch data/reddit_batches/batch_001.jsonl --mode gemini
    python label_batch.py --batch data/reddit_batches/batch_001.jsonl --mode auto --source bigpickle
"""

import os
import sys
import json
import sqlite3
import argparse
import logging
import time
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DISTILLED_DB = os.path.join(ROOT_DIR, "data", "reddit_distilled.db")

# ── Gemini config ────────────────────────────────────────────────────────
GEMINI_MODEL = "google/antigravity-gemini-3-flash"
GEMINI_PROMPT = (
    "Classify this Reddit post's sentiment about stocks/markets as bullish, "
    "bearish, or neutral. Return ONLY valid JSON with no markdown formatting: "
    '{"label": 0|1|2, "score": -1.0 to 1.0, "confidence": "high"|"medium"|"low"}\n\n'
    "Where:\n"
    "  label: 0=bearish, 1=neutral, 2=bullish\n"
    "  score: -1.0 (extreme bearish) to +1.0 (extreme bullish)\n"
    "  confidence: how certain you are\n\n"
    "Post text:\n"
)


def _connect_distilled() -> sqlite3.Connection:
    """Open the distilled DB."""
    conn = sqlite3.connect(DISTILLED_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _update_label(
    cursor: sqlite3.Cursor,
    post_id: str,
    label: int,
    score_continuous: float,
    confidence: str,
    labeled_by: str,
) -> None:
    """Write a label row for this labeler.

    reddit_labels is keyed by (id, labeled_by), so each labeler inserts its
    own row instead of overwriting another labeler's (or the 'unlabeled'
    marker that gemini_labeler.py relies on).
    """
    cursor.execute(
        """
        INSERT OR IGNORE INTO reddit_labels (
            id, subreddit, category, title, selftext, combined_text,
            score, upvote_ratio, num_comments, created_utc,
            label, score_continuous, confidence, labeled_by, labeled_at, batch_id
        )
        SELECT
            id, subreddit, category, title, selftext, combined_text,
            score, upvote_ratio, num_comments, created_utc,
            ?, ?, ?, ?, CURRENT_TIMESTAMP, batch_id
        FROM reddit_labels
        WHERE id = ? AND labeled_by = 'unlabeled'
        """,
        (label, score_continuous, confidence, labeled_by, post_id),
    )


def _parse_gemini_response(raw: str) -> Optional[Dict]:
    """Parse Gemini JSON response into label/score/confidence."""
    try:
        # Strip markdown code fences if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            lines = lines[1:]  # drop opening ```json or ```
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)

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
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"Failed to parse Gemini response: {e} | raw: {raw[:200]}")
        return None


def label_auto(batch_file: str, source: str = "auto") -> Dict:
    """
    Label a batch using RedditSentimentEngine (VADER + slang).

    Returns summary dict.
    """
    # Import the engine
    sys.path.insert(0, os.path.dirname(__file__))
    from reddit_sentiment import RedditSentimentEngine

    engine = RedditSentimentEngine()

    # Read batch
    posts = []
    with open(batch_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                posts.append(json.loads(line))

    logger.info(f"Labeling {len(posts)} posts from {batch_file} (mode=auto)")

    conn = _connect_distilled()
    cursor = conn.cursor()

    labeled = 0
    errors = 0
    for post in posts:
        try:
            result = engine.score(post["text"])
            _update_label(
                cursor,
                post_id=post["id"],
                label=result["label"],
                score_continuous=result["compound"],
                confidence=result["confidence"],
                labeled_by=source,
            )
            labeled += 1
        except Exception as e:
            logger.error(f"Error labeling {post['id']}: {e}")
            errors += 1

    conn.commit()
    conn.close()

    summary = {"mode": "auto", "source": source, "labeled": labeled, "errors": errors}
    logger.info(f"Auto-label complete: {summary}")
    return summary


def label_gemini(batch_file: str, source: str = "gemini") -> Dict:
    """
    Label a batch using Gemini API via Antigravity.

    Returns summary dict.
    """
    try:
        import openai
    except ImportError:
        logger.error("openai package not installed. Run: pip install openai")
        sys.exit(1)

    # Check for Antigravity API key
    api_key = os.environ.get("ANTIGRAVITY_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get(
        "ANTIGRAVITY_BASE_URL", "https://api.antigravity.dev/v1"
    )

    if not api_key:
        logger.error(
            "No API key found. Set ANTIGRAVITY_API_KEY or OPENAI_API_KEY env var."
        )
        sys.exit(1)

    client = openai.OpenAI(api_key=api_key, base_url=base_url)

    # Read batch
    posts = []
    with open(batch_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                posts.append(json.loads(line))

    logger.info(f"Labeling {len(posts)} posts from {batch_file} (mode=gemini)")

    conn = _connect_distilled()
    cursor = conn.cursor()

    labeled = 0
    errors = 0
    for i, post in enumerate(posts):
        try:
            response = client.chat.completions.create(
                model=GEMINI_MODEL,
                messages=[
                    {"role": "system", "content": "You are a financial sentiment analyzer."},
                    {"role": "user", "content": GEMINI_PROMPT + post["text"][:1000]},
                ],
                temperature=0.0,
                max_tokens=200,
            )
            raw = response.choices[0].message.content.strip()
            parsed = _parse_gemini_response(raw)

            if parsed:
                _update_label(
                    cursor,
                    post_id=post["id"],
                    label=parsed["label"],
                    score_continuous=parsed["score"],
                    confidence=parsed["confidence"],
                    labeled_by=source,
                )
                labeled += 1
            else:
                errors += 1

            # Rate limit: 1 req/sec
            if (i + 1) % 10 == 0:
                logger.info(f"  Progress: {i + 1}/{len(posts)}")
            time.sleep(1.0)

        except Exception as e:
            logger.error(f"Gemini error for {post['id']}: {e}")
            errors += 1
            time.sleep(2.0)  # back off on error

    conn.commit()
    conn.close()

    summary = {"mode": "gemini", "source": source, "labeled": labeled, "errors": errors}
    logger.info(f"Gemini-label complete: {summary}")
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Label a Reddit sentiment batch"
    )
    parser.add_argument(
        "--batch", required=True,
        help="Path to batch JSONL file (e.g. data/reddit_batches/batch_001.jsonl)"
    )
    parser.add_argument(
        "--mode", choices=["auto", "gemini"], default="auto",
        help="Labeling mode: auto (VADER+slang) or gemini (Antigravity API)"
    )
    parser.add_argument(
        "--source", default=None,
        help="Labeler name (default: mode name)"
    )
    args = parser.parse_args()

    if not os.path.exists(args.batch):
        logger.error(f"Batch file not found: {args.batch}")
        sys.exit(1)

    source = args.source or args.mode

    if args.mode == "auto":
        result = label_auto(args.batch, source=source)
    elif args.mode == "gemini":
        result = label_gemini(args.batch, source=source)
    else:
        parser.print_help()
        return

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
