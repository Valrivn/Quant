#!/usr/bin/env python3
"""Validate FinBERT sentiment grading against the Financial PhraseBank.

The Financial PhraseBank (Malo et al., 2014, Aalto University) is the canonical
financial-sentiment ground truth: 4,845 financial news sentences each annotated
by 16 annotators with finance backgrounds (incl. PhDs). The split files select
sentences by annotator agreement:

  Sentences_AllAgree.txt  - all 16 annotators agree (highest confidence)
  Sentences_75Agree.txt   - >= 75% agreement
  Sentences_66Agree.txt   - >= 66% agreement
  Sentences_50Agree.txt   - majority agreement

FinBERT (ProsusAI) is fine-tuned on this same dataset, so this script measures
agreement between the model and the PhD-labelled ground truth.

Usage:
    python scripts/validate_finbert.py [--split all|75|66|50] [--limit N]
"""

import argparse
import os
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "Qualitative"))

os.environ["DISCOVERY_LIVE"] = "1"

SPLIT_FILES = {
    "all": "Sentences_AllAgree.txt",
    "75": "Sentences_75Agree.txt",
    "66": "Sentences_66Agree.txt",
    "50": "Sentences_50Agree.txt",
}

PHBANK_URL = (
    "https://huggingface.co/datasets/takala/financial_phrasebank/"
    "resolve/main/data/FinancialPhraseBank-v1.0.zip"
)


def download_phrasebank(cache_path: Path) -> Path:
    """Download the Financial PhraseBank zip into the opencode temp cache."""
    if cache_path.exists():
        return cache_path
    import requests

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(PHBANK_URL, timeout=120)
    resp.raise_for_status()
    cache_path.write_bytes(resp.content)
    return cache_path


def load_sentences(split: str, cache_path: Path):
    """Load (text, label) pairs from the chosen agreement split.

    File lines are ``text@label`` (label in positive/neutral/negative).
    """
    zip_path = download_phrasebank(cache_path)
    member = "FinancialPhraseBank-v1.0/" + SPLIT_FILES[split]
    with zipfile.ZipFile(zip_path) as zf:
        raw = zf.read(member).decode("utf-8", errors="replace")
    pairs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "@" not in line:
            continue
        text, label = line.rsplit("@", 1)
        text = text.strip()
        label = label.strip().lower()
        if text and label in ("positive", "neutral", "negative"):
            pairs.append((text, label))
    return pairs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", default="all", choices=sorted(SPLIT_FILES))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--cache",
        default=r"C:\Users\Hayden\AppData\Local\Temp\opencode\fpb.zip",
    )
    args = parser.parse_args()

    from psychological.scrapers.finbert_sentiment import grade_batch

    pairs = load_sentences(args.split, Path(args.cache))
    if args.limit:
        pairs = pairs[: args.limit]
    if not pairs:
        print("[!] No sentences loaded.")
        sys.exit(1)

    print(f"[*] Financial PhraseBank split '{args.split}': {len(pairs)} sentences")
    t0 = time.time()
    results = grade_batch([t for t, _ in pairs])
    elapsed = time.time() - t0

    confusion = Counter()
    correct = 0
    graded = 0
    per_class = {label: Counter() for label in ("positive", "neutral", "negative")}
    for (text, truth), res in zip(pairs, results):
        if res is None:
            continue
        graded += 1
        pred = res["label"]
        confusion[(truth, pred)] += 1
        per_class[truth][pred] += 1
        if pred == truth:
            correct += 1

    accuracy = correct / graded if graded else 0.0
    print(f"\n{'=' * 50}")
    print(f" FINBERT vs PHD-ANNOTATED GROUND TRUTH (split={args.split})")
    print(f"{'=' * 50}")
    print(f"  Sentences graded : {graded:,} / {len(pairs):,}")
    print(f"  Accuracy        : {accuracy:.1%}")
    print(f"  Time            : {elapsed:.1f}s ({len(pairs) / max(elapsed, 1e-6):.0f} sentences/s)")
    print(f"  GPU active      : model runs on CUDA when available")

    print(f"\n  Confusion matrix (truth -> predicted):")
    for truth in ("positive", "neutral", "negative"):
        row = "    {:>8} ->".format(truth)
        for pred in ("positive", "neutral", "negative"):
            n = confusion[(truth, pred)]
            total = sum(confusion[(truth, p)] for p in ("positive", "neutral", "negative"))
            pct = n / total if total else 0.0
            row += f" {pred:>8}:{n:>4} ({pct:.0%})"
        print(row)

    print(f"\n  Per-class recall (truth class correctly predicted):")
    for truth in ("positive", "neutral", "negative"):
        total = sum(per_class[truth].values())
        hit = per_class[truth][truth]
        print(f"    {truth:>8}: {hit:>4} / {total:>4} = {hit / total if total else 0:.1%}")


if __name__ == "__main__":
    main()