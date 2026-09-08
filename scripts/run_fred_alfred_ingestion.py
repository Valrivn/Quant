"""Script to run FRED/ALFRED ingestion pipeline for all 8 mandatory series with full ALFRED vintage data."""

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Add project root to sys.path
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from ingestion.data_lake import DataLakeStore
from ingestion.fred_alfred import FredAlfredClient, FredIngestionPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("fred_alfred_runner")

MANDATORY_SERIES = [
    {"id": "GOLDPMGBD228NLBM", "type": "financial", "domain": "fred"},
    {"id": "DFII10", "type": "financial", "domain": "fred"},
    {"id": "BAA10Y", "type": "financial", "domain": "fred"},
    {"id": "DGS10", "type": "financial", "domain": "fred"},
    {"id": "M2SL", "type": "macro", "domain": "alfred"},
    {"id": "CPIAUCSL", "type": "macro", "domain": "alfred"},
    {"id": "UNRATE", "type": "macro", "domain": "alfred"},
    {"id": "GDP", "type": "macro", "domain": "alfred"},
]

def main():
    print("=" * 80)
    print("FRED / ALFRED Mandatory Series Ingestion Execution")
    print("=" * 80)

    start_total = time.time()
    data_lake = DataLakeStore()
    client = FredAlfredClient()

    results = []

    for series_info in MANDATORY_SERIES:
        s_id = series_info["id"]
        s_type = series_info["type"]
        domain = series_info["domain"]

        print(f"\n---> Processing series: {s_id} (Type: {s_type}, Domain: {domain})")
        t_start = time.time()

        status = "SUCCESS"
        error_msg = None
        row_count = 0
        vintage_count = 0
        meta = {}

        try:
            # 1. Fetch metadata for all series
            meta = client.get_series_metadata(s_id)
            print(f"     Title: {meta.get('title', 'N/A')}")
            print(f"     Units: {meta.get('units', 'N/A')}")

            vintages = []
            if s_type == "macro":
                # 2. Fetch vintage dates for macro series
                vintages = client.get_vintage_dates(s_id)
                vintage_count = len(vintages)
                print(f"     Fetched {vintage_count} ALFRED vintage dates.")

                # 3. Fetch observations with all vintage dates batched x10 per request
                df, fetch_meta = client.get_series_observations(s_id, vintage_dates=vintages)
            else:
                # Standard observations for financial series
                df, fetch_meta = client.get_series_observations(s_id)

            row_count = len(df)
            meta.update(fetch_meta)
            meta["vintage_dates_count"] = vintage_count
            meta["ingested_at"] = datetime.now(timezone.utc).isoformat()
            t_elapsed = round(time.time() - t_start, 2)
            meta["execution_time_sec"] = t_elapsed

            # 4. Store in Data Lake
            parquet_path = data_lake.save_dataframe(
                domain=domain,
                series_id=s_id,
                df=df,
                metadata=meta,
            )
            json_path = data_lake.save_json(
                domain=domain,
                series_id=s_id,
                data=meta,
                filename=f"{s_id}_meta.json",
            )

            print(f"     Ingested {row_count} rows in {t_elapsed}s -> {parquet_path}")

        except Exception as exc:
            status = "FAILED"
            error_msg = str(exc)
            t_elapsed = round(time.time() - t_start, 2)
            logger.error(f"Failed ingestion for {s_id}: {exc}", exc_info=True)
            print(f"     [ERROR] Ingestion failed for {s_id}: {exc}")

        results.append({
            "series_id": s_id,
            "type": s_type,
            "domain": domain,
            "status": status,
            "vintage_count": vintage_count,
            "row_count": row_count,
            "execution_time_sec": t_elapsed,
            "error": error_msg,
        })

    total_time = round(time.time() - start_total, 2)
    print("\n" + "=" * 80)
    print("INGESTION SUMMARY REPORT")
    print("=" * 80)
    print(f"{'Series ID':<18} | {'Type':<10} | {'Domain':<8} | {'Vintages':<9} | {'Rows Ingested':<14} | {'Time (s)':<9} | {'Status'}")
    print("-" * 85)
    for r in results:
        print(f"{r['series_id']:<18} | {r['type']:<10} | {r['domain']:<8} | {r['vintage_count']:<9} | {r['row_count']:<14} | {r['execution_time_sec']:<9} | {r['status']}")
    print("-" * 85)
    print(f"Total Elapsed Time: {total_time} seconds")
    print("=" * 80)

if __name__ == "__main__":
    main()
