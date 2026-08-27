#!/usr/bin/env python3
"""CLI entry point for training the dual-head FinBERT sentiment model.

Usage:
    python scripts/train_sentiment_model.py --fraction 1.0 --seed 42 --epochs 5 --batch-size 32
"""

import argparse
import os
import sys
import logging

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "Qualitative"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("train_sentiment_model")


def main():
    parser = argparse.ArgumentParser(description="Train dual-head FinBERT sentiment model")
    parser.add_argument("--fraction", type=float, default=1.0,
                        choices=[0.1, 0.25, 0.5, 1.0],
                        help="Fraction of training data (curriculum)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--epochs", type=int, default=5, help="Number of epochs")
    parser.add_argument("--batch-size", type=int, default=32, help="Batch size")
    parser.add_argument("--max-len", type=int, default=128, help="Max sequence length")
    parser.add_argument("--checkpoint-dir", type=str, default="models/sentiment_v1",
                        help="Directory for model checkpoints")
    parser.add_argument("--db-path", type=str, default="data/sentiment_training.db",
                        help="Path to SQLite DB")
    parser.add_argument("--skip-eval", action="store_true", help="Skip evaluation after training")
    args = parser.parse_args()

    # Ensure DB exists
    if not os.path.exists(args.db_path):
        logger.info("DB not found — building dataset...")
        from Qualitative.psychological.sentiment_training.data_layer import build_dataset
        stats = build_dataset(args.db_path)
        logger.info(f"Dataset built: {stats}")

    # Train
    from Qualitative.psychological.sentiment_training.trainer import train
    logger.info(f"Starting training: fraction={args.fraction}, seed={args.seed}, epochs={args.epochs}")
    train_results = train(
        db_path=args.db_path,
        checkpoint_dir=args.checkpoint_dir,
        seed=args.seed,
        epochs=args.epochs,
        batch_size=args.batch_size,
        fraction=args.fraction,
        max_len=args.max_len,
    )
    logger.info(f"Training complete. Best val F1: {train_results['best_val_f1']:.4f}")
    logger.info(f"VRAM peak: {train_results['vram_peak_gb']:.3f} GB")
    logger.info(f"Checkpoint: {train_results['best_checkpoint_path']}")

    # Evaluate
    if not args.skip_eval and train_results["best_checkpoint_path"]:
        from Qualitative.psychological.sentiment_training.evaluate import evaluate_model, save_results
        logger.info("Running evaluation on locked_test split...")
        eval_results = evaluate_model(
            model_path=train_results["best_checkpoint_path"],
            db_path=args.db_path,
            split="locked_test",
        )
        output_path = os.path.join(
            args.checkpoint_dir,
            f"eval_results_seed{args.seed}_f{args.fraction}.json"
        )
        save_results(eval_results, output_path)

        print("\n" + "=" * 60)
        print("EVALUATION RESULTS")
        print("=" * 60)
        print(f"  Macro F1:       {eval_results['macro_f1']:.4f}")
        print(f"  Accuracy:       {eval_results['accuracy']:.4f}")
        print(f"  Spearman corr:  {eval_results['spearman_corr']:.4f}")
        print(f"  Per-source F1:  {eval_results['per_source_f1']}")
        print(f"  Overall PASS:   {eval_results['overall_pass']}")
        for bar_name, bar_data in eval_results['bar_results'].items():
            status = "PASS" if bar_data.get("pass", bar_data.get("all_pass", False)) else "FAIL"
            print(f"  Bar {bar_name}: {status} (bar={bar_data.get('bar', 'N/A')}, actual={bar_data.get('actual', 'N/A')})")
        print("=" * 60)


if __name__ == "__main__":
    main()
