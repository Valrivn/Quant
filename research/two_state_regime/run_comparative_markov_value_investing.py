"""
research/two_state_regime/run_comparative_markov_value_investing.py

Head-to-Head Comparative Backtest (2018 - 2024, 83 Monthly Rebalance Periods):
Compares:
  1. Base Value Investing:
     - Arm 1: Full Quant (ROIC-WACC Spread + FCF Margin + Valuation Gate)
     - Arm 2: Qualitative Moat (Composite Q_i + Moat Alpha Spread + Archetype Multiplier)
     - Arm 3: Hybrid Blend (Quant baseline modulated by Moat conviction)
  2. Markov + Value Investing:
     - Markov Arm 1: Value Investing dynamically conditioned by 2-State Markov + Cooldown Buffer
     - Markov Arm 2: Qual Moat dynamically conditioned by 2-State Markov + Cooldown Buffer
     - Markov Arm 3: Hybrid Blend dynamically conditioned by 2-State Markov + Cooldown Buffer
  3. Benchmark: S&P 500 (SPY ETF)

All metrics strictly match the institutional dashboard requirements.
"""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from typing import Dict, List, Set, Tuple

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtesting.run_three_arm_comparative_backtest import (
    load_universe_and_qual_panel,
    load_price_matrices,
    compute_arm_analytics,
)
from research.two_state_regime.two_state_engine import TwoStateRegimeEngine
from research.two_state_regime.cooldown_buffer import TwoStateCooldownBuffer, CooldownBufferConfig


def simulate_comparative_suite(
    df_merged: pd.DataFrame,
    price_matrix: pd.DataFrame,
    vol_matrix: pd.DataFrame,
    max_equities: int = 25,
    tcost_bps: float = 15.0,
) -> Tuple[pd.DataFrame, Dict[str, any]]:
    rebalance_dates = sorted(df_merged['rebalance_date'].unique())

    # --- BASE VALUE INVESTING HOLDINGS ---
    h_arm1: Set[str] = set()
    ent_arm1: Dict[str, float] = {}
    h_arm2: Set[str] = set()
    ent_arm2: Dict[str, float] = {}
    h_arm3: Set[str] = set()
    ent_arm3: Dict[str, float] = {}

    # --- MARKOV + VALUE INVESTING HOLDINGS ---
    m_h_arm1: Set[str] = set()
    m_ent_arm1: Dict[str, float] = {}
    m_h_arm2: Set[str] = set()
    m_ent_arm2: Dict[str, float] = {}
    m_h_arm3: Set[str] = set()
    m_ent_arm3: Dict[str, float] = {}

    # Initialize Markov Engine & Cooldown Buffer strictly Point-in-Time
    regime_engine = TwoStateRegimeEngine()
    regime_buffer = TwoStateCooldownBuffer(CooldownBufferConfig(
        cooldown_days=25,
        confirmation_trigger_days=3,
        enter_threshold=0.60,
        min_dwell_days=15,
        emergency_vol_sigma=-3.5,
        emergency_5d_drop=-0.08
    ))

    spy = price_matrix['SPY'].dropna()
    spy_ret_daily = spy.pct_change()

    monthly_records = []

    for i in range(len(rebalance_dates) - 1):
        cur_date = rebalance_dates[i]
        next_date = rebalance_dates[i + 1]

        avail_dates = price_matrix.index
        t_starts = avail_dates[avail_dates >= cur_date]
        t_ends = avail_dates[avail_dates >= next_date]
        if len(t_starts) == 0 or len(t_ends) == 0:
            continue
        p_start = t_starts[0]
        p_end = t_ends[0]

        month_slice = df_merged[df_merged['rebalance_date'] == cur_date].set_index('ticker')

        # -------------------------------------------------------------
        # 1. Update Point-in-Time Markov Regime State
        # -------------------------------------------------------------
        feat = regime_engine.compute_daily_features(spy, p_start)
        if feat is not None:
            probs = regime_engine.calculate_posteriors(feat)
            r_1d = float(spy_ret_daily.loc[p_start]) if p_start in spy_ret_daily.index and not np.isnan(spy_ret_daily.loc[p_start]) else 0.0
            r_5d = float(spy.loc[p_start] / spy.loc[spy.index[max(0, spy.index.get_loc(p_start) - 5)]] - 1.0) if spy.index.get_loc(p_start) >= 5 else 0.0
            markov_regime = regime_buffer.update(p_start, probs, daily_return=r_1d, trailing_5d_return=r_5d)
        else:
            markov_regime = "BULL"

        # -------------------------------------------------------------
        # Helper: Select Candidates by Weights
        # -------------------------------------------------------------
        def select_picks(w_q: float, w_moat: float, n_needed: int, existing: Set[str], require_positive_spread: bool = True) -> List[str]:
            cands = month_slice[
                (month_slice['fcf_margin'] > 0.0) &
                (month_slice['archetype'] != 'High_Friction_Distress') &
                (month_slice.index.isin(price_matrix.columns))
            ].copy()
            if require_positive_spread:
                cands = cands[cands['roic_wacc_spread'] > 0.0]

            if len(cands) == 0 or n_needed <= 0:
                return []

            sp_z = (cands['roic_wacc_spread'] - cands['roic_wacc_spread'].mean()) / (cands['roic_wacc_spread'].std() + 1e-6)
            fc_z = (cands['fcf_margin'] - cands['fcf_margin'].mean()) / (cands['fcf_margin'].std() + 1e-6)
            q_sc = (cands['qual_score_Q'] - cands['qual_score_Q'].mean()) / (cands['qual_score_Q'].std() + 1e-6)
            alpha_z = (cands['alpha_spread'] - cands['alpha_spread'].mean()) / (cands['alpha_spread'].std() + 1e-6)

            q_comp = sp_z + 0.5 * fc_z
            moat_comp = 0.6 * alpha_z + 0.4 * q_sc

            score = (w_q * q_comp + w_moat * moat_comp) * cands['archetype_multiplier']

            valid = [
                tk for tk in cands.index
                if tk not in existing and (
                    tk not in vol_matrix.columns or
                    np.isnan(vol_matrix.loc[p_start, tk]) or
                    (0.15 <= vol_matrix.loc[p_start, tk] <= 0.40)
                )
            ]
            return list(cands.loc[valid].assign(score=score).sort_values('score', ascending=False).head(n_needed).index)

        # -------------------------------------------------------------
        # A. Simulate BASE VALUE INVESTING ARMS (Fixed 85% Eq / 15% Bond)
        # -------------------------------------------------------------
        # Arm 1 Base (Full Quant)
        sells_a1 = {t for t in list(h_arm1) if (t in month_slice.index and month_slice.loc[t, 'roic_wacc_spread'] <= 0.0) or (t in price_matrix.columns and t in ent_arm1 and ent_arm1[t] > 0 and price_matrix.loc[p_start, t] / ent_arm1[t] >= 1.15) or t not in price_matrix.columns}
        for s in sells_a1: h_arm1.remove(s); ent_arm1.pop(s, None)
        buys_a1 = select_picks(w_q=1.0, w_moat=0.0, n_needed=max_equities - len(h_arm1), existing=h_arm1)
        for b in buys_a1: h_arm1.add(b); ent_arm1[b] = price_matrix.loc[p_start, b] if b in price_matrix.columns else 1.0
        turnover_a1 = (len(sells_a1) + len(buys_a1)) / (2.0 * max_equities)

        # Arm 2 Base (Qual Moat)
        sells_a2 = {t for t in list(h_arm2) if (t in month_slice.index and month_slice.loc[t, 'archetype'] == 'High_Friction_Distress') or (t in price_matrix.columns and t in ent_arm2 and ent_arm2[t] > 0 and (price_matrix.loc[p_start, t] / ent_arm2[t] >= 1.25 or price_matrix.loc[p_start, t] / ent_arm2[t] <= 0.80)) or t not in price_matrix.columns}
        for s in sells_a2: h_arm2.remove(s); ent_arm2.pop(s, None)
        buys_a2 = select_picks(w_q=0.0, w_moat=1.0, n_needed=max_equities - len(h_arm2), existing=h_arm2, require_positive_spread=False)
        for b in buys_a2: h_arm2.add(b); ent_arm2[b] = price_matrix.loc[p_start, b] if b in price_matrix.columns else 1.0
        turnover_a2 = (len(sells_a2) + len(buys_a2)) / (2.0 * max_equities)

        # Arm 3 Base (Hybrid Blend: 70% Quant / 30% Moat)
        sells_a3 = {t for t in list(h_arm3) if (t in month_slice.index and (month_slice.loc[t, 'roic_wacc_spread'] <= 0.0 or month_slice.loc[t, 'archetype'] == 'High_Friction_Distress')) or (t in price_matrix.columns and t in ent_arm3 and ent_arm3[t] > 0 and price_matrix.loc[p_start, t] / ent_arm3[t] >= 1.20) or t not in price_matrix.columns}
        for s in sells_a3: h_arm3.remove(s); ent_arm3.pop(s, None)
        buys_a3 = select_picks(w_q=0.70, w_moat=0.30, n_needed=max_equities - len(h_arm3), existing=h_arm3)
        for b in buys_a3: h_arm3.add(b); ent_arm3[b] = price_matrix.loc[p_start, b] if b in price_matrix.columns else 1.0
        turnover_a3 = (len(sells_a3) + len(buys_a3)) / (2.0 * max_equities)

        # -------------------------------------------------------------
        # B. Simulate MARKOV + VALUE INVESTING ARMS (Dynamic Conditioning)
        # -------------------------------------------------------------
        # Configure Regime-Conditioned Parameter Set
        if markov_regime == "BEAR":
            m_bond = 0.50             # 50% Defensive Treasuries BIL in Bear
            m_w_quant = 0.20          # 80% Moat preservation focus to avoid bankruptcies
            m_w_moat = 0.80
            m_profit_barrier = 1.35   # Wide threshold to avoid premature selling during volatility
            m_deadband = 0.20
        elif markov_regime == "COOLDOWN":
            m_bond = 0.25             # 25% Cooldown buffer
            m_w_quant = 0.70          # Anchor strictly on cash-flow rents during uncertainty
            m_w_moat = 0.30
            m_profit_barrier = 1.15   # Lock in profits quickly (+15%) in stagnant chop
            m_deadband = 0.20
        else: # BULL
            m_bond = 0.05             # 95% Equities / 5% BIL in Bull expansion
            m_w_quant = 0.65          # Balanced compounding
            m_w_moat = 0.35
            m_profit_barrier = 1.25   # Let winners run (+25%)
            m_deadband = 0.10

        # Markov Arm 1 (Full Quant with Markov Allocation Cushion)
        m_sells_a1 = {t for t in list(m_h_arm1) if (t in month_slice.index and month_slice.loc[t, 'roic_wacc_spread'] <= 0.0) or (t in price_matrix.columns and t in m_ent_arm1 and m_ent_arm1[t] > 0 and price_matrix.loc[p_start, t] / m_ent_arm1[t] >= m_profit_barrier) or t not in price_matrix.columns}
        for s in m_sells_a1: m_h_arm1.remove(s); m_ent_arm1.pop(s, None)
        m_buys_a1 = select_picks(w_q=1.0, w_moat=0.0, n_needed=max_equities - len(m_h_arm1), existing=m_h_arm1)
        for b in m_buys_a1: m_h_arm1.add(b); m_ent_arm1[b] = price_matrix.loc[p_start, b] if b in price_matrix.columns else 1.0
        m_turnover_a1 = (len(m_sells_a1) + len(m_buys_a1)) / (2.0 * max_equities)

        # Markov Arm 2 (Qual Moat with Markov Allocation Cushion)
        m_sells_a2 = {t for t in list(m_h_arm2) if (t in month_slice.index and month_slice.loc[t, 'archetype'] == 'High_Friction_Distress') or (t in price_matrix.columns and t in m_ent_arm2 and m_ent_arm2[t] > 0 and (price_matrix.loc[p_start, t] / m_ent_arm2[t] >= m_profit_barrier or price_matrix.loc[p_start, t] / m_ent_arm2[t] <= 0.80)) or t not in price_matrix.columns}
        for s in m_sells_a2: m_h_arm2.remove(s); m_ent_arm2.pop(s, None)
        m_buys_a2 = select_picks(w_q=0.0, w_moat=1.0, n_needed=max_equities - len(m_h_arm2), existing=m_h_arm2, require_positive_spread=False)
        for b in m_buys_a2: m_h_arm2.add(b); m_ent_arm2[b] = price_matrix.loc[p_start, b] if b in price_matrix.columns else 1.0
        m_turnover_a2 = (len(m_sells_a2) + len(m_buys_a2)) / (2.0 * max_equities)

        # Markov Arm 3 (Markov Hybrid Synergy: Dynamic w_quant & w_moat modulation)
        m_sells_a3 = {t for t in list(m_h_arm3) if (t in month_slice.index and (month_slice.loc[t, 'roic_wacc_spread'] <= 0.0 or month_slice.loc[t, 'archetype'] == 'High_Friction_Distress')) or (t in price_matrix.columns and t in m_ent_arm3 and m_ent_arm3[t] > 0 and price_matrix.loc[p_start, t] / m_ent_arm3[t] >= m_profit_barrier) or t not in price_matrix.columns}
        for s in m_sells_a3: m_h_arm3.remove(s); m_ent_arm3.pop(s, None)
        m_buys_a3 = select_picks(w_q=m_w_quant, w_moat=m_w_moat, n_needed=max_equities - len(m_h_arm3), existing=m_h_arm3)
        for b in m_buys_a3: m_h_arm3.add(b); m_ent_arm3[b] = price_matrix.loc[p_start, b] if b in price_matrix.columns else 1.0
        m_turnover_a3 = (len(m_sells_a3) + len(m_buys_a3)) / (2.0 * max_equities)

        # -------------------------------------------------------------
        # Compute Period Returns
        # -------------------------------------------------------------
        def calc_ret(holding_set: Set[str]) -> float:
            rets = [price_matrix.loc[p_end, tk] / price_matrix.loc[p_start, tk] - 1.0 for tk in holding_set if tk in price_matrix.columns and price_matrix.loc[p_start, tk] > 0]
            return float(np.mean(rets)) if rets else 0.0

        r_base_eq1 = calc_ret(h_arm1)
        r_base_eq2 = calc_ret(h_arm2)
        r_base_eq3 = calc_ret(h_arm3)

        r_mkv_eq1 = calc_ret(m_h_arm1)
        r_mkv_eq2 = calc_ret(m_h_arm2)
        r_mkv_eq3 = calc_ret(m_h_arm3)

        b0 = price_matrix.loc[p_start, 'BIL'] if 'BIL' in price_matrix.columns else 1.0
        b1 = price_matrix.loc[p_end, 'BIL'] if 'BIL' in price_matrix.columns else 1.0
        bond_ret = (b1 / b0 - 1.0) if b0 > 0 else 0.0015

        s0 = price_matrix.loc[p_start, 'SPY'] if 'SPY' in price_matrix.columns else 1.0
        s1 = price_matrix.loc[p_end, 'SPY'] if 'SPY' in price_matrix.columns else 1.0
        spy_ret = (s1 / s0 - 1.0) if s0 > 0 else 0.0

        # Base Net Returns (Fixed 85% Eq / 15% Bond)
        net_base_1 = (0.85 * r_base_eq1 + 0.15 * bond_ret) - turnover_a1 * (tcost_bps / 10000.0)
        net_base_2 = (0.85 * r_base_eq2 + 0.15 * bond_ret) - turnover_a2 * (tcost_bps / 10000.0)
        net_base_3 = (0.85 * r_base_eq3 + 0.15 * bond_ret) - turnover_a3 * (tcost_bps / 10000.0)

        # Markov Net Returns (Dynamic m_bond cushion)
        net_mkv_1 = ((1.0 - m_bond) * r_mkv_eq1 + m_bond * bond_ret) - m_turnover_a1 * (tcost_bps / 10000.0)
        net_mkv_2 = ((1.0 - m_bond) * r_mkv_eq2 + m_bond * bond_ret) - m_turnover_a2 * (tcost_bps / 10000.0)
        net_mkv_3 = ((1.0 - m_bond) * r_mkv_eq3 + m_bond * bond_ret) - m_turnover_a3 * (tcost_bps / 10000.0)

        monthly_records.append({
            'rebalance_date': cur_date,
            'markov_regime': markov_regime,
            'base_arm1_net': net_base_1,
            'base_arm2_net': net_base_2,
            'base_arm3_net': net_base_3,
            'mkv_arm1_net': net_mkv_1,
            'mkv_arm2_net': net_mkv_2,
            'mkv_arm3_net': net_mkv_3,
            'spy_ret': spy_ret,
            'turnover_base1': turnover_a1,
            'turnover_base2': turnover_a2,
            'turnover_base3': turnover_a3,
            'turnover_mkv1': m_turnover_a1,
            'turnover_mkv2': m_turnover_a2,
            'turnover_mkv3': m_turnover_a3,
        })

    df_res = pd.DataFrame(monthly_records)
    return df_res


def main():
    print("=" * 110)
    print("HEAD-TO-HEAD: VALUE INVESTING VS (MARKOV + VALUE INVESTING) ACROSS ARMS 1, 2, 3 VS S&P 500")
    print("=" * 110)

    df_merged = load_universe_and_qual_panel()
    tickers = df_merged['ticker'].unique().tolist()
    price_matrix, vol_matrix = load_price_matrices(tickers)

    print("\nSimulating 83-Month Comparative Walk-Forward Portfolios (2018 - 2024)...")
    df_perf = simulate_comparative_suite(df_merged, price_matrix, vol_matrix)

    benchmark_r = df_perf['spy_ret'].values

    series_map = {
        'Value Investing (Arm 1: Quant)': (df_perf['base_arm1_net'].values, df_perf['turnover_base1'].mean()),
        'Value Investing (Arm 2: Moat)': (df_perf['base_arm2_net'].values, df_perf['turnover_base2'].mean()),
        'Value Investing (Arm 3: Hybrid)': (df_perf['base_arm3_net'].values, df_perf['turnover_base3'].mean()),
        'Markov + Value Investing (Arm 1)': (df_perf['mkv_arm1_net'].values, df_perf['turnover_mkv1'].mean()),
        'Markov + Value Investing (Arm 2)': (df_perf['mkv_arm2_net'].values, df_perf['turnover_mkv2'].mean()),
        'Markov + Value Investing (Arm 3)': (df_perf['mkv_arm3_net'].values, df_perf['turnover_mkv3'].mean()),
        'S&P 500 (SPY Benchmark)': (benchmark_r, 0.0),
    }

    metrics_out = {}
    for name, (r_arr, avg_t) in series_map.items():
        m = compute_arm_analytics(r_arr, benchmark_r, rf_annual=0.02, initial_capital=10000.0)
        m['avg_monthly_turnover'] = avg_t
        metrics_out[name] = m

    # Construct complete tabular comparison
    metric_rows = [
        ('CAGR (%)', lambda m: f"{m['cagr']*100:.2f}%"),
        ('Cumulative Net Return', lambda m: f"{m['cumulative_return']*100:.2f}%"),
        ('Final Portfolio Wealth ($10k)', lambda m: f"${m['final_wealth']:,.2f}"),
        ('Annualized Volatility', lambda m: f"{m['annualized_vol']*100:.2f}%"),
        ('Net Sharpe Ratio (Rf = 2%)', lambda m: f"{m['sharpe_ratio']:.2f}"),
        ('Sortino Ratio', lambda m: f"{m['sortino_ratio']:.2f}"),
        ('Maximum Drawdown', lambda m: f"{m['max_drawdown']*100:.2f}%"),
        ('Calmar Ratio', lambda m: f"{m['calmar_ratio']:.2f}"),
        ('Market Beta vs SPY', lambda m: f"{m['beta']:.2f}"),
        ('Jensen\'s Alpha', lambda m: f"{m['alpha']*100:.2f}%"),
        ('Information Ratio', lambda m: f"{m['information_ratio']:.2f}"),
        ('Up-Market Capture Ratio', lambda m: f"{m['up_capture']*100:.1f}%"),
        ('Down-Market Capture Ratio', lambda m: f"{m['down_capture']*100:.1f}%"),
        ('Average Monthly Turnover', lambda m: f"{m['avg_monthly_turnover']*100:.2f}%"),
    ]

    col_names = list(series_map.keys())
    table_data = {'Metric': [r[0] for r in metric_rows]}
    for col in col_names:
        table_data[col] = [r[1](metrics_out[col]) for r in metric_rows]

    df_table = pd.DataFrame(table_data)

    print("\n" + "=" * 110)
    print("COMPARATIVE PERFORMANCE EVALUATION TABLE")
    print("=" * 110)
    print(df_table.to_string(index=False))
    print("=" * 110)

    out_json = REPO_ROOT / 'data' / 'backtests' / 'markov_vs_value_investing_comparison.json'
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with open(out_json, 'w') as f:
        json.dump(metrics_out, f, indent=2)
    print(f"Results saved to: {out_json}")


if __name__ == '__main__':
    main()
