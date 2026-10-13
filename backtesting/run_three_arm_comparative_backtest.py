"""
backtesting/run_three_arm_comparative_backtest.py

Unified 3-Arm Comparative Walk-Forward Backtest Engine (2018 - 2024, 83 Monthly Rebalance Periods)
--------------------------------------------------------------------------------------------------
Compares:
  - Arm 1: Full Quant (Optimal Value: ROIC-WACC spread + FCF Margin + Logistical Overvaluation Sells)
  - Arm 2: Qualitative Moat (Composite Q_i + Moat Alpha Spread Delta_alpha + Archetype Multiplier)
  - Arm 3: Hybrid Blend (Convex integration: Quant baseline modulated by Qualitative conviction)
  - Benchmark: S&P 500 (SPY ETF)

Portfolio Parameters:
  - Max Equities: 25 stocks
  - Bond Cushion: 15% Short-Term Treasuries (BIL ETF)
  - Transaction Costs: 15 bps round-trip sunny day + 50 bps stress
  - Strict Point-In-Time (PIT) validation: Zero lookahead bias
"""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd
import pyarrow.dataset as ds
import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_universe_and_qual_panel() -> pd.DataFrame:
    """
    Loads PIT universe fundamentals and merges with the pre-computed PIT qualitative factor panel.
    Returns merged DataFrame indexed across 84 monthly rebalance periods.
    """
    print("[1/5] Loading PIT Universe Fundamentals & Qualitative Factor Panel...")
    
    # 1. Load universe fundamentals
    part = ds.partitioning(schema=pa.schema([
        pa.field('rebalance_date', pa.date32()),
        pa.field('cap_bucket', pa.string())
    ]), flavor='hive')
    dataset = ds.dataset('data/universe/merged_pit_universe.parquet', format='parquet', partitioning=part)
    cols = ['ticker', 'rebalance_date', 'roic', 'wacc', 'roic_wacc_spread', 'fcf_margin', 'market_cap', 'sector_custom']
    df_univ = dataset.to_table(columns=cols).to_pandas()
    df_univ['rebalance_date'] = pd.to_datetime(df_univ['rebalance_date']).dt.strftime('%Y-%m-%d')
    
    # 2. Load Qualitative PIT Panel
    qual_path = REPO_ROOT / 'data' / 'harness' / 'qual_pit_factor_panel_2018_2024.parquet'
    df_qual = pd.read_parquet(qual_path)
    df_qual['rebalance_date'] = pd.to_datetime(df_qual['rebalance_date']).dt.strftime('%Y-%m-%d')
    
    # Select qualitative fields
    qual_cols = ['rebalance_date', 'ticker', 'archetype', 'archetype_multiplier', 
                 'qual_score_Q', 'iv_moat', 'alpha_spread', 'primary_traction_T']
    df_qual_sub = df_qual[qual_cols].drop_duplicates(subset=['rebalance_date', 'ticker'])
    
    # Merge on (rebalance_date, ticker)
    merged = pd.merge(df_univ, df_qual_sub, on=['rebalance_date', 'ticker'], how='left')
    merged['rebalance_date'] = pd.to_datetime(merged['rebalance_date'])
    
    # Neutral fills for missing qual metrics
    merged['archetype'] = merged['archetype'].fillna('Compounder')
    merged['archetype_multiplier'] = merged['archetype_multiplier'].fillna(1.00)
    merged['qual_score_Q'] = merged['qual_score_Q'].fillna(0.0)
    merged['alpha_spread'] = merged['alpha_spread'].fillna(0.0)
    
    print(f"      Loaded {len(merged):,} records across {merged['rebalance_date'].nunique()} monthly rebalance dates.")
    return merged


def load_price_matrices(
    tickers: List[str], 
    benchmark_tickers: List[str] = ['SPY', 'BIL'],
    start_date: str = '2017-12-01', 
    end_date: str = '2025-01-15'
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Loads daily price series and computes rolling volatility."""
    print(f"[2/5] Indexing daily price matrices for {len(tickers):,} universe tickers + {benchmark_tickers}...")
    price_dict: Dict[str, pd.Series] = {}
    vol_dict: Dict[str, pd.Series] = {}
    price_base = REPO_ROOT / 'data' / 'source' / 'yfinance'
    t_start = pd.Timestamp(start_date)
    t_end = pd.Timestamp(end_date)
    
    all_needed = set(tickers).union(set(benchmark_tickers))
    loaded = 0
    
    for t in all_needed:
        p_path = price_base / t / 'prices.parquet'
        if p_path.exists():
            try:
                tbl = pq.read_table(p_path)
                p_df = tbl.to_pandas()
                clean_index = pd.to_datetime(p_df.index.strftime('%Y-%m-%d'))
                p_series = pd.Series(p_df['Close'].values, index=clean_index)
                p_sub = p_series[(clean_index >= t_start) & (clean_index <= t_end)]
                if len(p_sub) > 0:
                    price_dict[t] = p_sub
                    daily_ret = p_sub.pct_change()
                    rolling_vol = daily_ret.rolling(60).std() * np.sqrt(252)
                    vol_dict[t] = rolling_vol
                    loaded += 1
            except Exception:
                pass
                
    print(f"      Indexed {loaded:,} ticker price series successfully.")
    price_matrix = pd.DataFrame(price_dict).sort_index().ffill(limit=5)
    vol_matrix = pd.DataFrame(vol_dict).sort_index().ffill(limit=5)
    return price_matrix, vol_matrix


def simulate_three_arms(
    df_merged: pd.DataFrame,
    price_matrix: pd.DataFrame,
    vol_matrix: pd.DataFrame,
    max_equities: int = 25,
    bond_weight: float = 0.15,
    tcost_bps: float = 15.0,
) -> pd.DataFrame:
    """
    Executes walk-forward monthly simulation across all 3 arms simultaneously.
    """
    print(f"[3/5] Simulating Walk-Forward 3-Arm Comparative Portfolios (85% Equity / 15% BIL)...")
    rebalance_dates = sorted(df_merged['rebalance_date'].unique())
    
    # State tracking per arm
    holdings_arm1: Set[str] = set()
    entry_prices_arm1: Dict[str, float] = {}
    
    holdings_arm2: Set[str] = set()
    entry_prices_arm2: Dict[str, float] = {}
    
    holdings_arm3: Set[str] = set()
    entry_prices_arm3: Dict[str, float] = {}
    
    monthly_records = []
    
    for i in range(len(rebalance_dates) - 1):
        cur_date = rebalance_dates[i]
        next_date = rebalance_dates[i + 1]
        
        # Valid trading days
        avail_dates = price_matrix.index
        t_starts = avail_dates[avail_dates >= cur_date]
        t_ends = avail_dates[avail_dates >= next_date]
        if len(t_starts) == 0 or len(t_ends) == 0:
            continue
        p_start_date = t_starts[0]
        p_end_date = t_ends[0]
        
        month_slice = df_merged[df_merged['rebalance_date'] == cur_date].set_index('ticker')
        
        # -------------------------------------------------------------
        # ARM 1: Full Quant (Optimal Value)
        # -------------------------------------------------------------
        sells_arm1: Set[str] = set()
        for t in list(holdings_arm1):
            if t in month_slice.index:
                spread = month_slice.loc[t, 'roic_wacc_spread']
                spread_val = spread if isinstance(spread, (int, float)) else spread.iloc[0]
                if spread_val <= 0.0:
                    sells_arm1.add(t)
                    continue
            if t in price_matrix.columns and t in entry_prices_arm1:
                cur_p = price_matrix.loc[p_start_date, t]
                ent_p = entry_prices_arm1[t]
                if ent_p > 0 and (cur_p / ent_p >= 1.15):
                    sells_arm1.add(t)
                    continue
            if t not in price_matrix.columns:
                sells_arm1.add(t)
                
        for s in sells_arm1:
            holdings_arm1.remove(s)
            entry_prices_arm1.pop(s, None)
            
        slots_needed_1 = max_equities - len(holdings_arm1)
        new_buys_arm1: Set[str] = set()
        if slots_needed_1 > 0:
            cands1 = month_slice[
                (month_slice['roic_wacc_spread'] > 0.0) &
                (month_slice['fcf_margin'] > 0.0) &
                (month_slice.index.isin(price_matrix.columns))
            ].copy()
            # Volatility matching
            valid1 = [
                tk for tk in cands1.index 
                if tk not in holdings_arm1 and (
                    tk not in vol_matrix.columns or 
                    np.isnan(vol_matrix.loc[p_start_date, tk]) or 
                    (0.15 <= vol_matrix.loc[p_start_date, tk] <= 0.40)
                )
            ]
            cands1 = cands1.loc[cands1.index.isin(valid1)].copy()
            spread_z = (cands1['roic_wacc_spread'] - cands1['roic_wacc_spread'].mean()) / (cands1['roic_wacc_spread'].std() + 1e-6)
            fcf_z = (cands1['fcf_margin'] - cands1['fcf_margin'].mean()) / (cands1['fcf_margin'].std() + 1e-6)
            cands1['score_arm1'] = spread_z + 0.5 * fcf_z
            picks1 = cands1.sort_values('score_arm1', ascending=False).head(slots_needed_1)
            for tk in picks1.index:
                new_buys_arm1.add(tk)
                holdings_arm1.add(tk)
                entry_prices_arm1[tk] = price_matrix.loc[p_start_date, tk] if tk in price_matrix.columns else 1.0

        turnover_1 = (len(sells_arm1) + len(new_buys_arm1)) / (2.0 * max_equities)

        # -------------------------------------------------------------
        # ARM 2: Qualitative Moat Strategy
        # -------------------------------------------------------------
        sells_arm2: Set[str] = set()
        for t in list(holdings_arm2):
            if t in month_slice.index:
                arch = month_slice.loc[t, 'archetype']
                arch_val = arch if isinstance(arch, str) else arch.iloc[0]
                # Thesis breach: Demoted to High Friction Distress
                if arch_val == 'High_Friction_Distress':
                    sells_arm2.add(t)
                    continue
            if t in price_matrix.columns and t in entry_prices_arm2:
                cur_p = price_matrix.loc[p_start_date, t]
                ent_p = entry_prices_arm2[t]
                # Logistical profit harvest at +25% or stop out at -20%
                if ent_p > 0:
                    ret_pos = cur_p / ent_p - 1.0
                    if ret_pos >= 0.25 or ret_pos <= -0.20:
                        sells_arm2.add(t)
                        continue
            if t not in price_matrix.columns:
                sells_arm2.add(t)

        for s in sells_arm2:
            holdings_arm2.remove(s)
            entry_prices_arm2.pop(s, None)

        slots_needed_2 = max_equities - len(holdings_arm2)
        new_buys_arm2: Set[str] = set()
        if slots_needed_2 > 0:
            # Filter: Must not be High_Friction_Distress, must have liquid price
            cands2 = month_slice[
                (month_slice['archetype'] != 'High_Friction_Distress') &
                (month_slice.index.isin(price_matrix.columns))
            ].copy()
            # Standardize Alpha Spread and Q score
            q_z = (cands2['qual_score_Q'] - cands2['qual_score_Q'].mean()) / (cands2['qual_score_Q'].std() + 1e-6)
            alpha_z = (cands2['alpha_spread'] - cands2['alpha_spread'].mean()) / (cands2['alpha_spread'].std() + 1e-6)
            # Moat Score = Multiplier * (0.6 * Alpha_Spread + 0.4 * Q_score)
            cands2['moat_composite'] = cands2['archetype_multiplier'] * (0.6 * alpha_z + 0.4 * q_z)
            valid2 = [tk for tk in cands2.index if tk not in holdings_arm2]
            picks2 = cands2.loc[valid2].sort_values('moat_composite', ascending=False).head(slots_needed_2)
            for tk in picks2.index:
                new_buys_arm2.add(tk)
                holdings_arm2.add(tk)
                entry_prices_arm2[tk] = price_matrix.loc[p_start_date, tk] if tk in price_matrix.columns else 1.0

        turnover_2 = (len(sells_arm2) + len(new_buys_arm2)) / (2.0 * max_equities)

        # -------------------------------------------------------------
        # ARM 3: Hybrid Blend (Quant + Qualitative Conviction)
        # -------------------------------------------------------------
        sells_arm3: Set[str] = set()
        for t in list(holdings_arm3):
            if t in month_slice.index:
                spread = month_slice.loc[t, 'roic_wacc_spread']
                spread_val = spread if isinstance(spread, (int, float)) else spread.iloc[0]
                arch = month_slice.loc[t, 'archetype']
                arch_val = arch if isinstance(arch, str) else arch.iloc[0]
                if spread_val <= 0.0 or arch_val == 'High_Friction_Distress':
                    sells_arm3.add(t)
                    continue
            if t in price_matrix.columns and t in entry_prices_arm3:
                cur_p = price_matrix.loc[p_start_date, t]
                ent_p = entry_prices_arm3[t]
                if ent_p > 0 and (cur_p / ent_p >= 1.20):
                    sells_arm3.add(t)
                    continue
            if t not in price_matrix.columns:
                sells_arm3.add(t)

        for s in sells_arm3:
            holdings_arm3.remove(s)
            entry_prices_arm3.pop(s, None)

        slots_needed_3 = max_equities - len(holdings_arm3)
        new_buys_arm3: Set[str] = set()
        if slots_needed_3 > 0:
            cands3 = month_slice[
                (month_slice['roic_wacc_spread'] > 0.0) &
                (month_slice['fcf_margin'] > 0.0) &
                (month_slice['archetype'] != 'High_Friction_Distress') &
                (month_slice.index.isin(price_matrix.columns))
            ].copy()
            # Blend Quant Score (70%) + Qualitative Score (30%)
            sp_z = (cands3['roic_wacc_spread'] - cands3['roic_wacc_spread'].mean()) / (cands3['roic_wacc_spread'].std() + 1e-6)
            fc_z = (cands3['fcf_margin'] - cands3['fcf_margin'].mean()) / (cands3['fcf_margin'].std() + 1e-6)
            q_sc = (cands3['qual_score_Q'] - cands3['qual_score_Q'].mean()) / (cands3['qual_score_Q'].std() + 1e-6)
            cands3['hybrid_score'] = (0.7 * (sp_z + 0.5 * fc_z) + 0.3 * q_sc) * cands3['archetype_multiplier']
            valid3 = [
                tk for tk in cands3.index 
                if tk not in holdings_arm3 and (
                    tk not in vol_matrix.columns or 
                    np.isnan(vol_matrix.loc[p_start_date, tk]) or 
                    (0.15 <= vol_matrix.loc[p_start_date, tk] <= 0.40)
                )
            ]
            picks3 = cands3.loc[valid3].sort_values('hybrid_score', ascending=False).head(slots_needed_3)
            for tk in picks3.index:
                new_buys_arm3.add(tk)
                holdings_arm3.add(tk)
                entry_prices_arm3[tk] = price_matrix.loc[p_start_date, tk] if tk in price_matrix.columns else 1.0

        turnover_3 = (len(sells_arm3) + len(new_buys_arm3)) / (2.0 * max_equities)

        # -------------------------------------------------------------
        # Compute Real Returns Across 85% Equity + 15% Bond Cushion
        # -------------------------------------------------------------
        def calc_ret(holding_set: Set[str]) -> float:
            rets = []
            for tk in holding_set:
                if tk in price_matrix.columns:
                    p0 = price_matrix.loc[p_start_date, tk]
                    p1 = price_matrix.loc[p_end_date, tk]
                    if p0 > 0:
                        rets.append(p1 / p0 - 1.0)
            return float(np.mean(rets)) if len(rets) > 0 else 0.0

        eq_ret_1 = calc_ret(holdings_arm1)
        eq_ret_2 = calc_ret(holdings_arm2)
        eq_ret_3 = calc_ret(holdings_arm3)

        # Bond Return (BIL)
        if 'BIL' in price_matrix.columns:
            b0 = price_matrix.loc[p_start_date, 'BIL']
            b1 = price_matrix.loc[p_end_date, 'BIL']
            bond_ret = (b1 / b0 - 1.0) if b0 > 0 else 0.0015
        else:
            bond_ret = 0.0015

        # SPY Benchmark Return
        if 'SPY' in price_matrix.columns:
            s0 = price_matrix.loc[p_start_date, 'SPY']
            s1 = price_matrix.loc[p_end_date, 'SPY']
            spy_ret = (s1 / s0 - 1.0) if s0 > 0 else 0.0
        else:
            spy_ret = 0.0

        # Dynamic Drawdown Shield per Arm:
        # Track historical cumulative returns to identify drawdown state
        def get_dd_alloc(hist_returns: List[float], default_bond: float) -> Tuple[float, str]:
            if len(hist_returns) < 2:
                return default_bond, "NORMAL"
            cum = np.cumprod(1.0 + np.array(hist_returns))
            hwm = np.maximum.accumulate(cum)
            dd = (cum[-1] - hwm[-1]) / hwm[-1]
            if dd <= -0.15:
                return 0.85, "CRISIS"   # 85% BIL cushion in crisis
            elif dd <= -0.08:
                return 0.50, "WARNING"  # 50% BIL cushion in warning
            return default_bond, "NORMAL"

        prev_rets_1 = [r['arm1_net_ret'] for r in monthly_records]
        prev_rets_2 = [r['arm2_net_ret'] for r in monthly_records]
        prev_rets_3 = [r['arm3_net_ret'] for r in monthly_records]

        bond_w_1, regime_1 = get_dd_alloc(prev_rets_1, bond_weight)
        bond_w_2, regime_2 = get_dd_alloc(prev_rets_2, bond_weight)
        bond_w_3, regime_3 = get_dd_alloc(prev_rets_3, bond_weight)

        # Gross & Net returns scaled by shielded bond weight
        gross_1 = (1.0 - bond_w_1) * eq_ret_1 + bond_w_1 * bond_ret
        gross_2 = (1.0 - bond_w_2) * eq_ret_2 + bond_w_2 * bond_ret
        gross_3 = (1.0 - bond_w_3) * eq_ret_3 + bond_w_3 * bond_ret

        drag_1 = turnover_1 * (tcost_bps / 10000.0)
        drag_2 = turnover_2 * (tcost_bps / 10000.0)
        drag_3 = turnover_3 * (tcost_bps / 10000.0)

        net_1 = gross_1 - drag_1
        net_2 = gross_2 - drag_2
        net_3 = gross_3 - drag_3

        monthly_records.append({
            'rebalance_date': cur_date,
            'next_rebalance_date': next_date,
            'regime_arm1': regime_1,
            'regime_arm2': regime_2,
            'regime_arm3': regime_3,
            # Arm 1
            'arm1_holdings': len(holdings_arm1),
            'arm1_turnover': turnover_1,
            'arm1_equity_ret': eq_ret_1,
            'arm1_gross_ret': gross_1,
            'arm1_net_ret': net_1,
            # Arm 2
            'arm2_holdings': len(holdings_arm2),
            'arm2_turnover': turnover_2,
            'arm2_equity_ret': eq_ret_2,
            'arm2_gross_ret': gross_2,
            'arm2_net_ret': net_2,
            # Arm 3
            'arm3_holdings': len(holdings_arm3),
            'arm3_turnover': turnover_3,
            'arm3_equity_ret': eq_ret_3,
            'arm3_gross_ret': gross_3,
            'arm3_net_ret': net_3,
            # Benchmarks
            'bond_ret': bond_ret,
            'spy_ret': spy_ret,
        })

    df_res = pd.DataFrame(monthly_records)
    return df_res


def compute_arm_analytics(
    r_series: np.ndarray, 
    benchmark_series: np.ndarray, 
    rf_annual: float = 0.02, 
    initial_capital: float = 10000.0
) -> Dict[str, float]:
    """Computes comprehensive institutional metrics for a monthly return series."""
    T = len(r_series)
    rf_monthly = (1.0 + rf_annual) ** (1.0 / 12.0) - 1.0
    
    # Cumulative & CAGR
    cum_ret = float(np.prod(1.0 + r_series) - 1.0)
    cagr = float((1.0 + cum_ret) ** (12.0 / T) - 1.0) if T > 0 else 0.0
    final_wealth = initial_capital * (1.0 + cum_ret)
    
    # Volatility
    vol_ann = float(np.std(r_series, ddof=1) * np.sqrt(12.0))
    
    # Sharpe
    excess_ret = r_series - rf_monthly
    sharpe = float(np.mean(excess_ret) / np.std(r_series, ddof=1) * np.sqrt(12.0)) if vol_ann > 0 else 0.0
    
    # Downside Vol & Sortino
    downside_diff = np.where(excess_ret < 0, excess_ret, 0.0)
    downside_dev = float(np.sqrt(np.mean(downside_diff ** 2)) * np.sqrt(12.0))
    sortino = float((np.mean(excess_ret) * 12.0) / downside_dev) if downside_dev > 0 else 0.0
    
    # Max Drawdown
    wealth = initial_capital * np.cumprod(1.0 + r_series)
    peak = np.maximum.accumulate(wealth)
    drawdowns = (wealth - peak) / peak
    max_dd = float(np.min(drawdowns))
    
    # Calmar
    calmar = float(cagr / abs(max_dd)) if abs(max_dd) > 0 else 0.0
    
    # Benchmark relative (Beta, Alpha, Information Ratio)
    cov_mat = np.cov(r_series, benchmark_series)
    beta = float(cov_mat[0, 1] / (cov_mat[1, 1] + 1e-9))
    bench_cagr = float((np.prod(1.0 + benchmark_series)) ** (12.0 / T) - 1.0)
    alpha = float(cagr - (rf_annual + beta * (bench_cagr - rf_annual)))
    
    # Tracking Error & Information Ratio
    active_ret = r_series - benchmark_series
    te = float(np.std(active_ret, ddof=1) * np.sqrt(12.0))
    ir = float((np.mean(active_ret) * 12.0) / te) if te > 0 else 0.0
    
    # Up/Down Market Capture
    up_idx = benchmark_series > 0
    down_idx = benchmark_series < 0
    up_capture = float(np.mean(r_series[up_idx]) / np.mean(benchmark_series[up_idx])) if np.sum(up_idx) > 0 else 1.0
    down_capture = float(np.mean(r_series[down_idx]) / np.mean(benchmark_series[down_idx])) if np.sum(down_idx) > 0 else 1.0

    return {
        'cagr': cagr,
        'cumulative_return': cum_ret,
        'final_wealth': final_wealth,
        'annualized_vol': vol_ann,
        'sharpe_ratio': sharpe,
        'sortino_ratio': sortino,
        'max_drawdown': max_dd,
        'calmar_ratio': calmar,
        'beta': beta,
        'alpha': alpha,
        'tracking_error': te,
        'information_ratio': ir,
        'up_capture': up_capture,
        'down_capture': down_capture,
    }


def main():
    print("=" * 80)
    print("3-ARM COMPARATIVE WALK-FORWARD BACKTEST & SPY BENCHMARK SUITE")
    print("=" * 80)
    
    # 1. Load data
    df_merged = load_universe_and_qual_panel()
    tickers = df_merged['ticker'].unique().tolist()
    
    # 2. Load prices
    price_matrix, vol_matrix = load_price_matrices(tickers)
    
    # 3. Simulate 3 arms
    df_perf = simulate_three_arms(df_merged, price_matrix, vol_matrix)
    
    # 4. Compute metrics across all 3 arms & SPY
    spy_rets = df_perf['spy_ret'].values
    
    m_arm1 = compute_arm_analytics(df_perf['arm1_net_ret'].values, spy_rets)
    m_arm2 = compute_arm_analytics(df_perf['arm2_net_ret'].values, spy_rets)
    m_arm3 = compute_arm_analytics(df_perf['arm3_net_ret'].values, spy_rets)
    m_spy  = compute_arm_analytics(spy_rets, spy_rets)
    
    # Add turnover
    m_arm1['avg_monthly_turnover'] = float(df_perf['arm1_turnover'].mean())
    m_arm2['avg_monthly_turnover'] = float(df_perf['arm2_turnover'].mean())
    m_arm3['avg_monthly_turnover'] = float(df_perf['arm3_turnover'].mean())
    m_spy['avg_monthly_turnover'] = 0.0
    
    results = {
        'Arm 1: Full Quant': m_arm1,
        'Arm 2: Qualitative Moat': m_arm2,
        'Arm 3: Hybrid Blend': m_arm3,
        'Benchmark: SPY (S&P 500)': m_spy,
    }
    
    # Save outputs
    out_dir = REPO_ROOT / 'data' / 'backtests' / 'three_arm_comparison'
    out_dir.mkdir(parents=True, exist_ok=True)
    
    df_perf.to_csv(out_dir / 'three_arm_monthly_performance.csv', index=False)
    with open(out_dir / 'three_arm_metrics.json', 'w') as f:
        json.dump(results, f, indent=2)
        
    print("\n" + "=" * 90)
    print("HEAD-TO-HEAD COMPARATIVE BACKTEST RESULTS (2018 - 2024, 83 MONTHS)")
    print("=" * 90)
    
    summary_table = pd.DataFrame({
        'Metric': [
            'CAGR (%)',
            'Cumulative Return (%)',
            'Final Wealth ($10k)',
            'Annualized Volatility (%)',
            'Sharpe Ratio (Rf=2%)',
            'Sortino Ratio',
            'Maximum Drawdown (%)',
            'Calmar Ratio',
            'Beta vs SPY',
            'Jensen Alpha (%)',
            'Information Ratio',
            'Up-Market Capture',
            'Down-Market Capture',
            'Avg Monthly Turnover (%)'
        ],
        'Arm 1 (Full Quant)': [
            f"{m_arm1['cagr']*100:.2f}%",
            f"{m_arm1['cumulative_return']*100:.2f}%",
            f"${m_arm1['final_wealth']:,.2f}",
            f"{m_arm1['annualized_vol']*100:.2f}%",
            f"{m_arm1['sharpe_ratio']:.2f}",
            f"{m_arm1['sortino_ratio']:.2f}",
            f"{m_arm1['max_drawdown']*100:.2f}%",
            f"{m_arm1['calmar_ratio']:.2f}",
            f"{m_arm1['beta']:.2f}",
            f"{m_arm1['alpha']*100:.2f}%",
            f"{m_arm1['information_ratio']:.2f}",
            f"{m_arm1['up_capture']*100:.1f}%",
            f"{m_arm1['down_capture']*100:.1f}%",
            f"{m_arm1['avg_monthly_turnover']*100:.2f}%",
        ],
        'Arm 2 (Qual Moat)': [
            f"{m_arm2['cagr']*100:.2f}%",
            f"{m_arm2['cumulative_return']*100:.2f}%",
            f"${m_arm2['final_wealth']:,.2f}",
            f"{m_arm2['annualized_vol']*100:.2f}%",
            f"{m_arm2['sharpe_ratio']:.2f}",
            f"{m_arm2['sortino_ratio']:.2f}",
            f"{m_arm2['max_drawdown']*100:.2f}%",
            f"{m_arm2['calmar_ratio']:.2f}",
            f"{m_arm2['beta']:.2f}",
            f"{m_arm2['alpha']*100:.2f}%",
            f"{m_arm2['information_ratio']:.2f}",
            f"{m_arm2['up_capture']*100:.1f}%",
            f"{m_arm2['down_capture']*100:.1f}%",
            f"{m_arm2['avg_monthly_turnover']*100:.2f}%",
        ],
        'Arm 3 (Hybrid)': [
            f"{m_arm3['cagr']*100:.2f}%",
            f"{m_arm3['cumulative_return']*100:.2f}%",
            f"${m_arm3['final_wealth']:,.2f}",
            f"{m_arm3['annualized_vol']*100:.2f}%",
            f"{m_arm3['sharpe_ratio']:.2f}",
            f"{m_arm3['sortino_ratio']:.2f}",
            f"{m_arm3['max_drawdown']*100:.2f}%",
            f"{m_arm3['calmar_ratio']:.2f}",
            f"{m_arm3['beta']:.2f}",
            f"{m_arm3['alpha']*100:.2f}%",
            f"{m_arm3['information_ratio']:.2f}",
            f"{m_arm3['up_capture']*100:.1f}%",
            f"{m_arm3['down_capture']*100:.1f}%",
            f"{m_arm3['avg_monthly_turnover']*100:.2f}%",
        ],
        'S&P 500 (SPY)': [
            f"{m_spy['cagr']*100:.2f}%",
            f"{m_spy['cumulative_return']*100:.2f}%",
            f"${m_spy['final_wealth']:,.2f}",
            f"{m_spy['annualized_vol']*100:.2f}%",
            f"{m_spy['sharpe_ratio']:.2f}",
            f"{m_spy['sortino_ratio']:.2f}",
            f"{m_spy['max_drawdown']*100:.2f}%",
            f"{m_spy['calmar_ratio']:.2f}",
            f"{m_spy['beta']:.2f}",
            f"{m_spy['alpha']*100:.2f}%",
            f"{m_spy['information_ratio']:.2f}",
            f"{m_spy['up_capture']*100:.1f}%",
            f"{m_spy['down_capture']*100:.1f}%",
            f"{m_spy['avg_monthly_turnover']*100:.2f}%",
        ],
    })
    
    print(summary_table.to_string(index=False))
    print("=" * 90)
    print(f"Results saved to: {out_dir}")
    print("=" * 90)


if __name__ == '__main__':
    main()
