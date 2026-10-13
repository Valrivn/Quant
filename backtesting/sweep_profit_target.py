"""
Experiment: Systematic Harvest Threshold Sweep (Arm 1 Full Quant)
Branch: experiment/profit-harvest-threshold

Tests the impact of the profit-harvesting overvaluation exit threshold (cur_p / entry_p >= thresh)
on Point-in-Time 2018-2024 returns, turnover, and maximum drawdown.
"""

import sys, os
import pandas as pd
import numpy as np

from backtesting.run_three_arm_comparative_backtest import (
    load_universe_and_qual_panel,
    load_price_matrices
)

def run_threshold_sweep():
    print("[1/3] Loading PIT Universe Fundamentals & Price Panel...")
    df_merged = load_universe_and_qual_panel()
    price_matrix, vol_matrix = load_price_matrices(df_merged['ticker'].unique())

    rebalance_dates = sorted(df_merged['rebalance_date'].unique())
    thresholds = [1.08, 1.12, 1.15, 1.20, 1.25, 1.35, 999.0]
    results = []

    print(f"[2/3] Simulating across {len(thresholds)} threshold regimes...")
    for thresh in thresholds:
        holdings = set()
        entry_prices = {}
        monthly_net_rets = []
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
            
            sells = set()
            for t in list(holdings):
                if t in month_slice.index:
                    spread = month_slice.loc[t, 'roic_wacc_spread']
                    spread_val = spread if isinstance(spread, (int, float)) else spread.iloc[0]
                    if spread_val <= 0.0:
                        sells.add(t)
                        continue
                if t in price_matrix.columns and t in entry_prices:
                    cur_p = price_matrix.loc[p_start_date, t]
                    ent_p = entry_prices[t]
                    if ent_p > 0 and (cur_p / ent_p >= thresh):
                        sells.add(t)
                        continue
                if t not in price_matrix.columns:
                    sells.add(t)
                    
            for s in sells:
                holdings.remove(s)
                entry_prices.pop(s, None)
                
            slots_needed = 25 - len(holdings)
            new_buys = set()
            if slots_needed > 0:
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
                picks = cands.sort_values('score', ascending=False).head(slots_needed)
                for tk in picks.index:
                    new_buys.add(tk)
                    holdings.add(tk)
                    entry_prices[tk] = price_matrix.loc[p_start_date, tk] if tk in price_matrix.columns else 1.0
                    
            turnover = (len(sells) + len(new_buys)) / 50.0
            total_trades += (len(sells) + len(new_buys))
            
            # Monthly return
            rets = []
            for t in holdings:
                if t in price_matrix.columns:
                    p0 = price_matrix.loc[p_start_date, t]
                    p1 = price_matrix.loc[p_end_date, t]
                    if p0 > 0:
                        rets.append(p1 / p0 - 1.0)
            eq_ret = np.mean(rets) if len(rets) > 0 else 0.0
            
            # BIL bond return
            b0 = price_matrix.loc[p_start_date, 'BIL'] if 'BIL' in price_matrix.columns else 1.0
            b1 = price_matrix.loc[p_end_date, 'BIL'] if 'BIL' in price_matrix.columns else 1.0
            bond_ret = (b1 / b0 - 1.0) if b0 > 0 else 0.0
            
            # Drawdown circuit breaker
            cum = np.cumprod(1.0 + np.array(monthly_net_rets)) if len(monthly_net_rets) >= 2 else np.array([1.0])
            hwm = np.maximum.accumulate(cum)
            dd = (cum[-1] - hwm[-1]) / hwm[-1] if len(cum) > 0 else 0.0
            bond_w = 0.85 if dd <= -0.15 else (0.50 if dd <= -0.08 else 0.15)
            
            gross = (1.0 - bond_w) * eq_ret + bond_w * bond_ret
            drag = turnover * (15.0 / 10000.0)
            net = gross - drag
            monthly_net_rets.append(net)
            
        cum_ret = np.prod(1.0 + np.array(monthly_net_rets)) - 1.0
        n_years = len(monthly_net_rets) / 12.0
        cagr = (1.0 + cum_ret) ** (1.0 / n_years) - 1.0
        vol = np.std(monthly_net_rets) * np.sqrt(12)
        sharpe = (cagr - 0.02) / (vol + 1e-6)
        
        cum_series = np.cumprod(1.0 + np.array(monthly_net_rets))
        hwm_series = np.maximum.accumulate(cum_series)
        max_dd = np.min((cum_series - hwm_series) / hwm_series)
        avg_turnover = total_trades / (len(monthly_net_rets) * 50.0)
        
        label = f"+{int((thresh-1.0)*100)}%" if thresh < 10 else "No Cap (Fundamental Only)"
        results.append({
            'Threshold': label,
            'CAGR': f"{cagr*100:.2f}%",
            'Sharpe': f"{sharpe:.2f}",
            'Max DD': f"{max_dd*100:.2f}%",
            'Turnover/Mo': f"{avg_turnover*100:.2f}%",
            'Total Trades': total_trades
        })

    res_df = pd.DataFrame(results)
    print("\n[3/3] Sweep Complete:")
    print("==========================================================================")
    print("      OVERVALUATION HARVEST THRESHOLD PARAMETER SWEEP (ARM 1)")
    print("==========================================================================")
    print(res_df.to_string(index=False))
    print("==========================================================================")
    return res_df

if __name__ == '__main__':
    run_threshold_sweep()
