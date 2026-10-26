import pandas as pd
import os

# Check merged_pit_universe
print('=== MERGED PIT UNIVERSE ===')
try:
    df = pd.read_parquet('data/universe/merged_pit_universe.parquet')
    print(f'Shape: {df.shape}')
    print(f'Columns: {list(df.columns)}')
    print(f'Date range: {df["date"].min()} to {df["date"].max()}')
    print(f'Unique tickers: {df["ticker"].nunique()}')
    print(f'Unique months: {df["date"].dt.to_period("M").nunique()}')
    print(f'Tickers per month: {df.groupby(df["date"].dt.to_period("M"))["ticker"].nunique().describe()}')
    print(f'Sample rows:')
    print(df.head(3).to_string())
except Exception as e:
    print(f'Error: {e}')

print()
print('=== MONTHLY RECONSTITUTION ===')
try:
    df = pd.read_parquet('data/universe/monthly_reconstitution.parquet')
    print(f'Shape: {df.shape}')
    print(f'Columns: {list(df.columns)}')
    print(f'Date range: {df["date"].min()} to {df["date"].max()}')
    print(f'Unique tickers: {df["ticker"].nunique()}')
    print(f'Tickers per month: {df.groupby(df["date"].dt.to_period("M"))["ticker"].nunique().describe()}')
except Exception as e:
    print(f'Error: {e}')

print()
print('=== VALUATION FUNDAMENTALS ===')
try:
    df = pd.read_parquet('data/pit_fundamentals/valuation_fundamentals.parquet')
    print(f'Shape: {df.shape}')
    print(f'Columns: {list(df.columns)}')
    print(f'Date range: {df["filed_date"].min()} to {df["filed_date"].max()}')
    print(f'Unique tickers: {df["ticker"].nunique()}')
    print(f'Unique fiscal periods: {df["fiscal_period"].nunique() if "fiscal_period" in df.columns else "N/A"}')
    # Check key fields
    for col in ['roic', 'fcf_margin', 'wacc_estimate', 'cost_of_equity_est', 'roic_wacc_spread', 'icr', 'sector_custom', 'market_cap']:
        if col in df.columns:
            non_null = df[col].notna().sum()
            print(f'  {col}: {non_null}/{len(df)} non-null ({non_null/len(df)*100:.1f}%)')
except Exception as e:
    print(f'Error: {e}')

print()
print('=== PIT UNIVERSE 2018-2024 ===')
try:
    df = pd.read_parquet('data/universe/pit_universe_2018_2024.parquet')
    print(f'Shape: {df.shape}')
    print(f'Columns: {list(df.columns)}')
    print(f'Date range: {df["date"].min()} to {df["date"].max()}')
    print(f'Unique tickers: {df["ticker"].nunique()}')
    print(f'Tickers per month: {df.groupby(df["date"].dt.to_period("M"))["ticker"].nunique().describe()}')
except Exception as e:
    print(f'Error: {e}')

print()
print('=== RAW XBRL FACTS ===')
try:
    df = pd.read_parquet('data/pit_fundamentals/raw_xbrl_facts.parquet')
    print(f'Shape: {df.shape}')
    print(f'Columns: {list(df.columns)}')
    print(f'Unique tickers: {df["ticker"].nunique() if "ticker" in df.columns else "N/A"}')
    print(f'Unique concepts: {df["concept"].nunique() if "concept" in df.columns else "N/A"}')
except Exception as e:
    print(f'Error: {e}')