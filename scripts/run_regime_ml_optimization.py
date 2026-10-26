"""
scripts/run_regime_ml_optimization.py

Point-in-Time (PIT) Regime-Conditional Machine Learning Parameter Optimization:
1. Partitions historical 2018-2024 dates strictly Point-in-Time:
   - Bull Market: SPY 6-month trailing return > +4% and rolling vol < 20%
   - Bear Market: SPY trailing drawdown <= -10% or 6-month trailing return < -5%
   - Stagnant Market: Choppy/sideways range (-5% <= 6m return <= +4%)
2. Uses SciPy differential evolution / constrained optimization on parameters:
   - w_quant vs w_qual (Quant economic rents vs Qualitative moat conviction)
   - w_bond (Protective Treasury BIL cushion)
   - take_profit (Profit harvesting threshold)
   - turnover_deadband (Rebalancing hysteresis threshold)
3. Evaluates whether optimal parameters diverge across market regimes,
   answering whether genuine regime-conditioned alpha exists.
"""

import os
import sys
import json
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import minimize

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtesting.run_three_arm_comparative_backtest import (
    load_universe_and_qual_panel,
    load_price_matrices
)


def classify_pit_regimes(price_matrix: pd.DataFrame, rebalance_dates: List[pd.Timestamp]) -> Dict[str, List[pd.Timestamp]]:
    """
    Classifies each rebalance date strictly Point-in-Time using data <= rebalance_date.
    Returns mapping of regime -> list of rebalance dates.
    """
    spy = price_matrix['SPY'].dropna()
    regimes = {'BULL': [], 'BEAR': [], 'STAGNANT': []}
    
    for d in rebalance_dates:
        # Strict PIT: lookback strictly on data <= d
        spy_hist = spy[spy.index <= d]
        if len(spy_hist) < 126:
            regimes['STAGNANT'].append(d)
            continue
            
        p_cur = spy_hist.iloc[-1]
        p_6m = spy_hist.iloc[-126]
        ret_6m = (p_cur / p_6m) - 1.0
        
        # Trailing 60-day vol
        ret_daily = spy_hist.pct_change().dropna()
        vol_60d = float(ret_daily.iloc[-60:].std() * np.sqrt(252)) if len(ret_daily) >= 60 else 0.15
        
        # Trailing 1-year drawdown
        spy_1y = spy_hist.iloc[-252:] if len(spy_hist) >= 252 else spy_hist
        hwm = spy_1y.max()
        dd = (p_cur - hwm) / hwm
        
        if dd <= -0.10 or ret_6m < -0.05:
            regimes['BEAR'].append(d)
        elif ret_6m > 0.04 and vol_60d < 0.20:
            regimes['BULL'].append(d)
        else:
            regimes['STAGNANT'].append(d)
            
    return regimes


def evaluate_regime_performance(
    params: np.ndarray,
    target_dates: List[pd.Timestamp],
    df_merged: pd.DataFrame,
    price_matrix: pd.DataFrame,
    rebalance_dates_sorted: List[pd.Timestamp]
) -> float:
    """
    Simulates portfolio across specific regime dates and returns negative Sharpe loss.
    params = [w_quant, w_bond, take_profit, deadband]
    """
    w_quant = float(params[0])
    w_qual = 1.0 - w_quant
    w_bond = float(params[1])
    take_profit = float(params[2])
    deadband = float(params[3])
    
    target_set = set(target_dates)
    date_to_idx = {d: i for i, d in enumerate(rebalance_dates_sorted)}
    
    returns = []
    
    for cur_date in target_dates:
        idx = date_to_idx.get(cur_date)
        if idx is None or idx >= len(rebalance_dates_sorted) - 1:
            continue
        next_date = rebalance_dates_sorted[idx + 1]
        
        avail_dates = price_matrix.index
        t_starts = avail_dates[avail_dates >= cur_date]
        t_ends = avail_dates[avail_dates >= next_date]
        if len(t_starts) == 0 or len(t_ends) == 0:
            continue
        p_start = t_starts[0]
        p_end = t_ends[0]
        
        month_slice = df_merged[df_merged['rebalance_date'] == cur_date].set_index('ticker')
        
        # Candidate selection
        cands = month_slice[
            (month_slice['roic_wacc_spread'] > 0.0) &
            (month_slice['fcf_margin'] > 0.0) &
            (month_slice['archetype'] != 'High_Friction_Distress') &
            (month_slice.index.isin(price_matrix.columns))
        ].copy()
        
        if len(cands) == 0:
            continue
            
        sp_z = (cands['roic_wacc_spread'] - cands['roic_wacc_spread'].mean()) / (cands['roic_wacc_spread'].std() + 1e-6)
        fc_z = (cands['fcf_margin'] - cands['fcf_margin'].mean()) / (cands['fcf_margin'].std() + 1e-6)
        q_sc = (cands['qual_score_Q'] - cands['qual_score_Q'].mean()) / (cands['qual_score_Q'].std() + 1e-6)
        
        q_composite = sp_z + 0.5 * fc_z
        score = (w_quant * q_composite + w_qual * q_sc) * cands['archetype_multiplier']
        picks = cands.assign(score=score).sort_values('score', ascending=False).head(20).index
        
        # Calculate returns
        stock_rets = []
        for tk in picks:
            p0 = price_matrix.loc[p_start, tk]
            p1 = price_matrix.loc[p_end, tk]
            if p0 > 0:
                ret = p1 / p0 - 1.0
                # Take profit ceiling
                if ret >= take_profit:
                    ret = take_profit
                stock_rets.append(ret)
                
        eq_ret = float(np.mean(stock_rets)) if stock_rets else 0.0
        
        # BIL bond return
        if 'BIL' in price_matrix.columns:
            b0 = price_matrix.loc[p_start, 'BIL']
            b1 = price_matrix.loc[p_end, 'BIL']
            bond_ret = (b1 / b0 - 1.0) if b0 > 0 else 0.0015
        else:
            bond_ret = 0.0015
            
        # Friction penalty for low deadband
        friction_drag = 0.0015 * (1.0 - deadband)
        net_m_ret = (1.0 - w_bond) * eq_ret + w_bond * bond_ret - friction_drag
        returns.append(net_m_ret)
        
    if len(returns) < 4:
        return 0.0
        
    ret_arr = np.array(returns)
    mean_r = np.mean(ret_arr)
    std_r = np.std(ret_arr)
    if std_r < 1e-6:
        return -float(mean_r)
        
    # Annualized Sharpe objective (loss is negative Sharpe)
    sharpe = (mean_r / std_r) * np.sqrt(12.0)
    return -float(sharpe)


def main():
    print("=" * 80)
    print("REGIME-CONDITIONAL MACHINE LEARNING PARAMETER OPTIMIZATION")
    print("=" * 80)
    
    df_merged = load_universe_and_qual_panel()
    tickers = df_merged['ticker'].unique().tolist()
    price_matrix, vol_matrix = load_price_matrices(tickers)
    
    rebalance_dates = sorted(df_merged['rebalance_date'].unique())
    print("\n[Step 1] Classifying Historical Dates into Point-in-Time Regimes...")
    regimes = classify_pit_regimes(price_matrix, rebalance_dates)
    
    for reg, d_list in regimes.items():
        print(f"  -> {reg:<8}: {len(d_list)} monthly periods ({len(d_list)/len(rebalance_dates)*100:.1f}%)")
        
    # Bounds for parameters: [w_quant, w_bond, take_profit, deadband]
    bounds = [
        (0.20, 0.90),  # w_quant (balance is w_qual)
        (0.00, 0.50),  # w_bond
        (0.10, 0.40),  # take_profit
        (0.05, 0.25),  # deadband
    ]
    initial_guess = [0.70, 0.15, 0.20, 0.15]
    
    results = {}
    
    print("\n[Step 2] Optimizing Parameters Conditionally Per Regime...")
    for reg, d_list in regimes.items():
        if len(d_list) < 5:
            continue
        print(f"\n  Running ML Optimization for [{reg}] Market ({len(d_list)} periods)...")
        opt = minimize(
            evaluate_regime_performance,
            x0=initial_guess,
            args=(d_list, df_merged, price_matrix, rebalance_dates),
            bounds=bounds,
            method='L-BFGS-B',
            options={'maxiter': 50, 'ftol': 1e-4}
        )
        
        opt_params = opt.x
        opt_sharpe = -opt.fun
        
        # Baseline comparison with default parameters [0.70, 0.15, 0.20, 0.15]
        base_sharpe = -evaluate_regime_performance(
            np.array(initial_guess), d_list, df_merged, price_matrix, rebalance_dates
        )
        
        results[reg] = {
            'months_count': len(d_list),
            'w_quant': round(float(opt_params[0]), 3),
            'w_qual': round(float(1.0 - opt_params[0]), 3),
            'w_bond': round(float(opt_params[1]), 3),
            'take_profit': round(float(opt_params[2]), 3),
            'deadband': round(float(opt_params[3]), 3),
            'optimized_sharpe': round(float(opt_sharpe), 2),
            'baseline_sharpe': round(float(base_sharpe), 2),
            'sharpe_delta': round(float(opt_sharpe - base_sharpe), 2)
        }
        
    out_path = REPO_ROOT / 'data' / 'backtests' / 'regime_ml_optimization_results.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(results, f, indent=2)
        
    print("\n" + "=" * 80)
    print("REGIME-CONDITIONAL MACHINE LEARNING OPTIMIZATION SUMMARY")
    print("=" * 80)
    summary_df = pd.DataFrame(results).T
    print(summary_df.to_string())
    print("\nResults saved to:", out_path)
    print("=" * 80)


if __name__ == '__main__':
    main()
