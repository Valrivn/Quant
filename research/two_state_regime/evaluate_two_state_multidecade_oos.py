"""
research/two_state_regime/evaluate_two_state_multidecade_oos.py

Multi-Decade Long-Horizon (1994 - 2026, 32+ Years, 8,200+ Sessions)
Out-Of-Sample (OOS) Evaluation of the 2-State (Bull/Bear) Markov Engine + Empirical Cooldown.

Partition Architecture:
1. In-Sample Training / Formulation Window:
   - 2018-01-01 to 2022-12-31 (5 Years, where parameters & features were initially formulated)
2. Post-2022 Out-of-Sample (Forward Walk):
   - 2023-01-01 to 2026-09-04 (Modern AI Bull & Recent Markets)
3. Historical Pre-2018 Pure Out-of-Sample (Backward Walk):
   - 1994-01-01 to 2017-12-31 (24 Years, unseen multi-cycle history)
   Includes:
   - Dot-Com Bubble & Bust (2000 - 2002 Bear)
   - 2003 - 2007 Housing & Credit Expansion (Bull)
   - 2007 - 2009 Great Financial Crisis (GFC Global Meltdown Bear)
   - 2010 - 2017 Zero-Interest Rate Expansion (Bull) with 2011 & 2015-16 Corrections

Reports:
- Accuracy, Recall, Precision, and Annual Swaps across each multi-decade era.
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


def get_macro_ground_truth_multi_decade(d: pd.Timestamp) -> str:
    """
    Comprehensive historical market ground truth based on NBER recessions and
    consensus macro cycles across 1994 - 2026:
    """
    # 1. Dot-com Crash & Recession (2000 - 2002)
    if pd.Timestamp("2000-09-01") <= d <= pd.Timestamp("2002-10-10"):
        return "BEAR"
    # 2. Great Financial Crisis (GFC, 2007 - 2009)
    elif pd.Timestamp("2007-10-15") <= d <= pd.Timestamp("2009-03-09"):
        return "BEAR"
    # 3. 2011 US Debt Downgrade / European Sovereign Crisis
    elif pd.Timestamp("2011-07-22") <= d <= pd.Timestamp("2011-10-04"):
        return "BEAR"
    # 4. 2015 - 2016 Commodity / Chinese Devaluation Stagnation-Crash
    elif pd.Timestamp("2015-08-10") <= d <= pd.Timestamp("2016-02-11"):
        return "BEAR"
    # 5. Q4 2018 Volmageddon / Fed Rate Pivot
    elif pd.Timestamp("2018-10-01") <= d <= pd.Timestamp("2018-12-31"):
        return "BEAR"
    # 6. COVID Crash
    elif pd.Timestamp("2020-02-20") <= d <= pd.Timestamp("2020-04-15"):
        return "BEAR"
    # 7. 2022 Fed Tightening Bear Market
    elif pd.Timestamp("2022-01-05") <= d <= pd.Timestamp("2022-10-31"):
        return "BEAR"
    # All other historical periods represent sustained Bull/Expansion regimes
    else:
        return "BULL"


def evaluate_partition(
    dates_sub: pd.Index,
    spy: pd.Series,
    engine: TwoStateRegimeEngine,
    buffer: TwoStateCooldownBuffer,
    partition_name: str,
) -> dict:
    ret_daily = spy.pct_change()
    gt_list, unbuf_list, buf_list = [], [], []

    for d in dates_sub:
        gt = get_macro_ground_truth_multi_decade(d)
        gt_list.append(gt)

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

        eff = buffer.update(d, probs, daily_return=r_1d, trailing_5d_return=r_5d)
        buf_list.append(eff)

    df_eval = pd.DataFrame({
        "Ground_Truth": gt_list,
        "Raw_Unbuffered": unbuf_list,
        "Buffered_Effective": buf_list,
    }, index=dates_sub)

    # Accuracy (Strict Bull/Bear match or Cooldown during transition)
    strict_match = (df_eval["Buffered_Effective"] == df_eval["Ground_Truth"])
    accuracy = float(strict_match.mean())

    swaps = int((df_eval["Buffered_Effective"] != df_eval["Buffered_Effective"].shift()).sum() - 1)
    unbuf_swaps = int((df_eval["Raw_Unbuffered"] != df_eval["Raw_Unbuffered"].shift()).sum() - 1)

    n_years = max(0.1, (dates_sub[-1] - dates_sub[0]).days / 365.25)
    swaps_per_year = swaps / n_years

    bull_actual = (df_eval["Ground_Truth"] == "BULL").sum()
    bear_actual = (df_eval["Ground_Truth"] == "BEAR").sum()

    bull_correct = ((df_eval["Ground_Truth"] == "BULL") & (df_eval["Buffered_Effective"] == "BULL")).sum()
    bear_correct = ((df_eval["Ground_Truth"] == "BEAR") & (df_eval["Buffered_Effective"] == "BEAR")).sum()
    cooldown_days = (df_eval["Buffered_Effective"] == "COOLDOWN").sum()

    bull_recall = (bull_correct / bull_actual) if bull_actual else 0.0
    bear_recall = (bear_correct / bear_actual) if bear_actual else 0.0

    return {
        "partition": partition_name,
        "start_date": str(dates_sub[0].date()),
        "end_date": str(dates_sub[-1].date()),
        "trading_days": len(dates_sub),
        "years": round(n_years, 1),
        "accuracy_pct": round(accuracy * 100, 2),
        "swaps_per_year": round(swaps_per_year, 2),
        "unbuf_swaps_per_year": round(unbuf_swaps / n_years, 2),
        "whipsaw_reduction_pct": round(((unbuf_swaps - swaps) / max(1, unbuf_swaps)) * 100, 2),
        "bull_recall": round(bull_recall * 100, 2),
        "bear_recall": round(bear_recall * 100, 2),
        "cooldown_pct": round(cooldown_days / len(dates_sub) * 100, 2),
        "total_flips": swaps
    }


def main():
    print("=" * 90)
    print("MULTI-DECADE OUT-OF-SAMPLE (OOS) EVALUATION: 1994 - 2026 (32+ YEARS)")
    print("=" * 90)

    spy_path = REPO_ROOT / "data" / "source" / "yfinance" / "SPY" / "prices.parquet"
    spy_tbl = pq.read_table(spy_path).to_pandas()
    clean_idx = pd.to_datetime(spy_tbl.index.strftime("%Y-%m-%d"))
    spy = pd.Series(spy_tbl["Close"].values, index=clean_idx).sort_index()

    # Need 252 days of initialization for features (1993 used as warm-up)
    available_dates = spy.index[spy.index >= pd.Timestamp("1994-01-01")]
    print(f"Total available trading days: {len(available_dates):,} sessions ({available_dates[0].strftime('%Y-%m-%d')} to {available_dates[-1].strftime('%Y-%m-%d')}).\n")

    engine = TwoStateRegimeEngine()

    partitions = [
        # 1. Historical Pure OOS (Dot-Com Era): 1994 - 2002
        ("OOS: Dot-Com Cycle (1994-2002)", pd.Timestamp("1994-01-01"), pd.Timestamp("2002-12-31")),
        # 2. Historical Pure OOS (GFC Era): 2003 - 2009
        ("OOS: GFC & Recovery (2003-2009)", pd.Timestamp("2003-01-01"), pd.Timestamp("2009-12-31")),
        # 3. Historical Pure OOS (ZIRP Bull & Corrections): 2010 - 2017
        ("OOS: ZIRP Expansion (2010-2017)", pd.Timestamp("2010-01-01"), pd.Timestamp("2017-12-31")),
        # 4. In-Sample Formulation Window: 2018 - 2022
        ("IN-SAMPLE: Baseline (2018-2022)", pd.Timestamp("2018-01-01"), pd.Timestamp("2022-12-31")),
        # 5. Modern Forward OOS: 2023 - 2026
        ("OOS: Modern AI Era (2023-2026)", pd.Timestamp("2023-01-01"), pd.Timestamp("2026-09-04")),
    ]

    results = []

    for name, start_t, end_t in partitions:
        dates_sub = available_dates[(available_dates >= start_t) & (available_dates <= end_t)]
        if len(dates_sub) == 0:
            continue
        print(f"Evaluating [{name}] ({len(dates_sub):,} days)...")
        buf = TwoStateCooldownBuffer(CooldownBufferConfig(cooldown_days=25, min_dwell_days=15))
        res = evaluate_partition(dates_sub, spy, engine, buf, name)
        results.append(res)

    # Full Continuous 32.7-year walk
    print(f"\nEvaluating [FULL CONTINUOUS 32-YEAR WALK (1994-2026)] ({len(available_dates):,} days)...")
    buf_full = TwoStateCooldownBuffer(CooldownBufferConfig(cooldown_days=25, min_dwell_days=15))
    res_full = evaluate_partition(available_dates, spy, engine, buf_full, "FULL MULTI-DECADE (1994-2026)")
    results.append(res_full)

    df_res = pd.DataFrame(results).set_index("partition")

    print("\n" + "=" * 90)
    print("MULTI-DECADE OUT-OF-SAMPLE PERFORMANCE MATRIX (1994 - 2026)")
    print("=" * 90)
    print(df_res[[
        "years", "trading_days", "accuracy_pct", "swaps_per_year",
        "unbuf_swaps_per_year", "whipsaw_reduction_pct", "bull_recall", "bear_recall", "cooldown_pct"
    ]].to_string())

    out_file = REPO_ROOT / "data" / "backtests" / "multidecade_two_state_oos_results.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nResults saved to: {out_file}")
    print("=" * 90)


if __name__ == "__main__":
    main()
