"""
Research Study: Pure Equity (0% Bonds) Static Allocation & Portfolio Distribution
Branch: main

Tests:
1. 100% Pure Equity (Zero BIL cash cushion drag).
2. Portfolio Concentration Sweep: Number of slots N in [10, 15, 20, 25, 30].
3. Static Multi-Arm Blends: Testing fixed allocations between Arm 1 (Quant) and Arm 2 (Qual Moat).
4. Weighting Schemes: Equal-Weight (1/N) vs Conviction-Weight (Score-proportional).
5. Benchmark against S&P 500 (SPY 13.00% CAGR).
"""

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pandas as pd
import numpy as np

from backtesting.run_three_arm_comparative_backtest import (
    load_universe_and_qual_panel,
    load_price_matrices
)
from backtesting.run_enhanced_arms import compute_15_metrics

def simulate_pure_equity(
    df_merged: pd.DataFrame,
    price_matrix: pd.DataFrame,
    vol_matrix: pd.DataFrame,
    n_slots: int = 25,
    arm_type: str = 'arm1',             # 'arm1', 'arm2', 'blend_50_50', 'blend_70_30', 'blend_30_70'
    weighting: str = 'equal',           # 'equal', 'conviction'
    harvest_threshold: float = 1.15,
    holding_min_months: int = 1
):
    rebalance_dates = sorted(df_merged['rebalance_date'].unique())
    holdings = set()
    entry_prices = {}
    holding_entry_date = {}
    monthly_net_rets = []
    monthly_spy_rets = []
    total_trades = 0
    
    for i in range(len(rebalance_dates) - 1):
        cur_date = rebalance_dates[i]
        next_date = rebalance_dates[i + 1]
        avail_dates = price_matrix.index
        t_starts = avail_dates[avail_dates >= cur_date]
        t_ends = avail_dates[avail_dates >= next_date]
        if len(t_starts) == 0 or len(t_ends) == 0:
            continue
        p_start_date = t_starts[0]
        p_end_date = t_ends[0]
        
        month_slice = df_merged[df_merged['rebalance_date'] == cur_date].set_index('ticker')
        
        # 1. Sells
        sells = set()
        for t in list(holdings):
            age_mo = i - holding_entry_date.get(t, i)
            # Fundamental exit
            if t in month_slice.index:
                spread = month_slice.loc[t, 'roic_wacc_spread']
                spread_val = spread if isinstance(spread, (int, float)) else spread.iloc[0]
                if 'arm1' in arm_type and spread_val <= 0.0:
                    sells.add(t)
                    continue
                arch = month_slice.loc[t, 'archetype']
                arch_val = arch if isinstance(arch, str) else arch.iloc[0]
                if 'arm2' in arm_type and arch_val == 'High_Friction_Distress':
                    sells.add(t)
                    continue
            # Harvest exit
            if age_mo >= holding_min_months and t in price_matrix.columns and t in entry_prices:
                cur_p = price_matrix.loc[p_start_date, t]
                ent_p = entry_prices[t]
                if ent_p > 0 and (cur_p / ent_p >= harvest_threshold):
                    sells.add(t)
                    continue
            if t not in price_matrix.columns:
                sells.add(t)
                
        for s in sells:
            holdings.remove(s)
            entry_prices.pop(s, None)
            holding_entry_date.pop(s, None)
            
        # 2. Buys
        slots_needed = n_slots - len(holdings)
        new_buys = set()
        if slots_needed > 0:
            if arm_type == 'arm1':
                cands = month_slice[
                    (month_slice['roic_wacc_spread'] > 0.0) &
                    (month_slice['fcf_margin'] > 0.0) &
                    (month_slice.index.isin(price_matrix.columns))
                ].copy()
                valid = [
                    tk for tk in cands.index 
                    if tk not in holdings and (
                        tk not in vol_matrix.columns or 
                        np.isnan(vol_matrix.loc[p_start_date, tk]) or 
                        (0.15 <= vol_matrix.loc[p_start_date, tk] <= 0.40)
                    )
                ]
                cands = cands.loc[cands.index.isin(valid)].copy()
                spread_z = (cands['roic_wacc_spread'] - cands['roic_wacc_spread'].mean()) / (cands['roic_wacc_spread'].std() + 1e-6)
                fcf_z = (cands['fcf_margin'] - cands['fcf_margin'].mean()) / (cands['fcf_margin'].std() + 1e-6)
                cands['score'] = spread_z + 0.5 * fcf_z
            elif arm_type == 'arm2':
                cands = month_slice[
                    (month_slice['archetype'] != 'High_Friction_Distress') &
                    (month_slice.index.isin(price_matrix.columns))
                ].copy()
                valid = [tk for tk in cands.index if tk not in holdings]
                cands = cands.loc[cands.index.isin(valid)].copy()
                alpha_z = (cands['alpha_spread'] - cands['alpha_spread'].mean()) / (cands['alpha_spread'].std() + 1e-6)
                q_z = (cands['qual_score_Q'] - cands['qual_score_Q'].mean()) / (cands['qual_score_Q'].std() + 1e-6)
                cands['score'] = (alpha_z + q_z) * cands['archetype_multiplier']
            else: # Blend: convex combination
                cands = month_slice[
                    (month_slice['roic_wacc_spread'] > 0.0) &
                    (month_slice['archetype'] != 'High_Friction_Distress') &
                    (month_slice.index.isin(price_matrix.columns))
                ].copy()
                valid = [tk for tk in cands.index if tk not in holdings]
                cands = cands.loc[cands.index.isin(valid)].copy()
                spread_z = (cands['roic_wacc_spread'] - cands['roic_wacc_spread'].mean()) / (cands['roic_wacc_spread'].std() + 1e-6)
                alpha_z = (cands['alpha_spread'] - cands['alpha_spread'].mean()) / (cands['alpha_spread'].std() + 1e-6)
                w1 = 0.5 if '50_50' in arm_type else (0.7 if '70_30' in arm_type else 0.3)
                cands['score'] = w1 * spread_z + (1.0 - w1) * alpha_z
                
            picks = cands.sort_values('score', ascending=False).head(slots_needed)
            for tk in picks.index:
                new_buys.add(tk)
                holdings.add(tk)
                entry_prices[tk] = price_matrix.loc[p_start_date, tk] if tk in price_matrix.columns else 1.0
                holding_entry_date[tk] = i
                
        turnover = (len(sells) + len(new_buys)) / (2.0 * n_slots)
        total_trades += (len(sells) + len(new_buys))
        
        # Monthly return: 100% Equities (0% BIL Bonds)
        rets = []
        scores = []
        for t in holdings:
            if t in price_matrix.columns:
                p0 = price_matrix.loc[p_start_date, t]
                p1 = price_matrix.loc[p_end_date, t]
                if p0 > 0:
                    rets.append(p1 / p0 - 1.0)
                    if weighting == 'conviction' and t in month_slice.index:
                        sc = month_slice.loc[t, 'roic_wacc_spread']
                        scores.append(max(float(sc if isinstance(sc, (int, float)) else sc.iloc[0]), 0.01))
                    else:
                        scores.append(1.0)
                        
        if len(rets) > 0:
            weights = np.array(scores) / np.sum(scores)
            eq_ret = float(np.sum(weights * np.array(rets)))
        else:
            eq_ret = 0.0
            
        # Benchmark SPY
        s0 = price_matrix.loc[p_start_date, 'SPY'] if 'SPY' in price_matrix.columns else 1.0
        s1 = price_matrix.loc[p_end_date, 'SPY'] if 'SPY' in price_matrix.columns else 1.0
        spy_ret = (s1 / s0 - 1.0) if s0 > 0 else 0.0
        monthly_spy_rets.append(spy_ret)
        
        # Pure Equity Return with 15 bps fee drag (No bond dilution!)
        drag = turnover * (15.0 / 10000.0)
        net_ret = eq_ret - drag
        monthly_net_rets.append(net_ret)
        
    metrics = compute_15_metrics(monthly_net_rets, monthly_spy_rets, total_trades)
    return metrics

def run_study():
    print("[1/3] Loading Universe & Prices...")
    df_merged = load_universe_and_qual_panel()
    tickers = df_merged['ticker'].unique()
    price_matrix, vol_matrix = load_price_matrices(tickers)
    
    print("\n[2/3] Experiment 1: 100% Pure Equities (0% Bonds) Across Concentration Slots (N = 10, 15, 20, 25, 30)")
    slot_results = []
    for n in [10, 15, 20, 25, 30]:
        m1 = simulate_pure_equity(df_merged, price_matrix, vol_matrix, n_slots=n, arm_type='arm1', harvest_threshold=1.15)
        m2 = simulate_pure_equity(df_merged, price_matrix, vol_matrix, n_slots=n, arm_type='arm2', harvest_threshold=1.25)
        slot_results.append({
            'Slots (N)': n,
            'Arm 1 CAGR': m1['CAGR (%)'],
            'Arm 1 Sharpe': m1['Net Sharpe Ratio (Rf = 2%)'],
            'Arm 1 Max DD': m1['Maximum Drawdown'],
            'Arm 2 CAGR': m2['CAGR (%)'],
            'Arm 2 Sharpe': m2['Net Sharpe Ratio (Rf = 2%)'],
            'Arm 2 Max DD': m2['Maximum Drawdown'],
        })
    df_slots = pd.DataFrame(slot_results)
    print("==========================================================================================")
    print("               100% EQUITIES: CONCENTRATION (SLOTS) SWEEP")
    print("==========================================================================================")
    print(df_slots.to_string(index=False))
    print("==========================================================================================")
    
    print("\n[3/3] Experiment 2: Static Multi-Arm Blends (100% Equity, Zero Market Timing)")
    blend_results = []
    blends = [
        ("100% Arm 1 (Full Quant)", 'arm1', 1.15),
        ("100% Arm 2 (Qual Moat)", 'arm2', 1.25),
        ("70% Arm 1 / 30% Arm 2", 'blend_70_30', 1.18),
        ("50% Arm 1 / 50% Arm 2", 'blend_50_50', 1.20),
        ("30% Arm 1 / 70% Arm 2", 'blend_30_70', 1.22),
    ]
    for b_name, b_type, thresh in blends:
        m_res = simulate_pure_equity(df_merged, price_matrix, vol_matrix, n_slots=15, arm_type=b_type, harvest_threshold=thresh)
        blend_results.append({
            'Static Strategy': b_name,
            'CAGR (%)': m_res['CAGR (%)'],
            'Cumulative Return': m_res['Cumulative Net Return'],
            'Net Sharpe': m_res['Net Sharpe Ratio (Rf = 2%)'],
            'Sortino': m_res['Sortino Ratio'],
            'Max Drawdown': m_res['Maximum Drawdown'],
            'Market Beta': m_res['Market Beta vs SPY'],
            'Jensen Alpha': m_res['Jensen\'s Alpha'],
            'Monthly Turnover': m_res['Average Monthly Turnover'],
        })
    df_blends = pd.DataFrame(blend_results)
    print("==========================================================================================")
    print("            100% EQUITIES: STATIC ALLOCATION BENCHMARK (VS S&P 500)")
    print("==========================================================================================")
    print(df_blends.to_string(index=False))
    print("------------------------------------------------------------------------------------------")
    print("S&P 500 Benchmark: CAGR = 13.00% | Sharpe = 0.64 | Sortino = 0.97 | Max DD = -23.93%")
    print("==========================================================================================")

if __name__ == '__main__':
    run_study()
