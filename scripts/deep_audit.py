import sqlite3
import pandas as pd
import numpy as np
import glob
import os

print("--- DEEP AUDIT OF REDDIT_QUANT.DB ---")
conn = sqlite3.connect("reddit_quant.db")
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [t[0] for t in cursor.fetchall()]

for t in sorted(tables):
    cursor.execute(f'SELECT COUNT(*) FROM "{t}"')
    cnt = cursor.fetchone()[0]
    cursor.execute(f'PRAGMA table_info("{t}")')
    cols = [c[1] for c in cursor.fetchall()]
    print(f"Table: {t:30s} | Rows: {cnt:7d} | Cols: {len(cols)}")
    print(f"   Cols: {cols[:8]}")

conn.close()

print("\n--- DEEP AUDIT OF SEC FACTS PARQUET ---")
for f in ["data/sec_facts/concepts/concepts.parquet", "data/sec_facts/facts/facts.parquet", "data/sec_facts/filings/filings.parquet"]:
    if os.path.exists(f):
        df = pd.read_parquet(f)
        print(f"\nFile: {f} | Shape: {df.shape}")
        print("Columns:", list(df.columns))
        print("Head:")
        print(df.head(3))

print("\n--- DEEP AUDIT OF FACTOR PANEL PARQUET ---")
if os.path.exists("data/research/panels/factor_panel.parquet"):
    df_fp = pd.read_parquet("data/research/panels/factor_panel.parquet")
    print(f"factor_panel.parquet shape: {df_fp.shape}")
    print("Columns:", list(df_fp.columns))
    print("Head:")
    print(df_fp.head(5))
    if 'ticker' in df_fp.columns:
        print("Unique tickers in factor_panel:", df_fp['ticker'].nunique())
    if 'factor' in df_fp.columns or 'factor_name' in df_fp.columns:
        fcol = 'factor' if 'factor' in df_fp.columns else 'factor_name'
        print("Unique factors:", df_fp[fcol].unique())

print("\n--- DEEP AUDIT OF UNIVERSE & RECONSTITUTION ---")
for f in ["data/universe/delisting_events.parquet", "data/universe/monthly_reconstitution.parquet", "data/universe/pit_universe_2018_2024.parquet"]:
    if os.path.exists(f):
        df = pd.read_parquet(f)
        print(f"\nFile: {f} | Shape: {df.shape}")
        print("Columns:", list(df.columns))
        print("Head:")
        print(df.head(3))

