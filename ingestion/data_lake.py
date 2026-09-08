"""Data Lake Storage Abstraction for Quant Ingestion Pipelines.

Provides unified storage and cataloging of structured and semi-structured dataset series,
supporting Parquet tables, JSON metadata, partition organization, and audit indexing.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_DATA_LAKE_DIR = Path(__file__).resolve().parents[1] / "data" / "data_lake"


class DataLakeStore:
    """Unified Data Lake store for managing FRED/ALFRED, Google Trends, and alt data."""

    def __init__(self, base_dir: Optional[Union[str, Path]] = None):
        self.base_dir = Path(base_dir) if base_dir else DEFAULT_DATA_LAKE_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.base_dir / "catalog.db"
        self._lock = threading.Lock()
        self._init_catalog()

    def _init_catalog(self) -> None:
        """Initialize the catalog SQLite index for metadata and lineage tracking."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS dataset_catalog (
                        domain TEXT NOT NULL,
                        series_id TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        format TEXT NOT NULL,
                        row_count INTEGER DEFAULT 0,
                        last_updated TEXT NOT NULL,
                        metadata_json TEXT,
                        PRIMARY KEY (domain, series_id)
                    )
                """)
                conn.commit()

    def _get_domain_dir(self, domain: str) -> Path:
        domain_dir = self.base_dir / domain
        domain_dir.mkdir(parents=True, exist_ok=True)
        return domain_dir

    def save_dataframe(
        self,
        domain: str,
        series_id: str,
        df: pd.DataFrame,
        metadata: Optional[Dict[str, Any]] = None,
        filename: Optional[str] = None,
    ) -> Path:
        """Save a pandas DataFrame as a Parquet file in the data lake and index it.

        Args:
            domain: Storage namespace (e.g., 'fred', 'alfred', 'google_trends')
            series_id: Identifier for the series (e.g., 'GDP', 'AAPL_stock')
            df: Pandas DataFrame containing observations
            metadata: Optional dict of attributes / metadata
            filename: Custom filename override (defaults to '{series_id}.parquet')

        Returns:
            Path to saved parquet file
        """
        domain_dir = self._get_domain_dir(domain)
        fname = filename or f"{series_id}.parquet"
        file_path = domain_dir / fname

        df_to_save = df.copy()
        df_to_save.to_parquet(file_path, index=True)

        meta = metadata or {}
        now_str = datetime.now(timezone.utc).isoformat()

        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO dataset_catalog
                    (domain, series_id, file_path, format, row_count, last_updated, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        domain,
                        series_id,
                        str(file_path),
                        "parquet",
                        len(df),
                        now_str,
                        json.dumps(meta),
                    ),
                )
                conn.commit()

        logger.info(f"Saved DataFrame to Data Lake [{domain}/{series_id}]: {len(df)} rows -> {file_path}")
        return file_path

    def save_json(
        self,
        domain: str,
        series_id: str,
        data: Union[Dict[str, Any], List[Any]],
        metadata: Optional[Dict[str, Any]] = None,
        filename: Optional[str] = None,
    ) -> Path:
        """Save semi-structured JSON data to data lake."""
        domain_dir = self._get_domain_dir(domain)
        fname = filename or f"{series_id}.json"
        file_path = domain_dir / fname

        meta = metadata or {}
        payload = {
            "series_id": series_id,
            "domain": domain,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "metadata": meta,
            "data": data,
        }

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        row_count = len(data) if isinstance(data, list) else 1
        now_str = datetime.now(timezone.utc).isoformat()

        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO dataset_catalog
                    (domain, series_id, file_path, format, row_count, last_updated, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        domain,
                        series_id,
                        str(file_path),
                        "json",
                        row_count,
                        now_str,
                        json.dumps(meta),
                    ),
                )
                conn.commit()

        logger.info(f"Saved JSON to Data Lake [{domain}/{series_id}] -> {file_path}")
        return file_path

    def load_dataframe(self, domain: str, series_id: str, filename: Optional[str] = None) -> pd.DataFrame:
        """Load a DataFrame series from Parquet in the data lake."""
        domain_dir = self._get_domain_dir(domain)
        fname = filename or f"{series_id}.parquet"
        file_path = domain_dir / fname

        if not file_path.exists():
            raise FileNotFoundError(f"Data Lake dataset not found: {file_path}")

        return pd.read_parquet(file_path)

    def load_json(self, domain: str, series_id: str, filename: Optional[str] = None) -> Dict[str, Any]:
        """Load a JSON dataset from data lake."""
        domain_dir = self._get_domain_dir(domain)
        fname = filename or f"{series_id}.json"
        file_path = domain_dir / fname

        if not file_path.exists():
            raise FileNotFoundError(f"Data Lake dataset not found: {file_path}")

        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_metadata(self, domain: str, series_id: str) -> Dict[str, Any]:
        """Fetch metadata for a dataset from catalog index."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM dataset_catalog WHERE domain = ? AND series_id = ?",
                    (domain, series_id),
                )
                row = cursor.fetchone()
                if not row:
                    return {}
                res = dict(row)
                if res.get("metadata_json"):
                    res["metadata"] = json.loads(res["metadata_json"])
                return res

    def list_series(self, domain: Optional[str] = None) -> List[Dict[str, Any]]:
        """List cataloged series in data lake."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if domain:
                    cursor = conn.execute(
                        "SELECT * FROM dataset_catalog WHERE domain = ? ORDER BY series_id",
                        (domain,),
                    )
                else:
                    cursor = conn.execute(
                        "SELECT * FROM dataset_catalog ORDER BY domain, series_id"
                    )
                rows = cursor.fetchall()
                results = []
                for r in rows:
                    item = dict(r)
                    if item.get("metadata_json"):
                        item["metadata"] = json.loads(item["metadata_json"])
                    results.append(item)
                return results
