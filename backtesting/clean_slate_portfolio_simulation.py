"""
backtesting/clean_slate_portfolio_simulation.py

Mathematical Simulation and Performance Comparison:
1. Current User Portfolio ($11,903.68, 41% NVDA, BND, CSCO, JPM, V, etc.)
2. Clean-Slate Best-in-Breed Tri-State Target ($11,903.68, unconstrained universe compounders)
3. S&P 500 (SPY Benchmark)

Horizon: 2018-01-01 to 2024-12-31 (7 full years, 84 monthly rebalance cycles).
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

CLEAN_SLATE_TARGET_WEIGHTS = {
    # Pillar 1: Growth Engine (70% = $8,332.58)
    'VTI': 0.16,      # Total US market bedrock
    'NVDA': 0.10,     # Trimmed conviction core AI
    'GOOG': 0.09,     # Search / Cloud / AI infrastructure (keeps existing position)
    'MSFT': 0.09,     # Enterprise software / Cloud lock-in
    'AMZN': 0.09,     # AWS Cloud / Logistics moat
    'META': 0.09,     # Digital ad cash machine / Llama AI
    'QCOM': 0.08,     # Edge AI compute / Patent licensing tollbooth
    # Pillar 2: Cash Flow Cushion (10% = $1,190.37)
    'O': 0.10,        # Real Estate monthly cash distribution
    # Pillar 3: Ballast & Dry Powder (20% = $2,380.74)
    'BIL': 0.14,      # 1-3M Treasury Bills (zero duration risk)
    'GLD': 0.06       # Physical gold store of value
}


def load_price_matrix(tickers: List[str], start_date: str = '2018-01-01', end_date: str = '2024-12-31') -> pd.DataFrame:
    price_base = REPO_ROOT / 'data' / 'source' / 'yfinance'
    dfs = {}
    for t in tickers:
        p_path = price_base / t / 'prices.parquet'
        if p_path.exists():
            tbl = pq.read_table(p_path)
            df = tbl.to_pandas()
            idx = pd.to_datetime(df.index.strftime('%Y-%m-%d'))
            close_col = 'Close' if 'Close' in df.columns else df.columns[0]
            vals = df[close_col].values.flatten()
            dfs[t] = pd.Series(vals, index=idx)
        else:
            raise FileNotFoundError(f"Missing price parquet for {t}")

    price_df = pd.DataFrame(dfs).sort_index().ffill()
    return price_df[(price_df.index >= start_date) & (price_df.index <= end_date)]


def simulate_portfolio_returns(
    price_df: pd.DataFrame,
    weights_dict: Dict[str, float]
) -> Tuple[List[float], List[float]]:
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

        m_ret = 0.0
        for t, w in weights_dict.items():
            if t == 'Cash':
                b0 = price_df.loc[s_dt, 'BIL']
                b1 = price_df.loc[e_dt, 'BIL']
                r = (b1 / b0 - 1.0) if b0 > 0 else 0.0
            else:
                p0 = price_df.loc[s_dt, t]
                p1 = price_df.loc[e_dt, t]
                r = (p1 / p0 - 1.0) if p0 > 0 else 0.0
            m_ret += w * r

        sp0 = price_df.loc[s_dt, 'SPY']
        sp1 = price_df.loc[e_dt, 'SPY']
        r_spy = (sp1 / sp0 - 1.0) if sp0 > 0 else 0.0

        port_rets.append(float(m_ret))
        spy_rets.append(float(r_spy))

    return port_rets, spy_rets


def generate_clean_slate_recipe() -> pd.DataFrame:
    all_tickers = sorted(set(list(CURRENT_HOLDINGS.keys()) + list(CLEAN_SLATE_TARGET_WEIGHTS.keys())))
    rows = []
    for t in all_tickers:
        cur_d = CURRENT_HOLDINGS.get(t, 0.0)
        cur_w = cur_d / TOTAL_WEALTH
        tgt_w = CLEAN_SLATE_TARGET_WEIGHTS.get(t, 0.0)
        tgt_d = tgt_w * TOTAL_WEALTH
        delta_d = tgt_d - cur_d

        if delta_d < -5.0:
            if tgt_w == 0.0:
                action = f"FULL LIQUIDATE (-${abs(delta_d):,.2f})"
            else:
                action = f"TRIM (-${abs(delta_d):,.2f})"
        elif delta_d > 5.0:
            if cur_d == 0.0:
                action = f"NEW POSITION / BUY (+${delta_d:,.2f})"
            else:
                action = f"ADD / BUY (+${delta_d:,.2f})"
        else:
            action = "HOLD"

        rows.append({
            'Ticker': t,
            'Current Value': f"${cur_d:,.2f}",
            'Current Weight': f"{cur_w*100:.2f}%",
            'Target Weight': f"{tgt_w*100:.2f}%",
            'Target Value': f"${tgt_d:,.2f}",
            'Action': action,
            '_delta': delta_d
        })

    return pd.DataFrame(rows).sort_values('_delta')


def main():
    print("=" * 95)
    print("CLEAN-SLATE UNCONSTRAINED TRI-STATE PORTFOLIO SIMULATION")
    print("House of Quant — Quantitative Production Engine")
    print(f"Total Equity Capital: ${TOTAL_WEALTH:,.2f}")
    print("=" * 95)

    all_tickers = list(set(
        list(CURRENT_HOLDINGS.keys()) +
        list(CLEAN_SLATE_TARGET_WEIGHTS.keys()) +
        ['SPY', 'BIL', 'GLD', 'GOOGL']
    ))
    all_tickers = [t for t in all_tickers if t != 'Cash']

    price_df = load_price_matrix(all_tickers)

    # 1. Simulate Current Portfolio
    curr_weights = {k: v / TOTAL_WEALTH for k, v in CURRENT_HOLDINGS.items()}
    curr_rets, spy_rets = simulate_portfolio_returns(price_df, curr_weights)
    m_curr = compute_15_metrics(curr_rets, spy_rets, total_trades=0)
    raw_curr = m_curr['_raw']

    # 2. Simulate Clean-Slate Target Portfolio
    # (Deduct 15 bps annual rebalancing drag)
    clean_rets, _ = simulate_portfolio_returns(price_df, CLEAN_SLATE_TARGET_WEIGHTS)
    clean_rets_net = [r - (0.0015 / 12.0) for r in clean_rets]
    m_clean = compute_15_metrics(clean_rets_net, spy_rets, total_trades=84)
    raw_clean = m_clean['_raw']

    # 3. SPY Benchmark
    m_spy = compute_15_metrics(spy_rets, spy_rets, total_trades=0)
    raw_spy = m_spy['_raw']

    # Comparison Table
    comp_df = pd.DataFrame({
        'Metric': [
            'CAGR (%)', 'Cumulative Net Return', 'Net Sharpe Ratio (Rf = 2%)',
            'Sortino Ratio', 'Maximum Drawdown', 'Calmar Ratio',
            'Annualized Volatility', 'Market Beta vs SPY', "Jensen's Alpha",
            'Up-Market Capture', 'Down-Market Capture'
        ],
        'Current Fragile Portfolio': [
            f"{raw_curr['cagr']*100:.2f}%", f"{raw_curr['cum_ret']*100:.2f}%",
            f"{raw_curr['sharpe']:.2f}", f"{raw_curr['sortino']:.2f}",
            f"{raw_curr['max_dd']*100:.2f}%", f"{raw_curr['calmar']:.2f}",
            f"{raw_curr['vol']*100:.2f}%", f"{raw_curr['beta']:.2f}",
            f"{raw_curr['jensen_alpha']*100:.2f}%",
            f"{raw_curr['up_capture']*100:.1f}%", f"{raw_curr['down_capture']*100:.1f}%"
        ],
        'Clean-Slate Tri-State Target': [
            f"{raw_clean['cagr']*100:.2f}%", f"{raw_clean['cum_ret']*100:.2f}%",
            f"{raw_clean['sharpe']:.2f}", f"{raw_clean['sortino']:.2f}",
            f"{raw_clean['max_dd']*100:.2f}%", f"{raw_clean['calmar']:.2f}",
            f"{raw_clean['vol']*100:.2f}%", f"{raw_clean['beta']:.2f}",
            f"{raw_clean['jensen_alpha']*100:.2f}%",
            f"{raw_clean['up_capture']*100:.1f}%", f"{raw_clean['down_capture']*100:.1f}%"
        ],
        'S&P 500 (SPY Benchmark)': [
            f"{raw_spy['cagr']*100:.2f}%", f"{raw_spy['cum_ret']*100:.2f}%",
            f"{raw_spy['sharpe']:.2f}", f"{raw_spy['sortino']:.2f}",
            f"{raw_spy['max_dd']*100:.2f}%", f"{raw_spy['calmar']:.2f}",
            f"{raw_spy['vol']*100:.2f}%", f"{raw_spy['beta']:.2f}",
            "0.00%", "100.0%", "100.0%"
        ]
    })

    print("\n--- 15-METRIC PRODUCTION PERFORMANCE MATRIX (2018-2024) ---")
    print(comp_df.to_string(index=False))

    # Recipe
    print("\n--- EXACT CLEAN-SLATE REBALANCING & LIQUIDATION BLUEPRINT ---")
    recipe_df = generate_clean_slate_recipe()
    disp_recipe = recipe_df[['Ticker', 'Current Value', 'Current Weight', 'Target Weight', 'Target Value', 'Action']]
    print(disp_recipe.to_string(index=False))

    sells = recipe_df[recipe_df['_delta'] < 0]['_delta'].sum()
    buys = recipe_df[recipe_df['_delta'] > 0]['_delta'].sum()
    print(f"\nTotal Capital Harvested (Sells/Trims): ${abs(sells):,.2f}")
    print(f"Total Capital Deployed (Buys):         ${buys:,.2f}")
    print(f"Net Cash Delta:                        ${(buys + sells):,.2f} (Strict Dollar Conservation)")


if __name__ == '__main__':
    main()
