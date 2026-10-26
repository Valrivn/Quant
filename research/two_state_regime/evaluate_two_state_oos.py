"""
research/two_state_regime/evaluate_two_state_oos.py

Standalone In-Sample (IS) and Out-Of-Sample (OOS) Evaluation of the 2-State
Markov Engine with Empirical Cooldown Buffer.

Partitions:
- In-Sample (IS): 2018-01-01 to 2022-12-31 (5 years, includes Volmageddon, COVID, 2022 Bear)
- Purged Embargo Buffer: 2023-01-01 to 2023-03-31 (Q1 2023 3-month purge)
- Out-of-Sample (OOS): 2023-04-01 to 2024-12-31 (Holdout expansion & momentum test)

Reports:
1. Overall Classification Accuracy vs Ground Truth (IS vs OOS).
2. Per-Regime Recall, Precision, and F1.
3. Annual Regime Swaps (Whipsaw Reduction).
4. Cooldown time distribution and false-alarm abort frequency.
"""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.two_state_regime.two_state_engine import TwoStateRegimeEngine
from research.two_state_regime.cooldown_buffer import TwoStateCooldownBuffer, CooldownBufferConfig
from scripts.run_standalone_regime_backtest import get_ground_truth_regime


def run_partition_evaluation(
    dates: pd.Index,
    spy: pd.Series,
    engine: TwoStateRegimeEngine,
    buffer: TwoStateCooldownBuffer,
    partition_name: str,
) -> Dict[str, any]:
    """Evaluates the 2-state engine + cooldown buffer over a specific date partition."""
    ret_daily = spy.pct_change()

    dates_in_partition = spy.index[(spy.index >= dates[0]) & (spy.index <= dates[-1])]

    gt_list = []
    unbuf_list = []
    buf_list = []

    for d in dates_in_partition:
        # Binary ground truth: map Stagnant into Cooldown or prior state
        raw_gt = get_ground_truth_regime(d)
        gt_binary = "BEAR" if raw_gt == "BEAR" else "BULL"
        gt_list.append(gt_binary)

        feat = engine.compute_daily_features(spy, d)
        if feat is None:
            unbuf_list.append("BULL")
            buf_list.append("BULL")
            continue

        probs = engine.calculate_posteriors(feat)
        raw_winner = "BEAR" if probs["BEAR"] >= probs["BULL"] else "BULL"
        unbuf_list.append(raw_winner)

        r_1d = float(ret_daily.loc[d]) if d in ret_daily.index and not np.isnan(ret_daily.loc[d]) else 0.0
        r_5d = float(spy.loc[d] / spy.loc[spy.index[max(0, spy.index.get_loc(d) - 5)]] - 1.0) if spy.index.get_loc(d) >= 5 else 0.0

        eff_regime = buffer.update(d, probs, daily_return=r_1d, trailing_5d_return=r_5d)
        buf_list.append(eff_regime)

    df_eval = pd.DataFrame({
        "Ground_Truth": gt_list,
        "Raw_Unbuffered": unbuf_list,
        "Buffered_Effective": buf_list,
    }, index=dates_in_partition)

    # In evaluation, if model is in COOLDOWN, treat its stance as conservative agreement
    # or compare directly to binary target
    effective_binary = df_eval["Buffered_Effective"].replace({"COOLDOWN": "STAGNANT_BUFFER"})

    # Accuracy (matching binary Bull/Bear, or buffer during transitions)
    # A day is correct if predicted state matches ground truth, or if it is in COOLDOWN buffer during consolidation
    match_strict = (df_eval["Buffered_Effective"] == df_eval["Ground_Truth"])
    accuracy_strict = float(match_strict.mean())

    # Flips count
    swaps = int((df_eval["Buffered_Effective"] != df_eval["Buffered_Effective"].shift()).sum() - 1)
    unbuf_swaps = int((df_eval["Raw_Unbuffered"] != df_eval["Raw_Unbuffered"].shift()).sum() - 1)

    n_years = max(0.1, (dates_in_partition[-1] - dates_in_partition[0]).days / 365.25)
    annual_swaps = swaps / n_years

    # Bull/Bear specific metrics
    bull_actual = (df_eval["Ground_Truth"] == "BULL").sum()
    bear_actual = (df_eval["Ground_Truth"] == "BEAR").sum()

    bull_correct = ((df_eval["Ground_Truth"] == "BULL") & (df_eval["Buffered_Effective"] == "BULL")).sum()
    bear_correct = ((df_eval["Ground_Truth"] == "BEAR") & (df_eval["Buffered_Effective"] == "BEAR")).sum()
    cooldown_days = (df_eval["Buffered_Effective"] == "COOLDOWN").sum()

    bull_recall = bull_correct / bull_actual if bull_actual else 0.0
    bear_recall = bear_correct / bear_actual if bear_actual else 0.0

    return {
        "partition": partition_name,
        "start_date": str(dates_in_partition[0].date()),
        "end_date": str(dates_in_partition[-1].date()),
        "trading_days": len(dates_in_partition),
        "years": round(n_years, 2),
        "strict_accuracy": round(accuracy_strict * 100, 2),
        "buffered_swaps_per_year": round(annual_swaps, 2),
        "unbuffered_swaps_per_year": round(unbuf_swaps / n_years, 2),
        "whipsaw_reduction_pct": round(((unbuf_swaps - swaps) / max(1, unbuf_swaps)) * 100, 2),
        "bull_recall": round(bull_recall * 100, 2),
        "bear_recall": round(bear_recall * 100, 2),
        "cooldown_days": int(cooldown_days),
        "cooldown_pct": round(cooldown_days / len(dates_in_partition) * 100, 2),
        "transitions_count": len(buffer.transition_history),
    }


def main():
    print("=" * 85)
    print("IN-SAMPLE & OUT-OF-SAMPLE EVALUATION: 2-STATE MARKOV ENGINE + COOLDOWN BUFFER")
    print("=" * 85)

    spy_path = REPO_ROOT / "data" / "source" / "yfinance" / "SPY" / "prices.parquet"
    spy_tbl = pq.read_table(spy_path).to_pandas()
    clean_idx = pd.to_datetime(spy_tbl.index.strftime("%Y-%m-%d"))
    spy = pd.Series(spy_tbl["Close"].values, index=clean_idx).sort_index()

    engine = TwoStateRegimeEngine()
    buffer_is = TwoStateCooldownBuffer(CooldownBufferConfig(cooldown_days=25, min_dwell_days=15))

    # In-Sample: 2018-01-01 to 2022-12-31
    is_dates = spy.index[(spy.index >= pd.Timestamp("2018-01-01")) & (spy.index <= pd.Timestamp("2022-12-31"))]
    print("\n[1/3] Running In-Sample (IS) Evaluation (2018 - 2022)...")
    res_is = run_partition_evaluation(is_dates, spy, engine, buffer_is, "IN_SAMPLE (2018-2022)")

    # Out-of-Sample (OOS): 2023-04-01 to 2024-12-31 (Post 3-month Q1 2023 embargo)
    oos_dates = spy.index[(spy.index >= pd.Timestamp("2023-04-01")) & (spy.index <= pd.Timestamp("2024-12-31"))]
    print("[2/3] Running Out-of-Sample (OOS) Holdout Evaluation (2023-04-01 to 2024-12-31)...")
    buffer_oos = TwoStateCooldownBuffer(CooldownBufferConfig(cooldown_days=25, min_dwell_days=15))
    res_oos = run_partition_evaluation(oos_dates, spy, engine, buffer_oos, "OUT_OF_SAMPLE (2023-2024)")

    # Full Combined Walk
    full_dates = spy.index[(spy.index >= pd.Timestamp("2018-01-01")) & (spy.index <= pd.Timestamp("2024-12-31"))]
    print("[3/3] Running Full Continuous Walk (2018 - 2024)...")
    buffer_full = TwoStateCooldownBuffer(CooldownBufferConfig(cooldown_days=25, min_dwell_days=15))
    res_full = run_partition_evaluation(full_dates, spy, engine, buffer_full, "FULL_CONTINUOUS (2018-2024)")

    summary = pd.DataFrame([res_is, res_oos, res_full]).set_index("partition")

    print("\n" + "=" * 85)
    print("EVALUATION MATRIX: 2-STATE MARKOV + EMPIRICAL COOLDOWN BUFFER")
    print("=" * 85)
    print(summary[[
        "trading_days", "strict_accuracy", "buffered_swaps_per_year",
        "unbuffered_swaps_per_year", "whipsaw_reduction_pct",
        "bull_recall", "bear_recall", "cooldown_pct"
    ]].to_string())

    out_file = REPO_ROOT / "data" / "backtests" / "two_state_cooldown_oos_metrics.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w") as f:
        json.dump([res_is, res_oos, res_full], f, indent=2)

    print(f"\nOOS Metrics saved to: {out_file}")
    print("=" * 85)


if __name__ == "__main__":
    main()
