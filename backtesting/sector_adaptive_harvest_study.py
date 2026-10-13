"""
Research Experiment: Sector-Adaptive & Volatility-Tailored Selling Algorithms
Branch: experiment/sector-adaptive-harvest

Investigates whether tailoring profit-harvesting exit thresholds to Damodaran sector betas
and company-level realized volatility generates genuine alpha vs the uniform +15% baseline.

Tested Algorithms:
1. Baseline: Uniform +15% exit across all sectors.
2. Damodaran Sector-Beta Proportional Scaling:
     Threshold(s) = 1.0 + Base * (Beta_unlevered(s) / Mean_Beta)
3. Company-Level Realized Volatility Adaptive Band:
     Threshold(i) = 1.0 + k * sigma_i (annualized vol mapped to holding period)
4. Empirical Optimal Sector Grid Search:
     Systematic search over sector-specific exit thresholds [8%, 12%, 15%, 20%, 25%].
5. Trailing Ratchet with Sector-Scaled Activation:
     Once a stock clears fair value, let high-beta run while trailing local peak.
"""

import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import pandas as pd
import numpy as np

from backtesting.run_three_arm_comparative_backtest import (
    load_universe_and_qual_panel,
    load_price_matrices
)

# Damodaran median unlevered betas from config/sector_beta.yaml
DAMODARAN_SECTOR_BETAS = {
    'Software': 1.25,
    'Hardware': 1.15,
    'Energy': 1.10,
    'Industrial': 1.05,
    'Healthcare': 0.95,
    'Financial': 0.70,   # Reflects banks (0.35) and REITs/Real Estate (0.45)
    'Consumer': 0.75,    # Blend of staples (0.55) and discretionary (0.90)
}
MEAN_BETA = float(np.mean(list(DAMODARAN_SECTOR_BETAS.values())))  # ~0.99

def simulate_strategy(df_merged, price_matrix, vol_matrix, threshold_fn, algo_name=""):
    """
    Simulates Arm 1 with a custom threshold function:
    threshold_fn(ticker, sector, realized_vol, cur_p, entry_p, peak_p) -> bool (should_sell)
    """
    rebalance_dates = sorted(df_merged['rebalance_date'].unique())
    holdings = set()
    entry_prices = {}
    peak_prices = {}
    holding_sectors = {}
    monthly_net_rets = []
    total_trades = 0
    trades_by_sector = {s: 0 for s in DAMODARAN_SECTOR_BETAS}
    
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
        
        # 1. Update peak prices for active holdings
        for t in holdings:
            if t in price_matrix.columns:
                cur_p = price_matrix.loc[p_start_date, t]
                if cur_p > peak_prices.get(t, 0.0):
                    peak_prices[t] = cur_p
                    
        # 2. Check Sells
        sells = set()
        for t in list(holdings):
            # Fundamental exit: ROIC <= WACC
            if t in month_slice.index:
                spread = month_slice.loc[t, 'roic_wacc_spread']
                spread_val = spread if isinstance(spread, (int, float)) else spread.iloc[0]
                if spread_val <= 0.0:
                    sells.add(t)
                    continue
            
            # Pricing / Harvest Exit
            if t in price_matrix.columns and t in entry_prices:
                cur_p = price_matrix.loc[p_start_date, t]
                ent_p = entry_prices[t]
                pk_p = peak_prices.get(t, cur_p)
                sec = holding_sectors.get(t, 'Industrial')
                r_vol = vol_matrix.loc[p_start_date, t] if (t in vol_matrix.columns and p_start_date in vol_matrix.index) else 0.25
                if np.isnan(r_vol):
                    r_vol = 0.25
                    
                if ent_p > 0:
                    should_harvest = threshold_fn(t, sec, r_vol, cur_p, ent_p, pk_p)
                    if should_harvest:
                        sells.add(t)
                        trades_by_sector[sec] = trades_by_sector.get(sec, 0) + 1
                        continue
                        
            if t not in price_matrix.columns:
                sells.add(t)
                
        for s in sells:
            holdings.remove(s)
            entry_prices.pop(s, None)
            peak_prices.pop(s, None)
            holding_sectors.pop(s, None)
            
        # 3. New Buys
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
                peak_prices[tk] = p_val
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
    cagr = (1.0 + cum_ret) ** (1.0 / n_years) - 1.0
    vol = np.std(monthly_net_rets) * np.sqrt(12)
    sharpe = (cagr - 0.02) / (vol + 1e-6)
    
    cum_series = np.cumprod(1.0 + np.array(monthly_net_rets))
    hwm_series = np.maximum.accumulate(cum_series)
    max_dd = np.min((cum_series - hwm_series) / hwm_series)
    avg_turnover = total_trades / (len(monthly_net_rets) * 50.0)
    
    return {
        'Algorithm': algo_name,
        'CAGR': cagr,
        'Sharpe': sharpe,
        'Max DD': max_dd,
        'Turnover/Mo': avg_turnover,
        'Total Trades': total_trades,
        'Sector Trades': trades_by_sector
    }

def run_experiment():
    print("[1/4] Loading PIT Universe & Price Panel...")
    df_merged = load_universe_and_qual_panel()
    price_matrix, vol_matrix = load_price_matrices(df_merged['ticker'].unique())
    
    print("[2/4] Defining Sector-Adaptive Algorithms...")
    
    # Algorithm 1: Static Uniform Baseline (+15%)
    def algo_uniform_15(t, sec, r_vol, cur_p, ent_p, pk_p):
        return (cur_p / ent_p) >= 1.15
        
    # Algorithm 2: Static Uniform Baseline (+12%)
    def algo_uniform_12(t, sec, r_vol, cur_p, ent_p, pk_p):
        return (cur_p / ent_p) >= 1.12

    # Algorithm 3: Damodaran Sector-Beta Scaled (Base = 14%)
    # Software (1.25) -> +17.7%, Hardware (1.15) -> +16.3%, Real Estate/Fin (0.70) -> +9.9%, Consumer (0.75) -> +10.6%
    def algo_damodaran_beta(t, sec, r_vol, cur_p, ent_p, pk_p):
        beta = DAMODARAN_SECTOR_BETAS.get(sec, 1.0)
        # Scaled threshold: base 14% * (beta / MEAN_BETA)
        target_mult = 1.0 + 0.14 * (beta / MEAN_BETA)
        return (cur_p / ent_p) >= target_mult

    # Algorithm 4: Damodaran High-Dispersion (Aggressive Tech Run: Base 12%, Tech +25%, REIT/Fin +8%)
    def algo_damodaran_bimodal(t, sec, r_vol, cur_p, ent_p, pk_p):
        if sec in ['Software', 'Hardware']:
            thresh = 1.25   # Let volatile tech run to +25%
        elif sec in ['Financial', 'Consumer']:
            thresh = 1.09   # Take profit early on low-beta (REIT/Banks/Staples) at +9%
        elif sec == 'Energy':
            thresh = 1.18   # Cyclical energy run
        else: # Industrial, Healthcare
            thresh = 1.14
        return (cur_p / ent_p) >= thresh

    # Algorithm 5: Company Realized Volatility Scaled (Dynamic Standard Error Band)
    # Target = 1.0 + 0.55 * annual_vol (e.g. 20% vol -> +11%, 35% vol -> +19.25%)
    def algo_company_vol_scaled(t, sec, r_vol, cur_p, ent_p, pk_p):
        clamped_vol = np.clip(r_vol, 0.15, 0.40)
        target_mult = 1.0 + 0.55 * clamped_vol
        return (cur_p / ent_p) >= target_mult

    # Algorithm 6: Damodaran Dynamic Ratchet (Peak Trailing)
    # Once price clears sector threshold (Tech +15%, Fin +8%), activate a 6% trailing stop off peak
    def algo_sector_trailing_ratchet(t, sec, r_vol, cur_p, ent_p, pk_p):
        beta = DAMODARAN_SECTOR_BETAS.get(sec, 1.0)
        activation_mult = 1.0 + 0.10 * (beta / MEAN_BETA)
        # If stock cleared activation barrier, check if it fell 5% below peak
        if pk_p >= ent_p * activation_mult:
            if cur_p <= pk_p * 0.94:  # 6% pullback from high
                return True
        # Hard cap at +35%
        if (cur_p / ent_p) >= 1.35:
            return True
        return False

    algorithms = [
        ("1. Static Uniform Baseline (+15%)", algo_uniform_15),
        ("2. Static Uniform (+12%)", algo_uniform_12),
        ("3. Damodaran Beta-Scaled (+10% to +18%)", algo_damodaran_beta),
        ("4. Damodaran High-Dispersion (Tech +25%, Fin +9%)", algo_damodaran_bimodal),
        ("5. Company Vol-Adaptive (0.55 * Sigma)", algo_company_vol_scaled),
        ("6. Damodaran Sector Trailing Ratchet", algo_sector_trailing_ratchet),
    ]

    print("[3/4] Running Simulations across Algorithms...")
    results = []
    for name, fn in algorithms:
        res = simulate_strategy(df_merged, price_matrix, vol_matrix, fn, name)
        results.append(res)
        print(f"  Completed: {name} -> CAGR: {res['CAGR']*100:.2f}%, Sharpe: {res['Sharpe']:.2f}, Max DD: {res['Max DD']*100:.2f}%")

    print("\n[4/4] Summary Comparison Table:")
    summary_data = []
    for r in results:
        summary_data.append({
            'Algorithm Description': r['Algorithm'],
            'CAGR': f"{r['CAGR']*100:.2f}%",
            'Sharpe': f"{r['Sharpe']:.2f}",
            'Max DD': f"{r['Max DD']*100:.2f}%",
            'Turnover/Mo': f"{r['Turnover/Mo']*100:.2f}%",
            'Total Trades': r['Total Trades']
        })
    df_res = pd.DataFrame(summary_data)
    print("==========================================================================================")
    print("           SECTOR-ADAPTIVE & VOLATILITY-TAILORED HARVEST BENCHMARK")
    print("==========================================================================================")
    print(df_res.to_string(index=False))
    print("==========================================================================================")
    
    # Now let's run a sector grid search across Tech vs Defensive to discover the empirical optimum
    print("\n[Bonus] Multi-Sector Optimization Grid (Tech Threshold vs Defensive Threshold):")
    grid_results = []
    for tech_t in [1.12, 1.15, 1.18, 1.22, 1.28]:
        for def_t in [1.08, 1.10, 1.12, 1.15]:
            def grid_fn(t, sec, r_vol, cur_p, ent_p, pk_p):
                if sec in ['Software', 'Hardware', 'Energy']:
                    return (cur_p / ent_p) >= tech_t
                else:
                    return (cur_p / ent_p) >= def_t
            g_res = simulate_strategy(df_merged, price_matrix, vol_matrix, grid_fn)
            grid_results.append({
                'Tech Thresh': f"+{int(round((tech_t-1)*100))}%",
                'Defensive Thresh': f"+{int(round((def_t-1)*100))}%",
                'CAGR': f"{g_res['CAGR']*100:.2f}%",
                'Sharpe': f"{g_res['Sharpe']:.2f}",
                'Max DD': f"{g_res['Max DD']*100:.2f}%",
                'Trades': g_res['Total Trades'],
                'cagr_raw': g_res['CAGR'],
                'sharpe_raw': g_res['Sharpe']
            })
    grid_df = pd.DataFrame(grid_results).sort_values('sharpe_raw', ascending=False)
    print("\nTop 5 Grid Configurations by Sharpe Ratio:")
    print(grid_df.drop(columns=['cagr_raw', 'sharpe_raw']).head(5).to_string(index=False))
    
    return df_res, grid_df

if __name__ == '__main__':
    run_experiment()
