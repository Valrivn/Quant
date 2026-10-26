import pandas as pd
import os
import json

print('=== REGIME VALIDATION RESULTS (v7 - from Tomorrow.md) ===')
print('Per Tomorrow.md: Concat ARI 0.6650 on 4/4 folds (tuned params)')
print('Fold 1: ARI 0.6172, max state 56.16%')
print('Fold 2: ARI 0.6958, max state 51.85%')
print('Fold 3: ARI 0.5798, max state 62.74%')
print('Fold 4: ARI 0.7985, max state 88.80% (diagnosed VALID_BULL_MARKET)')
print('Target: >= 0.45 on >= 3/4 folds -> PASSED')

print()
print('=== REGIME DAILY NOWCAST ===')
df = pd.read_parquet('data/regime/regime_daily_nowcast.parquet')
print(f'Shape: {df.shape}')
print(f'Date range: {df["timestamp_utc"].min()} to {df["timestamp_utc"].max()}')
print(f'Regime state distribution:')
print(df['regime_state'].value_counts())
print(f'Regime switch flags: {df["regime_switch_flag"].sum()}')

print()
print('=== MASTER PRICES - PROPER CHECK ===')
price_dirs = [d for d in os.listdir('data/master_prices') if not d.startswith('.')]
print(f'Total entries in master_prices: {len(price_dirs)}')
dir_count = 0
for ticker in price_dirs[:20]:
    ticker_path = os.path.join('data/master_prices', ticker)
    if os.path.isdir(ticker_path):
        dir_count += 1
        files = [f for f in os.listdir(ticker_path) if f.endswith('.parquet')]
        if files:
            df = pd.read_parquet(os.path.join(ticker_path, files[0]))
            print(f'  {ticker}: {df.shape}, date_range={df["date"].min()} to {df["date"].max()}')
print(f'Total directories: {dir_count}')

print()
print('=== PRICES DIRECTORY (alternative) ===')
if os.path.exists('data/prices'):
    price_files = [f for f in os.listdir('data/prices') if f.endswith('.parquet')]
    print(f'Price files in data/prices: {len(price_files)}')
    for f in price_files[:5]:
        df = pd.read_parquet(f'data/prices/{f}')
        print(f'  {f}: {df.shape}, cols={list(df.columns)}, date_range={df["date"].min()} to {df["date"].max()}')

print()
print('=== FACTOR STORE ===')
if os.path.exists('data/factor_store.duckdb'):
    import duckdb
    conn = duckdb.connect('data/factor_store.duckdb', read_only=True)
    tables = conn.execute("SHOW TABLES").fetchall()
    print(f'Tables: {tables}')
    for table in tables:
        name = table[0]
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            cols = conn.execute(f"DESCRIBE {name}").fetchall()
            print(f'  {name}: {count} rows, cols={[c[0] for c in cols]}')
        except Exception as e:
            print(f'  {name}: Error - {e}')
    conn.close()

print()
print('=== BACKTEST PERFORMANCE SUMMARY (from artifacts) ===')
# Check the actual backtest results
for arm in ['a', 'b', 'c']:
    try:
        path = f'data/backtests/arm_{arm}_primary.parquet'
        if os.path.exists(path):
            df = pd.read_parquet(path, partitioning=None)
            print(f'Arm {arm.upper()}: {df.shape}, cols={list(df.columns)}')
            if 'return' in df.columns:
                print(f'  Return stats: mean={df["return"].mean():.4f}, std={df["return"].std():.4f}')
            if 'weight' in df.columns:
                print(f'  Weight stats: mean={df["weight"].mean():.4f}, sum={df["weight"].sum():.4f}')
    except Exception as e:
        print(f'Arm {arm.upper()}: Error - {e}')

# Check portfolio returns
for arm in ['a', 'b', 'c']:
    try:
        path = f'data/backtests/arm_{arm}_primary_portfolio_returns.parquet'
        if os.path.exists(path):
            df = pd.read_parquet(path)
            print(f'Arm {arm.upper()} Portfolio: {df.shape}, cols={list(df.columns)}')
            if 'portfolio_return' in df.columns:
                ann_ret = (1 + df['portfolio_return']).prod() ** (252/len(df)) - 1
                ann_vol = df['portfolio_return'].std() * (252**0.5)
                sharpe = ann_ret / ann_vol if ann_vol > 0 else 0
                max_dd = (df['portfolio_return'].cumsum().cummax() - df['portfolio_return'].cumsum()).max()
                print(f'  Ann Return: {ann_ret:.2%}, Ann Vol: {ann_vol:.2%}, Sharpe: {sharpe:.2f}, Max DD: {max_dd:.2%}')
    except Exception as e:
        print(f'Arm {arm.upper()} Portfolio: Error - {e}')