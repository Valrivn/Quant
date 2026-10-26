import pandas as pd
import os

# Check merged_pit_universe - try reading with partitioning=None
print('=== MERGED PIT UNIVERSE (with partitioning=None) ===')
try:
    df = pd.read_parquet('data/universe/merged_pit_universe.parquet', partitioning=None)
    print(f'Shape: {df.shape}')
    print(f'Columns: {list(df.columns)}')
    print(f'Date range: {df["rebalance_date"].min()} to {df["rebalance_date"].max()}')
    print(f'Unique tickers: {df["ticker"].nunique()}')
    print(f'Unique months: {df["rebalance_date"].dt.to_period("M").nunique()}')
    print(f'Tickers per month: {df.groupby(df["rebalance_date"].dt.to_period("M"))["ticker"].nunique().describe()}')
    print(f'Sample rows:')
    print(df.head(3).to_string())
    
    # Check fallback tiers
    print(f'\nFallback tiers:')
    for col in ['wacc_fallback_tier', 'icr_fallback_tier', 'sector_fallback_tier', 'roic_fallback_tier']:
        if col in df.columns:
            print(f'  {col}: {df[col].value_counts().to_dict()}')
except Exception as e:
    print(f'Error: {e}')

print()
print('=== FRED MACRO DATA (for regime) ===')
fred_files = [f for f in os.listdir('data/fred_cache') if f.endswith('.parquet')]
for f in fred_files:
    try:
        df = pd.read_parquet(f'data/fred_cache/{f}')
        print(f'{f}: {df.shape}, cols={list(df.columns)}, date_range={df.iloc[:,0].min()} to {df.iloc[:,0].max()}')
    except Exception as e:
        print(f'{f}: Error - {e}')

print()
print('=== BACKTEST ARTIFACTS ===')
if os.path.exists('data/backtests'):
    for root, dirs, files in os.walk('data/backtests'):
        for f in files:
            if f.endswith('.parquet'):
                path = os.path.join(root, f)
                try:
                    df = pd.read_parquet(path)
                    print(f'{path}: {df.shape}, cols={list(df.columns)}')
                except Exception as e:
                    print(f'{path}: Error - {e}')

print()
print('=== REGIME VALIDATION DATA ===')
if os.path.exists('data/phase9_full'):
    for root, dirs, files in os.walk('data/phase9_full'):
        for f in files:
            if f.endswith('.parquet') or f.endswith('.json'):
                path = os.path.join(root, f)
                try:
                    if f.endswith('.parquet'):
                        df = pd.read_parquet(path)
                        print(f'{path}: {df.shape}, cols={list(df.columns)}')
                    else:
                        import json
                        with open(path) as fp:
                            data = json.load(fp)
                        print(f'{path}: keys={list(data.keys()) if isinstance(data, dict) else "list"}')
                except Exception as e:
                    print(f'{path}: Error - {e}')

print()
print('=== MASTER PRICES SAMPLE ===')
# Check a few price files
price_dirs = [d for d in os.listdir('data/master_prices') if not d.startswith('.')]
print(f'Total ticker directories: {len(price_dirs)}')
for ticker in price_dirs[:5]:
    try:
        files = [f for f in os.listdir(f'data/master_prices/{ticker}') if f.endswith('.parquet')]
        for f in files:
            df = pd.read_parquet(f'data/master_prices/{ticker}/{f}')
            print(f'  {ticker}/{f}: {df.shape}, cols={list(df.columns)}, date_range={df["date"].min()} to {df["date"].max()}')
            break
    except Exception as e:
        print(f'  {ticker}: Error - {e}')