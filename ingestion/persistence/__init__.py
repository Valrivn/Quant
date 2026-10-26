"""
ingestion.persistence — canonical persistence contracts (bigpickle-worker).

Workstream B owns the data contracts, schemas, persistence layer and provenance
infrastructure for the merged PIT universe.  Nothing in this package computes a
signal, sizes a position or gates a trade; it only guarantees that whatever
lands on disk is schema-valid, PIT-clean, hash-chained and auditable.

    from ingestion.persistence import MergedUniverseStore, MERGED_UNIVERSE_SCHEMA

    store = MergedUniverseStore()                    # data/universe
    store.write_merged(merged_df)                    # validates + chains + partitions
    df = store.read_merged("2018-01-01", "2024-12-31")
    store.write_ticker_usage("a", "primary", usage_df)

See ``merged_universe`` for the frozen schemas, the fallback-tier ladder and the
PIT/provenance contract.
"""

from ingestion.persistence.merged_universe import (  # noqa: F401
    CAP_BUCKETS,
    CUSTOM_SECTORS,
    DATASET_SOURCE,
    DEFAULT_SOURCE_VERSION,
    FALLBACK_PARTITION_COLS,
    FALLBACK_TRACKING_SCHEMA,
    FALLBACK_TIERS,
    ICR_SOURCES,
    MERGED_PARTITION_COLS,
    MERGED_UNIVERSE_SCHEMA,
    ROIC_SOURCES,
    SCHEMA_VERSION,
    SECTOR_SOURCES,
    TICKER_USAGE_SCHEMA,
    TIER_CONSTANT,
    TIER_FILED,
    TIER_FORWARD,
    TIER_SECTOR_MEDIAN,
    TIER_SOURCE,
    USAGE_PARTITION_COLS,
    WACC_SOURCES,
    MergedUniverseStore,
    SchemaContractError,
    WriteReceipt,
    assign_provenance,
    build_merged_pit_universe,
    canonical_row_payload,
    compute_row_sha256,
    enforce_schema,
    make_uuid7,
    migrate_ticker_usage_datasets,
    migrate_ticker_usage_frame,
    schema_contract,
    validate_fallback_consistency,
    validate_pit,
)

__all__ = [
    "MergedUniverseStore", "WriteReceipt", "SchemaContractError",
    "MERGED_UNIVERSE_SCHEMA", "FALLBACK_TRACKING_SCHEMA", "TICKER_USAGE_SCHEMA",
    "MERGED_PARTITION_COLS", "FALLBACK_PARTITION_COLS", "USAGE_PARTITION_COLS",
    "CAP_BUCKETS", "CUSTOM_SECTORS", "FALLBACK_TIERS", "TIER_SOURCE",
    "TIER_FILED", "TIER_FORWARD", "TIER_SECTOR_MEDIAN", "TIER_CONSTANT",
    "WACC_SOURCES", "ICR_SOURCES", "SECTOR_SOURCES", "ROIC_SOURCES",
    "SCHEMA_VERSION", "DATASET_SOURCE", "DEFAULT_SOURCE_VERSION",
    "assign_provenance", "canonical_row_payload", "compute_row_sha256",
    "make_uuid7", "enforce_schema", "validate_pit", "validate_fallback_consistency",
    "schema_contract", "build_merged_pit_universe",
    "migrate_ticker_usage_frame", "migrate_ticker_usage_datasets",
]