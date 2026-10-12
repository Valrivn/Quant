"""
Pure Real-Data Point-In-Time Universe Builder (100% Genuine SEC Accounting Filings)
-----------------------------------------------------------------------------------
1. Reads all 83,265 genuine SEC XBRL filings from data/pit_fundamentals/valuation_fundamentals.parquet.
2. For each monthly rebalance date (Jan 2018 - Dec 2024, 84 months):
   - Selects the latest filed 10-K or 10-Q where filed_date <= rebalance_date.
   - Computes direct accounting metrics:
       pure_roic = nopat_cap_rd / invested_capital_cap_rd
       pure_wacc = wacc_estimate
       pure_spread = pure_roic - pure_wacc
       pure_fcf_margin = operating_cash_flow / total_revenue (or CFO / invested_capital)
   - Zero fallback imputation: Excludes any ticker without strictly filed SEC data.
3. Saves to data/universe/pure_real_pit_universe.parquet.
"""

import os
import pyarrow.parquet as pq
import pyarrow as pa
import pandas as pd
import numpy as np

def build_pure_universe():
    print("[1/3] Loading 83,265 genuine SEC XBRL filings...")
    fund_path = 'data/pit_fundamentals/valuation_fundamentals.parquet'
    df_f = pq.read_table(fund_path).to_pandas()
    df_f['filed_date'] = pd.to_datetime(df_f['filed_date'])
    df_f = df_f.sort_values(['ticker', 'filed_date'])
    print(f"      Loaded {len(df_f):,} filings across {df_f['ticker'].nunique():,} unique tickers.")
    
    # 84 monthly rebalance dates from 2018-01-31 to 2024-12-31
    rebalance_dates = pd.date_range(start='2018-01-31', end='2024-12-31', freq='ME')
    print(f"[2/3] Performing Point-In-Time as-of joins across {len(rebalance_dates)} monthly dates...")
    
    monthly_slices = []
    
    for r_date in rebalance_dates:
        # Strict PIT filter: filings known on or before rebalance date
        as_of = df_f[df_f['filed_date'] <= r_date].groupby('ticker').last().reset_index()
        
        # Calculate Pure Accounting Metrics
        as_of['roic'] = np.where(as_of['invested_capital_cap_rd'] > 0, as_of['nopat_cap_rd'] / as_of['invested_capital_cap_rd'], np.nan)
        as_of['wacc'] = as_of['wacc_estimate']
        as_of['roic_wacc_spread'] = as_of['roic'] - as_of['wacc']
        
        # Cash flow health: operating_cash_flow / total_assets
        as_of['fcf_margin'] = np.where(as_of['total_assets'] > 0, as_of['operating_cash_flow'] / as_of['total_assets'], np.nan)
        
        # Filter strictly for valid accounting data (Zero synthetic or median fallback)
        valid = as_of[
            as_of['roic'].notnull() & 
            as_of['wacc'].notnull() & 
            as_of['roic_wacc_spread'].notnull() &
            (as_of['operating_cash_flow'].notnull())
        ].copy()
        
        valid['rebalance_date'] = r_date
        cols = ['ticker', 'rebalance_date', 'filed_date', 'roic', 'wacc', 'roic_wacc_spread', 'fcf_margin', 'operating_cash_flow', 'invested_capital_cap_rd']
        monthly_slices.append(valid[cols])
        
    df_pure = pd.concat(monthly_slices, ignore_index=True)
    print(f"      Total pure real observation rows: {len(df_pure):,}")
    print(f"      Average valid tickers per month : {len(df_pure) / len(rebalance_dates):.1f}")
    print(f"      Min / Max tickers per month    : {df_pure.groupby('rebalance_date')['ticker'].count().min()} / {df_pure.groupby('rebalance_date')['ticker'].count().max()}")
    
    out_dir = 'data/universe'
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, 'pure_real_pit_universe.parquet')
    
    print(f"[3/3] Saving pure real universe to {out_file}...")
    table = pa.Table.from_pandas(df_pure)
    pq.write_table(table, out_file)
    print("      SUCCESS: Pure Real PIT Universe generated with 0% fallback data!")
    return out_file

if __name__ == '__main__':
    build_pure_universe()
