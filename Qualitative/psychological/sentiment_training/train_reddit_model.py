"""
Train the dual-head FinBERT model on Reddit-labeled data.

Usage:
    python train_reddit_model.py --epochs 5 --batch-size 32
    python train_reddit_model.py --fraction 0.5 --seed 42
"""

import os
import sys
import json
import sqlite3
import random
import math
import logging
import argparse
from typing import Dict, List, Tuple

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
from torch.optim import AdamW
from sklearn.metrics import (
    f1_score,
    classification_report,
    confusion_matrix,
    accuracy_score,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DISTILLED_DB = os.path.join(ROOT_DIR, "data", "reddit_distilled.db")
CHECKPOINT_DIR = os.path.join(ROOT_DIR, "models", "reddit_sentiment_v1")

LABEL_NAMES = {0: "bearish", 1: "neutral", 2: "bullish"}


# ── Dataset ──────────────────────────────────────────────────────────────
class RedditSentimentDataset(Dataset):
    """Dataset from the reddit_labels table (labeled rows only)."""

    def __init__(self, texts: List[str], labels: List[int], scores: List[float],
                 tokenizer, max_len: int = 128):
        self.texts = texts
        self.labels = labels
        self.scores = scores
        self.tokenizer = tokenizer
        self.max_len = max_len

    def __len__(self):
        return len(self.texts)

    def __getitem__(self, idx):
        text = self.texts[idx]
        encoding = self.tokenizer(
            text,
            max_length=self.max_len,
            padding="max_length",
            truncation=True,
            return_tensors="pt",
        )
        return {
            "input_ids": encoding["input_ids"].squeeze(0),
            "attention_mask": encoding["attention_mask"].squeeze(0),
            "label": torch.tensor(self.labels[idx], dtype=torch.long),
            "score": torch.tensor(self.scores[idx], dtype=torch.float),
        }


def load_labeled_data(db_path: str, min_len: int = 10) -> Tuple[List, List, List]:
    """Load labeled data from reddit_labels table."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT combined_text, label, score_continuous "
        "FROM reddit_labels "
        "WHERE label IS NOT NULL AND score_continuous IS NOT NULL "
        "AND length(combined_text) >= ?",
        (min_len,),
    )
    rows = cursor.fetchall()
    conn.close()

    texts = [r[0] for r in rows]
    labels = [r[1] for r in rows]
    scores = [r[2] for r in rows]

    logger.info(f"Loaded {len(texts)} labeled rows from {db_path}")
    return texts, labels, scores


def stratified_split(
    texts: List[str],
    labels: List[int],
    scores: List[float],
    train_ratio: float = 0.75,
    seed: int = 42,
) -> Tuple:
    """Stratified 75/25 train/test split."""
    # Group by label
    groups = {}
    for i, lbl in enumerate(labels):
        groups.setdefault(lbl, []).append(i)

    rng = random.Random(seed)
    train_indices = []
    test_indices = []

    for lbl, indices in groups.items():
        rng.shuffle(indices)
        n_train = max(1, int(len(indices) * train_ratio))
        train_indices.extend(indices[:n_train])
        test_indices.extend(indices[n_train:])

    rng.shuffle(train_indices)
    rng.shuffle(test_indices)

    def _select(indices):
        return (
            [texts[i] for i in indices],
            [labels[i] for i in indices],
            [scores[i] for i in indices],
        )

    train_data = _select(train_indices)
    test_data = _select(test_indices)

    logger.info(
        f"Split: {len(train_indices)} train, {len(test_indices)} test "
        f"(ratio={train_ratio})"
    )
    return train_data, test_data


def make_balanced_sampler(labels: List[int]) -> WeightedRandomSampler:
    """Class-balanced sampler."""
    counts = [0, 0, 0]
    for l in labels:
        counts[l] += 1
    total = len(labels)
    weights = [total / (3 * max(c, 1)) for c in counts]
    sample_weights = [weights[l] for l in labels]
    return WeightedRandomSampler(sample_weights, num_samples=total, replacement=True)


def train(
    db_path: str = DISTILLED_DB,
    checkpoint_dir: str = CHECKPOINT_DIR,
    model_name: str = "ProsusAI/finbert",
    epochs: int = 5,
    batch_size: int = 32,
    max_len: int = 128,
    lr_backbone: float = 2e-5,
    lr_heads: float = 2e-6,
    warmup_ratio: float = 0.1,
    patience: int = 2,
    huber_delta: float = 0.1,
    score_loss_weight: float = 0.5,
    fraction: float = 1.0,
    seed: int = 42,
    freeze_backbone: bool = False,
) -> Dict:
    """
    Full training loop for Reddit sentiment model.
    Returns metrics dict.
    """
    # Seed
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info(f"Training on {device}, seed={seed}")

    # Load data
    texts, labels, scores = load_labeled_data(db_path)
    if len(texts) < 100:
        raise ValueError(f"Need >= 100 labeled rows, got {len(texts)}")

    # Curriculum subsample
    if fraction < 1.0:
        n = max(100, int(len(texts) * fraction))
        rng = random.Random(seed)
        indices = rng.sample(range(len(texts)), n)
        texts = [texts[i] for i in indices]
        labels = [labels[i] for i in indices]
        scores = [scores[i] for i in indices]

    # Split
    (train_texts, train_labels, train_scores), (test_texts, test_labels, test_scores) = (
        stratified_split(texts, labels, scores, train_ratio=0.75, seed=seed)
    )

    # Tokenizer & model
    from transformers import AutoTokenizer
    sys.path.insert(0, os.path.dirname(__file__))
    from model import SentimentDualHead

    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = SentimentDualHead(model_name=model_name, freeze_backbone=freeze_backbone)
    model.to(device)

    # Datasets & loaders
    train_dataset = RedditSentimentDataset(
        train_texts, train_labels, train_scores, tokenizer, max_len
    )
    test_dataset = RedditSentimentDataset(
        test_texts, test_labels, test_scores, tokenizer, max_len
    )

    train_sampler = make_balanced_sampler(train_labels)
    train_loader = DataLoader(
        train_dataset, batch_size=batch_size, sampler=train_sampler,
        num_workers=0, pin_memory=True,
    )
    test_loader = DataLoader(
        test_dataset, batch_size=batch_size, shuffle=False,
        num_workers=0, pin_memory=True,
    )

    # Optimizer
    backbone_params = list(model.backbone.parameters())
    head_params = list(model.class_head.parameters()) + list(model.score_head.parameters())
    optimizer = AdamW(
        [
            {"params": backbone_params, "lr": lr_backbone},
            {"params": head_params, "lr": lr_heads},
        ],
        weight_decay=0.01,
    )

    total_steps = len(train_loader) * epochs
    warmup_steps = int(total_steps * warmup_ratio)

    def lr_lambda(step):
        if step < warmup_steps:
            return float(step) / float(max(1, warmup_steps))
        progress = float(step - warmup_steps) / float(max(1, total_steps - warmup_steps))
        return max(0.0, 0.5 * (1.0 + math.cos(math.pi * progress)))

    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)

    # Losses
    ce_loss = nn.CrossEntropyLoss()
    huber_loss = nn.HuberLoss(delta=huber_delta)

    use_amp = device.type == "cuda"
    scaler = torch.amp.GradScaler(enabled=use_amp)

    # Training loop
    os.makedirs(checkpoint_dir, exist_ok=True)
    best_f1 = 0.0
    best_ckpt = None
    no_improve = 0
    metrics_history = []

    for epoch in range(1, epochs + 1):
        model.train()
        epoch_loss = 0.0
        n_batches = 0

        for batch in train_loader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            labels_b = batch["label"].to(device)
            scores_b = batch["score"].to(device)

            optimizer.zero_grad()
            with torch.amp.autocast(device_type="cuda", enabled=use_amp):
                class_logits, pred_scores = model(input_ids, attention_mask)
                loss_cls = ce_loss(class_logits, labels_b)
                loss_score = huber_loss(pred_scores, scores_b)
                loss = loss_cls + score_loss_weight * loss_score

            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(optimizer)
            scaler.update()
            scheduler.step()

            epoch_loss += loss.item()
            n_batches += 1

        avg_train_loss = epoch_loss / max(n_batches, 1)

        # Evaluate on test set
        model.eval()
        all_preds = []
        all_labels = []
        test_loss = 0.0
        test_batches = 0

        with torch.no_grad():
            for batch in test_loader:
                input_ids = batch["input_ids"].to(device)
                attention_mask = batch["attention_mask"].to(device)
                labels_b = batch["label"].to(device)
                scores_b = batch["score"].to(device)

                with torch.amp.autocast(device_type="cuda", enabled=use_amp):
                    class_logits, pred_scores = model(input_ids, attention_mask)
                    loss_cls = ce_loss(class_logits, labels_b)
                    loss_score = huber_loss(pred_scores, scores_b)
                    loss = loss_cls + score_loss_weight * loss_score

                test_loss += loss.item()
                test_batches += 1

                preds = class_logits.argmax(dim=-1).cpu().tolist()
                all_preds.extend(preds)
                all_labels.extend(labels_b.cpu().tolist())

        avg_test_loss = test_loss / max(test_batches, 1)
        test_f1 = f1_score(all_labels, all_preds, average="macro")
        test_acc = accuracy_score(all_labels, all_preds)

        epoch_metrics = {
            "epoch": epoch,
            "train_loss": round(avg_train_loss, 4),
            "test_loss": round(avg_test_loss, 4),
            "test_f1": round(test_f1, 4),
            "test_acc": round(test_acc, 4),
        }
        metrics_history.append(epoch_metrics)
        logger.info(
            f"Epoch {epoch}: train_loss={avg_train_loss:.4f}, "
            f"test_loss={avg_test_loss:.4f}, test_F1={test_f1:.4f}, "
            f"test_acc={test_acc:.4f}"
        )

        # Early stopping on F1
        if test_f1 > best_f1:
            best_f1 = test_f1
            no_improve = 0
            best_ckpt = os.path.join(
                checkpoint_dir, f"best_reddit_seed{seed}_e{epoch}.pt"
            )
            torch.save(
                {
                    "model_state_dict": model.state_dict(),
                    "test_f1": best_f1,
                    "test_acc": test_acc,
                    "epoch": epoch,
                    "seed": seed,
                    "fraction": fraction,
                    "config": {
                        "model_name": model_name,
                        "max_len": max_len,
                        "lr_backbone": lr_backbone,
                        "lr_heads": lr_heads,
                        "huber_delta": huber_delta,
                        "score_loss_weight": score_loss_weight,
                        "freeze_backbone": freeze_backbone,
                    },
                },
                best_ckpt,
            )
            logger.info(f"  Saved best model: {best_ckpt}")
        else:
            no_improve += 1
            if no_improve >= patience:
                logger.info(f"  Early stopping after {patience} epochs without improvement.")
                break

    # Final evaluation with best model
    if best_ckpt and os.path.exists(best_ckpt):
        checkpoint = torch.load(best_ckpt, map_location=device, weights_only=False)
        model.load_state_dict(checkpoint["model_state_dict"])

    model.eval()
    all_preds = []
    all_labels = []
    with torch.no_grad():
        for batch in test_loader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            labels_b = batch["label"].to(device)

            class_logits, _ = model(input_ids, attention_mask)
            preds = class_logits.argmax(dim=-1).cpu().tolist()
            all_preds.extend(preds)
            all_labels.extend(labels_b.cpu().tolist())

    # Final metrics
    final_acc = accuracy_score(all_labels, all_preds)
    final_f1 = f1_score(all_labels, all_preds, average="macro")
    cm = confusion_matrix(all_labels, all_preds)
    report = classification_report(
        all_labels, all_preds,
        target_names=[LABEL_NAMES[i] for i in sorted(set(all_labels))],
        output_dict=True,
    )

    # Save metrics
    final_metrics = {
        "accuracy": round(final_acc, 4),
        "macro_f1": round(final_f1, 4),
        "confusion_matrix": cm.tolist(),
        "classification_report": report,
        "best_checkpoint": best_ckpt,
        "train_size": len(train_labels),
        "test_size": len(test_labels),
        "label_distribution_train": {
            LABEL_NAMES[i]: train_labels.count(i) for i in sorted(set(train_labels))
        },
        "label_distribution_test": {
            LABEL_NAMES[i]: test_labels.count(i) for i in sorted(set(test_labels))
        },
        "history": metrics_history,
    }

    metrics_path = os.path.join(checkpoint_dir, f"reddit_metrics_seed{seed}.json")
    with open(metrics_path, "w") as f:
        json.dump(final_metrics, f, indent=2)

    # Print summary
    print(f"\n{'='*60}")
    print(f"  Reddit Sentiment Model — Final Results")
    print(f"{'='*60}")
    print(f"  Accuracy:       {final_acc:.4f}")
    print(f"  Macro F1:       {final_f1:.4f}")
    print(f"  Confusion mat:  {cm.tolist()}")
    print(f"  Train size:     {len(train_labels):,}")
    print(f"  Test size:      {len(test_labels):,}")
    print(f"  Checkpoint:     {best_ckpt}")
    print(f"  Metrics:        {metrics_path}")
    print(f"{'='*60}\n")

    return final_metrics


def main():
    parser = argparse.ArgumentParser(description="Train Reddit sentiment model")
    parser.add_argument("--db", default=DISTILLED_DB, help="Path to distilled DB")
    parser.add_argument("--checkpoint-dir", default=CHECKPOINT_DIR, help="Save dir")
    parser.add_argument("--model-name", default="ProsusAI/finbert", help="HF model")
    parser.add_argument("--epochs", type=int, default=5, help="Max epochs")
    parser.add_argument("--batch-size", type=int, default=32, help="Batch size")
    parser.add_argument("--max-len", type=int, default=128, help="Max token length")
    parser.add_argument("--lr-backbone", type=float, default=2e-5, help="LR for backbone")
    parser.add_argument("--lr-heads", type=float, default=2e-6, help="LR for heads")
    parser.add_argument("--patience", type=int, default=2, help="Early stopping patience")
    parser.add_argument("--fraction", type=float, default=1.0, help="Data fraction")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--freeze-backbone", action="store_true", help="Freeze backbone")
    args = parser.parse_args()

    result = train(
        db_path=args.db,
        checkpoint_dir=args.checkpoint_dir,
        model_name=args.model_name,
        epochs=args.epochs,
        batch_size=args.batch_size,
        max_len=args.max_len,
        lr_backbone=args.lr_backbone,
        lr_heads=args.lr_heads,
        patience=args.patience,
        fraction=args.fraction,
        seed=args.seed,
        freeze_backbone=args.freeze_backbone,
    )

    print(json.dumps({k: v for k, v in result.items() if k != "classification_report"}, indent=2))


if __name__ == "__main__":
    main()
