"""
backtesting/user_portfolio_real_world_simulation.py

Mathematical Diagnostics, Historical Backtest, and Real-Life Simulation
of the User's Real-World Brokerage Portfolio ($11,903.68) vs. Tri-State Target.

Holdings:
  - NVDA:   $4,897.41 (41.14%) -> Cost: $2,826.54 (+73.26% gain)
  - FNCMX:  $2,163.31 (18.17%) -> Cost: $1,422.25 (+52.10% gain)
  - VTI:    $1,423.84 (11.96%) -> Cost: $968.15   (+47.06% gain)
  - BND:    $1,259.53 (10.58%) -> Cost: $1,309.21 (-3.80% loss)
  - CSCO:     $498.36 ( 4.19%) -> Cost: $300.24   (+65.98% gain)
  - O:        $424.35 ( 3.56%) -> Cost: $440.93   (-3.76% loss)
  - GOOG:     $349.52 ( 2.94%) -> Cost: $333.00   (+4.96% gain)
  - V:        $319.22 ( 2.68%) -> Cost: $286.84   (+11.29% gain)
  - JPM:      $283.37 ( 2.38%) -> Cost: $249.88   (+13.40% gain)
  - Cash:     $284.77 ( 2.39%)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Tuple
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtesting.run_enhanced_arms import compute_15_metrics
from backtesting.tri_state_portfolio_builder import build_pillar_monthly_returns


CURRENT_HOLDINGS = {
    'NVDA': 4897.41,
    'FNCMX': 2163.31,
    'VTI': 1423.84,
    'BND': 1259.53,
    'CSCO': 498.36,
    'O': 424.35,
    'GOOG': 349.52,
    'V': 319.22,
    'JPM': 283.37,
    'Cash': 284.77
}
TOTAL_WEALTH = sum(CURRENT_HOLDINGS.values())


def load_user_price_matrices(
    start_date: str = '2018-01-01',
    end_date: str = '2024-12-31'
) -> pd.DataFrame:
    """Loads daily prices for all user holdings + benchmark assets."""
    price_base = REPO_ROOT / 'data' / 'source' / 'yfinance'
    tickers = ['NVDA', 'FNCMX', 'VTI', 'BND', 'CSCO', 'O', 'GOOG', 'V', 'JPM', 'SPY', 'BIL', 'GLD']
    dfs = {}
    
    for t in tickers:
        p_path = price_base / t / 'prices.parquet'
        if p_path.exists():
            tbl = pq.read_table(p_path)
            df = tbl.to_pandas()
            # clean index
            idx = pd.to_datetime(df.index.strftime('%Y-%m-%d'))
            close_col = 'Close' if 'Close' in df.columns else df.columns[0]
            vals = df[close_col].values.flatten()
            dfs[t] = pd.Series(vals, index=idx)
            
    price_df = pd.DataFrame(dfs).sort_index().ffill()
    price_df['Cash'] = 1.0  # Cash is constant nominal $1
    return price_df[(price_df.index >= start_date) & (price_df.index <= end_date)]


def simulate_current_portfolio_history(
    price_df: pd.DataFrame
) -> Tuple[List[float], List[float]]:
    """
    Simulates monthly rebalancing returns of the user's current static weights.
    """
    weights = {k: v / TOTAL_WEALTH for k, v in CURRENT_HOLDINGS.items()}
    m_ends = pd.date_range(price_df.index.min(), price_df.index.max(), freq='ME')
    
    port_rets = []
    spy_rets = []
    avail = price_df.index

    for i in range(len(m_ends) - 1):
        c_dt = m_ends[i]
        n_dt = m_ends[i + 1]
        s_arr = avail[avail >= c_dt]
        e_arr = avail[avail >= n_dt]
        if len(s_arr) == 0 or len(e_arr) == 0:
            continue
        s_dt = s_arr[0]
        e_dt = e_arr[0]

        # Monthly return per asset
        m_ret = 0.0
        for t, w in weights.items():
            if t == 'Cash':
                # Cash yield from BIL
                b0 = price_df.loc[s_dt, 'BIL']
                b1 = price_df.loc[e_dt, 'BIL']
                ret_asset = (b1 / b0 - 1.0) if b0 > 0 else 0.0
            else:
                p0 = price_df.loc[s_dt, t]
                p1 = price_df.loc[e_dt, t]
                ret_asset = (p1 / p0 - 1.0) if p0 > 0 else 0.0
            m_ret += w * ret_asset

        # SPY Return
        sp0 = price_df.loc[s_dt, 'SPY']
        sp1 = price_df.loc[e_dt, 'SPY']
        ret_spy = (sp1 / sp0 - 1.0) if sp0 > 0 else 0.0

        port_rets.append(float(m_ret))
        spy_rets.append(float(ret_spy))

    return port_rets, spy_rets


def generate_mathematical_transition_plan() -> pd.DataFrame:
    """
    Computes the exact dollar rebalance recipe to transition from Current Portfolio to Tri-State Target.
    Target:
      Pillar 1: Growth Engine (70% = $8,332.58)
        - Core Conviction Compounders: NVDA (10%), GOOG (8%), V (8%), JPM (7%), CSCO (5%), VTI (16%), FNCMX (16%) -> total 70%
      Pillar 2: Cash-Flow Cushion (10% = $1,190.37)
        - O (Realty Income): 10%
      Pillar 3: Black Swan Ballast (20% = $2,380.74)
        - BIL / SGOV (Short Treasuries): 14% ($1,666.52)
        - GLD (Gold Trust): 6% ($714.22)
    """
    target_weights = {
        'NVDA': 0.10,     # Trim from 41.14% down to 10.00%
        'FNCMX': 0.16,    # Slight trim from 18.17% down to 16.00%
        'VTI': 0.16,      # Increase from 11.96% up to 16.00%
        'GOOG': 0.08,     # Increase from 2.94% up to 8.00%
        'V': 0.08,        # Increase from 2.68% up to 8.00%
        'JPM': 0.07,      # Increase from 2.38% up to 7.00%
        'CSCO': 0.05,     # Slight increase from 4.19% to 5.00%
        'O': 0.10,        # Increase from 3.56% up to 10.00% (Pillar 2 Cash Flow)
        'BIL': 0.14,      # New Ballast asset (replace BND duration)
        'GLD': 0.06,      # New Ballast asset (gold crisis protection)
        'BND': 0.00,      # Liquidate (eliminate rate sensitivity)
        'Cash': 0.00      # Deploy into BIL / yield
    }

    rows = []
    for asset in sorted(set(list(CURRENT_HOLDINGS.keys()) + list(target_weights.keys()))):
        cur_d = CURRENT_HOLDINGS.get(asset, 0.0)
        cur_w = cur_d / TOTAL_WEALTH
        tgt_w = target_weights.get(asset, 0.0)
        tgt_d = tgt_w * TOTAL_WEALTH
        delta_d = tgt_d - cur_d

        action = "HOLD"
        if delta_d < -5.0:
            action = f"TRIM / SELL (${abs(delta_d):,.2f})"
        elif delta_d > 5.0:
            action = f"BUY / ADD (${delta_d:,.2f})"

        rows.append({
            'Asset / Ticker': asset,
            'Current Value': f"${cur_d:,.2f}",
            'Current Weight': f"{cur_w*100:.2f}%",
            'Target Weight': f"{tgt_w*100:.2f}%",
            'Target Value': f"${tgt_d:,.2f}",
            'Rebalance Action': action,
            '_delta': delta_d
        })

    return pd.DataFrame(rows).sort_values('_delta')


def main():
    print("=" * 90)
    print("REAL-WORLD PORTFOLIO DIAGNOSTIC & MATHEMATICAL TRANSITION ENGINE")
    print("=" * 90)
    print(f"Total Account Equity: ${TOTAL_WEALTH:,.2f}")

    # 1. Load Prices & Simulate Current Portfolio
    price_df = load_user_price_matrices()
    port_rets, spy_rets = simulate_current_portfolio_history(price_df)

    # 2. Compute Current Portfolio Metrics
    m_curr = compute_15_metrics(port_rets, spy_rets, total_trades=0)
    raw_curr = m_curr['_raw']

    # 3. Simulate Rebalanced Tri-State Target
    # 70% Growth / 10% Cash Flow / 20% Ballast
    df_pillars = build_pillar_monthly_returns()
    wg, wc, wb = 0.70, 0.10, 0.20
    tri_rets = (wg * df_pillars['growth'] + wc * df_pillars['cash_flow'] + wb * df_pillars['ballast']).tolist()
    m_tri = compute_15_metrics(tri_rets, spy_rets, total_trades=240)
    raw_tri = m_tri['_raw']
    m_spy = compute_15_metrics(spy_rets, spy_rets, total_trades=0)
    raw_spy = m_spy['_raw']

    # 4. Comparative Metrics Table
    comp_df = pd.DataFrame({
        'Metric': [
            'CAGR (%)', 'Cumulative Net Return', 'Net Sharpe Ratio (Rf = 2%)',
            'Sortino Ratio', 'Maximum Drawdown', 'Calmar Ratio',
            'Annualized Volatility', 'Market Beta vs SPY', "Jensen's Alpha",
            'Up-Market Capture', 'Down-Market Capture'
        ],
        'Your Current Portfolio': [
            f"{raw_curr['cagr']*100:.2f}%", f"{raw_curr['cum_ret']*100:.2f}%",
            f"{raw_curr['sharpe']:.2f}", f"{raw_curr['sortino']:.2f}",
            f"{raw_curr['max_dd']*100:.2f}%", f"{raw_curr['calmar']:.2f}",
            f"{raw_curr['vol']*100:.2f}%", f"{raw_curr['beta']:.2f}",
            f"{raw_curr['jensen_alpha']*100:.2f}%",
            f"{raw_curr['up_capture']*100:.1f}%", f"{raw_curr['down_capture']*100:.1f}%"
        ],
        'Tri-State Hedged Target': [
            f"{raw_tri['cagr']*100:.2f}%", f"{raw_tri['cum_ret']*100:.2f}%",
            f"{raw_tri['sharpe']:.2f}", f"{raw_tri['sortino']:.2f}",
            f"{raw_tri['max_dd']*100:.2f}%", f"{raw_tri['calmar']:.2f}",
            f"{raw_tri['vol']*100:.2f}%", f"{raw_tri['beta']:.2f}",
            f"{raw_tri['jensen_alpha']*100:.2f}%",
            f"{raw_tri['up_capture']*100:.1f}%", f"{raw_tri['down_capture']*100:.1f}%"
        ],
        'S&P 500 (SPY Benchmark)': [
            f"{raw_spy['cagr']*100:.2f}%", f"{raw_spy['cum_ret']*100:.2f}%",
            f"{raw_spy['sharpe']:.2f}", f"{raw_spy['sortino']:.2f}",
            f"{raw_spy['max_dd']*100:.2f}%", f"{raw_spy['calmar']:.2f}",
            f"{raw_spy['vol']*100:.2f}%", f"{raw_spy['beta']:.2f}",
            "0.00%", "100.0%", "100.0%"
        ]
    })

    print("\n--- PERFORMANCE & RISK BENCHMARK: CURRENT vs. TARGET vs. SPY ---")
    print(comp_df.to_string(index=False))

    # 5. Exact Rebalance Transition Plan
    print("\n--- EXACT MATHEMATICAL REBALANCE RECIPE ($11,903.68 Total Capital) ---")
    plan_df = generate_mathematical_transition_plan()
    disp_plan = plan_df[['Asset / Ticker', 'Current Value', 'Current Weight', 'Target Weight', 'Target Value', 'Rebalance Action']]
    print(disp_plan.to_string(index=False))

    # Save to JSON
    out_dir = REPO_ROOT / 'data' / 'backtests' / 'user_portfolio'
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_df.to_csv(out_dir / 'rebalance_recipe.csv', index=False)
    comp_df.to_csv(out_dir / 'current_vs_target_metrics.csv', index=False)


if __name__ == '__main__':
    main()
