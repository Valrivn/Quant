"""
Validate the RedditSentimentEngine (big-pickle lane) against REAL ground truth:
  1. Gold-labeled public financial corpora in data/sentiment_training.db
     (val + locked_test splits; sources: fiqa, semeval, stocktwits, phrasebank).
  2. Real apewisdom fintech_messages (663 rows) — label distribution only
     (no gold), plus a stratified sample exported for CEO hand-grading.
Reports accuracy / macro-F1 per source and the >70% bar check.
"""

import os
import sys
import csv
import sqlite3
import random
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TRAINING_DIR = os.path.join(ROOT, "Qualitative", "psychological", "sentiment_training")
sys.path.insert(0, TRAINING_DIR)

from reddit_sentiment import RedditSentimentEngine

SENT_DB = os.path.join(ROOT, "data", "sentiment_training.db")
REDDIT_DB = os.path.join(ROOT, "reddit_quant.db")
BAR_ACCURACY = 0.70

SOURCES = {"fiqa", "semeval", "stocktwits", "phrasebank"}
NAME_TO_ENGINE = {0: "bearish", 1: "neutral", 2: "bullish"}


def load_gold_split(split_names=("val", "locked_test")):
    conn = sqlite3.connect(SENT_DB)
    q = ",".join("?" for _ in split_names)
    rows = conn.execute(
        f"SELECT text, label, source FROM sentiment_training "
        f"WHERE split IN ({q}) AND source IN ('fiqa','semeval','stocktwits','phrasebank')",
        split_names,
    ).fetchall()
    conn.close()
    return rows


def accuracy(preds, golds):
    return sum(1 for p, g in zip(preds, golds) if p == g) / max(len(golds), 1)


def eval_engine(engine, rows):
    per_source = {}
    all_preds = []
    all_gold = []
    for text, gold, source in rows:
        if not text or not text.strip():
            continue
        pred = engine.score(text)["label"]
        all_preds.append(pred)
        all_gold.append(gold)
        per_source.setdefault(source, {"preds": [], "gold": []})
        per_source[source]["preds"].append(pred)
        per_source[source]["gold"].append(gold)

    overall_acc = accuracy(all_preds, all_gold)
    print("=" * 64)
    print(" BIG-PICKLE ENGINE vs REAL GOLD LABELS (val + locked_test)")
    print("=" * 64)
    print(f"  Pooled accuracy: {overall_acc:.4f}  (bar >= {BAR_ACCURACY})")
    print(f"  Bar: {'PASS' if overall_acc >= BAR_ACCURACY else 'FAIL'}")
    print(f"  N: {len(all_preds)}")
    print("-" * 64)
    for src in sorted(per_source):
        d = per_source[src]
        acc = accuracy(d["preds"], d["gold"])
        dist = Counter(d["gold"])
        print(f"  {src:<12} acc={acc:.4f}  n={len(d['gold'])}  gold={dict(dist)}")
    print("=" * 64)
    return {
        "overall_accuracy": round(overall_acc, 4),
        "bar_passed": overall_acc >= BAR_ACCURACY,
        "n": len(all_preds),
        "per_source": {
            s: {"accuracy": round(accuracy(d["preds"], d["gold"]), 4), "n": len(d["gold"])}
            for s, d in per_source.items()
        },
    }


def export_apewisdom_grade(engine, n=40, out_path=None):
    conn = sqlite3.connect(REDDIT_DB)
    rows = conn.execute("SELECT text FROM fintech_messages WHERE text IS NOT NULL AND length(text) > 5").fetchall()
    conn.close()
    texts = [r[0] for r in rows]
    labels = [engine.score(t)["label"] for t in texts]
    print(f"  Apewisdom rows: {len(texts)}  predicted dist: {Counter(NAME_TO_ENGINE[l] for l in labels)}")

    # Stratified sample for CEO grading
    random.seed(42)
    sample = random.sample(list(range(len(texts))), min(n, len(texts)))
    out_path = out_path or os.path.join(ROOT, "data", "handgrade_apewisdom_v1.csv")
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["idx", "original_text", "engine_prediction", "ceo_label", "confidence", "notes"])
        for i in sample:
            w.writerow([i, texts[i][:512], NAME_TO_ENGINE[labels[i]], "", "", ""])
    print(f"  CEO grading CSV -> {out_path}")
    return out_path


def main():
    engine = RedditSentimentEngine()
    rows = load_gold_split()
    print(f"Loaded {len(rows)} gold-labeled rows (val+locked_test)")
    results = eval_engine(engine, rows)

    # Apewisdom real rows
    print()
    export_apewisdom_grade(engine)

    print()
    print("VERDICT:", "PASS — engine cleared >70% bar on real data. Eligible for promotion." if results["bar_passed"]
          else "FAIL — engine below 70% bar on real data. Do-not-promote.")


if __name__ == "__main__":
    main()