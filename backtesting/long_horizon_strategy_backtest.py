"""
backtesting/long_horizon_strategy_backtest.py

17.2-Year Comprehensive Long-Horizon Strategy Backtest (2009-2026)
House of Quant — Quant Platform Production Engine

Evaluates the strategy as far back as SEC Point-In-Time fundamental filings go:
  - Evaluation Window: 2009-06-30 to 2026-08-31 (206 monthly rebalance cycles, 17.2 years)
  - Qualitative Data Policy:
      * 2018-2024: 30% Quant / 70% Qualitative Hybrid (where qualitative data exists)
      * 2009-2017 & 2025-2026: 100% Pure Quant Compounders (Arm 1 ROIC > WACC + High FCF margin)
  - Benchmark: S&P 500 (SPY ETF) across all 206 periods
  - Tri-State Optimal Weights: 70% Growth / 10% Cash Flow / 20% Ballast
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Tuple, Set
import datetime
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtesting.run_enhanced_arms import compute_15_metrics


def load_long_horizon_data():
    print("[1/4] Loading Point-In-Time SEC Valuation Fundamentals (2009-2026)...")
    fund_path = REPO_ROOT / 'data' / 'pit_fundamentals' / 'valuation_fundamentals.parquet'
    df_fund = pq.read_table(fund_path).to_pandas()
    df_fund['filed_dt'] = pd.to_datetime(df_fund['filed_date'])
    if 'roic_wacc_spread' not in df_fund.columns:
        df_fund['roic_wacc_spread'] = df_fund['roic'] - df_fund['wacc_estimate']

    print(f"      Loaded {len(df_fund):,} SEC filings across {df_fund['ticker'].nunique():,} unique tickers.")
    print(f"      Filing Date Range: {df_fund['filed_dt'].min().strftime('%Y-%m-%d')} to {df_fund['filed_dt'].max().strftime('%Y-%m-%d')}")

    # Load Qualitative Panel if available (2018-2024)
    qual_path = REPO_ROOT / 'data' / 'harness' / 'qual_pit_factor_panel_2018_2024.parquet'
    df_qual = None
    if qual_path.exists():
        df_qual = pd.read_parquet(qual_path)
        df_qual['rebalance_date'] = pd.to_datetime(df_qual['rebalance_date'])
        print(f"      Loaded Qualitative Panel: {len(df_qual):,} records (2018-2024).")

    return df_fund, df_qual


def load_prices_and_benchmarks(tickers: List[str], start_date: str = '2009-05-01', end_date: str = '2026-09-04'):
    print("[2/4] Indexing Daily Closes for Universe & Macro Benchmark Assets...")
    price_base = REPO_ROOT / 'data' / 'source' / 'yfinance'
    all_needed = set(tickers).union({'SPY', 'BIL', 'GLD', 'VNQ'})

    price_dict = {}
    for t in all_needed:
        p_path = price_base / t / 'prices.parquet'
        if p_path.exists():
            try:
                tbl = pq.read_table(p_path)
                p_df = tbl.to_pandas()
                clean_idx = pd.to_datetime(p_df.index.strftime('%Y-%m-%d'))
                close_col = 'Close' if 'Close' in p_df.columns else p_df.columns[0]
                s = pd.Series(p_df[close_col].values.flatten(), index=clean_idx)
                s = s[(s.index >= start_date) & (s.index <= end_date)]
                if len(s) > 0:
                    price_dict[t] = s
            except Exception:
                pass

    price_df = pd.DataFrame(price_dict).sort_index().ffill()
    print(f"      Successfully indexed {len(price_df.columns):,} price series from {price_df.index.min().strftime('%Y-%m-%d')} to {price_df.index.max().strftime('%Y-%m-%d')}.")
    return price_df


def run_long_horizon_simulation():
    df_fund, df_qual = load_long_horizon_data()
    all_fund_tickers = df_fund['ticker'].unique().tolist()
    price_df = load_prices_and_benchmarks(all_fund_tickers)

    print("\n[3/4] Running 17.2-Year Monthly Rebalancing Engine (2009-2026)...")

    # Generate monthly rebalance dates from 2009-06-30 to 2026-08-31
    m_ends = pd.date_range('2009-06-30', '2026-08-31', freq='ME')
    avail_dates = price_df.index

    # Qualitative lookup
    qual_lookup = {}
    if df_qual is not None:
        for row in df_qual[['rebalance_date', 'ticker', 'archetype', 'archetype_multiplier', 'qual_score_Q', 'alpha_spread']].itertuples(index=False):
            qual_lookup[(row.rebalance_date.strftime('%Y-%m-%d'), row.ticker)] = row

    # Tracking lists
    rebalance_records = []
    
    # Holdings state
    holdings_growth = set()
    entry_growth = {}

    holdings_fcf = set()
    entry_fcf = {}

    for i in range(len(m_ends) - 1):
        cur_m = m_ends[i]
        next_m = m_ends[i + 1]

        s_arr = avail_dates[avail_dates >= cur_m]
        e_arr = avail_dates[avail_dates >= next_m]
        if len(s_arr) == 0 or len(e_arr) == 0:
            continue
        s_dt = s_arr[0]
        e_dt = e_arr[0]
        cur_dt_str = cur_m.strftime('%Y-%m-%d')

        # ---------------------------------------------------------------------
        # 1. PIT Fundamental Slice: filings strictly published <= s_dt - 1 day
        # ---------------------------------------------------------------------
        pit_cutoff = s_dt - pd.Timedelta(days=1)
        valid_filings = df_fund[df_fund['filed_dt'] <= pit_cutoff]
        if len(valid_filings) == 0:
            continue

        # Latest filing per ticker
        latest_pit = valid_filings.sort_values('filed_dt').groupby('ticker').last().reset_index()

        # Candidates with positive ROIC-WACC spread and positive FCF margin
        cands = latest_pit[
            (latest_pit['roic_wacc_spread'] > 0.0) &
            (latest_pit['fcf_margin'] > 0.0) &
            (latest_pit['ticker'].isin(price_df.columns))
        ].copy()

        # ---------------------------------------------------------------------
        # 2. PILLAR 1: GROWTH ENGINE (Hybrid when Qual available, 100% Quant otherwise)
        # ---------------------------------------------------------------------
        # Sells check for Growth
        sells_g = set()
        for t in list(holdings_growth):
            if t in price_df.columns and t in entry_growth:
                p0 = entry_growth[t]
                p_cur = price_df.loc[s_dt, t]
                if p0 > 0:
                    ret = p_cur / p0 - 1.0
                    # Patient 12-month trailing exit (+25% harvest or -20% stop)
                    if ret >= 0.25 or ret <= -0.20:
                        sells_g.add(t)
                        continue
            if t not in price_df.columns:
                sells_g.add(t)

        for s in sells_g:
            holdings_growth.remove(s)
            entry_growth.pop(s, None)

        slots_g = 20 - len(holdings_growth)
        if slots_g > 0 and len(cands) > 0:
            # Check if qualitative data is available for this date
            has_qual = any((cur_dt_str, tk) in qual_lookup for tk in cands['ticker'])

            spread_z = (cands['roic_wacc_spread'] - cands['roic_wacc_spread'].mean()) / (cands['roic_wacc_spread'].std() + 1e-6)
            fcf_z = (cands['fcf_margin'] - cands['fcf_margin'].mean()) / (cands['fcf_margin'].std() + 1e-6)
            quant_z = spread_z + 0.5 * fcf_z

            if has_qual:
                # 30% Quant / 70% Qualitative
                qual_scores = []
                for tk in cands['ticker']:
                    q_entry = qual_lookup.get((cur_dt_str, tk))
                    if q_entry is not None:
                        q_val = (q_entry.alpha_spread + q_entry.qual_score_Q) * q_entry.archetype_multiplier
                    else:
                        q_val = 0.0
                    qual_scores.append(q_val)
                qual_series = pd.Series(qual_scores, index=cands.index)
                qual_z = (qual_series - qual_series.mean()) / (qual_series.std() + 1e-6)
                cands['score'] = 0.30 * quant_z + 0.70 * qual_z
            else:
                # User Policy: Replace with 100% Quant when no qualitative data!
                # Quant score: ROIC-WACC spread + FCF margin + Operating margin
                op_margin = cands['operating_margin'].fillna(0.0)
                op_z = (op_margin - op_margin.mean()) / (op_margin.std() + 1e-6)
                cands['score'] = quant_z + 0.30 * op_z

            # Exclude existing holdings
            new_cands = cands[~cands['ticker'].isin(holdings_growth)].sort_values('score', ascending=False)
            picks = new_cands.head(slots_g)
            for tk in picks['ticker']:
                holdings_growth.add(tk)
                entry_growth[tk] = price_df.loc[s_dt, tk] if tk in price_df.columns else 1.0

        # Calculate Growth Return
        rets_g = []
        for t in holdings_growth:
            if t in price_df.columns:
                p0 = price_df.loc[s_dt, t]
                p1 = price_df.loc[e_dt, t]
                if p0 > 0:
                    rets_g.append(p1 / p0 - 1.0)
        ret_g_gross = np.mean(rets_g) if len(rets_g) > 0 else 0.0
        turnover_g = len(sells_g) / 20.0
        ret_g_net = ret_g_gross - (turnover_g * 0.0015)  # 15 bps friction

        # ---------------------------------------------------------------------
        # 3. PILLAR 2: CASH-FLOW & STAGNANT CUSHION (Top FCF Margin + VNQ)
        # ---------------------------------------------------------------------
        sells_fcf = set()
        for t in list(holdings_fcf):
            if t in price_df.columns and t in entry_fcf:
                p0 = entry_fcf[t]
                p_cur = price_df.loc[s_dt, t]
                if p0 > 0 and (p_cur / p0 >= 1.25 or p_cur / p0 <= 0.85):
                    sells_fcf.add(t)
            if t not in price_df.columns:
                sells_fcf.add(t)

        for s in sells_fcf:
            holdings_fcf.remove(s)
            entry_fcf.pop(s, None)

        slots_fcf = 15 - len(holdings_fcf)
        if slots_fcf > 0:
            cands_fcf = latest_pit[
                (latest_pit['fcf_margin'] >= 0.15) &
                (latest_pit['roic'] > 0.08) &
                (latest_pit['ticker'].isin(price_df.columns)) &
                (~latest_pit['ticker'].isin(holdings_fcf))
            ]
            if len(cands_fcf) > 0:
                picks_fcf = cands_fcf.sort_values('fcf_margin', ascending=False).head(slots_fcf)
                for tk in picks_fcf['ticker']:
                    holdings_fcf.add(tk)
                    entry_fcf[tk] = price_df.loc[s_dt, tk]

        rets_fcf = []
        for t in holdings_fcf:
            if t in price_df.columns:
                p0 = price_df.loc[s_dt, t]
                p1 = price_df.loc[e_dt, t]
                if p0 > 0:
                    rets_fcf.append(p1 / p0 - 1.0)
        ret_fcf_equity = np.mean(rets_fcf) if len(rets_fcf) > 0 else 0.0

        # VNQ Real estate
        vnq0 = price_df.loc[s_dt, 'VNQ'] if 'VNQ' in price_df.columns else 1.0
        vnq1 = price_df.loc[e_dt, 'VNQ'] if 'VNQ' in price_df.columns else 1.0
        ret_vnq = (vnq1 / vnq0 - 1.0) if vnq0 > 0 else 0.0
        ret_p2 = 0.60 * ret_fcf_equity + 0.40 * ret_vnq

        # ---------------------------------------------------------------------
        # 4. PILLAR 3: BLACK SWAN BALLAST (70% BIL T-Bills + 30% GLD Gold Trust)
        # ---------------------------------------------------------------------
        b0 = price_df.loc[s_dt, 'BIL'] if 'BIL' in price_df.columns else 1.0
        b1 = price_df.loc[e_dt, 'BIL'] if 'BIL' in price_df.columns else 1.0
        ret_bil = (b1 / b0 - 1.0) if b0 > 0 else 0.0

        g0 = price_df.loc[s_dt, 'GLD'] if 'GLD' in price_df.columns else 1.0
        g1 = price_df.loc[e_dt, 'GLD'] if 'GLD' in price_df.columns else 1.0
        ret_gld = (g1 / g0 - 1.0) if g0 > 0 else 0.0

        ret_ballast = 0.70 * ret_bil + 0.30 * ret_gld

        # ---------------------------------------------------------------------
        # 5. BENCHMARK: SPY S&P 500
        # ---------------------------------------------------------------------
        sp0 = price_df.loc[s_dt, 'SPY']
        sp1 = price_df.loc[e_dt, 'SPY']
        ret_spy = (sp1 / sp0 - 1.0) if sp0 > 0 else 0.0

        # ---------------------------------------------------------------------
        # 6. PORTFOLIO COMBINATIONS
        # ---------------------------------------------------------------------
        # Tri-State Optimal (70% Growth / 10% Cash Flow / 20% Ballast)
        ret_tristate = 0.70 * ret_g_net + 0.10 * ret_p2 + 0.20 * ret_ballast

        rebalance_records.append({
            'date': cur_dt_str,
            'growth': ret_g_net,
            'cash_flow': ret_p2,
            'ballast': ret_ballast,
            'tri_state': ret_tristate,
            'spy': ret_spy,
            'era': 'Quant Only (2009-17)' if cur_dt_str < '2018-01-01' else ('Hybrid Qual/Quant (2018-24)' if cur_dt_str <= '2024-12-31' else 'OOS Expansion (2025-26)')
        })

    df_res = pd.DataFrame(rebalance_records)
    print(f"      Completed {len(df_res)} monthly simulation periods across 17.2 years.")
    return df_res


def main():
    print("=" * 95)
    print("   17.2-YEAR LONG-HORIZON PRODUCTION BACKTEST: OUR STRATEGY vs. S&P 500 (SPY)")
    print("   House of Quant — Quant Platform Engine")
    print("   Data Span: June 30, 2009 to August 31, 2026 (206 Monthly Cycles)")
    print("=" * 95)

    df_res = run_long_horizon_simulation()

    print("\n[4/4] Computing 15 Production Metrics Across Distinct Eras & Full Cycle...")

    eras = [
        ('FULL 17.2-YEAR CRUCIBLE (2009-2026)', df_res),
        ('PRE-QUALITATIVE ERA: 100% PURE QUANT (2009-2017)', df_res[df_res['era'] == 'Quant Only (2009-17)']),
        ('HYBRID QUAL/QUANT ERA (2018-2024)', df_res[df_res['era'] == 'Hybrid Qual/Quant (2018-24)']),
        ('OUT-OF-SAMPLE EXPANSION ERA (2025-2026)', df_res[df_res['era'] == 'OOS Expansion (2025-26)'])
    ]

    for era_name, sub_df in eras:
        if len(sub_df) < 6:
            continue
        print("\n" + "=" * 95)
        print(f"   ERA: {era_name} ({len(sub_df)} Monthly Periods)")
        print("=" * 95)

        m_tri = compute_15_metrics(sub_df['tri_state'].tolist(), sub_df['spy'].tolist(), total_trades=len(sub_df)*3)
        raw_tri = m_tri['_raw']
        m_grow = compute_15_metrics(sub_df['growth'].tolist(), sub_df['spy'].tolist(), total_trades=len(sub_df)*2)
        raw_grow = m_grow['_raw']
        m_spy = compute_15_metrics(sub_df['spy'].tolist(), sub_df['spy'].tolist(), total_trades=0)
        raw_spy = m_spy['_raw']

        comp_table = pd.DataFrame({
            'Production Metric': [
                'CAGR (%)', 'Cumulative Net Return', 'Net Sharpe (Rf = 2%)',
                'Sortino Ratio', 'Maximum Drawdown', 'Calmar Ratio',
                'Annualized Volatility', 'Market Beta vs SPY', "Jensen's Alpha",
                'Up-Market Capture', 'Down-Market Capture'
            ],
            'Tri-State Hedged (70/10/20)': [
                f"{raw_tri['cagr']*100:.2f}%", f"{raw_tri['cum_ret']*100:.2f}%",
                f"{raw_tri['sharpe']:.2f}", f"{raw_tri['sortino']:.2f}",
                f"{raw_tri['max_dd']*100:.2f}%", f"{raw_tri['calmar']:.2f}",
                f"{raw_tri['vol']*100:.2f}%", f"{raw_tri['beta']:.2f}",
                f"{raw_tri['jensen_alpha']*100:.2f}%",
                f"{raw_tri['up_capture']*100:.1f}%", f"{raw_tri['down_capture']*100:.1f}%"
            ],
            'Pillar 1: Growth Engine (100% Equity)': [
                f"{raw_grow['cagr']*100:.2f}%", f"{raw_grow['cum_ret']*100:.2f}%",
                f"{raw_grow['sharpe']:.2f}", f"{raw_grow['sortino']:.2f}",
                f"{raw_grow['max_dd']*100:.2f}%", f"{raw_grow['calmar']:.2f}",
                f"{raw_grow['vol']*100:.2f}%", f"{raw_grow['beta']:.2f}",
                f"{raw_grow['jensen_alpha']*100:.2f}%",
                f"{raw_grow['up_capture']*100:.1f}%", f"{raw_grow['down_capture']*100:.1f}%"
            ],
            'S&P 500 (SPY Benchmark)': [
                f"{raw_spy['cagr']*100:.2f}%", f"{raw_spy['cum_ret']*100:.2f}%",
                f"{raw_spy['sharpe']:.2f}", f"{raw_spy['sortino']:.2f}",
                f"{raw_spy['max_dd']*100:.2f}%", f"{raw_spy['calmar']:.2f}",
                f"{raw_spy['vol']*100:.2f}%", f"{raw_spy['beta']:.2f}",
                "0.00%", "100.0%", "100.0%"
            ]
        })
        print(comp_table.to_string(index=False))

    # Save output to csv
    out_dir = REPO_ROOT / 'data' / 'backtests' / 'long_horizon'
    out_dir.mkdir(parents=True, exist_ok=True)
    df_res.to_csv(out_dir / 'long_horizon_monthly_returns_2009_2026.csv', index=False)
    print(f"\n[Artifact Persisted] Results cleanly saved to: {out_dir / 'long_horizon_monthly_returns_2009_2026.csv'}")


if __name__ == '__main__':
    main()
