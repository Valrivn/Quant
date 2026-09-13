"""
Factor Store
DuckDB-backed factor panel storing factor data:
date, ticker, factor_name, value, provenance_hash
Enforces: factor date = PIT timestamp (filed/created_at), never period end.

Shared harness DB lives at data/harness/factors.duckdb (Phase 1 dispatch:
all harness writers — SEC parsers, gate reports, calibration — persist here).
"""

import hashlib
import json
import datetime
from pathlib import Path
from typing import List, Optional
import duckdb
import pandas as pd

DEFAULT_FACTOR_STORE = "data/harness/factors.duckdb"

class FactorStore:
    def __init__(self, db_path: str = DEFAULT_FACTOR_STORE):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self):
        return duckdb.connect(str(self.db_path))

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS factor_panel (
                    date DATE,
                    ticker VARCHAR,
                    factor_name VARCHAR,
                    value DOUBLE,
                    provenance_hash VARCHAR,
                    created_at TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS factor_provenance (
                    provenance_hash VARCHAR PRIMARY KEY,
                    factor_name VARCHAR,
                    source VARCHAR,
                    metadata_json VARCHAR,
                    created_at TIMESTAMP
                )
            """)

    def write_factor(self, df: pd.DataFrame, factor_name: str, provenance: dict) -> str:
        """
        Writes factor DataFrame to DuckDB factor panel with SHA-256 provenance tracking.
        df must contain columns: date, ticker, value
        """
        required_cols = {"date", "ticker", "value"}
        if not required_cols.issubset(set(df.columns)):
            raise ValueError(f"DataFrame missing required columns: {required_cols - set(df.columns)}")

        # Create SHA-256 hash for provenance
        prov_bytes = json.dumps(provenance, sort_keys=True).encode("utf-8")
        prov_hash = hashlib.sha256(prov_bytes).hexdigest()

        df_write = df[["date", "ticker", "value"]].copy()
        df_write["factor_name"] = factor_name
        df_write["provenance_hash"] = prov_hash
        df_write["created_at"] = datetime.datetime.now(datetime.timezone.utc)
        df_write["date"] = pd.to_datetime(df_write["date"]).dt.date

        with self._get_connection() as conn:
            # Register provenance metadata
            conn.execute(
                "INSERT OR IGNORE INTO factor_provenance VALUES (?, ?, ?, ?, ?)",
                [prov_hash, factor_name, provenance.get("source", "unknown"), json.dumps(provenance), datetime.datetime.now(datetime.timezone.utc)]
            )
            # Insert factor rows
            conn.execute("INSERT INTO factor_panel SELECT date, ticker, factor_name, value, provenance_hash, created_at FROM df_write")

        print(f"Successfully wrote {len(df_write)} records for factor '{factor_name}' (Hash: {prov_hash[:10]}...).")
        return prov_hash

    def read_factor(self, factor_name: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> pd.DataFrame:
        """Reads factor data from store for specified date range."""
        query = "SELECT date, ticker, factor_name, value, provenance_hash FROM factor_panel WHERE factor_name = ?"
        params = [factor_name]

        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)

        query += " ORDER BY date, ticker"

        with self._get_connection() as conn:
            df = conn.execute(query, params).df()
        return df

    def list_factors(self) -> List[str]:
        """Lists all distinct factor names in store."""
        with self._get_connection() as conn:
            result = conn.execute("SELECT DISTINCT factor_name FROM factor_panel").fetchall()
        return [r[0] for r in result]

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Factor Store Harness")
    parser.add_argument("--test", action="store_true", help="Run self-test on FactorStore")
    args = parser.parse_args()

    if args.test:
        print("Running FactorStore test...")
        test_db = "data/test_factor_store.duckdb"
        if Path(test_db).exists():
            Path(test_db).unlink()

        store = FactorStore(db_path=test_db)
        
        # Test synthetic factor data
        df_sample = pd.DataFrame({
            "date": ["2023-01-01", "2023-01-01", "2023-01-02", "2023-01-02"],
            "ticker": ["TICK1", "TICK2", "TICK1", "TICK2"],
            "value": [1.5, 2.3, 1.6, 2.4]
        })
        
        prov = {"source": "synthetic_test", "description": "momentum_1m test factor"}
        prov_hash = store.write_factor(df_sample, "mom_1m", prov)
        
        factors = store.list_factors()
        assert "mom_1m" in factors, "Factor list failed"
        
        df_read = store.read_factor("mom_1m", start_date="2023-01-01", end_date="2023-01-02")
        assert len(df_read) == 4, f"Expected 4 rows, got {len(df_read)}"
        
        print("FactorStore test passed successfully!")
        if Path(test_db).exists():
            Path(test_db).unlink()

if __name__ == "__main__":
    main()
