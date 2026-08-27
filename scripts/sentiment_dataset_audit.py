"""Phase 0 — Dataset audit for sentiment model (B-20260824-001).

Downloads and inventories all candidate training corpora:
  1. Financial PhraseBank (HuggingFace zip)
  2. FiQA 2018 (HuggingFace / fallback note)
  3. SemEval-2017 Task 5 (HuggingFace / fallback note)
  4. StockTwits labeled (HuggingFace / fallback note)

Per-corpus output: row count, label distribution, text length stats,
license, availability status.

Output: data/sentiment_corpus_inventory.json

Usage:
    python scripts/sentiment_dataset_audit.py
"""

import json
import os
import re
import statistics
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

DATA_DIR = Path("data")
OUTPUT = DATA_DIR / "sentiment_corpus_inventory.json"

# ---------------------------------------------------------------------------
# PhraseBank — direct zip download (datasets 5.x dropped script support)
# ---------------------------------------------------------------------------

PHRASEBANK_ZIP_URL = (
    "https://huggingface.co/datasets/financial_phrasebank/resolve/main/"
    "data/FinancialPhraseBank-v1.0.zip"
)
PHRASEBANK_LICENSE = "CC BY-NC-SA 4.0"
LABEL_MAP = {"negative": 0, "neutral": 1, "positive": 2}


def _download_phrasebank_zip(dest: Path) -> Path:
    """Download the PhraseBank zip if not already cached."""
    zip_path = dest / "FinancialPhraseBank-v1.0.zip"
    if zip_path.exists() and zip_path.stat().st_size > 100_000:
        print(f"  [cached] {zip_path}")
        return zip_path
    print(f"  Downloading PhraseBank zip from HuggingFace ...")
    resp = requests.get(PHRASEBANK_ZIP_URL, stream=True, timeout=120)
    resp.raise_for_status()
    with open(zip_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    print(f"  Downloaded {zip_path.stat().st_size / 1e6:.1f} MB")
    return zip_path


def _parse_phrasebank_file(lines: List[str]) -> Dict[str, Any]:
    """Parse lines of the form ``text@label`` and return stats."""
    texts, labels = [], []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Format: "text @label" or "text@label"
        # The label is the last token after the final @
        idx = line.rfind("@")
        if idx < 0:
            continue
        text = line[:idx].strip()
        label_str = line[idx + 1:].strip().lower()
        texts.append(text)
        labels.append(label_str)

    lengths = [len(t.split()) for t in texts]
    label_counts: Dict[str, int] = {}
    for lb in labels:
        label_counts[lb] = label_counts.get(lb, 0) + 1

    return {
        "row_count": len(texts),
        "label_distribution": label_counts,
        "text_length_words": {
            "mean": round(statistics.mean(lengths), 1) if lengths else 0,
            "median": round(statistics.median(lengths), 1) if lengths else 0,
            "min": min(lengths) if lengths else 0,
            "max": max(lengths) if lengths else 0,
            "stdev": round(statistics.stdev(lengths), 1) if len(lengths) > 1 else 0,
        },
        "sample_texts": texts[:3] if texts else [],
    }


def audit_phrasebank() -> Dict[str, Any]:
    """Download, parse, and inventory Financial PhraseBank."""
    print("\n=== Financial PhraseBank ===")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_dir = DATA_DIR / "phrasebank_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {
        "name": "Financial PhraseBank",
        "license": PHRASEBANK_LICENSE,
        "source": PHRASEBANK_ZIP_URL,
        "availability": "unknown",
        "subsets": {},
    }

    try:
        zip_path = _download_phrasebank_zip(cache_dir)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            target_files = [
                n for n in names if n.endswith(".txt") and "Sentences_" in n
            ]
            for tf in target_files:
                subset_name = Path(tf).stem  # e.g. Sentences_75Agree
                with zf.open(tf) as fh:
                    raw = fh.read().decode("latin-1")
                lines = raw.splitlines()
                stats = _parse_phrasebank_file(lines)
                result["subsets"][subset_name] = stats
                print(
                    f"  {subset_name}: {stats['row_count']} sentences, "
                    f"labels={stats['label_distribution']}"
                )
        result["availability"] = "downloaded"
    except Exception as exc:
        result["availability"] = f"download_failed: {exc}"
        print(f"  Download/parse failed: {exc}")

    return result


# ---------------------------------------------------------------------------
# HuggingFace datasets loader — tries datasets library first, falls back
# to direct notes
# ---------------------------------------------------------------------------


def _try_load_hf_dataset(
    name: str, subset: str = None, split: str = "train"
) -> Optional[Any]:
    """Attempt to load via datasets 5.x API. Returns rows or None."""
    try:
        from datasets import load_dataset

        kwargs: Dict[str, Any] = {}
        if subset:
            kwargs["name"] = subset
        ds = load_dataset(name, split=split, trust_remote_code=True, **kwargs)
        return ds
    except Exception as exc:
        print(f"  datasets.load_dataset failed for {name}: {exc}")
        return None


def _hf_stats(rows, text_key: str = "text", label_key: str = "label") -> Dict[str, Any]:
    """Compute inventory stats from HuggingFace dataset rows."""
    texts = []
    labels = []
    for row in rows:
        t = row.get(text_key, "") or ""
        texts.append(t)
        lb = row.get(label_key, None)
        labels.append(str(lb) if lb is not None else "unknown")

    lengths = [len(t.split()) for t in texts]
    label_counts: Dict[str, int] = {}
    for lb in labels:
        label_counts[lb] = label_counts.get(lb, 0) + 1

    return {
        "row_count": len(texts),
        "label_distribution": label_counts,
        "text_length_words": {
            "mean": round(statistics.mean(lengths), 1) if lengths else 0,
            "median": round(statistics.median(lengths), 1) if lengths else 0,
            "min": min(lengths) if lengths else 0,
            "max": max(lengths) if lengths else 0,
            "stdev": round(statistics.stdev(lengths), 1) if len(lengths) > 1 else 0,
        },
        "sample_texts": texts[:3],
    }


# ---------------------------------------------------------------------------
# FiQA 2018 sentiment
# ---------------------------------------------------------------------------


def audit_fiqa() -> Dict[str, Any]:
    """Audit the FiQA 2018 sentiment dataset."""
    print("\n=== FiQA 2018 Sentiment ===")
    result: Dict[str, Any] = {
        "name": "FiQA 2018 Sentiment",
        "license": "CC BY 4.0",
        "source": "https://github.com/yfpeng/fiqa_sentiment",
        "availability": "unknown",
    }

    # Try HuggingFace first
    for repo in [
        "pauri32/fiqa-2018",
        "malteads/fiqa-sentiment",
        "gsarti/fiqa-sentiment",
    ]:
        ds = _try_load_hf_dataset(repo)
        if ds is not None:
            result["source"] = f"huggingface:{repo}"
            # Try common text/label field names
            rows_list = list(ds)
            # Auto-detect field names
            if rows_list:
                sample = rows_list[0]
                keys = list(sample.keys())
                text_key = next(
                    (k for k in keys if "text" in k.lower() or "sentence" in k.lower()),
                    keys[0],
                )
                label_key = next(
                    (
                        k
                        for k in keys
                        if "label" in k.lower() or "sentiment" in k.lower()
                    ),
                    keys[-1],
                )
                result["fields_detected"] = keys
                result["text_field"] = text_key
                result["label_field"] = label_key
                stats = _hf_stats(rows_list, text_key, label_key)
                result.update(stats)
                result["availability"] = "downloaded"
                print(f"  Found at {repo}: {stats['row_count']} rows")
                return result

    # Fallback: note manual download needed
    result["availability"] = "manual_download_required"
    result["manual_url"] = "https://github.com/yfpeng/fiqa_sentiment"
    result["note"] = (
        "FiQA sentiment data needs manual download from the GitHub repo. "
        "It contains financial opinions with continuous sentiment scores "
        "(-1 to 1). The HuggingFace Hub repos tried were not available or "
        "had incompatible schemas."
    )
    print("  Not available via HF — manual download required")
    return result


# ---------------------------------------------------------------------------
# SemEval-2017 Task 5
# ---------------------------------------------------------------------------


def audit_semeval() -> Dict[str, Any]:
    """Audit SemEval-2017 Task 5 (financial microblog/news headlines)."""
    print("\n=== SemEval-2017 Task 5 ===")
    result: Dict[str, Any] = {
        "name": "SemEval-2017 Task 5",
        "license": "Research use (SemEval terms)",
        "source": "semeval-2017-task5-official",
        "availability": "unknown",
    }

    for repo in [
        "sem_eval_2017_task_5",
        "nataliecheff/semeval2017_task5",
        "jiehang/semeval-2017-task5",
    ]:
        ds = _try_load_hf_dataset(repo)
        if ds is not None:
            result["source"] = f"huggingface:{repo}"
            rows_list = list(ds)
            if rows_list:
                keys = list(rows_list[0].keys())
                text_key = next(
                    (k for k in keys if "text" in k.lower() or "headline" in k.lower()),
                    keys[0],
                )
                label_key = next(
                    (k for k in keys if "label" in k.lower() or "sentiment" in k.lower() or "score" in k.lower()),
                    keys[-1],
                )
                result["fields_detected"] = keys
                result["text_field"] = text_key
                result["label_field"] = label_key
                stats = _hf_stats(rows_list, text_key, label_key)
                result.update(stats)
                result["availability"] = "downloaded"
                print(f"  Found at {repo}: {stats['row_count']} rows")
                return result

    result["availability"] = "manual_download_required"
    result["manual_url"] = "https://alt.qcri.org/semeval2017/task5/"
    result["note"] = (
        "SemEval-2017 Task 5 data requires manual download from the "
        "official SemEval site or shared task organizers. Contains "
        "financial microblog/headline sentiment with -1 to 1 scores."
    )
    print("  Not available via HF — manual download required")
    return result


# ---------------------------------------------------------------------------
# StockTwits Labeled Sentiment
# ---------------------------------------------------------------------------


def audit_stocktwits() -> Dict[str, Any]:
    """Audit StockTwits labeled sentiment datasets on HuggingFace."""
    print("\n=== StockTwits Labeled Sentiment ===")
    result: Dict[str, Any] = {
        "name": "StockTwits Labeled Sentiment",
        "license": "Community / varies",
        "source": "huggingface search",
        "availability": "unknown",
    }

    for repo in [
        "markusbayer/stocktwits-sentiment",
        "mickey0524/stocktwits-sentiment-dataset",
        "sujay-27/stocktwits-sentiment",
    ]:
        ds = _try_load_hf_dataset(repo)
        if ds is not None:
            result["source"] = f"huggingface:{repo}"
            rows_list = list(ds)
            if rows_list:
                keys = list(rows_list[0].keys())
                text_key = next(
                    (k for k in keys if "text" in k.lower() or "message" in k.lower()),
                    keys[0],
                )
                label_key = next(
                    (k for k in keys if "label" in k.lower() or "sentiment" in k.lower() or "bull" in k.lower()),
                    keys[-1],
                )
                result["fields_detected"] = keys
                result["text_field"] = text_key
                result["label_field"] = label_key
                stats = _hf_stats(rows_list, text_key, label_key)
                result.update(stats)
                result["availability"] = "downloaded"
                print(f"  Found at {repo}: {stats['row_count']} rows")
                return result

    result["availability"] = "manual_search_required"
    result["note"] = (
        "StockTwits sentiment datasets vary in quality on HuggingFace. "
        "The labeled data has bull/bear tags. Some repos are private or "
        "have been removed. Manual verification needed."
    )
    print("  No confirmed HF repo — manual search required")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    print("=" * 60)
    print("SENTIMENT DATASET AUDIT — Phase 0 (B-20260824-001)")
    print("=" * 60)

    inventory: Dict[str, Any] = {
        "audit_date": __import__("datetime").datetime.now().isoformat(),
        "brief": "B-20260824-001",
        "purpose": "Phase 0 corpus inventory for FinBERT dual-head sentiment model",
    }

    corpora = [
        ("phrasebank", audit_phrasebank),
        ("fiqa_2018", audit_fiqa),
        ("semeval_2017_task5", audit_semeval),
        ("stocktwits", audit_stocktwits),
    ]

    for key, fn in corpora:
        try:
            inventory[key] = fn()
        except Exception as exc:
            inventory[key] = {"name": key, "availability": f"error: {exc}"}
            print(f"  ERROR auditing {key}: {exc}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(inventory, f, indent=2, default=str)

    print(f"\n{'=' * 60}")
    print(f"Inventory written to {OUTPUT}")
    print(f"{'=' * 60}")

    # Summary table
    print(f"\n{'Corpus':<30} {'Rows':>8} {'Availability':<30}")
    print("-" * 70)
    for key, _ in corpora:
        entry = inventory.get(key, {})
        rows = entry.get("row_count", "n/a")
        avail = entry.get("availability", "unknown")
        print(f"{entry.get('name', key):<30} {str(rows):>8} {avail:<30}")


if __name__ == "__main__":
    main()
