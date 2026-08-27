"""
Data Layer for FinBERT Sentiment Model.
Handles unified schema creation, downloading corpora, deduplication, normalization,
stratified splits, and SQLite storage in WAL mode.
"""

import os
import sqlite3
import hashlib
import zipfile
import urllib.request
import random
import logging
from typing import Dict, List, Tuple, Any, Optional

logger = logging.getLogger(__name__)

# Source priority for deduplication: higher number = higher quality/preferred
SOURCE_PRIORITY = {
    'hand_graded': 5,
    'phrasebank': 4,
    'fiqa': 3,
    'semeval': 2,
    'stocktwits': 1
}


def normalize_text(text: str) -> str:
    """
    Normalizes text: lowercase, strip excess whitespace, truncate to 512 chars.
    """
    if not text:
        return ""
    text = text.lower().strip()
    text = " ".join(text.split())
    return text[:512]


def download_phrasebank(cache_dir: str) -> List[Dict[str, Any]]:
    """
    Downloads and parses Financial PhraseBank.
    """
    os.makedirs(cache_dir, exist_ok=True)
    zip_path = os.path.join(cache_dir, "FinancialPhraseBank-v1.0.zip")
    url = "https://huggingface.co/datasets/financial_phrasebank/resolve/main/data/FinancialPhraseBank-v1.0.zip"

    if not os.path.exists(zip_path):
        logger.info(f"Downloading PhraseBank from {url}")
        urllib.request.urlretrieve(url, zip_path)

    results = []
    with zipfile.ZipFile(zip_path, "r") as z:
        # Search for Sentences_50Agree.txt in the zip
        target_name = None
        for name in z.namelist():
            if "Sentences_50Agree.txt" in name:
                target_name = name
                break

        if not target_name:
            raise FileNotFoundError("Sentences_50Agree.txt not found in PhraseBank zip.")

        with z.open(target_name) as f:
            for line_bytes in f:
                line = line_bytes.decode("latin-1").strip()
                if not line:
                    continue
                if "@" not in line:
                    continue
                parts = line.rsplit("@", 1)
                if len(parts) != 2:
                    continue
                text, label_str = parts[0].strip(), parts[1].strip().lower()

                # Map labels: negative->0, neutral->1, positive->2
                if label_str == "negative":
                    label_int = 0
                    score = -1.0
                elif label_str == "neutral":
                    label_int = 1
                    score = 0.0
                elif label_str == "positive":
                    label_int = 2
                    score = 1.0
                else:
                    continue

                results.append({
                    "text": text,
                    "label": label_int,
                    "score": score,
                    "source": "phrasebank",
                    "orig_score": score,
                    "orig_label": label_str
                })

    logger.info(f"Loaded {len(results)} rows from PhraseBank.")
    return results


def download_fiqa(cache_dir: str) -> List[Dict[str, Any]]:
    """
    Loads FiQA 2018 Sentiment dataset from Hugging Face.
    """
    results = []
    try:
        from datasets import load_dataset
        logger.info("Attempting to load FiQA from HF (pauri32/fiqa-2018)...")
        ds = load_dataset("pauri32/fiqa-2018")
        splits = ["train", "validation", "test"]
    except Exception as e:
        logger.warning(f"Could not load FiQA via datasets: {e}. Trying fallback...")
        ds = None

    if ds is not None:
        for split in splits:
            if split not in ds:
                continue
            for row in ds[split]:
                text = row.get("sentence") or ""
                score_val = row.get("sentiment_score")
                if score_val is None or text == "":
                    continue

                score_float = float(score_val)
                # Map to 3-class via thresholds: score < -0.2 -> neg, score > 0.2 -> pos, else neutral
                if score_float < -0.2:
                    label_int = 0
                elif score_float > 0.2:
                    label_int = 2
                else:
                    label_int = 1

                results.append({
                    "text": text,
                    "label": label_int,
                    "score": score_float,
                    "source": "fiqa",
                    "orig_score": score_float,
                    "orig_label": str(score_val)
                })
    else:
        # Static mock/dummy data for testing or offline safety
        logger.warning("Using synthetic/mock fallback for FiQA.")
        dummy_data = [
            ("Company X reports record high profits, stock jumps.", 0.8, "0.8"),
            ("Firm suffers massive decline in Q3 revenue.", -0.6, "-0.6"),
            ("Corporation X launches new project in Europe.", 0.0, "0.0"),
        ]
        for text, score_float, orig_lbl in dummy_data:
            if score_float < -0.2:
                label_int = 0
            elif score_float > 0.2:
                label_int = 2
            else:
                label_int = 1
            results.append({
                "text": text,
                "label": label_int,
                "score": score_float,
                "source": "fiqa",
                "orig_score": score_float,
                "orig_label": orig_lbl
            })

    logger.info(f"Loaded {len(results)} rows from FiQA.")
    return results


def download_semeval(cache_dir: str) -> List[Dict[str, Any]]:
    """
    Loads SemEval sentiment dataset from Hugging Face.
    """
    results = []
    try:
        from datasets import load_dataset
        logger.info("Attempting to load SemEval from HF (maxmoynan/SemEval2017-Task4aEnglish)...")
        ds = load_dataset("maxmoynan/SemEval2017-Task4aEnglish")
        splits = ["train", "test", "development"]
    except Exception as e:
        logger.warning(f"Could not load SemEval via datasets: {e}. Trying fallback...")
        ds = None

    if ds is not None:
        for split in splits:
            if split not in ds:
                continue
            for row in ds[split]:
                text = row.get("tweet") or ""
                sentiment = row.get("sentiment")
                if sentiment is None or text == "":
                    continue

                label_int = int(sentiment)
                # Map label to continuous score: negative(0) -> -1.0, neutral(1) -> 0.0, positive(2) -> 1.0
                if label_int == 0:
                    score = -1.0
                elif label_int == 1:
                    score = 0.0
                elif label_int == 2:
                    score = 1.0
                else:
                    continue

                results.append({
                    "text": text,
                    "label": label_int,
                    "score": score,
                    "source": "semeval",
                    "orig_score": None,
                    "orig_label": str(sentiment)
                })
    else:
        logger.warning("Using synthetic/mock fallback for SemEval.")
        dummy_data = [
            ("Twitter reaction to company merger is extremely positive", 2),
            ("Disappointed in the new product release, very laggy", 0),
            ("We are watching the market developments closely", 1)
        ]
        for text, label_int in dummy_data:
            if label_int == 0:
                score = -1.0
            elif label_int == 1:
                score = 0.0
            elif label_int == 2:
                score = 1.0
            else:
                continue
            results.append({
                "text": text,
                "label": label_int,
                "score": score,
                "source": "semeval",
                "orig_score": None,
                "orig_label": str(label_int)
            })

    logger.info(f"Loaded {len(results)} rows from SemEval.")
    return results


def download_stocktwits(cache_dir: str) -> List[Dict[str, Any]]:
    """
    Loads StockTwits dataset from Hugging Face.
    """
    results = []
    try:
        from datasets import load_dataset
        logger.info("Attempting to load StockTwits from HF (jinlibao/stocktwits_volatility)...")
        ds = load_dataset("jinlibao/stocktwits_volatility")
        splits = ["train"]
    except Exception as e:
        logger.warning(f"Could not load StockTwits via datasets: {e}. Trying fallback...")
        ds = None

    if ds is not None:
        for split in splits:
            if split not in ds:
                continue
            for row in ds[split]:
                text = row.get("stocktwits") or ""
                # remove prefix "Stocktwits: " if present
                if text.startswith("Stocktwits: "):
                    text = text[len("Stocktwits: "):]
                label_val = row.get("label")
                if label_val is None or text == "":
                    continue

                # 0 indicates rise (bullish/positive -> 2), 1 indicates fall (bearish/negative -> 0)
                label_val = int(label_val)
                if label_val == 0:
                    label_int = 2
                    score = 1.0
                elif label_val == 1:
                    label_int = 0
                    score = -1.0
                else:
                    continue

                results.append({
                    "text": text,
                    "label": label_int,
                    "score": score,
                    "source": "stocktwits",
                    "orig_score": score,
                    "orig_label": str(label_val)
                })
    else:
        logger.warning("Using synthetic/mock fallback for StockTwits.")
        dummy_data = [
            ("Bullish on $AAPL, heading to the moon!", 0),
            ("Bearish on $TSLA, options putting hard", 1)
        ]
        for text, label_val in dummy_data:
            if label_val == 0:
                label_int = 2
                score = 1.0
            elif label_val == 1:
                label_int = 0
                score = -1.0
            else:
                continue
            results.append({
                "text": text,
                "label": label_int,
                "score": score,
                "source": "stocktwits",
                "orig_score": score,
                "orig_label": str(label_val)
            })

    logger.info(f"Loaded {len(results)} rows from StockTwits.")
    return results


def build_dataset(db_path: str, cache_dir: str = "data/sentiment_training_cache") -> Dict[str, Any]:
    """
    Orchestrates downloading, normalizing, deduplicating, splitting, and storing the dataset.
    """
    logger.info("Starting dataset build orchestration.")
    
    # 1. Download datasets
    phrasebank_data = download_phrasebank(cache_dir)
    fiqa_data = download_fiqa(cache_dir)
    semeval_data = download_semeval(cache_dir)
    stocktwits_data = download_stocktwits(cache_dir)

    all_data = phrasebank_data + fiqa_data + semeval_data + stocktwits_data
    
    # 2. Normalize and Deduplicate
    deduped_data: Dict[str, Dict[str, Any]] = {}
    
    for item in all_data:
        norm_text = normalize_text(item["text"])
        if not norm_text:
            continue
        
        text_hash = hashlib.sha256(norm_text.encode('utf-8')).hexdigest()
        item_source = item["source"]
        
        if text_hash in deduped_data:
            existing_item = deduped_data[text_hash]
            existing_source = existing_item["source"]
            
            # Keep higher quality source (PhraseBank > FiQA > SemEval > StockTwits)
            if SOURCE_PRIORITY.get(item_source, 0) > SOURCE_PRIORITY.get(existing_source, 0):
                item_copy = item.copy()
                item_copy["text"] = norm_text
                item_copy["text_hash"] = text_hash
                deduped_data[text_hash] = item_copy
        else:
            item_copy = item.copy()
            item_copy["text"] = norm_text
            item_copy["text_hash"] = text_hash
            deduped_data[text_hash] = item_copy

    deduped_list = list(deduped_data.values())

    # 3. Stratified split (70/15/15 + 10% of test to locked_test)
    # Group by (source, label)
    groups: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    for item in deduped_list:
        key = (item["source"], item["label"])
        groups.setdefault(key, []).append(item)

    final_split_data: List[Dict[str, Any]] = []

    for key, group in groups.items():
        source, label = key
        # Shuffle with fixed seed for reproducibility
        rng = random.Random(42)
        rng.shuffle(group)

        n = len(group)
        n_train = int(round(n * 0.70))
        n_val = int(round(n * 0.15))
        n_test_total = n - n_train - n_val

        train_set = group[:n_train]
        val_set = group[n_train:n_train + n_val]
        test_total_set = group[n_train + n_val:]

        # Move 10% of test to locked_test
        n_locked = int(round(len(test_total_set) * 0.10))
        locked_test_set = test_total_set[:n_locked]
        test_set = test_total_set[n_locked:]

        for item in train_set:
            item["split"] = "train"
            final_split_data.append(item)
        for item in val_set:
            item["split"] = "val"
            final_split_data.append(item)
        for item in test_set:
            item["split"] = "test"
            final_split_data.append(item)
        for item in locked_test_set:
            item["split"] = "locked_test"
            final_split_data.append(item)

    # 4. Store in SQLite in WAL mode
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sentiment_training (
            id INTEGER PRIMARY KEY,
            text TEXT NOT NULL,
            label INTEGER NOT NULL,
            score REAL,
            source TEXT NOT NULL,
            orig_score REAL,
            orig_label TEXT,
            text_hash TEXT NOT NULL,
            split TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()

    # Clear existing entries before reloading
    conn.execute("DELETE FROM sentiment_training")
    conn.commit()

    conn.executemany("""
        INSERT INTO sentiment_training (text, label, score, source, orig_score, orig_label, text_hash, split)
        VALUES (:text, :label, :score, :source, :orig_score, :orig_label, :text_hash, :split)
    """, final_split_data)
    conn.commit()

    # Compute statistics
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM sentiment_training")
    total_count = cursor.fetchone()[0]

    cursor.execute("SELECT split, COUNT(*) FROM sentiment_training GROUP BY split")
    split_counts = dict(cursor.fetchall())

    cursor.execute("SELECT source, COUNT(*) FROM sentiment_training GROUP BY source")
    source_counts = dict(cursor.fetchall())

    cursor.execute("SELECT label, COUNT(*) FROM sentiment_training GROUP BY label")
    label_counts = dict(cursor.fetchall())

    conn.close()

    stats = {
        "total_rows": total_count,
        "splits": split_counts,
        "sources": source_counts,
        "labels": label_counts
    }
    logger.info(f"Dataset build completed. Stats: {stats}")
    return stats
