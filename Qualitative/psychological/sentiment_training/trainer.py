"""
Training loop for the dual-head FinBERT sentiment model.

Features:
  - Differential lr: backbone (2e-5) vs heads (2e-6)
  - Warmup 10% + cosine decay
  - Multi-task loss: CrossEntropy(class) + 0.5 * HuberLoss(score, delta=0.1)
  - Class-balanced sampling for classification, score-quintile-balanced for regression
  - fp16 via torch.cuda.amp
  - Early stopping on val macro-F1, patience=2
  - Curriculum: accept fraction parameter (0.1, 0.25, 0.5, 1.0)
  - Seed sweep: {13, 42, 2026}
  - Logging: train_loss, val_loss, val_macro_F1, per_domain_f1, VRAM peak
"""

import os
import json
import sqlite3
import logging
import random
import math
from typing import Dict, List, Tuple, Optional

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
from torch.optim import AdamW
from torch.optim.lr_scheduler import CosineAnnealingLR
from sklearn.metrics import f1_score, classification_report

from .model import SentimentDualHead

logger = logging.getLogger(__name__)

# Label names for reporting
LABEL_NAMES = {0: "negative", 1: "neutral", 2: "positive"}


class SentimentDataset(Dataset):
    """Dataset loading from SQLite DB."""

    def __init__(self, db_path: str, split: str, tokenizer, max_len: int = 128,
                 fraction: float = 1.0):
        self.tokenizer = tokenizer
        self.max_len = max_len

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT text, label, score, source FROM sentiment_training WHERE split = ?",
            (split,)
        )
        rows = cursor.fetchall()
        conn.close()

        # Curriculum: subsample to fraction
        if fraction < 1.0:
            n = max(1, int(len(rows) * fraction))
            rng = random.Random(42)
            rows = rng.sample(rows, n)

        self.texts = [r[0] for r in rows]
        self.labels = [r[1] for r in rows]
        self.scores = [r[2] for r in rows]
        self.sources = [r[3] for r in rows]

        logger.info(f"Loaded {len(self.texts)} rows for split={split}, fraction={fraction}")

    def __len__(self):
        return len(self.texts)

    def __getitem__(self, idx):
        text = self.texts[idx]
        label = self.labels[idx]
        score = self.scores[idx]
        source = self.sources[idx]

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
            "label": torch.tensor(label, dtype=torch.long),
            "score": torch.tensor(score, dtype=torch.float),
            "source": source,
        }


def _make_class_balanced_sampler(labels: List[int]) -> WeightedRandomSampler:
    """Weighted sampler to handle class imbalance."""
    class_counts = [0, 0, 0]
    for l in labels:
        class_counts[l] += 1
    total = len(labels)
    class_weights = [total / (3 * max(c, 1)) for c in class_counts]
    sample_weights = [class_weights[l] for l in labels]
    return WeightedRandomSampler(sample_weights, num_samples=total, replacement=True)


def _make_score_quintile_balanced_sampler(scores: List[float]) -> WeightedRandomSampler:
    """Weighted sampler balanced by score quintiles for regression head."""
    if not scores:
        return None
    n = len(scores)
    quintile_size = max(1, n // 5)
    sorted_indices = np.argsort(scores)
    quintile_labels = np.zeros(n, dtype=int)
    for q in range(5):
        start = q * quintile_size
        end = start + quintile_size if q < 4 else n
        quintile_labels[sorted_indices[start:end]] = q

    quintile_counts = np.bincount(quintile_labels, minlength=5)
    quintile_weights = n / (5 * np.maximum(quintile_counts, 1))
    sample_weights = [quintile_weights[q] for q in quintile_labels]
    return WeightedRandomSampler(sample_weights, num_samples=n, replacement=True)


def _get_param_groups(model: SentimentDualHead, lr_backbone: float, lr_heads: float):
    """Separate param groups: backbone vs heads."""
    backbone_params = list(model.backbone.parameters())
    head_params = list(model.class_head.parameters()) + list(model.score_head.parameters())
    return [
        {"params": backbone_params, "lr": lr_backbone},
        {"params": head_params, "lr": lr_heads},
    ]


def train(
    db_path: str = "data/sentiment_training.db",
    checkpoint_dir: str = "models/sentiment_v1",
    seed: int = 42,
    epochs: int = 5,
    batch_size: int = 32,
    fraction: float = 1.0,
    max_len: int = 128,
    lr_backbone: float = 2e-5,
    lr_heads: float = 2e-6,
    warmup_ratio: float = 0.1,
    patience: int = 2,
    huber_delta: float = 0.1,
    score_loss_weight: float = 0.5,
    model_name: str = "ProsusAI/finbert",
    freeze_backbone: bool = False,
) -> Dict:
    """
    Full training loop.

    Returns dict with keys:
        best_checkpoint_path, metrics_history, best_val_f1, vram_peak_gb, config
    """
    # ---- Seed ----
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info(f"Training on {device}, seed={seed}, fraction={fraction}")

    # ---- Tokenizer & model ----
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = SentimentDualHead(model_name=model_name, freeze_backbone=freeze_backbone)
    model.to(device)

    # ---- Datasets ----
    train_dataset = SentimentDataset(db_path, "train", tokenizer, max_len, fraction)
    val_dataset = SentimentDataset(db_path, "val", tokenizer, max_len, fraction=1.0)

    # Use class-balanced sampling (classification head drives batching)
    train_sampler = _make_class_balanced_sampler(train_dataset.labels)
    train_loader = DataLoader(
        train_dataset, batch_size=batch_size, sampler=train_sampler,
        num_workers=0, pin_memory=True,
    )
    val_loader = DataLoader(
        val_dataset, batch_size=batch_size, shuffle=False,
        num_workers=0, pin_memory=True,
    )

    # ---- Optimizer & scheduler ----
    param_groups = _get_param_groups(model, lr_backbone, lr_heads)
    optimizer = AdamW(param_groups, weight_decay=0.01)

    total_steps = len(train_loader) * epochs
    warmup_steps = int(total_steps * warmup_ratio)

    def lr_lambda(current_step):
        if current_step < warmup_steps:
            return float(current_step) / float(max(1, warmup_steps))
        progress = float(current_step - warmup_steps) / float(max(1, total_steps - warmup_steps))
        return max(0.0, 0.5 * (1.0 + math.cos(math.pi * progress)))

    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)

    # ---- Losses ----
    ce_loss = nn.CrossEntropyLoss()
    huber_loss = nn.HuberLoss(delta=huber_delta)

    # ---- Mixed precision ----
    use_amp = device.type == "cuda"
    scaler = torch.amp.GradScaler(enabled=use_amp)

    # ---- Training loop ----
    os.makedirs(checkpoint_dir, exist_ok=True)
    best_val_f1 = 0.0
    best_checkpoint_path = None
    no_improve = 0
    vram_peak = 0.0
    metrics_history = []

    for epoch in range(1, epochs + 1):
        model.train()
        epoch_loss = 0.0
        n_batches = 0

        for batch in train_loader:
            input_ids = batch["input_ids"].to(device)
            attention_mask = batch["attention_mask"].to(device)
            labels = batch["label"].to(device)
            scores = batch["score"].to(device)

            optimizer.zero_grad()

            with torch.amp.autocast(device_type="cuda", enabled=use_amp):
                class_logits, pred_scores = model(input_ids, attention_mask)
                loss_cls = ce_loss(class_logits, labels)
                loss_score = huber_loss(pred_scores, scores)
                loss = loss_cls + score_loss_weight * loss_score

            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(optimizer)
            scaler.update()
            scheduler.step()

            epoch_loss += loss.item()
            n_batches += 1

            # Track VRAM peak
            if device.type == "cuda":
                current_peak = torch.cuda.max_memory_allocated(device) / (1024 ** 3)
                if current_peak > vram_peak:
                    vram_peak = current_peak

        avg_train_loss = epoch_loss / max(n_batches, 1)

        # ---- Validation ----
        model.eval()
        val_loss = 0.0
        val_batches = 0
        all_preds = []
        all_labels = []
        per_domain_preds = {}  # source -> (preds, labels)

        with torch.no_grad():
            for batch in val_loader:
                input_ids = batch["input_ids"].to(device)
                attention_mask = batch["attention_mask"].to(device)
                labels = batch["label"].to(device)
                scores = batch["score"].to(device)
                sources = batch["source"]

                with torch.amp.autocast(device_type="cuda", enabled=use_amp):
                    class_logits, pred_scores = model(input_ids, attention_mask)
                    loss_cls = ce_loss(class_logits, labels)
                    loss_score = huber_loss(pred_scores, scores)
                    loss = loss_cls + score_loss_weight * loss_score

                val_loss += loss.item()
                val_batches += 1

                preds = class_logits.argmax(dim=-1).cpu().tolist()
                labels_cpu = labels.cpu().tolist()
                all_preds.extend(preds)
                all_labels.extend(labels_cpu)

                for i, src in enumerate(sources):
                    if src not in per_domain_preds:
                        per_domain_preds[src] = ([], [])
                    per_domain_preds[src][0].append(preds[i])
                    per_domain_preds[src][1].append(labels_cpu[i])

        avg_val_loss = val_loss / max(val_batches, 1)
        val_macro_f1 = f1_score(all_labels, all_preds, average="macro")

        # Per-domain F1
        per_domain_f1 = {}
        for src, (p, l) in per_domain_preds.items():
            if len(set(l)) > 1:
                per_domain_f1[src] = f1_score(l, p, average="macro")
            else:
                per_domain_f1[src] = 0.0

        epoch_metrics = {
            "epoch": epoch,
            "train_loss": round(avg_train_loss, 4),
            "val_loss": round(avg_val_loss, 4),
            "val_macro_f1": round(val_macro_f1, 4),
            "per_domain_f1": {k: round(v, 4) for k, v in per_domain_f1.items()},
            "vram_peak_gb": round(vram_peak, 3),
        }
        metrics_history.append(epoch_metrics)
        logger.info(
            f"Epoch {epoch}: train_loss={avg_train_loss:.4f}, "
            f"val_loss={avg_val_loss:.4f}, val_macro_F1={val_macro_f1:.4f}, "
            f"VRAM_peak={vram_peak:.3f}GB"
        )
        logger.info(f"  Per-domain F1: {per_domain_f1}")

        # ---- Early stopping ----
        if val_macro_f1 > best_val_f1:
            best_val_f1 = val_macro_f1
            no_improve = 0
            best_checkpoint_path = os.path.join(
                checkpoint_dir,
                f"best_model_seed{seed}_f{fraction}_e{epoch}.pt"
            )
            torch.save({
                "model_state_dict": model.state_dict(),
                "val_macro_f1": best_val_f1,
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
            }, best_checkpoint_path)
            logger.info(f"  Saved best model: {best_checkpoint_path}")
        else:
            no_improve += 1
            if no_improve >= patience:
                logger.info(f"  Early stopping after {patience} epochs without improvement.")
                break

    # Save metrics
    results_path = os.path.join(checkpoint_dir, f"metrics_seed{seed}_f{fraction}.json")
    with open(results_path, "w") as f:
        json.dump({
            "seed": seed,
            "fraction": fraction,
            "best_val_f1": best_val_f1,
            "vram_peak_gb": round(vram_peak, 3),
            "history": metrics_history,
        }, f, indent=2)

    return {
        "best_checkpoint_path": best_checkpoint_path,
        "metrics_history": metrics_history,
        "best_val_f1": best_val_f1,
        "vram_peak_gb": round(vram_peak, 3),
        "config": {
            "seed": seed,
            "fraction": fraction,
            "epochs": epochs,
            "batch_size": batch_size,
            "lr_backbone": lr_backbone,
            "lr_heads": lr_heads,
        },
    }
