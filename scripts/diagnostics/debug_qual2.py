import polars as pl
import pandas as pd
from pathlib import Path

qual_dir = Path('data/qual')
qual_files = {
    'gh_velocity_90d': (qual_dir / 'gh_velocity.parquet', 'gh_commits_90d'),
    'ats_velocity_90d': (qual_dir / 'ats_velocity.parquet', 'ats_posted_90d'),
    'app_velocity_90d': (qual_dir / 'app_store_velocity.parquet', 'reviews_90d_z'),
    'patent_accel_1y': (qual_dir / 'patents_signals.parquet', 'patent_citations_1y'),
    'cfpb_velocity_90d': (qual_dir / 'cfpb_velocity.parquet', 'cfpb_complaints_90d'),
    'nhtsa_velocity_90d': (qual_dir / 'nhtsa_signals.parquet', 'nhtsa_complaints_90d'),
}

qual_dfs = []
for factor_col, (f_path, src_col) in qual_files.items():
    if f_path.exists():
        try:
            q_df = pl.read_parquet(f_path).to_pandas()
            q_df.columns = [c.lower() for c in q_df.columns]
            src_lower = src_col.lower()
            if 'date' in q_df.columns and 'ticker' in q_df.columns and src_lower in q_df.columns:
                sub = q_df[['date', 'ticker', src_lower]].rename(columns={src_lower: factor_col})
                sub['date'] = pd.to_datetime(sub['date']).dt.tz_localize(None)
                qual_dfs.append(sub)
        except Exception as e:
            print('ERROR:', e)

if qual_dfs:
    qual_df = qual_dfs[0]
    print('First df:', qual_df.shape)
    for d in qual_dfs[1:]:
        print('Merging with:', d.shape)
        qual_df = pd.merge(qual_df, d, on=['date', 'ticker'], how='outer')
        print('  Result:', qual_df.shape)
    print('Final qual_df:', qual_df.shape)
    print('Columns:', qual_df.columns.tolist())
    print('Date range:', qual_df['date'].min(), 'to', qual_df['date'].max())