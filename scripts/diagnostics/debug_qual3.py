import polars as pl
import pandas as pd
import numpy as np
from pathlib import Path
from ingestion.harness.three_arm_runner import QUAL_FACTOR_COLS

qual_dir = Path("data/qual")
qual_files = {
    "gh_velocity_90d": (qual_dir / "gh_velocity.parquet", "gh_commits_90d"),
    "ats_velocity_90d": (qual_dir / "ats_velocity.parquet", "ats_posted_90d"),
    "app_velocity_90d": (qual_dir / "app_store_velocity.parquet", "reviews_90d_z"),
    "patent_accel_1y": (qual_dir / "patents_signals.parquet", "patent_citations_1y"),
    "cfpb_velocity_90d": (qual_dir / "cfpb_velocity.parquet", "cfpb_complaints_90d"),
    "nhtsa_velocity_90d": (qual_dir / "nhtsa_signals.parquet", "nhtsa_complaints_90d"),
}

qual_dfs = []
for factor_col, (f_path, src_col) in qual_files.items():
    if f_path.exists():
        try:
            q_df = pl.read_parquet(f_path).to_pandas()
            q_df.columns = [c.lower() for c in q_df.columns]
            print(f"Loading {factor_col} from {f_path.name}")
            print(f"  columns: {q_df.columns.tolist()}")
            print(f"  date in cols: {'date' in q_df.columns}")
            print(f"  ticker in cols: {'ticker' in q_df.columns}")
            print(f"  {src_col.lower()} in cols: {src_col.lower() in q_df.columns}")
            if "date" in q_df.columns and "ticker" in q_df.columns and src_col.lower() in q_df.columns:
                sub = q_df[["date", "ticker", src_col.lower()]].rename(columns={src_col.lower(): factor_col})
                sub["date"] = pd.to_datetime(sub["date"]).dt.tz_localize(None)
                print(f"  sub shape: {sub.shape}")
                qual_dfs.append(sub)
            else:
                print(f"  SKIPPED - missing columns")
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

print(f"qual_dfs count: {len(qual_dfs)}")
if qual_dfs:
    qual_df = qual_dfs[0]
    print(f"First df: {qual_df.shape}")
    for d in qual_dfs[1:]:
        print(f"Merging with: {d.shape}")
        qual_df = pd.merge(qual_df, d, on=["date", "ticker"], how="outer")
        print(f"  Result: {qual_df.shape}")
    for col in QUAL_FACTOR_COLS:
        if col not in qual_df.columns:
            qual_df[col] = np.nan
    print(f"Final qual_df: {qual_df.shape}")
    print(f"Columns: {qual_df.columns.tolist()}")
else:
    print("qual_dfs is empty!")