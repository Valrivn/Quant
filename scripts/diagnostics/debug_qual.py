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
            print('File:', f_path.name)
            print('  cols:', q_df.columns.tolist())
            src_lower = src_col.lower()
            has_date = 'date' in q_df.columns
            has_ticker = 'ticker' in q_df.columns
            has_src = src_lower in q_df.columns
            print('  has_date:', has_date, 'has_ticker:', has_ticker, 'has_src:', has_src)
            if has_date and has_ticker and has_src:
                sub = q_df[['date', 'ticker', src_lower]].rename(columns={src_lower: factor_col})
                sub['date'] = pd.to_datetime(sub['date']).dt.tz_localize(None)
                print('  sub shape:', sub.shape)
                qual_dfs.append(sub)
            else:
                print('  SKIPPED')
        except Exception as e:
            print('  ERROR:', e)

print('Total qual_dfs:', len(qual_dfs))