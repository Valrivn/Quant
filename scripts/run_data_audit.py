import sqlite3
import os
import glob
import pandas as pd
import numpy as np
from pathlib import Path

print("=== 1. REDDIT_QUANT.DB AUDIT ===")
if os.path.exists("reddit_quant.db"):
    conn = sqlite3.connect("reddit_quant.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [t[0] for t in cursor.fetchall()]
    print(f"Total tables: {len(tables)}")
    for t in sorted(tables):
        cursor.execute(f'SELECT COUNT(*) FROM "{t}"')
        cnt = cursor.fetchone()[0]
        cursor.execute(f'PRAGMA table_info("{t}")')
        cols = [c[1] for c in cursor.fetchall()]
        ticker_col = next((c for c in cols if 'ticker' in c.lower() or 'symbol' in c.lower()), None)
        n_tickers = 0
        if ticker_col:
            cursor.execute(f'SELECT COUNT(DISTINCT "{ticker_col}") FROM "{t}"')
            n_tickers = cursor.fetchone()[0]
        print(f"  Table {t:30s}: {cnt:8d} rows | Tickers: {n_tickers:5d} ({ticker_col})")
    conn.close()

print("\n=== 2. SEC XBRL & FUNDAMENTAL TABLES AUDIT ===")
# Check sec_xbrl_facts or any sec related tables in reddit_quant.db
if os.path.exists("reddit_quant.db"):
    conn = sqlite3.connect("reddit_quant.db")
    cursor = conn.cursor()
    for tname in ["sec_xbrl_facts", "quantitative_dcf_floor", "submissions", "daily_aggregations"]:
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{tname}'")
        if cursor.fetchone():
            df = pd.read_sql_query(f'SELECT * FROM "{tname}" LIMIT 5', conn)
            print(f"\nSchema for {tname}:")
            print(df.dtypes)
            print(f"Sample data for {tname}:")
            print(df.head(2))
    conn.close()

print("\n=== 3. PARQUET DATASET INVENTORY ===")
parquet_files = glob.glob("data/**/*.parquet", recursive=True)
print(f"Total parquet files in data/: {len(parquet_files)}")

# Group master_prices separately
master_prices = [p for p in parquet_files if "master_prices" in p]
other_parquets = [p for p in parquet_files if "master_prices" not in p]

print(f"Master price files: {len(master_prices)}")
if master_prices:
    tickers_prices = [Path(p).stem for p in master_prices]
    print(f"Sample price tickers (total {len(tickers_prices)}): {tickers_prices[:10]}")
    # Inspect one price parquet file
    df_p = pd.read_parquet(master_prices[0])
    print(f"Sample price parquet ({master_prices[0]}): rows={len(df_p)}, cols={list(df_p.columns)}, date range: {df_p['date'].min() if 'date' in df_p else 'N/A'} to {df_p['date'].max() if 'date' in df_p else 'N/A'}")

for pf in sorted(other_parquets):
    try:
        df = pd.read_parquet(pf)
        size_mb = os.path.getsize(pf) / (1024 * 1024)
        cols = list(df.columns)
        date_cols = [c for c in cols if "date" in c.lower() or "time" in c.lower() or "year" in c.lower()]
        ticker_col = next((c for c in cols if "ticker" in c.lower() or "symbol" in c.lower()), None)
        n_tickers = df[ticker_col].nunique() if ticker_col and ticker_col in df else "N/A"
        date_range = "N/A"
        if date_cols and len(df) > 0:
            dcol = date_cols[0]
            date_range = f"{df[dcol].min()} to {df[dcol].max()}"
        print(f"  {pf:50s}: {size_mb:6.2f}MB | Rows: {len(df):8d} | Cols: {len(cols):2d} | Tickers: {str(n_tickers):5s} | Dates: {date_range}")
    except Exception as e:
        print(f"  {pf}: ERROR {e}")
