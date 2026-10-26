import pandas as pd
import os

print('=== MERGED PIT UNIVERSE - FALLBACK ANALYSIS ===')
try:
    df = pd.read_parquet('data/universe/merged_pit_universe.parquet', partitioning=None)
    print(f'Total rows: {len(df)}')
    print(f'Unique tickers: {df["ticker"].nunique()}')
    print(f'Unique months: {df["rebalance_date"].dt.to_period("M").nunique()}')
    
    # Fallback analysis
    for col in ['wacc_fallback_tier', 'icr_fallback_tier', 'sector_fallback_tier', 'roic_fallback_tier']:
        if col in df.columns:
            vc = df[col].value_counts()
            print(f'\n{col}:')
            for k, v in vc.items():
                print(f'  {k}: {v} ({v/len(df)*100:.1f}%)')
    
    # Any fallback
    df['any_fallback'] = (
        (df['wacc_fallback_tier'] != 'primary') | 
        (df['icr_fallback_tier'] != 'primary') | 
        (df['sector_fallback_tier'] != 'primary') | 
        (df['roic_fallback_tier'] != 'primary')
    )
    print(f'\nAny fallback: {df["any_fallback"].sum()}/{len(df)} ({df["any_fallback"].mean()*100:.1f}%)')
    
    # Tickers per month
    monthly = df.groupby(df['rebalance_date'].dt.to_period('M'))['ticker'].nunique()
    print(f'\nTickers per month:')
    print(monthly.describe())
    print(f'Monthly breakdown:')
    for period, count in monthly.items():
        print(f'  {period}: {count}')
        
except Exception as e:
    print(f'Error: {e}')

print()
print('=== FRED CACHE (regime macro data) ===')
fred_files = [f for f in os.listdir('data/fred_cache') if f.endswith('.parquet')]
for f in fred_files:
    try:
        df = pd.read_parquet(f'data/fred_cache/{f}')
        date_col = df.columns[0]
        print(f'{f}: {df.shape}, date_range={df[date_col].min()} to {df[date_col].max()}, freq={pd.infer_freq(df[date_col].dropna()) if len(df) > 1 else "N/A"}')
    except Exception as e:
        print(f'{f}: Error - {e}')

print()
print('=== MASTER PRICES - SAMPLE ===')
# Check a few price files properly
price_dirs = [d for d in os.listdir('data/master_prices') if not d.startswith('.') and os.path.isdir(os.path.join('data/master_prices', d))]
print(f'Total ticker directories: {len(price_dirs)}')
for ticker in price_dirs[:10]:
    try:
        ticker_path = os.path.join('data/master_prices', ticker)
        files = [f for f in os.listdir(ticker_path) if f.endswith('.parquet')]
        if files:
            df = pd.read_parquet(os.path.join(ticker_path, files[0]))
            print(f'  {ticker}: {df.shape}, cols={list(df.columns)}, date_range={df["date"].min()} to {df["date"].max()}')
    except Exception as e:
        print(f'  {ticker}: Error - {e}')

print()
print('=== REGIME VALIDATION - TRIAL LEDGER ===')
import json
with open('data/phase9_full/trial_ledger_v5.json') as f:
    data = json.load(f)
print(f'Total trials: {data["total_trials"]}')
print(f'Target N: {data["target_n"]}')
print(f'Track record days: {data["track_record_days"]}')
print(f'Schema: {data["schema"]}')
print(f'Genesis hash: {data["genesis_hash"][:16]}...')
print(f'Chain head: {data["chain_head"][:16]}...')
# Check first few trials
for i, trial in enumerate(data['trials'][:3]):
    print(f'  Trial {i}: id={trial.get("trial_id")}, metric_status={trial.get("metric_status")}, metric_value={trial.get("metric_value")}, composite_score={trial.get("composite_score")}, sharpe_flag={trial.get("sharpe_flag")}')