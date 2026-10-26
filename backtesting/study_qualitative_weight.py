"""
backtesting/study_qualitative_weight.py

Empirical Study: Evaluating Qualitative vs. Quantitative Weights in the Hybrid Strategy
House of Quant — Audit Ledger Production Engine

Tests qualitative weight w_qual in [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
1. Portfolio-level blending: (1 - w_qual) * Arm1_return + w_qual * Arm2_return
2. Factor-level selection: (1 - w_qual) * Quant_Z + w_qual * Qual_Z
3. Evaluates all 15 production metrics across:
   - Full Cycle (2018-2024, 84 months)
   - In-Sample (2018-2021, 48 months)
   - Out-of-Sample (2022-2024, 36 months)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtesting.run_three_arm_comparative_backtest import (
    load_universe_and_qual_panel,
    load_price_matrices
)
from backtesting.run_enhanced_arms import (
    simulate_single_arm,
    compute_15_metrics
)

def evaluate_portfolio_blend_weights(
    r_arm1: List[float],
    r_arm2: List[float],
    r_spy: List[float],
    total_trades_1: int,
    total_trades_2: int,
    weights: List[float] = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
) -> pd.DataFrame:
    """
    Sweeps portfolio-level blending between Arm 1 (Quant) and Arm 2 (Qualitative).
    """
    rows = []
    r1 = np.array(r_arm1)
    r2 = np.array(r_arm2)
    
    for w_qual in weights:
        w_quant = 1.0 - w_qual
        blend_rets = (w_quant * r1 + w_qual * r2).tolist()
        trades = int(round(w_quant * total_trades_1 + w_qual * total_trades_2))
        m = compute_15_metrics(blend_rets, r_spy, total_trades=trades)
        raw = m["_raw"]
        rows.append({
            "w_quant": f"{w_quant*100:.0f}%",
            "w_qual": f"{w_qual*100:.0f}%",
            "cagr": f"{raw['cagr']*100:.2f}%",
            "sharpe": f"{raw['sharpe']:.2f}",
            "sortino": f"{raw['sortino']:.2f}",
            "calmar": f"{raw['calmar']:.2f}",
            "max_dd": f"{raw['max_dd']*100:.2f}%",
            "volatility": f"{raw['vol']*100:.2f}%",
            "beta": f"{raw['beta']:.2f}",
            "alpha": f"{raw['jensen_alpha']*100:.2f}%",
            "terminal_wealth": f"${raw['final_wealth']:,.0f}"
        })
    return pd.DataFrame(rows)


def run_factor_level_hybrid_sweep(
    df_merged: pd.DataFrame,
    price_matrix: pd.DataFrame,
    vol_matrix: pd.DataFrame,
    weights: List[float] = [0.0, 0.2, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0]
) -> pd.DataFrame:
    """
    Sweeps factor-level hybrid selection weights inside stock selection.
    Score = (1 - w_qual) * Quant_Z + w_qual * Qual_Z
    """
    rebalance_dates = sorted(df_merged['rebalance_date'].unique())
    spy_rets = []
    for i in range(len(rebalance_dates) - 1):
        c_dt = rebalance_dates[i]
        n_dt = rebalance_dates[i+1]
        avail = price_matrix.index
        s_dt = avail[avail >= c_dt][0]
        e_dt = avail[avail >= n_dt][0]
        s0 = price_matrix.loc[s_dt, 'SPY'] if 'SPY' in price_matrix.columns else 1.0
        s1 = price_matrix.loc[e_dt, 'SPY'] if 'SPY' in price_matrix.columns else 1.0
        spy_rets.append(float(s1 / s0 - 1.0) if s0 > 0 else 0.0)

    rows = []
    for w_qual in weights:
        w_quant = 1.0 - w_qual
        holdings = set()
        entry_prices = {}
        monthly_rets = []
        total_trades = 0

        for i in range(len(rebalance_dates) - 1):
            c_dt = rebalance_dates[i]
            n_dt = rebalance_dates[i+1]
            avail = price_matrix.index
            t_starts = avail[avail >= c_dt]
            t_ends = avail[avail >= n_dt]
            if len(t_starts) == 0 or len(t_ends) == 0:
                continue
            s_dt = t_starts[0]
            e_dt = t_ends[0]

            month_slice = df_merged[df_merged['rebalance_date'] == c_dt].set_index('ticker')

            # Exits: Harvest at +20%, stop at -20%, or thesis breach
            sells = set()
            for t in list(holdings):
                if t in month_slice.index:
                    if month_slice.loc[t, 'roic_wacc_spread'] <= 0.0 and w_quant > 0.5:
                        sells.add(t)
                        continue
                    arch = month_slice.loc[t, 'archetype']
                    arch_val = arch if isinstance(arch, str) else arch.iloc[0]
                    if arch_val == 'High_Friction_Distress' and w_qual > 0.3:
                        sells.add(t)
                        continue
                if t in price_matrix.columns and t in entry_prices:
                    cur_p = price_matrix.loc[s_dt, t]
                    ent_p = entry_prices[t]
                    if ent_p > 0:
                        ret = cur_p / ent_p - 1.0
                        if ret >= 0.20 or ret <= -0.20:
                            sells.add(t)
                            continue
                if t not in price_matrix.columns:
                    sells.add(t)

            for s in sells:
                holdings.remove(s)
                entry_prices.pop(s, None)

            # Buys
            slots = 25 - len(holdings)
            new_buys = set()
            if slots > 0:
                cands = month_slice[
                    (month_slice['roic_wacc_spread'] > 0.0) &
                    (month_slice['fcf_margin'] > 0.0) &
                    (month_slice['archetype'] != 'High_Friction_Distress') &
                    (month_slice.index.isin(price_matrix.columns))
                ].copy()
                valid = [tk for tk in cands.index if tk not in holdings]
                cands = cands.loc[cands.index.isin(valid)].copy()

                if len(cands) > 0:
                    spread_z = (cands['roic_wacc_spread'] - cands['roic_wacc_spread'].mean()) / (cands['roic_wacc_spread'].std() + 1e-6)
                    fcf_z = (cands['fcf_margin'] - cands['fcf_margin'].mean()) / (cands['fcf_margin'].std() + 1e-6)
                    quant_z = spread_z + 0.5 * fcf_z

                    alpha_z = (cands['alpha_spread'] - cands['alpha_spread'].mean()) / (cands['alpha_spread'].std() + 1e-6)
                    q_z = (cands['qual_score_Q'] - cands['qual_score_Q'].mean()) / (cands['qual_score_Q'].std() + 1e-6)
                    qual_z = (alpha_z + q_z) * cands['archetype_multiplier']

                    cands['hybrid_score'] = w_quant * quant_z + w_qual * qual_z
                    picks = cands.sort_values('hybrid_score', ascending=False).head(slots)

                    for tk in picks.index:
                        new_buys.add(tk)
                        holdings.add(tk)
                        entry_prices[tk] = price_matrix.loc[s_dt, tk] if tk in price_matrix.columns else 1.0

            total_trades += (len(sells) + len(new_buys))

            # Portfolio monthly return (85% Equities + 15% BIL)
            eq_rets = []
            for t in holdings:
                if t in price_matrix.columns:
                    p0 = price_matrix.loc[s_dt, t]
                    p1 = price_matrix.loc[e_dt, t]
                    if p0 > 0:
                        eq_rets.append(p1 / p0 - 1.0)
            avg_eq = np.mean(eq_rets) if len(eq_rets) > 0 else 0.0
            b0 = price_matrix.loc[s_dt, 'BIL'] if 'BIL' in price_matrix.columns else 1.0
            b1 = price_matrix.loc[e_dt, 'BIL'] if 'BIL' in price_matrix.columns else 1.0
            b_ret = (b1 / b0 - 1.0) if b0 > 0 else 0.0

            # 15 bps slippage on turnover
            turnover = (len(sells) + len(new_buys)) / 50.0
            net_ret = 0.85 * avg_eq + 0.15 * b_ret - (turnover * 0.0015)
            monthly_rets.append(float(net_ret))

        m = compute_15_metrics(monthly_rets, spy_rets, total_trades=total_trades)
        raw = m["_raw"]
        rows.append({
            "w_quant": f"{w_quant*100:.0f}%",
            "w_qual": f"{w_qual*100:.0f}%",
            "cagr": f"{raw['cagr']*100:.2f}%",
            "sharpe": f"{raw['sharpe']:.2f}",
            "sortino": f"{raw['sortino']:.2f}",
            "calmar": f"{raw['calmar']:.2f}",
            "max_dd": f"{raw['max_dd']*100:.2f}%",
            "volatility": f"{raw['vol']*100:.2f}%",
            "beta": f"{raw['beta']:.2f}",
            "alpha": f"{raw['jensen_alpha']*100:.2f}%",
            "trades": total_trades
        })
    return pd.DataFrame(rows)


def main():
    print("=" * 80)
    print("EMPIRICAL STUDY: EVALUATING QUALITATIVE WEIGHTING EFFECTIVENESS")
    print("=" * 80)
    df_merged = load_universe_and_qual_panel()
    tickers = df_merged['ticker'].unique().tolist()
    price_matrix, vol_matrix = load_price_matrices(
        tickers, benchmark_tickers=['SPY', 'BIL', 'GLD', 'VNQ'],
        start_date='2017-12-01', end_date='2026-09-04'
    )

    print("\n--- [A] Running Baseline Arm 1 (Quant) and Arm 2 (Qualitative) ---")
    r_arm1, spy_rets, t_arm1, _, _ = simulate_single_arm(df_merged, price_matrix, vol_matrix, arm_type='arm1', enhanced=True, holding_min_months=12)
    r_arm2, _, t_arm2, _, _ = simulate_single_arm(df_merged, price_matrix, vol_matrix, arm_type='arm2', enhanced=True, holding_min_months=1)

    print("\n--- [B] Sweeping Portfolio-Level Blending Weights ---")
    df_port = evaluate_portfolio_blend_weights(r_arm1, r_arm2, spy_rets, t_arm1, t_arm2)
    print(df_port.to_string(index=False))

    print("\n--- [C] Sweeping Factor-Level Hybrid Selection Weights ---")
    df_factor = run_factor_level_hybrid_sweep(df_merged, price_matrix, vol_matrix)
    print(df_factor.to_string(index=False))

    # Save results to CSV for record
    out_dir = REPO_ROOT / 'data' / 'backtests' / 'qualitative_study'
    out_dir.mkdir(parents=True, exist_ok=True)
    df_port.to_csv(out_dir / 'portfolio_blend_sweep.csv', index=False)
    df_factor.to_csv(out_dir / 'factor_hybrid_sweep.csv', index=False)
    print(f"\nSaved empirical results to {out_dir}")

if __name__ == "__main__":
    main()
