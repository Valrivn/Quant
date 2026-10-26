# Alpha Analysis: 4-Stage Dynamic Value vs Baselines
import numpy as np

# BACKTEST RESULTS FROM GEMINI-WORKER (2026-09-17)
results = {
    'S7_4stage': {
        'total_return': 0.6669,
        'ann_return': 0.3188,
        'sharpe': 1.52,
        'max_dd': -0.0963,
        'win_rate': 0.478,
        'avg_holding': 128,
        'period_years': 2.0
    },
    'S7_baseline': {
        'total_return': 0.4680,
        'ann_return': 0.2116,
        'sharpe': 1.62,
        'max_dd': -0.0703,
        'win_rate': 0.521,
        'avg_holding': 145,
        'period_years': 2.0
    },
    'S7_spy': {
        'total_return': 0.5882,
        'ann_return': 0.2941,
        'sharpe': 1.88,
        'max_dd': -0.0997,
        'period_years': 2.0
    },
    'S8_4stage': {
        'total_return': -0.0641,
        'ann_return': -0.0264,
        'sharpe': -0.15,
        'max_dd': -0.1911,
        'win_rate': 0.302,
        'avg_holding': 112,
        'period_years': 2.0
    },
    'S8_baseline': {
        'total_return': -0.0116,
        'ann_return': -0.0116,
        'sharpe': -0.11,
        'max_dd': -0.1123,
        'win_rate': 0.345,
        'avg_holding': 130,
        'period_years': 2.0
    },
    'S8_spy': {
        'total_return': 0.3399,
        'ann_return': 0.16995,
        'sharpe': 1.10,
        'max_dd': -0.1876,
        'period_years': 2.0
    }
}

def compute_alpha(strategy, benchmark, period_years):
    excess_total = strategy['total_return'] - benchmark['total_return']
    excess_ann = strategy['ann_return'] - benchmark['ann_return']
    te_approx = abs(strategy['ann_return']) / abs(strategy['sharpe']) if strategy['sharpe'] != 0 else 0.15
    ir = excess_ann / te_approx if te_approx > 0 else 0
    rf = 0.045
    beta = 1.0
    jensen_alpha = strategy['ann_return'] - (rf + beta * (benchmark['ann_return'] - rf))
    return {
        'excess_total_return': excess_total,
        'excess_ann_return': excess_ann,
        'info_ratio_approx': ir,
        'jensen_alpha_ann': jensen_alpha,
        'tracking_error_approx': te_approx
    }

print("=" * 80)
print("ALPHA ANALYSIS: 4-STAGE DYNAMIC VALUE vs BASELINES")
print("=" * 80)

# S7 Analysis
print("\nSTRATUM S7 (2023-2024: AI BOOM)")
print("-" * 80)

alpha_vs_baseline_s7 = compute_alpha(results['S7_4stage'], results['S7_baseline'], 2.0)
alpha_vs_spy_s7 = compute_alpha(results['S7_4stage'], results['S7_spy'], 2.0)

print(f"\n4-Stage vs Core DCF Baseline:")
print(f"  Excess Total Return:      {alpha_vs_baseline_s7['excess_total_return']:+.2%} ({alpha_vs_baseline_s7['excess_total_return']*100:+.1f} bps)")
print(f"  Excess Annualized Return: {alpha_vs_baseline_s7['excess_ann_return']:+.2%} ({alpha_vs_baseline_s7['excess_ann_return']*100:+.1f} bps)")
print(f"  Jensen's Alpha (ann):     {alpha_vs_baseline_s7['jensen_alpha_ann']:+.2%}")
print(f"  Info Ratio (approx):      {alpha_vs_baseline_s7['info_ratio_approx']:.2f}")
print(f"  Tracking Error (approx):  {alpha_vs_baseline_s7['tracking_error_approx']:.2%}")

print(f"\n4-Stage vs SPY:")
print(f"  Excess Total Return:      {alpha_vs_spy_s7['excess_total_return']:+.2%} ({alpha_vs_spy_s7['excess_total_return']*100:+.1f} bps)")
print(f"  Excess Annualized Return: {alpha_vs_spy_s7['excess_ann_return']:+.2%} ({alpha_vs_spy_s7['excess_ann_return']*100:+.1f} bps)")
print(f"  Jensen's Alpha (ann):     {alpha_vs_spy_s7['jensen_alpha_ann']:+.2%}")
print(f"  Info Ratio (approx):      {alpha_vs_spy_s7['info_ratio_approx']:.2f}")

# S8 Analysis
print("\n\nSTRATUM S8 (2025-2026: CURRENT / GROWTH-LED)")
print("-" * 80)

alpha_vs_baseline_s8 = compute_alpha(results['S8_4stage'], results['S8_baseline'], 2.0)
alpha_vs_spy_s8 = compute_alpha(results['S8_4stage'], results['S8_spy'], 2.0)

print(f"\n4-Stage vs Core DCF Baseline:")
print(f"  Excess Total Return:      {alpha_vs_baseline_s8['excess_total_return']:+.2%} ({alpha_vs_baseline_s8['excess_total_return']*100:+.1f} bps)")
print(f"  Excess Annualized Return: {alpha_vs_baseline_s8['excess_ann_return']:+.2%} ({alpha_vs_baseline_s8['excess_ann_return']*100:+.1f} bps)")
print(f"  Jensen's Alpha (ann):     {alpha_vs_baseline_s8['jensen_alpha_ann']:+.2%}")
print(f"  Info Ratio (approx):      {alpha_vs_baseline_s8['info_ratio_approx']:.2f}")

print(f"\n4-Stage vs SPY:")
print(f"  Excess Total Return:      {alpha_vs_spy_s8['excess_total_return']:+.2%} ({alpha_vs_spy_s8['excess_total_return']*100:+.1f} bps)")
print(f"  Excess Annualized Return: {alpha_vs_spy_s8['excess_ann_return']:+.2%} ({alpha_vs_spy_s8['excess_ann_return']*100:+.1f} bps)")
print(f"  Jensen's Alpha (ann):     {alpha_vs_spy_s8['jensen_alpha_ann']:+.2%}")
print(f"  Info Ratio (approx):      {alpha_vs_spy_s8['info_ratio_approx']:.2f}")

# Combined
print("\n\nCOMBINED 4-YEAR (S7 + S8, Equal Weight)")
print("-" * 80)

combined_4stage_ann = (results['S7_4stage']['ann_return'] + results['S8_4stage']['ann_return']) / 2
combined_baseline_ann = (results['S7_baseline']['ann_return'] + results['S8_baseline']['ann_return']) / 2
combined_spy_ann = (results['S7_spy']['ann_return'] + results['S8_spy']['ann_return']) / 2

combined_4stage_total = (1 + results['S7_4stage']['total_return']) * (1 + results['S8_4stage']['total_return']) - 1
combined_baseline_total = (1 + results['S7_baseline']['total_return']) * (1 + results['S8_baseline']['total_return']) - 1
combined_spy_total = (1 + results['S7_spy']['total_return']) * (1 + results['S8_spy']['total_return']) - 1

alpha_combined_baseline = combined_4stage_ann - combined_baseline_ann
alpha_combined_spy = combined_4stage_ann - combined_spy_ann

print(f"4-Stage Combined Ann Return:  {combined_4stage_ann:+.2%}")
print(f"Baseline Combined Ann Return: {combined_baseline_ann:+.2%}")
print(f"SPY Combined Ann Return:      {combined_spy_ann:+.2%}")
print(f"\nAlpha vs Baseline (ann): {alpha_combined_baseline:+.2%} ({alpha_combined_baseline*100:+.1f} bps)")
print(f"Alpha vs SPY (ann):      {alpha_combined_spy:+.2%} ({alpha_combined_spy*100:+.1f} bps)")

print(f"\n4-Stage Combined Total:  {combined_4stage_total:+.2%}")
print(f"Baseline Combined Total: {combined_baseline_total:+.2%}")
print(f"SPY Combined Total:      {combined_spy_total:+.2%}")

# Risk-adjusted comparison
print("\n\nRISK-ADJUSTED COMPARISON")
print("-" * 80)

print(f"\n{'Metric':<30} {'S7 4-Stage':>12} {'S7 Baseline':>12} {'S7 SPY':>12} | {'S8 4-Stage':>12} {'S8 Baseline':>12} {'S8 SPY':>12}")
print(f"{'─'*30} {'─'*12} {'─'*12} {'─'*12} | {'─'*12} {'─'*12} {'─'*12}")
print(f"{'Ann Return':<30} {results['S7_4stage']['ann_return']:>11.2%} {results['S7_baseline']['ann_return']:>11.2%} {results['S7_spy']['ann_return']:>11.2%} | {results['S8_4stage']['ann_return']:>11.2%} {results['S8_baseline']['ann_return']:>11.2%} {results['S8_spy']['ann_return']:>11.2%}")
print(f"{'Sharpe':<30} {results['S7_4stage']['sharpe']:>12.2f} {results['S7_baseline']['sharpe']:>12.2f} {results['S7_spy']['sharpe']:>12.2f} | {results['S8_4stage']['sharpe']:>12.2f} {results['S8_baseline']['sharpe']:>12.2f} {results['S8_spy']['sharpe']:>12.2f}")
print(f"{'Max DD':<30} {results['S7_4stage']['max_dd']:>11.2%} {results['S7_baseline']['max_dd']:>11.2%} {results['S7_spy']['max_dd']:>11.2%} | {results['S8_4stage']['max_dd']:>11.2%} {results['S8_baseline']['max_dd']:>11.2%} {results['S8_spy']['max_dd']:>11.2%}")
calmar_4s7 = results['S7_4stage']['ann_return']/abs(results['S7_4stage']['max_dd'])
calmar_b7 = results['S7_baseline']['ann_return']/abs(results['S7_baseline']['max_dd'])
calmar_s7 = results['S7_spy']['ann_return']/abs(results['S7_spy']['max_dd'])
calmar_4s8 = results['S8_4stage']['ann_return']/abs(results['S8_4stage']['max_dd'])
calmar_b8 = results['S8_baseline']['ann_return']/abs(results['S8_baseline']['max_dd'])
calmar_s8 = results['S8_spy']['ann_return']/abs(results['S8_spy']['max_dd'])
print(f"{'Calmar (Ann/DD)':<30} {calmar_4s7:>12.2f} {calmar_b7:>12.2f} {calmar_s7:>12.2f} | {calmar_4s8:>12.2f} {calmar_b8:>12.2f} {calmar_s8:>12.2f}")

# Value Add Summary
print("\n\nVALUE ADD SUMMARY")
print("-" * 80)
print(f"S7 (AI Boom): 4-Stage BEATS baseline by +10.72% ann (+1,072 bps)")
print(f"               4-Stage BEATS SPY by +2.47% ann (+247 bps)")  
print(f"               BUT: Lower Sharpe (1.52 vs 1.62 baseline, 1.88 SPY) - more vol for alpha")
print(f"\nS8 (Growth): 4-Stage LOSES to baseline by -1.48% ann (-148 bps)")
print(f"               4-Stage LOSES to SPY by -19.6% ann (-1,960 bps)")
print(f"               Regime shield DRAGGED performance in choppy/recovery market")
print(f"\nCombined 4Yr: 4-Stage +14.6% ann vs Baseline +10.0% ann = +4.6% alpha")
print(f"               4-Stage +14.6% ann vs SPY +23.2% ann = -8.6% alpha (underperform SPY)")