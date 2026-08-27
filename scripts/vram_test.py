#!/usr/bin/env python3
"""VRAM smoke test for the dual-head FinBERT sentiment model.

Loads model, runs one forward+backward at batch=32, seq-len=128, fp16.
Reports torch.cuda.max_memory_allocated() in GB.
FAILs if > 6.5GB.

Usage:
    python scripts/vram_test.py [--batch-size 32] [--max-len 128]
"""

import argparse
import os
import sys
import logging

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "Qualitative"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("vram_test")

VRAM_LIMIT_GB = 6.5


def main():
    parser = argparse.ArgumentParser(description="VRAM smoke test for dual-head FinBERT")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-len", type=int, default=128)
    parser.add_argument("--model-name", type=str, default="ProsusAI/finbert")
    args = parser.parse_args()

    import torch
    import torch.nn as nn

    if not torch.cuda.is_available():
        logger.warning("No CUDA available — skipping VRAM test.")
        print("SKIP: No CUDA device available")
        sys.exit(0)

    device = torch.device("cuda")
    torch.cuda.reset_peak_memory_stats(device)

    # Load model
    from transformers import AutoTokenizer
    from Qualitative.psychological.sentiment_training.model import SentimentDualHead

    logger.info(f"Loading model: {args.model_name}")
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    model = SentimentDualHead(model_name=args.model_name)
    model.to(device)
    model.train()

    # Create dummy batch
    texts = ["Stock market rallies on strong earnings report."] * args.batch_size
    encoding = tokenizer(
        texts, max_length=args.max_len, padding="max_length",
        truncation=True, return_tensors="pt",
    )
    input_ids = encoding["input_ids"].to(device)
    attention_mask = encoding["attention_mask"].to(device)
    labels = torch.randint(0, 3, (args.batch_size,)).to(device)
    scores = torch.randn(args.batch_size).clamp(-1, 1).to(device)

    # Forward + backward with fp16
    ce_loss = nn.CrossEntropyLoss()
    huber_loss = nn.HuberLoss(delta=0.1)

    scaler = torch.amp.GradScaler(enabled=True)

    with torch.amp.autocast(device_type="cuda", enabled=True):
        class_logits, pred_scores = model(input_ids, attention_mask)
        loss_cls = ce_loss(class_logits, labels)
        loss_score = huber_loss(pred_scores, scores)
        loss = loss_cls + 0.5 * loss_score

    scaler.scale(loss).backward()
    scaler.step(torch.optim.Adam(model.parameters(), lr=1e-5))
    scaler.update()

    peak_gb = torch.cuda.max_memory_allocated(device) / (1024 ** 3)

    print(f"\n{'='*50}")
    print(f"VRAM SMOKE TEST")
    print(f"  Batch size:     {args.batch_size}")
    print(f"  Seq length:     {args.max_len}")
    print(f"  Peak VRAM:      {peak_gb:.3f} GB")
    print(f"  Limit:          {VRAM_LIMIT_GB} GB")
    if peak_gb > VRAM_LIMIT_GB:
        print(f"  Result:         FAIL (exceeds {VRAM_LIMIT_GB}GB)")
        sys.exit(1)
    else:
        print(f"  Result:         PASS")
        sys.exit(0)


if __name__ == "__main__":
    main()
