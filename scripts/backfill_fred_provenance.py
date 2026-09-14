"""Backfill script for FRED and ALFRED provenance metadata.

Populates missing `source`, `last_updated`, `frequency`, and `series_type` metadata
fields for all series in the data lake SQLite catalog (domain='fred' or domain='alfred').
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ingestion.data_lake import DEFAULT_DATA_LAKE_DIR, DataLakeStore
from ingestion.fred_alfred import EXPANDED_SERIES_CATALOG, KEY_ECONOMIC_SERIES

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("backfill_fred_provenance")


def lookup_catalog_info(series_id: str) -> Optional[Dict[str, Any]]:
    """Look up series entry in EXPANDED_SERIES_CATALOG."""
    for cat_name, series_list in EXPANDED_SERIES_CATALOG.items():
        for s in series_list:
            if s["series_id"] == series_id:
                info = dict(s)
                info["category"] = cat_name
                return info
    return None


def backfill_fred_provenance(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Backfill missing provenance metadata in dataset_catalog.

    Idempotently updates metadata_json for 'fred' and 'alfred' series in catalog.db.
    """
    store = DataLakeStore()
    target_db = db_path or store.db_path

    logger.info(f"Connecting to data lake catalog database at {target_db}")

    updated_count = 0
    total_count = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(target_db) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT domain, series_id, file_path, format, row_count, last_updated, metadata_json
            FROM dataset_catalog
            WHERE domain IN ('fred', 'alfred')
            """
        )
        rows = cursor.fetchall()
        total_count = len(rows)
        logger.info(f"Found {total_count} series in FRED/ALFRED catalog")

        for row in rows:
            domain = row["domain"]
            series_id = row["series_id"]
            row_last_updated = row["last_updated"]
            raw_meta = row["metadata_json"]

            meta: Dict[str, Any] = {}
            if raw_meta:
                try:
                    meta = json.loads(raw_meta)
                except Exception as exc:
                    logger.warning(f"Could not parse metadata_json for {domain}/{series_id}: {exc}")

            cat_info = lookup_catalog_info(series_id)

            # Also check sidecar JSON if meta is empty
            meta_json_path = store.base_dir / domain / f"{series_id}_meta.json"
            if meta_json_path.exists():
                try:
                    with open(meta_json_path, "r", encoding="utf-8") as f:
                        sidecar_data = json.load(f)
                    sidecar_meta = sidecar_data.get("data", {})
                    if isinstance(sidecar_meta, dict):
                        for k, v in sidecar_meta.items():
                            if k not in meta or not meta[k]:
                                meta[k] = v
                except Exception as exc:
                    logger.warning(f"Error reading sidecar JSON {meta_json_path}: {exc}")

            # Source provenance
            if "source" not in meta or not meta["source"]:
                meta["source"] = "FRED_PUBLIC_CSV_FALLBACK"

            # Last updated timestamp
            if "last_updated" not in meta or not meta["last_updated"]:
                meta["last_updated"] = row_last_updated or now_iso

            # Frequency
            if cat_info and "frequency" in cat_info:
                meta["frequency"] = cat_info["frequency"]
            elif "frequency" not in meta:
                meta["frequency"] = "daily" if cat_info and cat_info.get("type") == "financial_daily" else "monthly"

            # Series type
            if cat_info and "type" in cat_info:
                meta["series_type"] = cat_info["type"]
            if cat_info and "category" in cat_info and "category" not in meta:
                meta["category"] = cat_info["category"]
            if "title" not in meta or not meta["title"]:
                if cat_info and "title" in cat_info:
                    meta["title"] = cat_info["title"]
                elif series_id in KEY_ECONOMIC_SERIES:
                    meta["title"] = KEY_ECONOMIC_SERIES[series_id]

            # Write updated metadata_json back to dataset_catalog
            updated_json = json.dumps(meta)
            cursor.execute(
                """
                UPDATE dataset_catalog
                SET metadata_json = ?
                WHERE domain = ? AND series_id = ?
                """,
                (updated_json, domain, series_id),
            )

            # Also sync back to sidecar JSON file if present
            if meta_json_path.exists():
                try:
                    payload = {
                        "series_id": series_id,
                        "domain": domain,
                        "saved_at": now_iso,
                        "metadata": meta,
                        "data": meta,
                    }
                    with open(meta_json_path, "w", encoding="utf-8") as f:
                        json.dump(payload, f, indent=2)
                except Exception as exc:
                    logger.warning(f"Failed to update sidecar JSON for {series_id}: {exc}")

            updated_count += 1

        conn.commit()

    logger.info(f"Successfully backfilled provenance metadata for {updated_count}/{total_count} FRED/ALFRED series")
    return {
        "status": "SUCCESS",
        "total_series": total_count,
        "updated_series": updated_count,
    }


if __name__ == "__main__":
    backfill_fred_provenance()
