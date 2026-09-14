"""Diagnosis 2: WHY the big-pickle engine fails on real data.

1. Neutral-collapse check: engine prediction distribution vs gold per corpus.
2. Threshold sensitivity: does re-tuning the neutral bounds (fit on TRAIN)
   clear the 70% bar on the unseen val+locked_test set?
3. Majority-class baseline for framing.
4. Stocktwits deep-dive (closest domain to Reddit).
"""

import os
import sys
import sqlite3
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TRAINING_DIR = os.path.join(ROOT, "Qualitative", "psychological", "sentiment_training")
sys.path.insert(0, TRAINING_DIR)

from reddit_sentiment import RedditSentimentEngine

SENT_DB = os.path.join(ROOT, "data", "sentiment_training.db")
SOURCES = ("fiqa", "semeval", "stocktwits", "phrasebank")


def load_split(split_names):
    conn = sqlite3.connect(SENT_DB)
    q = ",".join("?" for _ in split_names)
    rows = conn.execute(
        f"SELECT text, label, source FROM sentiment_training WHERE split IN ({q}) AND source IN {SOURCES}",
        split_names,
    ).fetchall()
    conn.close()
    return rows


def engine_preds(engine, rows):
    return [(engine.score(r[0].strip())["label"] if r[0] and r[0].strip() else 1) for r in rows]


def acc(preds, golds):
    return sum(1 for p, g in zip(preds, golds) if p == g) / max(len(preds), 1)


def main():
    engine = RedditSentimentEngine()
    train = load_split(("train",))
    eval_rows = load_split(("val", "locked_test"))

    print("=" * 72)
    print(" DIAGNOSIS: ENGINE PREDICTIONS vs GOLD  (val+locked_test)")
    print("=" * 72)
    gold_global = Counter(r[1] for r in eval_rows)
    preds_global = Counter(engine_preds(engine, eval_rows))
    print(f"  gold dist : {dict(gold_global)}  (n={sum(gold_global.values())})")
    print(f"  engine dist: {dict(preds_global)}")
    print(f"  majority-baseline acc (predict gold mode): "
          f"{acc([gold_global.most_common(1)[0][0]]*len(eval_rows), [r[1] for r in eval_rows]):.4f}")
    for src in SOURCES:
        sub = [r for r in eval_rows if r[2] == src]
        g = Counter(r[1] for r in sub)
        m = Counter(engine_preds(engine, sub))
        print(f"  {src:<12} gold={dict(g)}  engine={dict(m)}  acc={acc(engine_preds(engine, sub), [r[1] for r in sub]):.4f}")

    print()
    print("=" * 72)
    print(" THRESHOLD SENSITIVITY  (neutral bounds fit on TRAIN, applied to val+locked_test)")
    print("=" * 72)
    # VADER compound space; try symmetric neutral bounds t in [0.05..0.45]
    best = (0.0, 0.0, 0.0)
    for t in [x / 100 for x in range(5, 46, 5)]:
        tr_preds = []
        for r in train:
            c = engine.score(r[0])["compound"] if r[0] and r[0].strip() else 0.0
            lab = 2 if c > t else (0 if c < -t else 1)
            tr_preds.append(lab)
        tr_acc = acc(tr_preds, [r[1] for r in train])
        if tr_acc > best[1]:
            best = (t, tr_acc, tr_acc)
    t, tr_acc, _ = best
    ev_preds = []
    for r in eval_rows:
        c = engine.score(r[0])["compound"] if r[0] and r[0].strip() else 0.0
        lab = 2 if c > t else (0 if c < -t else 1)
        ev_preds.append(lab)
    ev_acc = acc(ev_preds, [r[1] for r in eval_rows])
    print(f"  best neutral bound t={t:.2f}  train_acc={tr_acc:.4f}   ->   val_acc={ev_acc:.4f}  (bar=0.70)")
    print(f"  VERDICT: {'PASS' if ev_acc >= 0.70 else 'FAIL'}")

    print()
    print("=" * 72)
    print(" STOCKTWITS DEEP-DIVE (closest domain to short retail posts)")
    print("=" * 72)
    sw = [r for r in eval_rows if r[2] == "stocktwits"]
    g = Counter(r[1] for r in sw)
    m = Counter(engine_preds(engine, sw))
    print(f"  gold    : {dict(g)}  (binary: 0/2, no neutral class)")
    print(f"  engine  : {dict(m)}  <- neutral collapse is the failure mode")
    neut = m.get(1, 0)
    print(f"  neutral-collapse rate: {neut}/{len(sw)} = {neut / len(sw):.2%}")
    # binary-only comparison (map neutral-> nearest, i.e., ignore neutral misclass cost)
    bin_pairs = [(p, gr) for p, gr in zip(engine_preds(engine, sw), [r[1] for r in sw]) if p != 1]
    if bin_pairs:
        ba = acc([p for p, _ in bin_pairs], [g_ for _, g_ in bin_pairs])
        print(f"  accuracy excluding engine-neutral preds: {ba:.4f} (n={len(bin_pairs)})")
    else:
        print("  engine predicted neutral for every stocktwits row")

    print("=" * 72)


if __name__ == "__main__":
    main()