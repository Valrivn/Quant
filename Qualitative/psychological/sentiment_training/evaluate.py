"""
Evaluation harness for the dual-head FinBERT sentiment model.

Computes:
  - macro-F1 (pooled)
  - per-source F1
  - confusion matrix
  - Spearman correlation vs continuous scores
  - Comparison vs baselines (zero-shot finbert, majority class)
  - PASS/FAIL per bar from config/weights_sentiment_bars.yaml
"""

import os
import json
import sqlite3
import logging
from typing import Dict, List, Tuple, Optional
from collections import Counter

import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader
from scipy import stats
from sklearn.metrics import (
    f1_score, confusion_matrix, classification_report,
    accuracy_score,
)
import yaml

from .model import SentimentDualHead

logger = logging.getLogger(__name__)

LABEL_NAMES = {0: "negative", 1: "neutral", 2: "positive"}
# PhraseBank excluded from eval (contamination — ProsusAI trained on it)
EVAL_SOURCES = {"fiqa", "semeval", "stocktwits", "hand_graded"}


class EvalDataset(Dataset):
    """Dataset for evaluation."""

    def __init__(self, db_path: str, split: str, tokenizer, max_len: int = 128):
        self.tokenizer = tokenizer
        self.max_len = max_len

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        # Exclude phrasebank from eval
        cursor.execute(
            "SELECT text, label, score, source FROM sentiment_training "
            "WHERE split = ? AND source != 'phrasebank'",
            (split,)
        )
        rows = cursor.fetchall()
        conn.close()

        self.texts = [r[0] for r in rows]
        self.labels = [r[1] for r in rows]
        self.scores = [r[2] for r in rows]
        self.sources = [r[3] for r in rows]

        logger.info(f"Eval: loaded {len(self.texts)} rows, split={split} (excl phrasebank)")

    def __len__(self):
        return len(self.texts)

    def __getitem__(self, idx):
        text = self.texts[idx]
        encoding = self.tokenizer(
            text, max_length=self.max_len, padding="max_length",
            truncation=True, return_tensors="pt",
        )
        return {
            "input_ids": encoding["input_ids"].squeeze(0),
            "attention_mask": encoding["attention_mask"].squeeze(0),
            "label": self.labels[idx],
            "score": self.scores[idx],
            "source": self.sources[idx],
            "text": text,
        }


def _load_bars(bars_path: str = "config/weights_sentiment_bars.yaml") -> Dict:
    """Load acceptance bars from YAML."""
    with open(bars_path, "r") as f:
        return yaml.safe_load(f)


def _zero_shot_finbert_predict(texts: List[str], tokenizer, device, model_name="ProsusAI/finbert"):
    """Zero-shot FinBERT: use ProsusAI/finbert directly (3-class)."""
    from transformers import AutoModelForSequenceClassification
    zs_model = AutoModelForSequenceClassification.from_pretrained(model_name)
    zs_model.to(device)
    zs_model.eval()

    preds = []
    for text in texts:
        enc = tokenizer(text, return_tensors="pt", truncation=True, max_length=128, padding=True)
        enc = {k: v.to(device) for k, v in enc.items()}
        with torch.no_grad():
            logits = zs_model(**enc).logits
        # FinBERT: 0=positive, 1=negative, 2=neutral -> remap
        # ProsusAI/finbert label mapping
        pred = logits.argmax(dim=-1).item()
        # Remap: 0(positive)->2, 1(negative)->0, 2(neutral)->1
        remap = {0: 2, 1: 0, 2: 1}
        preds.append(remap.get(pred, pred))

    del zs_model
    torch.cuda.empty_cache() if torch.cuda.is_available() else None
    return preds


def evaluate_model(
    model_path: str,
    db_path: str = "data/sentiment_training.db",
    split: str = "locked_test",
    max_len: int = 128,
    batch_size: int = 32,
    bars_path: str = "config/weights_sentiment_bars.yaml",
    compute_baselines: bool = True,
) -> Dict:
    """
    Full evaluation of a trained model.

    Returns dict with all metrics and PASS/FAIL per bar.
    """
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # Load checkpoint
    from transformers import AutoTokenizer
    checkpoint = torch.load(model_path, map_location=device, weights_only=False)
    config = checkpoint.get("config", {})
    model_name = config.get("model_name", "ProsusAI/finbert")

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = SentimentDualHead(model_name=model_name)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.to(device)
    model.eval()

    # Dataset
    dataset = EvalDataset(db_path, split, tokenizer, max_len)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=False, num_workers=0)

    # Inference
    all_preds = []
    all_labels = []
    all_scores = []
    all_sources = []

    with torch.no_grad():
        for batch in loader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)

            class_logits, pred_scores = model(input_ids, attention_mask)
            preds = class_logits.argmax(dim=-1).cpu().tolist()

            all_preds.extend(preds)
            all_labels.extend(batch["label"])
            all_scores.extend(batch["score"])
            all_sources.extend(batch["source"])

    all_preds = np.array(all_preds)
    all_labels = np.array(all_labels)
    all_scores = np.array(all_scores, dtype=float)
    all_sources = np.array(all_sources)

    # ---- Core metrics ----
    macro_f1 = f1_score(all_labels, all_preds, average="macro")
    accuracy = accuracy_score(all_labels, all_preds)
    cm = confusion_matrix(all_labels, all_preds, labels=[0, 1, 2])

    # Spearman vs continuous scores
    spearman_corr, spearman_p = stats.spearmanr(all_scores, all_preds.astype(float))

    # Per-source F1
    per_source_f1 = {}
    per_source_count = {}
    for src in sorted(set(all_sources)):
        mask = all_sources == src
        if mask.sum() == 0:
            continue
        src_labels = all_labels[mask]
        src_preds = all_preds[mask]
        per_source_count[src] = int(mask.sum())
        if len(set(src_labels)) > 1:
            per_source_f1[src] = round(f1_score(src_labels, src_preds, average="macro"), 4)
        else:
            per_source_f1[src] = 0.0

    # ---- Baselines ----
    baselines = {}

    # Majority class
    majority_class = Counter(all_labels.tolist()).most_common(1)[0][0]
    majority_preds = [majority_class] * len(all_labels)
    baselines["majority_class"] = {
        "macro_f1": round(f1_score(all_labels, majority_preds, average="macro"), 4),
        "accuracy": round(accuracy_score(all_labels, majority_preds), 4),
    }

    if compute_baselines:
        # Zero-shot FinBERT
        try:
            texts = dataset.texts
            zs_preds = _zero_shot_finbert_predict(texts, tokenizer, device, model_name)
            baselines["zero_shot_finbert"] = {
                "macro_f1": round(f1_score(all_labels, zs_preds, average="macro"), 4),
                "accuracy": round(accuracy_score(all_labels, zs_preds), 4),
                "per_source_f1": {},
            }
            for src in sorted(set(all_sources)):
                mask = all_sources == src
                if mask.sum() > 0:
                    src_labels = all_labels[mask]
                    src_preds = np.array(zs_preds)[mask]
                    if len(set(src_labels)) > 1:
                        baselines["zero_shot_finbert"]["per_source_f1"][src] = round(
                            f1_score(src_labels, src_preds, average="macro"), 4
                        )
        except Exception as e:
            logger.warning(f"Zero-shot baseline failed: {e}")
            baselines["zero_shot_finbert"] = {"error": str(e)}

    # ---- Per-domain F1 (all eval sources) ----
    per_domain_f1 = per_source_f1

    # ---- Bar checks ----
    bars = _load_bars(bars_path)
    bar_results = {}

    # macro_f1_pooled_min
    bar_val = bars["scoring_quality"]["macro_f1_pooled_min"]
    bar_results["macro_f1_pooled_min"] = {
        "bar": bar_val, "actual": round(macro_f1, 4),
        "pass": macro_f1 >= bar_val,
    }

    # spearman_vs_human_min
    bar_val = bars["scoring_quality"]["spearman_vs_human_min"]
    bar_results["spearman_vs_human_min"] = {
        "bar": bar_val, "actual": round(spearman_corr, 4),
        "pass": spearman_corr >= bar_val,
    }

    # per_domain_floor
    bar_val = bars["scoring_quality"]["per_domain_floor"]
    domain_passes = {k: v >= bar_val for k, v in per_domain_f1.items()}
    all_domains_pass = all(domain_passes.values()) if domain_passes else False
    bar_results["per_domain_floor"] = {
        "bar": bar_val, "actual": per_domain_f1,
        "per_domain_pass": domain_passes, "all_pass": all_domains_pass,
    }

    # vram
    # (tracked during training, check if available)
    bar_val = bars["vram"]["peak_gb_max"]
    bar_results["peak_vram_gb_max"] = {"bar": bar_val, "note": "check vram_test.py"}

    overall_pass = all(
        r.get("pass", r.get("all_pass", False))
        for k, r in bar_results.items()
        if k != "peak_vram_gb_max"
    )

    # ---- Assemble results ----
    results = {
        "model_path": model_path,
        "split": split,
        "total_samples": len(all_labels),
        "macro_f1": round(macro_f1, 4),
        "accuracy": round(accuracy, 4),
        "spearman_corr": round(spearman_corr, 4),
        "spearman_p": round(spearman_p, 6),
        "confusion_matrix": cm.tolist(),
        "confusion_matrix_labels": ["negative", "neutral", "positive"],
        "per_source_f1": per_source_f1,
        "per_source_count": per_source_count,
        "per_domain_f1": per_domain_f1,
        "baselines": baselines,
        "bar_results": bar_results,
        "overall_pass": overall_pass,
        "report": classification_report(
            all_labels, all_preds,
            target_names=["negative", "neutral", "positive"],
            output_dict=True,
        ),
    }

    logger.info(f"Eval complete: macro_f1={macro_f1:.4f}, spearman={spearman_corr:.4f}")
    logger.info(f"Per-source F1: {per_source_f1}")
    logger.info(f"Overall PASS: {overall_pass}")

    return results


def save_results(results: Dict, output_path: str):
    """Save evaluation results to JSON."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info(f"Results saved to {output_path}")
