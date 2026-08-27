"""
Dual-head FinBERT model for sentiment classification + continuous scoring.

Heads:
  - class_head: 3-class (negative/neutral/positive)
  - score_head: continuous score in [-1, 1] via tanh

Backbone: ProsusAI/finbert (768-dim hidden)
"""

import torch
import torch.nn as nn
from transformers import AutoModel


class SentimentDualHead(nn.Module):
    """FinBERT backbone with classification and regression heads."""

    def __init__(self, model_name="ProsusAI/finbert", num_classes=3, freeze_backbone=False):
        super().__init__()
        self.backbone = AutoModel.from_pretrained(model_name)
        hidden = self.backbone.config.hidden_size  # 768

        if freeze_backbone:
            for p in self.backbone.parameters():
                p.requires_grad = False

        self.class_head = nn.Linear(hidden, num_classes)
        self.score_head = nn.Linear(hidden, 1)

    def forward(self, input_ids, attention_mask):
        """
        Returns:
            class_logits: (batch, num_classes)
            score: (batch,) continuous in [-1, 1]
        """
        out = self.backbone(input_ids=input_ids, attention_mask=attention_mask)
        pooled = out.last_hidden_state[:, 0]  # CLS token
        class_logits = self.class_head(pooled)
        score = torch.tanh(self.score_head(pooled))  # [-1, 1]
        return class_logits, score.squeeze(-1)
