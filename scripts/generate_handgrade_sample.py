"""
Generate a 250-sample hand-grading CSV for CEO manual sentiment labeling.

Stratification (val+test splits only, no train leakage):
  fiqa        — 70 samples: 25 neg / 25 neu / 20 pos
  semeval     — 70 samples: 25 neg / 25 neu / 20 pos
  stocktwits  — 60 samples: 30 neg / 30 pos (no neutral class exists)
  phrasebank  — 50 samples: 15 neg / 20 neu / 15 pos  (calibration anchor baseline)

Output: data/handgrade_samples_v1.csv (UTF-8-sig for Sheets compat)
"""

import csv
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("data/sentiment_training.db")
OUT_PATH = Path("data/handgrade_samples_v1.csv")

# source → {label: count}  (label: 0=neg, 1=neu, 2=pos)
STRATA = {
    "fiqa":        {0: 25, 1: 25, 2: 20},
    "semeval":     {0: 25, 1: 25, 2: 20},
    "stocktwits":  {0: 30, 1: 0,  2: 30},   # no neutral in stocktwits
    "phrasebank":  {0: 15, 1: 20, 2: 15},
}

LABEL_MAP = {0: "neg", 1: "neu", 2: "pos"}


def sample_source(cursor, source: str, counts: dict) -> list[dict]:
    """Draw stratified random samples from val+test for one source."""
    rows = []
    for label, n in counts.items():
        if n == 0:
            continue
        cursor.execute(
            """
            SELECT id, text, label, score, source
            FROM sentiment_training
            WHERE source = ? AND label = ?
              AND split IN ('val', 'test')
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (source, label, n),
        )
        fetched = cursor.fetchall()
        if len(fetched) < n:
            print(f"  WARNING: {source} label={LABEL_MAP[label]} requested {n} "
                  f"but only {len(fetched)} available in val+test")
        for row in fetched:
            rows.append({
                "id": row[0],
                "text": (row[1] or "")[:512],
                "label": row[2],
                "score": row[3],
                "source": row[4],
            })
    return rows


def main():
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    all_rows = []
    summary_lines = []

    for source, counts in STRATA.items():
        total_requested = sum(counts.values())
        rows = sample_source(cursor, source, counts)
        all_rows.extend(rows)

        # Build per-label breakdown
        breakdown = {}
        for r in rows:
            lbl = LABEL_MAP[r["label"]]
            breakdown[lbl] = breakdown.get(lbl, 0) + 1

        summary_lines.append({
            "source": source,
            "requested": total_requested,
            "got": len(rows),
            "neg": breakdown.get("neg", 0),
            "neu": breakdown.get("neu", 0),
            "pos": breakdown.get("pos", 0),
        })

    conn.close()

    # Sort: by source, then by current_score ascending
    source_order = {s: i for i, s in enumerate(STRATA)}
    all_rows.sort(key=lambda r: (source_order.get(r["source"], 99), r["score"]))

    # Write CSV
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "id", "source", "original_text",
            "current_label", "current_score",
            "ceo_label", "ceo_score", "confidence", "notes",
        ])
        for r in all_rows:
            writer.writerow([
                r["id"],
                r["source"],
                r["text"],
                r["label"],
                r["score"],
                "",  # ceo_label
                "",  # ceo_score
                "",  # confidence
                "",  # notes
            ])

    # Print summary
    total = len(all_rows)
    print(f"\n{'='*60}")
    print(f"  HAND-GRADE SAMPLE SUMMARY  —  {total} rows")
    print(f"{'='*60}")
    print(f"{'Source':<14} {'Request':>8} {'Got':>5}  {'Neg':>4} {'Neu':>4} {'Pos':>4}")
    print(f"{'-'*14} {'-'*8} {'-'*5}  {'-'*4} {'-'*4} {'-'*4}")
    for s in summary_lines:
        print(f"{s['source']:<14} {s['requested']:>8} {s['got']:>5}  "
              f"{s['neg']:>4} {s['neu']:>4} {s['pos']:>4}")
    print(f"{'-'*14} {'-'*8} {'-'*5}  {'-'*4} {'-'*4} {'-'*4}")
    all_neg = sum(s["neg"] for s in summary_lines)
    all_neu = sum(s["neu"] for s in summary_lines)
    all_pos = sum(s["pos"] for s in summary_lines)
    print(f"{'TOTAL':<14} {sum(s['requested'] for s in summary_lines):>8} "
          f"{total:>5}  {all_neg:>4} {all_neu:>4} {all_pos:>4}")
    print(f"\nOutput: {OUT_PATH}")
    print(f"Notes: stocktwits has no neutral class (0 neg/pos only).")
    print(f"       phrasebank is calibration anchor baseline (ProsusAI-trained).")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
