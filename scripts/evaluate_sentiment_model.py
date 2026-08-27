#!/usr/bin/env python3
"""Standalone evaluation script for the dual-head FinBERT sentiment model.

Usage:
    python scripts/evaluate_sentiment_model.py --model-path models/sentiment_v1/best_model.pt --split locked_test
"""

import argparse
import os
import sys
import logging

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "Qualitative"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("evaluate_sentiment_model")


def main():
    parser = argparse.ArgumentParser(description="Evaluate dual-head FinBERT sentiment model")
    parser.add_argument("--model-path", type=str, required=True,
                        help="Path to model checkpoint (.pt)")
    parser.add_argument("--split", type=str, default="locked_test",
                        choices=["locked_test", "test", "val"],
                        help="Data split to evaluate on")
    parser.add_argument("--db-path", type=str, default="data/sentiment_training.db",
                        help="Path to SQLite DB")
    parser.add_argument("--output", type=str, default=None,
                        help="Output JSON path (default: models/sentiment_v1/eval_<split>.json)")
    parser.add_argument("--no-baselines", action="store_true",
                        help="Skip zero-shot baseline (faster)")
    args = parser.parse_args()

    from Qualitative.psychological.sentiment_training.evaluate import evaluate_model, save_results

    logger.info(f"Evaluating model: {args.model_path} on split={args.split}")
    results = evaluate_model(
        model_path=args.model_path,
        db_path=args.db_path,
        split=args.split,
        compute_baselines=not args.no_baselines,
    )

    if args.output:
        output_path = args.output
    else:
        output_dir = "models/sentiment_v1"
        os.makedirs(output_dir, exist_ok=True)
        basename = os.path.splitext(os.path.basename(args.model_path))[0]
        output_path = os.path.join(output_dir, f"eval_{basename}_{args.split}.json")

    save_results(results, output_path)

    print("\n" + "=" * 60)
    print(f"EVALUATION RESULTS — split={args.split}")
    print("=" * 60)
    print(f"  Samples:        {results['total_samples']}")
    print(f"  Macro F1:       {results['macro_f1']:.4f}")
    print(f"  Accuracy:       {results['accuracy']:.4f}")
    print(f"  Spearman corr:  {results['spearman_corr']:.4f}")
    print(f"  Per-source F1:  {results['per_source_f1']}")
    print(f"  Overall PASS:   {results['overall_pass']}")
    for bar_name, bar_data in results['bar_results'].items():
        status = "PASS" if bar_data.get("pass", bar_data.get("all_pass", False)) else "FAIL"
        print(f"  Bar {bar_name}: {status}")
    print("=" * 60)


if __name__ == "__main__":
    main()
