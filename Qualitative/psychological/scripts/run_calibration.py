#!/usr/bin/env python3
"""
run_calibration.py — Walk-Forward Bayesian Calibration CLI

Runs a full walk-forward backtesting study for one or more tickers,
slicing historical data into 6-month blocks, running the Monte Carlo
at each checkpoint blind to future data, comparing predictions to
actual reported fundamentals, and updating simulation parameters
via Bayesian updating.

Usage:
    python -m Qualitative.psychological.scripts.run_calibration \
        --ticker NVDA \
        --start-date 2021-01-01 \
        --end-date 2024-01-01 \
        --block-size 6 \
        --horizon 6 \
        --db-path reddit_quant.db
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from psychological.bayesian_calibration import (
    WalkForwardEngine,
    BayesianCalibrationResult,
    create_walk_forward_engine,
)
from db.schema import create_calibration_tables

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("calibration")


def format_report(result: BayesianCalibrationResult) -> str:
    """Format calibration result as human-readable report."""
    lines = [
        "=" * 72,
        f"  BAYESIAN WALK-FORWARD CALIBRATION REPORT — {result.ticker}",
        "=" * 72,
        "",
        f"  Blocks tested:          {result.n_blocks_tested}",
        f"  Overall calibration err: {result.overall_calibration_error:.4f}",
        f"  Mean coverage (90% CI):  {result.mean_coverage_probability:.1%}",
        f"  Directional accuracy:    {result.directional_accuracy:.1%}",
        "",
        "  --- Distribution Parameter Evolution ---",
        f"  Growth std:  {result.pre_calibration_growth_std:.4f} -> "
        f"{result.post_calibration_growth_std:.4f} "
        f"({'widened' if result.post_calibration_growth_std > result.pre_calibration_growth_std else 'tightened'})",
        f"  Margin std:  {result.pre_calibration_margin_std:.4f} -> "
        f"{result.post_calibration_margin_std:.4f} "
        f"({'widened' if result.post_calibration_margin_std > result.pre_calibration_margin_std else 'tightened'})",
        "",
    ]

    # Source weight evolution
    if result.source_weight_evolution:
        lines.append("  --- Source Weight Evolution (Final Weights) ---")
        for src, weights in result.source_weight_evolution.items():
            if weights:
                final = weights[-1]
                lines.append(f"  {src:25s}  {final:.4f}")
        lines.append("")

    # Per-block summary
    if result.snapshots:
        lines.append("  --- Per-Block Summary ---")
        lines.append(
            f"  {'Block':>5s}  {'Predicted':>12s}  {'Actual':>12s}  "
            f"{'Error':>8s}  {'Coverage':>8s}  {'Dir OK':>6s}"
        )
        for snap in result.snapshots:
            actual_str = f"{snap.actual_mean_iv:,.0f}" if snap.actual_mean_iv else "N/A"
            lines.append(
                f"  {snap.block_id:5d}  {snap.predicted_mean_iv:12,.0f}  "
                f"{actual_str:>12s}  {snap.calibration_error:8.4f}  "
                f"{snap.pct_within_ci:8.1%}  "
                f"{'  Yes' if snap.directional_correct else '   No'}"
            )

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Walk-Forward Bayesian Calibration for Monte Carlo Engine"
    )
    parser.add_argument(
        "--ticker", required=True, help="Ticker symbol to calibrate (e.g. NVDA)"
    )
    parser.add_argument(
        "--start-date", required=True, help="Backtest start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end-date", required=True, help="Backtest end date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--block-size", type=int, default=6, help="Block size in months (default: 6)"
    )
    parser.add_argument(
        "--horizon", type=int, default=6, help="Prediction horizon in months (default: 6)"
    )
    parser.add_argument(
        "--db-path", default="reddit_quant.db", help="SQLite database path"
    )
    parser.add_argument(
        "--output", help="Output JSON file path (optional)"
    )
    args = parser.parse_args()

    # Initialize calibration tables
    import sqlite3
    conn = sqlite3.connect(args.db_path)
    create_calibration_tables(conn)
    conn.close()
    logger.info(f"Calibration tables initialized in {args.db_path}")

    # Run walk-forward
    engine = create_walk_forward_engine(db_path=args.db_path)
    result = engine.run_walk_forward(
        ticker=args.ticker,
        start_date=args.start_date,
        end_date=args.end_date,
        block_size_months=args.block_size,
        horizon_months=args.horizon,
    )

    # Print report
    report = format_report(result)
    print(report)

    # Save JSON if requested
    if args.output:
        output_data = result.to_dict()
        output_data["snapshots"] = [s.to_dict() for s in result.snapshots]
        with open(args.output, "w") as f:
            json.dump(output_data, f, indent=2)
        logger.info(f"Results saved to {args.output}")


if __name__ == "__main__":
    main()
