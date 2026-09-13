"""
Qualitative Coverage Audit (RS-07)

Audits all qualitative datasets in data/qual/ to build the matrix:
(ticker, month, source) -> has_data

Analyzes mapping confidence distributions (high/medium/low) and date coverage.
CLI:
    python -m validation.qual_coverage_audit --run --start 2018-01-01 --end 2024-12-31
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Any
import pandas as pd


QUAL_FILES = {
    "GH_ARCHIVE": "data/qual/gh_velocity.parquet",
    "NHTSA": "data/qual/nhtsa_signals.parquet",
    "CFPB": "data/qual/cfpb_velocity.parquet",
    "ATS_MAPPER": "data/qual/ats_mapping.parquet",
    "ATS_RSS": "data/qual/ats_velocity.parquet",
    "USPTO": "data/qual/patents_signals.parquet",
    "APP_STORE": "data/qual/app_store_velocity.parquet",
    "AMAZON": "data/qual/amazon_reviews.parquet",
    "SEC_13F": "data/qual/inst_ownership.parquet",
    "APEWISDOM": "data/qual/apewisdom_mentions.parquet",
}


def run_qual_coverage_audit(start_date: str, end_date: str) -> pd.DataFrame:
    print("=" * 80)
    print(f"RUNNING QUALITATIVE COVERAGE AUDIT (RS-07) [{start_date} to {end_date}]")
    print("=" * 80)

    audit_records = []
    confidence_stats: Dict[str, Dict[str, int]] = {}

    for source_name, rel_path in QUAL_FILES.items():
        file_path = Path(rel_path)
        confidence_stats[source_name] = {"high": 0, "medium": 0, "low": 0, "total": 0}

        if not file_path.exists():
            print(f"  [MISSING] {source_name}: {rel_path} not found.")
            continue

        try:
            df = pd.read_parquet(file_path)
            row_count = len(df)
            confidence_stats[source_name]["total"] = row_count

            # Count mapping confidence distribution
            if "mapping_confidence" in df.columns:
                conf_counts = df["mapping_confidence"].value_counts().to_dict()
                for c_level in ["high", "medium", "low"]:
                    confidence_stats[source_name][c_level] = conf_counts.get(c_level, 0)

            # Extract date column
            date_col = None
            for c in ["date", "source_filing_date", "review_date", "filed_date", "grant_date"]:
                if c in df.columns:
                    date_col = c
                    break

            if "ticker" in df.columns and date_col:
                df["month"] = pd.to_datetime(df[date_col]).dt.strftime("%Y-%m")
                sub_df = df[(df[date_col] >= start_date) & (df[date_col] <= end_date)]
                
                for (ticker, month), group in sub_df.groupby(["ticker", "month"]):
                    conf = group["mapping_confidence"].iloc[0] if "mapping_confidence" in group.columns else "high"
                    audit_records.append({
                        "ticker": ticker,
                        "month": month,
                        "source": source_name,
                        "has_data": 1,
                        "record_count": len(group),
                        "mapping_confidence": conf
                    })

            print(f"  [OK] {source_name}: {row_count} rows loaded. Confidence -> High: {confidence_stats[source_name]['high']}, Med: {confidence_stats[source_name]['medium']}, Low: {confidence_stats[source_name]['low']}")
        except Exception as exc:
            print(f"  [ERROR] {source_name}: Failed to read {rel_path} - {exc}")

    audit_df = pd.DataFrame(audit_records)
    out_dir = Path("data/provenance")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "qual_coverage_matrix.parquet"
    audit_df.to_parquet(out_path, index=False)

    print("\n" + "-" * 80)
    print("MAPPING CONFIDENCE DISTRIBUTION SUMMARY (RS-03):")
    print("-" * 80)
    for src, counts in confidence_stats.items():
        print(f"  {src:<12} | Total: {counts['total']:<6} | High: {counts['high']:<6} | Med: {counts['medium']:<6} | Low: {counts['low']:<6}")

    print("\n" + "-" * 80)
    print(f"Qualitative coverage audit complete. Saved matrix with {len(audit_df)} cells to {out_path}.")
    print("=" * 80)
    return audit_df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Qualitative Coverage Audit (RS-07)")
    parser.add_argument("--run", action="store_true", help="Run qualitative coverage audit")
    parser.add_argument("--start", default="2018-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2024-12-31", help="End date YYYY-MM-DD")
    args = parser.parse_args()

    run_qual_coverage_audit(args.start, args.end)
