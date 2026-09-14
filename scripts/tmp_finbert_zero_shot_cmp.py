"""FinBERT zero-shot comparison on the same real gold rows the RedditSentimentEngine failed.

Uses ProsusAI/finbert (classification head) on val + locked_test splits from
data/sentiment_training.db across fiqa/semeval/stocktwits/phrasebank.
Remap: finbert 0=positive,1=negative,2=neutral -> 2/0/1.
"""

import os
import sqlite3
import sys
import time
from collections import Counter

import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SENT_DB = os.path.join(ROOT, "data", "sentiment_training.db")
MODEL_NAME = "ProsusAI/finbert"
REMAP = {0: 2, 1: 0, 2: 1}  # pos->bullish, neg->bearish, neu->neutral


def load_gold_rows():
    conn = sqlite3.connect(SENT_DB)
    rows = conn.execute(
        "SELECT text, label, source FROM sentiment_training "
        "WHERE split IN ('val','locked_test') "
        "AND source IN ('fiqa','semeval','stocktwits','phrasebank')"
    ).fetchall()
    conn.close()
    return rows


def main():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tok = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME).to(device).eval()

    rows = load_gold_rows()
    t0 = time.time()
    per = {}
    all_preds, all_gold = [], []
    B = 64
    with torch.no_grad():
        for i in range(0, len(rows), B):
            batch = rows[i:i + B]
            texts = [r[0] for r in batch]
            enc = tok(texts, padding=True, truncation=True, max_length=128, return_tensors="pt")
            enc = {k: v.to(device) for k, v in enc.items()}
            logits = model(**enc).logits
            preds = logits.argmax(dim=-1).cpu().tolist()
            for (_, gold, src), p in zip(batch, preds):
                p = REMAP.get(p, p)
                all_preds.append(p)
                all_gold.append(gold)
                per.setdefault(src, {"p": [], "g": []})
                per[src]["p"].append(p)
                per[src]["g"].append(gold)

    acc = sum(1 for p, g in zip(all_preds, all_gold) if p == g) / max(len(all_preds), 1)
    print("=" * 64)
    print(" FINBERT ZERO-SHOT vs REAL GOLD LABELS (same rows as engine validation)")
    print("=" * 64)
    print(f"  Pooled accuracy: {acc:.4f}  N={len(all_preds)}  elapsed={time.time()-t0:.0f}s")
    for src in sorted(per):
        d = per[src]
        a = sum(1 for p, g in zip(d["p"], d["g"]) if p == g) / max(len(d["p"]), 1)
        print(f"  {src:<12} acc={a:.4f}  n={len(d['g'])}  gold={dict(Counter(d['g']))}")
    print("=" * 64)


if __name__ == "__main__":
    main()