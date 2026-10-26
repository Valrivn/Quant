import pandas as pd
import os
import json

print('=== REGIME VALIDATION RESULTS (v7) ===')
with open('data/regime/hmm_v3/oos_validation.json') as f:
    data = json.load(f)
print(f'Status: {data["status"]}')
print(f'Methodology: {data["methodology"]}')
print(f'Concatenated ARI: {data["concatenated_ari"]:.4f}')
print(f'Passing folds: {data["passing_folds"]}/{data["folds_required"]}')
print(f'ARI threshold: {data["ari_threshold"]}')
print(f'Overall pass: {data["overall_pass"]}')
print(f'Fold ARI scores: {data["fold_ari_scores"]}')
print(f'Fold details:')
for fd in data['fold_details']:
    print(f'  Fold {fd["fold"]}: train={fd["train_window"]}, test={fd["test_window"]}, ARI={fd["ari"]:.4f}, occupancy={fd.get("occupancy", "N/A")}')

print()
print('=== TWO-LAYER V1 VALIDATION ===')
with open('data/regime/two_layer_v1/oos_validation.json') as f:
    data = json.load(f)
print(f'Status: {data["status"]}')
print(f'Concatenated ARI: {data["concatenated_ari"]:.4f}')
print(f'Passing folds: {data["passing_folds"]}/{data["folds_required"]}')
print(f'Fold ARI scores: {data["fold_ari_scores"]}')

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
# They're directories, let me check
for ticker in price_dirs[:10]:
    ticker_path = os.path.join('data/master_prices', ticker)
    if os.path.isdir(ticker_path):
        files = [f for f in os.listdir(ticker_path) if f.endswith('.parquet')]
        if files:
            df = pd.read_parquet(os.path.join(ticker_path, files[0]))
            print(f'  {ticker}: {df.shape}, date_range={df["date"].min()} to {df["date"].max()}')
    else:
        print(f'  {ticker}: is file, not dir')

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