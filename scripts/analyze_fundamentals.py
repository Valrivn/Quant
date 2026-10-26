import sqlite3
import pandas as pd
import numpy as np
import os

print("=== 1. CIK MAP & SEC FACTS PARQUET ANALYSIS ===")
conn = sqlite3.connect("reddit_quant.db")
sec_cik_map = pd.read_sql_query("SELECT * FROM sec_cik_map", conn)
print(f"sec_cik_map total rows: {len(sec_cik_map)}, unique tickers: {sec_cik_map['ticker'].nunique()}, unique CIKs: {sec_cik_map['cik'].nunique()}")

# Clean CIK format (remove leading zeros for join if needed, or pad to 10 digits)
sec_cik_map['cik_padded'] = sec_cik_map['cik'].astype(str).str.zfill(10)
sec_cik_map['cik_int'] = pd.to_numeric(sec_cik_map['cik'], errors='coerce')

if os.path.exists("data/sec_facts/facts/facts.parquet"):
    df_facts = pd.read_parquet("data/sec_facts/facts/facts.parquet")
    print(f"\nfacts.parquet total rows: {len(df_facts)}")
    df_facts['cik_padded'] = df_facts['cik'].astype(str).str.zfill(10)
    df_facts['cik_int'] = pd.to_numeric(df_facts['cik'], errors='coerce')
    
    unique_ciks = df_facts['cik_padded'].nunique()
    print(f"Unique CIKs in facts.parquet: {unique_ciks}")
    
    # Merge with cik map to get tickers
    merged_facts = df_facts.merge(sec_cik_map[['cik_padded', 'ticker']], on='cik_padded', how='left')
    print(f"Facts merged with tickers: {merged_facts['ticker'].nunique()} tickers mapped from {unique_ciks} CIKs")
    
    print("\nDistinct concepts in facts.parquet:")
    concept_counts = df_facts['concept'].value_counts()
    print(concept_counts)
    
    print("\nDate range of filed column:")
    print(f"Filed min: {df_facts['filed'].min()}, max: {df_facts['filed'].max()}")
    print("Forms in facts.parquet:")
    print(df_facts['form'].value_counts())

print("\n=== 2. REDDIT_QUANT.DB SEC_XBRL_FACTS ANALYSIS ===")
df_db_xbrl = pd.read_sql_query("SELECT * FROM sec_xbrl_facts", conn)
print(f"sec_xbrl_facts rows: {len(df_db_xbrl)}, unique tickers: {df_db_xbrl['ticker'].nunique()}")
if len(df_db_xbrl) > 0:
    print("Distinct fact_names:")
    print(df_db_xbrl['fact_name'].value_counts())
    print("Filing date range:", df_db_xbrl['filing_date'].min(), "to", df_db_xbrl['filing_date'].max())

print("\n=== 3. QUANTITATIVE_DCF_FLOOR ANALYSIS ===")
df_dcf = pd.read_sql_query("SELECT * FROM quantitative_dcf_floor", conn)
print(f"quantitative_dcf_floor rows: {len(df_dcf)}, unique tickers: {df_dcf['ticker'].nunique()}")
if len(df_dcf) > 0:
    print("Date range:", df_dcf['date'].min(), "to", df_dcf['date'].max())
    print("Sample tickers:", df_dcf['ticker'].unique()[:15])

print("\n=== 4. FOUR_LANE_RESULTS & HISTORICAL_5Y_SLICE ANALYSIS ===")
for tname in ["four_lane_results", "historical_5y_slice"]:
    df_t = pd.read_sql_query(f"SELECT * FROM {tname}", conn)
    print(f"{tname} rows: {len(df_t)}, unique tickers: {df_t['ticker'].nunique()}")

print("\n=== 5. MONTHLY RECONSTITUTION & PIT UNIVERSE ANALYSIS ===")
if os.path.exists("data/universe/monthly_reconstitution.parquet"):
    df_recon = pd.read_parquet("data/universe/monthly_reconstitution.parquet")
    print(f"monthly_reconstitution shape: {df_recon.shape}")
    print(f"Unique tickers: {df_recon['ticker'].nunique()}")
    print("Sectors breakdown (unique tickers per sector):")
    print(df_recon.groupby('sector')['ticker'].nunique())
    print("\nCap buckets breakdown (unique tickers per cap_bucket):")
    print(df_recon.groupby('cap_bucket')['ticker'].nunique())
    print("\nMonthly rebalance dates:", df_recon['rebalance_date'].nunique(), "from", df_recon['rebalance_date'].min(), "to", df_recon['rebalance_date'].max())

print("\n=== 6. DELISTING EVENTS ANALYSIS ===")
if os.path.exists("data/universe/delisting_events.parquet"):
    df_delist = pd.read_parquet("data/universe/delisting_events.parquet")
    print(f"delisting_events shape: {df_delist.shape}")
    print("Unique tickers:", df_delist['ticker'].nunique())
    print("Unique CIKs:", df_delist['cik'].nunique())
    print("Forms:", df_delist['form'].value_counts())

conn.close()
