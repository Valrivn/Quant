import pandas as pd
import os

print('=== MERGED PIT UNIVERSE - FALLBACK ANALYSIS (fixed dates) ===')
try:
    df = pd.read_parquet('data/universe/merged_pit_universe.parquet', partitioning=None)
    df['rebalance_date'] = pd.to_datetime(df['rebalance_date'])
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
    print(f'Monthly breakdown (first 12):')
    for period, count in monthly.head(12).items():
        print(f'  {period}: {count}')
    print(f'Monthly breakdown (last 12):')
    for period, count in monthly.tail(12).items():
        print(f'  {period}: {count}')
        
    # Coverage of key fundamentals
    print(f'\nFundamental coverage (non-null):')
    for col in ['roic', 'fcf_margin', 'wacc_estimate', 'cost_of_equity_est', 'roic_wacc_spread', 'icr', 'wacc', 'ebit', 'interest_expense']:
        if col in df.columns:
            non_null = df[col].notna().sum()
            print(f'  {col}: {non_null}/{len(df)} ({non_null/len(df)*100:.1f}%)')
        
except Exception as e:
    import traceback
    print(f'Error: {e}')
    traceback.print_exc()

print()
print('=== MASTER PRICES - SAMPLE (fixed) ===')
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
print('=== BACKTEST TICKER USAGE SUMMARY ===')
# Check arm_a, arm_b, arm_c ticker usage
for arm in ['a', 'b', 'c']:
    try:
        path = f'data/backtests/ticker_usage_{arm}_primary.parquet'
        df = pd.read_parquet(path)
        df['rebalance_date'] = pd.to_datetime(df['rebalance_date'])
        monthly = df.groupby(df['rebalance_date'].dt.to_period('M'))['ticker'].nunique()
        print(f'Arm {arm.upper()}: {len(df)} total rows, {df["ticker"].nunique()} unique tickers, {monthly.nunique()} months')
        print(f'  Tickers/month: {monthly.describe().to_dict()}')
        # Fallback analysis
        if 'any_fallback' in df.columns:
            print(f'  Any fallback: {df["any_fallback"].sum()}/{len(df)} ({df["any_fallback"].mean()*100:.1f}%)')
    except Exception as e:
        print(f'Arm {arm.upper()}: Error - {e}')

print()
print('=== REGIME VALIDATION - FOLD GEOMETRY ===')
# Check the fold geometry used for validation
if os.path.exists('config/calibrated_unified_regime_params.json'):
    import json
    with open('config/calibrated_unified_regime_params.json') as f:
        params = json.load(f)
    print(f'Calibrated params keys: {list(params.keys())}')
    if 'folds' in params:
        print(f'Folds: {params["folds"]}')
    if 'train_start' in params:
        print(f'Train start: {params["train_start"]}')
    if 'train_end' in params:
        print(f'Train end: {params["train_end"]}')

print()
print('=== UNIFIED REGIME VALIDATION RESULTS ===')
# Check if there are validation results
if os.path.exists('data/regime'):
    for root, dirs, files in os.walk('data/regime'):
        for f in files:
            if f.endswith('.json') or f.endswith('.parquet'):
                path = os.path.join(root, f)
                try:
                    if f.endswith('.json'):
                        with open(path) as fp:
                            data = json.load(fp)
                        print(f'{path}: keys={list(data.keys()) if isinstance(data, dict) else "list"}')
                    else:
                        df = pd.read_parquet(path)
                        print(f'{path}: {df.shape}, cols={list(df.columns)}')
                except Exception as e:
                    print(f'{path}: Error - {e}')