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
from typing import List, Optional, Union
import duckdb
import pandas as pd

DEFAULT_FACTOR_STORE = "data/harness/factors.duckdb"

# Mapping-confidence levels per Data_Strategy.md §6 / §8. Qualitative entity
# mappings are graded {high, medium, low}; "low" is rejected from core arms,
# "medium" allowed at 0.5 weight, "high" full weight.
CONFIDENCE_LEVELS = ("high", "medium", "low")

# Qualitative factor catalogue for the 4-Stage strategy (Stage 2 Grassroots
# C_t, Stage 3 Hype P95). Each entry declares source mix, the mapping
# confidence the aggregated factor is allowed to carry, and its PIT anchor.
# Per Data_Strategy.md §6: ApeWisdom (Reddit/StockTwits) is Tier-3 contrarian
# overflow only (max 5% weight) and its mapping is ALWAYS low (MAP-01).
QUAL_FACTOR_CATALOG = {
    "grassroots_c_t": {
        "sources": ["app_store", "amazon", "gh_archive", "cfpb", "nhtsa", "gdelt"],
        "default_confidence": "medium",
        "allowed_confidences": ("high", "medium", "low"),
        "pit_anchor": "retrieval_timestamp",
        "description": "Grassroots consumer velocity composite (Stage 2 entry; Stage 4 black-swan moat invariance)",
    },
    "hype_p95": {
        "sources": ["apewisdom", "reddit"],
        "default_confidence": "low",
        "allowed_confidences": ("low",),
        "pit_anchor": "retrieval_timestamp",
        "description": "Wall-street hype percentile P95 (Stage 3 exit trigger). Tier-3 source: low confidence ONLY.",
    },
}

# Per-source confidence semantics for loaders aggregating into C_t (used by
# ingestion loaders and the consumer_signal aggregator when persisting).
SOURCE_CONFIDENCE_RULES = {
    "app_store": ("high", "medium"),   # direct brand map = high; heuristic = medium
    "amazon": ("high", "medium"),      # brand table = high; fuzzy = medium
    "gh_archive": ("high", "medium", "low"),
    "cfpb": ("medium", "low"),         # keyword match = medium; ambiguous = low
    "nhtsa": ("medium", "low"),
    "gdelt": ("medium", "low"),
    "apewisdom": ("low",),             # contrarian overflow only — always low (MAP-01)
    "reddit": ("low",),
}


def _validate_mapping_confidence(value: str, factor_name: str) -> None:
    """Raise ValueError on an invalid/undisclosed mapping confidence value."""
    if value not in CONFIDENCE_LEVELS:
        raise ValueError(
            f"mapping_confidence must be one of {CONFIDENCE_LEVELS}, got '{value}'"
        )
    allowed = QUAL_FACTOR_CATALOG.get(factor_name, {}).get("allowed_confidences")
    if allowed is not None and value not in allowed:
        raise ValueError(
            f"factor '{factor_name}' only allows mapping_confidence in {allowed}, got '{value}'"
        )


def ensure_factor_store_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotent schema guard for the shared harness factor DB.

    - factor_panel / factor_provenance (existing contract)
    - mapping_confidence column on factor_panel (schema migration v2:
      Data_Strategy.md §8 requires the confidence grade on every qualitative row)
    - convenience index for factor/date slice queries
    Safe on the existing data/harness/factors.duckdb: the ALTER is
    IF NOT EXISTS and never rewrites stored rows.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS factor_panel (
            date DATE,
            ticker VARCHAR,
            factor_name VARCHAR,
            value DOUBLE,
            provenance_hash VARCHAR,
            created_at TIMESTAMP,
            mapping_confidence VARCHAR
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
    conn.execute("ALTER TABLE factor_panel ADD COLUMN IF NOT EXISTS mapping_confidence VARCHAR")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_factor_panel_fdate ON factor_panel(date, factor_name)")


class FactorStore:
    def __init__(self, db_path: str = DEFAULT_FACTOR_STORE):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self):
        return duckdb.connect(str(self.db_path))

    def _init_db(self):
        with self._get_connection() as conn:
            ensure_factor_store_schema(conn)

    def write_factor(self, df: pd.DataFrame, factor_name: str, provenance: dict, mapping_confidence: Optional[Union[str, pd.Series]] = None) -> str:
        """
        Writes factor DataFrame to DuckDB factor panel with SHA-256 provenance tracking.
        df must contain columns: date, ticker, value

        mapping_confidence:
          - None        -> resolved from QUAL_FACTOR_CATALOG default (fundamental
                           factors default to 'high' — they are exact, not mapped)
          - str         -> scalar grade applied to every row ({high, medium, low})
          - Series/col  -> per-row grades: either df['mapping_confidence'] or a
                           Series aligned positionally with df
        """
        required_cols = {"date", "ticker", "value"}
        if not required_cols.issubset(set(df.columns)):
            raise ValueError(f"DataFrame missing required columns: {required_cols - set(df.columns)}")

        # Resolve per-row mapping_confidence
        if mapping_confidence is None:
            conf = QUAL_FACTOR_CATALOG.get(factor_name, {}).get("default_confidence", "high")
            _validate_mapping_confidence(conf, factor_name)
            conf_values = [conf] * len(df)
        elif isinstance(mapping_confidence, str):
            _validate_mapping_confidence(mapping_confidence, factor_name)
            conf_values = [mapping_confidence] * len(df)
        else:
            # Series (standalone or df column) -> per-row grades
            if isinstance(mapping_confidence, pd.Series):
                series = mapping_confidence.reset_index(drop=True)
                if len(series) != len(df):
                    raise ValueError(
                        f"mapping_confidence Series length {len(series)} != df rows {len(df)}"
                    )
                conf_values = [str(v) for v in series]
            elif "mapping_confidence" in df.columns:
                conf_values = [str(v) for v in df["mapping_confidence"]]
            else:
                raise ValueError("mapping_confidence must be None, a str, or a pandas Series")
            for v in conf_values:
                _validate_mapping_confidence(v, factor_name)

        # Create SHA-256 hash for provenance
        prov_bytes = json.dumps(provenance, sort_keys=True).encode("utf-8")
        prov_hash = hashlib.sha256(prov_bytes).hexdigest()

        df_write = df[["date", "ticker", "value"]].copy()
        df_write["factor_name"] = factor_name
        df_write["provenance_hash"] = prov_hash
        df_write["created_at"] = datetime.datetime.now(datetime.timezone.utc)
        df_write["mapping_confidence"] = conf_values
        df_write["date"] = pd.to_datetime(df_write["date"]).dt.date

        with self._get_connection() as conn:
            # Register provenance metadata
            conn.execute(
                "INSERT OR IGNORE INTO factor_provenance VALUES (?, ?, ?, ?, ?)",
                [prov_hash, factor_name, provenance.get("source", "unknown"), json.dumps(provenance), datetime.datetime.now(datetime.timezone.utc)]
            )
            # Insert factor rows
            conn.execute(
                "INSERT INTO factor_panel (date, ticker, factor_name, value, provenance_hash, created_at, mapping_confidence) "
                "SELECT date, ticker, factor_name, value, provenance_hash, created_at, mapping_confidence FROM df_write"
            )

        print(f"Successfully wrote {len(df_write)} records for factor '{factor_name}' (Hash: {prov_hash[:10]}...).")
        return prov_hash

    def read_factor(self, factor_name: str, start_date: Optional[str] = None, end_date: Optional[str] = None, min_confidence: Optional[str] = None) -> pd.DataFrame:
        """Reads factor data from store for specified date range.

        min_confidence: 'high' -> drop medium+low rows; 'medium' -> drop low rows;
        None -> no confidence filter (all rows).
        """
        query = "SELECT date, ticker, factor_name, value, provenance_hash, mapping_confidence FROM factor_panel WHERE factor_name = ?"
        params = [factor_name]

        if min_confidence == "high":
            query += " AND mapping_confidence = 'high'"
        elif min_confidence == "medium":
            query += " AND mapping_confidence != 'low'"
        elif min_confidence not in (None, "low"):
            raise ValueError(f"min_confidence must be one of {'high', 'medium', 'low', None}, got '{min_confidence}'")

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
        # Default confidence for non-catalogue factors must be 'high'
        assert set(df_read["mapping_confidence"]) == {"high"}, "Default confidence should be high"

        # Catalogue factor: grassroots_c_t defaults to 'medium'
        df_qual = pd.DataFrame({
            "date": ["2023-01-01", "2023-01-01", "2023-01-02", "2023-01-02"],
            "ticker": ["TICK1", "TICK2", "TICK1", "TICK2"],
            "value": [0.10, 0.05, 0.11, 0.06],
        })
        prov_qual = {"source": "app_store_test", "description": "grassroots c_t test"}
        store.write_factor(df_qual, "grassroots_c_t", prov_qual)  # default -> medium
        df_c = store.read_factor("grassroots_c_t")
        assert set(df_c["mapping_confidence"]) == {"medium"}, "grassroots_c_t should default to medium"

        # insistence on a per-row confidence column
        df_hy = pd.DataFrame({
            "date": ["2023-01-03"],
            "ticker": ["TICK1"],
            "value": [0.9],
        })
        try:
            store.write_factor(df_hy, "hype_p95", {"source": "apewisdom"}, mapping_confidence="high")
            raise AssertionError("hype_p95 must reject 'high' confidence (Tier-3 source)")
        except ValueError:
            pass
        store.write_factor(df_hy, "hype_p95", {"source": "apewisdom"}, mapping_confidence="low")

        # min_confidence filter (low rows dropped when min_confidence='medium')
        df_no_low = store.read_factor("hype_p95", min_confidence="medium")
        assert len(df_no_low) == 0, "low-confidence rows must be excluded from core reads"

        print("FactorStore test passed successfully!")
        if Path(test_db).exists():
            Path(test_db).unlink()

if __name__ == "__main__":
    main()
