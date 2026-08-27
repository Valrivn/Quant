"""
Unit tests for the dual-head FinBERT sentiment model.

Tests:
  - Model instantiation (from_pretrained succeeds)
  - Forward pass shape: class_logits (batch, 3), score (batch,) in [-1,1]
  - Loss computation: both heads contribute
  - VRAM test: peak < 6.5GB (skip if no CUDA)
"""

import pytest
import torch
import torch.nn as nn
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from Qualitative.psychological.sentiment_training.model import SentimentDualHead


@pytest.fixture(scope="module")
def finbert_model():
    """Load FinBERT model once for all tests in this module."""
    model = SentimentDualHead(model_name="ProsusAI/finbert")
    return model


@pytest.fixture(scope="module")
def dummy_batch():
    """Create a dummy batch for testing."""
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
    texts = ["Stock market rallies on earnings.", "Firm suffers massive decline."] * 4
    encoding = tokenizer(
        texts, max_length=64, padding="max_length",
        truncation=True, return_tensors="pt",
    )
    return {
        "input_ids": encoding["input_ids"],
        "attention_mask": encoding["attention_mask"],
        "labels": torch.tensor([2, 0, 1, 2, 0, 1, 2, 1], dtype=torch.long),
        "scores": torch.tensor([0.8, -0.6, 0.0, 0.9, -0.7, 0.1, 0.5, -0.2], dtype=torch.float),
    }


class TestModelInstantiation:
    """Test model can be loaded from pretrained."""

    def test_loads_pretrained(self, finbert_model):
        """Model loads from ProsusAI/finbert successfully."""
        assert finbert_model is not None
        assert hasattr(finbert_model, "backbone")
        assert hasattr(finbert_model, "class_head")
        assert hasattr(finbert_model, "score_head")

    def test_backbone_hidden_size(self, finbert_model):
        """Backbone hidden size is 768."""
        assert finbert_model.backbone.config.hidden_size == 768

    def test_head_output_dims(self, finbert_model):
        """Class head outputs 3 classes, score head outputs 1."""
        assert finbert_model.class_head.out_features == 3
        assert finbert_model.score_head.out_features == 1

    def test_freeze_backbone(self):
        """Frozen backbone has no grad on backbone params."""
        model = SentimentDualHead(model_name="ProsusAI/finbert", freeze_backbone=True)
        for p in model.backbone.parameters():
            assert not p.requires_grad
        # Heads should still have grad
        for p in model.class_head.parameters():
            assert p.requires_grad
        for p in model.score_head.parameters():
            assert p.requires_grad


class TestForwardPass:
    """Test forward pass shapes and value ranges."""

    def test_class_logits_shape(self, finbert_model, dummy_batch):
        """class_logits shape is (batch, 3)."""
        finbert_model.eval()
        with torch.no_grad():
            class_logits, score = finbert_model(
                dummy_batch["input_ids"], dummy_batch["attention_mask"]
            )
        assert class_logits.shape == (8, 3)

    def test_score_shape(self, finbert_model, dummy_batch):
        """score shape is (batch,)."""
        finbert_model.eval()
        with torch.no_grad():
            class_logits, score = finbert_model(
                dummy_batch["input_ids"], dummy_batch["attention_mask"]
            )
        assert score.shape == (8,)

    def test_score_range(self, finbert_model, dummy_batch):
        """Score values are in [-1, 1] via tanh."""
        finbert_model.eval()
        with torch.no_grad():
            _, score = finbert_model(
                dummy_batch["input_ids"], dummy_batch["attention_mask"]
            )
        assert score.min() >= -1.0 - 1e-6
        assert score.max() <= 1.0 + 1e-6

    def test_logits_require_grad(self, finbert_model, dummy_batch):
        """Output logits require grad for backprop."""
        class_logits, score = finbert_model(
            dummy_batch["input_ids"], dummy_batch["attention_mask"]
        )
        assert class_logits.requires_grad
        assert score.requires_grad


class TestLossComputation:
    """Test that both heads contribute to loss."""

    def test_both_heads_contribute(self, finbert_model, dummy_batch):
        """Loss includes contributions from both classification and score heads."""
        ce_loss = nn.CrossEntropyLoss()
        huber_loss = nn.HuberLoss(delta=0.1)

        class_logits, pred_scores = finbert_model(
            dummy_batch["input_ids"], dummy_batch["attention_mask"]
        )

        loss_cls = ce_loss(class_logits, dummy_batch["labels"])
        loss_score = huber_loss(pred_scores, dummy_batch["scores"])

        # Both losses should be positive
        assert loss_cls.item() > 0, "CrossEntropy loss should be positive"
        assert loss_score.item() >= 0, "Huber loss should be non-negative"

        # Combined loss
        total_loss = loss_cls + 0.5 * loss_score
        total_loss.backward()

        # Gradients should flow to both heads
        class_grad_norm = sum(
            p.grad.norm().item() for p in finbert_model.class_head.parameters()
            if p.grad is not None
        )
        score_grad_norm = sum(
            p.grad.norm().item() for p in finbert_model.score_head.parameters()
            if p.grad is not None
        )
        assert class_grad_norm > 0, "Class head should have gradients"
        assert score_grad_norm > 0, "Score head should have gradients"

    def test_loss_is_scalar(self, finbert_model, dummy_batch):
        """Loss is a scalar tensor."""
        ce_loss = nn.CrossEntropyLoss()
        huber_loss = nn.HuberLoss(delta=0.1)

        class_logits, pred_scores = finbert_model(
            dummy_batch["input_ids"], dummy_batch["attention_mask"]
        )
        total_loss = ce_loss(class_logits, dummy_batch["labels"]) + 0.5 * huber_loss(
            pred_scores, dummy_batch["scores"]
        )
        assert total_loss.dim() == 0, "Loss should be a scalar"


class TestVRAM:
    """VRAM smoke test — skip if no CUDA."""

    @pytest.mark.skipif(not torch.cuda.is_available(), reason="No CUDA")
    def test_peak_vram(self):
        """Peak VRAM < 6.5GB with batch=32, seq=128, fp16."""
        from transformers import AutoTokenizer

        device = torch.device("cuda")
        torch.cuda.reset_peak_memory_stats(device)

        tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        model = SentimentDualHead(model_name="ProsusAI/finbert")
        model.to(device)
        model.train()

        texts = ["Stock market rallies on strong earnings report."] * 32
        encoding = tokenizer(
            texts, max_length=128, padding="max_length",
            truncation=True, return_tensors="pt",
        )
        input_ids = encoding["input_ids"].to(device)
        attention_mask = encoding["attention_mask"].to(device)
        labels = torch.randint(0, 3, (32,)).to(device)
        scores = torch.randn(32).clamp(-1, 1).to(device)

        ce_loss = nn.CrossEntropyLoss()
        huber_loss = nn.HuberLoss(delta=0.1)
        scaler = torch.amp.GradScaler(enabled=True)

        with torch.amp.autocast(device_type="cuda", enabled=True):
            class_logits, pred_scores = model(input_ids, attention_mask)
            loss = ce_loss(class_logits, labels) + 0.5 * huber_loss(pred_scores, scores)

        scaler.scale(loss).backward()
        scaler.step(torch.optim.Adam(model.parameters(), lr=1e-5))
        scaler.update()

        peak_gb = torch.cuda.max_memory_allocated(device) / (1024 ** 3)
        assert peak_gb < 6.5, f"Peak VRAM {peak_gb:.3f}GB exceeds 6.5GB limit"
