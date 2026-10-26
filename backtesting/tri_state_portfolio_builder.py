"""
backtesting/tri_state_portfolio_builder.py

Tri-State Static Hedged Portfolio Pillar Return Builder
House of Quant — Audit Ledger Production Engine

Constructs monthly return series for the three structural pillars:
1. Pillar 1: Growth & Innovation Engine (The Alpha Engine)
   - 30% Quant (Arm 1 Value Compounders) / 70% Qualitative (Arm 2 Moat Innovators)
2. Pillar 2: Stagnant Cycle Cash-Flow Cushion (The Yield Stabilizer)
   - High Free-Cash-Flow Margin Equities (>15% FCF) blended with Real Estate REITs (VNQ)
3. Pillar 3: Black Swan Ballast & Dry Powder (The Capital Shield)
   - Short-Term 1-3M Treasury Bills (BIL, 70%) + Physical Gold Trust (GLD, 30%)
4. Benchmark: S&P 500 (SPY ETF)

Supports:
- Maximum PIT Universe Horizon: 2018-01-31 to 2024-12-31 (84 monthly periods)
- Long-Horizon Macro Asset Cycle: 2007-06-30 to 2026-08-31 (230 monthly periods, 19.2 years)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtesting.run_three_arm_comparative_backtest import (
    load_universe_and_qual_panel,
    load_price_matrices
)


def build_pillar_monthly_returns(
    df_merged: Optional[pd.DataFrame] = None,
    price_matrix: Optional[pd.DataFrame] = None,
    vol_matrix: Optional[pd.DataFrame] = None,
    qual_weight: float = 0.70,
    quant_weight: float = 0.30
) -> pd.DataFrame:
    """
    Constructs the exact monthly return series for all 3 pillars and the SPY benchmark.
    """
    if df_merged is None or price_matrix is None or vol_matrix is None:
        df_merged = load_universe_and_qual_panel()
        tickers = df_merged['ticker'].unique().tolist()
        price_matrix, vol_matrix = load_price_matrices(
            tickers, benchmark_tickers=['SPY', 'BIL', 'GLD', 'VNQ'],
            start_date='2017-12-01', end_date='2026-09-04'
        )

    rebalance_dates = sorted(df_merged['rebalance_date'].unique())
    dates_list = []
    r_growth_list = []
    r_cashflow_list = []
    r_ballast_list = []
    r_spy_list = []

    holdings_growth = set()
    entry_growth = {}

    holdings_fcf = set()
    entry_fcf = {}

    for i in range(len(rebalance_dates) - 1):
        cur_dt = rebalance_dates[i]
        next_dt = rebalance_dates[i + 1]
        avail = price_matrix.index
        t_starts = avail[avail >= cur_dt]
        t_ends = avail[avail >= next_dt]
        if len(t_starts) == 0 or len(t_ends) == 0:
            continue
        s_dt = t_starts[0]
        e_dt = t_ends[0]
        dates_list.append(str(cur_dt)[:10])

        month_slice = df_merged[df_merged['rebalance_date'] == cur_dt].set_index('ticker')

        # -------------------------------------------------------------------------
        # PILLAR 1: GROWTH ENGINE (30% Quant / 70% Qual Hybrid)
        # -------------------------------------------------------------------------
        sells_g = set()
        for t in list(holdings_growth):
            if t in month_slice.index:
                arch = month_slice.loc[t, 'archetype']
                arch_val = arch if isinstance(arch, str) else arch.iloc[0]
                if arch_val == 'High_Friction_Distress':
                    sells_g.add(t)
                    continue
            if t in price_matrix.columns and t in entry_growth:
                cur_p = price_matrix.loc[s_dt, t]
                ent_p = entry_growth[t]
                if ent_p > 0:
                    ret = cur_p / ent_p - 1.0
                    if ret >= 0.20 or ret <= -0.20:
                        sells_g.add(t)
                        continue
            if t not in price_matrix.columns:
                sells_g.add(t)

        for s in sells_g:
            holdings_growth.remove(s)
            entry_growth.pop(s, None)

        slots_g = 20 - len(holdings_growth)
        if slots_g > 0:
            cands_g = month_slice[
                (month_slice['roic_wacc_spread'] > 0.0) &
                (month_slice['fcf_margin'] > 0.0) &
                (month_slice['archetype'] != 'High_Friction_Distress') &
                (month_slice.index.isin(price_matrix.columns))
            ].copy()
            valid_g = [tk for tk in cands_g.index if tk not in holdings_growth]
            cands_g = cands_g.loc[cands_g.index.isin(valid_g)].copy()

            if len(cands_g) > 0:
                spread_z = (cands_g['roic_wacc_spread'] - cands_g['roic_wacc_spread'].mean()) / (cands_g['roic_wacc_spread'].std() + 1e-6)
                fcf_z = (cands_g['fcf_margin'] - cands_g['fcf_margin'].mean()) / (cands_g['fcf_margin'].std() + 1e-6)
                quant_z = spread_z + 0.5 * fcf_z

                alpha_z = (cands_g['alpha_spread'] - cands_g['alpha_spread'].mean()) / (cands_g['alpha_spread'].std() + 1e-6)
                q_z = (cands_g['qual_score_Q'] - cands_g['qual_score_Q'].mean()) / (cands_g['qual_score_Q'].std() + 1e-6)
                qual_z = (alpha_z + q_z) * cands_g['archetype_multiplier']

                cands_g['hybrid_score'] = quant_weight * quant_z + qual_weight * qual_z
                picks_g = cands_g.sort_values('hybrid_score', ascending=False).head(slots_g)

                for tk in picks_g.index:
                    holdings_growth.add(tk)
                    entry_growth[tk] = price_matrix.loc[s_dt, tk] if tk in price_matrix.columns else 1.0

        # Calculate Growth Return (minus 15 bps slippage on turnover)
        rets_g = []
        for t in holdings_growth:
            if t in price_matrix.columns:
                p0 = price_matrix.loc[s_dt, t]
                p1 = price_matrix.loc[e_dt, t]
                if p0 > 0:
                    rets_g.append(p1 / p0 - 1.0)
        ret_g_val = np.mean(rets_g) if len(rets_g) > 0 else 0.0
        turnover_g = len(sells_g) / 20.0
        ret_g_net = ret_g_val - (turnover_g * 0.0015)
        r_growth_list.append(float(ret_g_net))

        # -------------------------------------------------------------------------
        # PILLAR 2: CASH-FLOW & STAGNANT CUSHION (Top FCF Margin > 15% + VNQ)
        # -------------------------------------------------------------------------
        sells_fcf = set()
        for t in list(holdings_fcf):
            if t in month_slice.index:
                if month_slice.loc[t, 'fcf_margin'] < 0.10:  # demoted
                    sells_fcf.add(t)
                    continue
            if t in price_matrix.columns and t in entry_fcf:
                cur_p = price_matrix.loc[s_dt, t]
                ent_p = entry_fcf[t]
                if ent_p > 0 and (cur_p / ent_p >= 1.25 or cur_p / ent_p <= 0.85):
                    sells_fcf.add(t)
                    continue
            if t not in price_matrix.columns:
                sells_fcf.add(t)

        for s in sells_fcf:
            holdings_fcf.remove(s)
            entry_fcf.pop(s, None)

        slots_fcf = 15 - len(holdings_fcf)
        if slots_fcf > 0:
            cands_fcf = month_slice[
                (month_slice['fcf_margin'] >= 0.15) &
                (month_slice['roic'] > 0.08) &
                (month_slice.index.isin(price_matrix.columns))
            ].copy()
            valid_fcf = [tk for tk in cands_fcf.index if tk not in holdings_fcf]
            cands_fcf = cands_fcf.loc[cands_fcf.index.isin(valid_fcf)].copy()
            if len(cands_fcf) > 0:
                picks_fcf = cands_fcf.sort_values('fcf_margin', ascending=False).head(slots_fcf)
                for tk in picks_fcf.index:
                    holdings_fcf.add(tk)
                    entry_fcf[tk] = price_matrix.loc[s_dt, tk] if tk in price_matrix.columns else 1.0

        rets_fcf = []
        for t in holdings_fcf:
            if t in price_matrix.columns:
                p0 = price_matrix.loc[s_dt, t]
                p1 = price_matrix.loc[e_dt, t]
                if p0 > 0:
                    rets_fcf.append(p1 / p0 - 1.0)
        ret_fcf_equity = np.mean(rets_fcf) if len(rets_fcf) > 0 else 0.0

        # VNQ Real Estate Return
        vnq0 = price_matrix.loc[s_dt, 'VNQ'] if 'VNQ' in price_matrix.columns else 1.0
        vnq1 = price_matrix.loc[e_dt, 'VNQ'] if 'VNQ' in price_matrix.columns else 1.0
        ret_vnq = (vnq1 / vnq0 - 1.0) if vnq0 > 0 else 0.0

        # Pillar 2 blend: 60% High-FCF Equities + 40% Real Estate VNQ
        ret_p2 = 0.60 * ret_fcf_equity + 0.40 * ret_vnq
        r_cashflow_list.append(float(ret_p2))

        # -------------------------------------------------------------------------
        # PILLAR 3: BLACK SWAN BALLAST (70% BIL T-Bills + 30% GLD Gold Trust)
        # -------------------------------------------------------------------------
        b0 = price_matrix.loc[s_dt, 'BIL'] if 'BIL' in price_matrix.columns else 1.0
        b1 = price_matrix.loc[e_dt, 'BIL'] if 'BIL' in price_matrix.columns else 1.0
        ret_bil = (b1 / b0 - 1.0) if b0 > 0 else 0.0

        g0 = price_matrix.loc[s_dt, 'GLD'] if 'GLD' in price_matrix.columns else 1.0
        g1 = price_matrix.loc[e_dt, 'GLD'] if 'GLD' in price_matrix.columns else 1.0
        ret_gld = (g1 / g0 - 1.0) if g0 > 0 else 0.0

        ret_ballast = 0.70 * ret_bil + 0.30 * ret_gld
        r_ballast_list.append(float(ret_ballast))

        # -------------------------------------------------------------------------
        # BENCHMARK: SPY S&P 500
        # -------------------------------------------------------------------------
        s0 = price_matrix.loc[s_dt, 'SPY'] if 'SPY' in price_matrix.columns else 1.0
        s1 = price_matrix.loc[e_dt, 'SPY'] if 'SPY' in price_matrix.columns else 1.0
        ret_spy = (s1 / s0 - 1.0) if s0 > 0 else 0.0
        r_spy_list.append(float(ret_spy))

    df_pillars = pd.DataFrame({
        'rebalance_date': dates_list,
        'growth': r_growth_list,
        'cash_flow': r_cashflow_list,
        'ballast': r_ballast_list,
        'spy': r_spy_list
    })
    return df_pillars


def build_macro_long_horizon_returns(
    start_date: str = '2007-06-30',
    end_date: str = '2026-08-31'
) -> pd.DataFrame:
    """
    Constructs long-horizon monthly asset class returns across the full 19-year period (2007-2026).
    Assets: SPY (S&P 500), BIL (T-Bills), GLD (Gold), VNQ (Real Estate).
    """
    price_base = REPO_ROOT / 'data' / 'source' / 'yfinance'
    tickers = ['SPY', 'BIL', 'GLD', 'VNQ']
    dfs = {}
    for t in tickers:
        p_path = price_base / t / 'prices.parquet'
        if p_path.exists():
            df = pd.read_parquet(p_path)
            clean_idx = pd.to_datetime(df.index.strftime('%Y-%m-%d'))
            dfs[t] = pd.Series(df['Close'].values, index=clean_idx)

    price_df = pd.DataFrame(dfs).sort_index().ffill()
    
    # Generate monthly dates
    m_ends = pd.date_range(start_date, end_date, freq='ME')
    rows = []
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

        r_spy = float(price_df.loc[e_dt, 'SPY'] / price_df.loc[s_dt, 'SPY'] - 1.0)
        r_bil = float(price_df.loc[e_dt, 'BIL'] / price_df.loc[s_dt, 'BIL'] - 1.0)
        r_gld = float(price_df.loc[e_dt, 'GLD'] / price_df.loc[s_dt, 'GLD'] - 1.0)
        r_vnq = float(price_df.loc[e_dt, 'VNQ'] / price_df.loc[s_dt, 'VNQ'] - 1.0)

        # Ballast: 70% BIL + 30% GLD
        r_ballast = 0.70 * r_bil + 0.30 * r_gld
        # Cash Flow: VNQ Proxy
        r_cashflow = r_vnq

        rows.append({
            'date': str(c_dt)[:10],
            'spy': r_spy,
            'bil': r_bil,
            'gld': r_gld,
            'vnq': r_vnq,
            'ballast': r_ballast,
            'cash_flow': r_cashflow
        })

    return pd.DataFrame(rows)


if __name__ == '__main__':
    print("Testing Pillar Return Builder...")
    df_p = build_pillar_monthly_returns()
    print("Pillar Returns Shape:", df_p.shape)
    print(df_p.head())

    print("\nTesting Macro 19-Year Asset Class Returns...")
    df_m = build_macro_long_horizon_returns()
    print("Macro Returns Shape:", df_m.shape)
    print("Date range:", df_m['date'].iloc[0], "to", df_m['date'].iloc[-1])
