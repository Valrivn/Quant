"""
Research Study: Sector Spectrum Clustering, Out-of-Sample (OOS) Validation,
and Multi-Year Horizon Value Investing Dynamics.
Branch: experiment/sector-adaptive-harvest

1. Sector-by-Sector Threshold Sensitivity:
   Measures return and win rate for each of the 7 sectors under thresholds [8%, 12%, 15%, 20%, 25%].
2. Out-of-Sample (OOS) Split:
   - In-Sample (IS): 2018-01-31 to 2021-12-31 (48 months: COVID crash + bull run)
   - Out-of-Sample (OOS): 2022-01-31 to 2024-12-31 (36 months: 2022 rate shock bear + 2023-2024 recovery)
3. Multi-Year Holding Horizon Study:
   Tests Value Investing under multi-year patient horizons (1-year, 2-year, 3-year holding periods)
   vs monthly harvest recycling.
"""

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pandas as pd
import numpy as np

from backtesting.run_three_arm_comparative_backtest import (
    load_universe_and_qual_panel,
    load_price_matrices
)

DAMODARAN_SECTOR_BETAS = {
    'Software': 1.25,
    'Hardware': 1.15,
    'Energy': 1.10,
    'Industrial': 1.05,
    'Healthcare': 0.95,
    'Financial': 0.70,
    'Consumer': 0.75,
}

def run_simulation(df_merged, price_matrix, vol_matrix, date_filter=None, threshold_dict=None, holding_min_months=1):
    """
    Runs backtest with sector-specific thresholds and optional minimum holding horizon.
    threshold_dict: dict mapping sector -> float threshold multiplier (e.g. {'Software': 1.15, ...})
    """
    rebalance_dates = sorted(df_merged['rebalance_date'].unique())
    if date_filter is not None:
        rebalance_dates = [d for d in rebalance_dates if date_filter(d)]
        
    holdings = set()
    entry_prices = {}
    holding_entry_date = {}
    holding_sectors = {}
    monthly_net_rets = []
    total_trades = 0
    trade_returns = {sec: [] for sec in DAMODARAN_SECTOR_BETAS}
    
    default_thresh = 1.15
    if threshold_dict is None:
        threshold_dict = {s: default_thresh for s in DAMODARAN_SECTOR_BETAS}
        
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
            sec = holding_sectors.get(t, 'Industrial')
            # Check holding age in months
            ent_date_idx = holding_entry_date.get(t, i)
            age_months = i - ent_date_idx
            
            # Fundamental exit: ROIC <= WACC
            if t in month_slice.index:
                spread = month_slice.loc[t, 'roic_wacc_spread']
                spread_val = spread if isinstance(spread, (int, float)) else spread.iloc[0]
                if spread_val <= 0.0:
                    sells.add(t)
                    if t in price_matrix.columns and t in entry_prices:
                        cp = price_matrix.loc[p_start_date, t]
                        trade_returns[sec].append(cp / entry_prices[t] - 1.0)
                    continue
            
            # Threshold exit (only if minimum holding horizon elapsed)
            if age_months >= holding_min_months:
                if t in price_matrix.columns and t in entry_prices:
                    cur_p = price_matrix.loc[p_start_date, t]
                    ent_p = entry_prices[t]
                    t_mult = threshold_dict.get(sec, default_thresh)
                    if ent_p > 0 and (cur_p / ent_p >= t_mult):
                        sells.add(t)
                        trade_returns[sec].append(cur_p / ent_p - 1.0)
                        continue
                        
            if t not in price_matrix.columns:
                sells.add(t)
                
        for s in sells:
            holdings.remove(s)
            entry_prices.pop(s, None)
            holding_entry_date.pop(s, None)
            holding_sectors.pop(s, None)
            
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
                p_val = price_matrix.loc[p_start_date, tk] if tk in price_matrix.columns else 1.0
                entry_prices[tk] = p_val
                holding_entry_date[tk] = i
                sec_val = month_slice.loc[tk, 'sector_custom']
                sec_str = sec_val if isinstance(sec_val, str) else sec_val.iloc[0]
                holding_sectors[tk] = sec_str
                
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
    cagr = (1.0 + cum_ret) ** (1.0 / n_years) - 1.0 if n_years > 0 else 0.0
    vol = np.std(monthly_net_rets) * np.sqrt(12)
    sharpe = (cagr - 0.02) / (vol + 1e-6)
    
    cum_series = np.cumprod(1.0 + np.array(monthly_net_rets))
    hwm_series = np.maximum.accumulate(cum_series)
    max_dd = np.min((cum_series - hwm_series) / hwm_series) if len(cum_series) > 0 else 0.0
    avg_turnover = total_trades / (len(monthly_net_rets) * 50.0) if len(monthly_net_rets) > 0 else 0.0
    
    return {
        'CAGR': cagr,
        'Sharpe': sharpe,
        'Max DD': max_dd,
        'Turnover/Mo': avg_turnover,
        'Total Trades': total_trades,
        'Trade Returns': trade_returns,
        'Monthly Rets': monthly_net_rets
    }

def main():
    print("[1/4] Loading Full Point-in-Time Universe...")
    df_merged = load_universe_and_qual_panel()
    price_matrix, vol_matrix = load_price_matrices(df_merged['ticker'].unique())
    
    print("\n[2/4] Sector Spectrum Analysis: Where Does Each Sector Fall?")
    print("Testing each sector individually: Fast Harvest (+8%), Value Harvest (+15%), High-Run (+25%)\n")
    sector_summary = []
    for sec in DAMODARAN_SECTOR_BETAS:
        for t_val, label in [(1.08, '+8%'), (1.15, '+15%'), (1.25, '+25%')]:
            t_dict = {s: 1.15 for s in DAMODARAN_SECTOR_BETAS}
            t_dict[sec] = t_val
            res = run_simulation(df_merged, price_matrix, vol_matrix, threshold_dict=t_dict)
            sector_summary.append({
                'Sector': sec,
                'Damodaran Beta': DAMODARAN_SECTOR_BETAS[sec],
                'Tested Threshold': label,
                'CAGR': f"{res['CAGR']*100:.2f}%",
                'Sharpe': f"{res['Sharpe']:.2f}",
                'Max DD': f"{res['Max DD']*100:.2f}%",
                'Trades': res['Total Trades']
            })
    df_sec = pd.DataFrame(sector_summary)
    print("==========================================================================================")
    print("                     SECTOR SPECTRUM SENSITIVITY MATRIX")
    print("==========================================================================================")
    print(df_sec.to_string(index=False))
    print("==========================================================================================")
    
    print("\n[3/4] Out-of-Sample (OOS) Sampling Validation:")
    print("  In-Sample (IS):  2018-01-31 to 2021-12-31 (48 months: COVID crash, recovery, bull run)")
    print("  Out-of-Sample (OOS): 2022-01-31 to 2024-12-31 (36 months: 2022 rate shock bear market + 2023-2024 recovery)")
    
    is_filter = lambda d: d <= pd.Timestamp('2021-12-31')
    oos_filter = lambda d: d >= pd.Timestamp('2022-01-31')
    
    strategies = [
        ("Uniform Baseline (+15%)", {s: 1.15 for s in DAMODARAN_SECTOR_BETAS}),
        ("Uniform Tight (+12%)", {s: 1.12 for s in DAMODARAN_SECTOR_BETAS}),
        ("Asymmetric Spectrum (Tech +15%, Defensive +8%)", {
            'Software': 1.15, 'Hardware': 1.15, 'Energy': 1.15,
            'Industrial': 1.12, 'Healthcare': 1.12,
            'Financial': 1.08, 'Consumer': 1.08
        }),
        ("Fast Tech / Patient Defensive (Tech +12%, Defensive +15%)", {
            'Software': 1.12, 'Hardware': 1.12, 'Energy': 1.12,
            'Industrial': 1.15, 'Healthcare': 1.15,
            'Financial': 1.15, 'Consumer': 1.15
        }),
        ("High Run Trap (Tech +25%, Defensive +9%)", {
            'Software': 1.25, 'Hardware': 1.25, 'Energy': 1.18,
            'Industrial': 1.14, 'Healthcare': 1.14,
            'Financial': 1.09, 'Consumer': 1.09
        }),
    ]
    
    oos_table = []
    for s_name, s_dict in strategies:
        # Full period
        res_full = run_simulation(df_merged, price_matrix, vol_matrix, threshold_dict=s_dict)
        # In-sample
        res_is = run_simulation(df_merged, price_matrix, vol_matrix, date_filter=is_filter, threshold_dict=s_dict)
        # Out-of-sample
        res_oos = run_simulation(df_merged, price_matrix, vol_matrix, date_filter=oos_filter, threshold_dict=s_dict)
        
        oos_table.append({
            'Strategy': s_name,
            'IS CAGR (2018-21)': f"{res_is['CAGR']*100:.2f}%",
            'IS Sharpe': f"{res_is['Sharpe']:.2f}",
            'OOS CAGR (2022-24)': f"{res_oos['CAGR']*100:.2f}%",
            'OOS Sharpe': f"{res_oos['Sharpe']:.2f}",
            'OOS Max DD': f"{res_oos['Max DD']*100:.2f}%",
            'Full CAGR (7-Yr)': f"{res_full['CAGR']*100:.2f}%",
            'Full Sharpe': f"{res_full['Sharpe']:.2f}"
        })
    df_oos = pd.DataFrame(oos_table)
    print("\n==========================================================================================")
    print("            IN-SAMPLE vs OUT-OF-SAMPLE (OOS) VALIDATION REPORT")
    print("==========================================================================================")
    print(df_oos.to_string(index=False))
    print("==========================================================================================")

    print("\n[4/4] Multi-Year Holding Horizon Study: Decadal Patient Value Investing")
    print("What happens when Value Investors hold for 12, 24, or 36 months before applying harvest gates?")
    horizons = [
        ("Monthly Rebalance (1-Month Minimum)", 1),
        ("Patient Compounder (6-Month Minimum)", 6),
        ("Annual Patient Value (12-Month Minimum)", 12),
        ("Multi-Year Horizon (24-Month Minimum)", 24),
        ("Graham-Buffett Horizon (36-Month Minimum)", 36),
    ]
    
    horizon_table = []
    # Test on the robust baseline and the asymmetric spectrum
    for h_name, h_months in horizons:
        res_base = run_simulation(df_merged, price_matrix, vol_matrix, holding_min_months=h_months)
        horizon_table.append({
            'Holding Horizon': h_name,
            'CAGR': f"{res_base['CAGR']*100:.2f}%",
            'Sharpe': f"{res_base['Sharpe']:.2f}",
            'Max DD': f"{res_base['Max DD']*100:.2f}%",
            'Turnover/Mo': f"{res_base['Turnover/Mo']*100:.2f}%",
            'Total Trades': res_base['Total Trades']
        })
    df_horizon = pd.DataFrame(horizon_table)
    print("\n==========================================================================================")
    print("           PATIENT MULTI-YEAR HOLDING HORIZON VALUE INVESTING BENCHMARK")
    print("==========================================================================================")
    print(df_horizon.to_string(index=False))
    print("==========================================================================================")

if __name__ == '__main__':
    main()
