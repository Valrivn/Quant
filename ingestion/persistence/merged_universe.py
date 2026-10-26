"""
ingestion/persistence — canonical persistence contracts for the merged PIT universe.

This sub-package owns ONE thing: the *data contracts and storage layer* that
Workstream A (merge + WACC/ICR engine) and Workstream B (3-arm backtest) read
and write through.  No signal logic, no alpha, no gating lives here.

P0A deliverables owned by this package
-------------------------------------
  1. ``MERGED_UNIVERSE_SCHEMA``    data/universe/merged_pit_universe.parquet
  2. ``FALLBACK_TRACKING_SCHEMA``  data/universe/fallback_tracking.parquet
  3. ``TICKER_USAGE_SCHEMA``       data/backtests/ticker_usage_{arm}_{window}.parquet
  4. ``MergedUniverseStore``       validated, chained, partitioned persistence
  5. Provenance primitives         row SHA-256 chain, UUIDv7, ns timestamps
  6. ``build_merged_pit_universe`` reference producer (PIT fallback ladder)

STORAGE LAYOUT — WHY A DIRECTORY, NOT A FILE
--------------------------------------------
The brief asks for BOTH ``data/universe/merged_pit_universe.parquet`` AND hive
partitioning on ``[rebalance_date, cap_bucket]``.  A single parquet file cannot
be hive-partitioned, so the *path* is honoured and the *layout* is the standard
hive dataset::

    data/universe/merged_pit_universe.parquet/          <- dataset root (a dir)
        _manifest.json
        rebalance_date=2018-01-31/cap_bucket=large/part-0000.parquet
        rebalance_date=2018-01-31/cap_bucket=mid/part-0000.parquet
        ...

Both ``pyarrow.parquet.ParquetDataset`` and DuckDB's
``read_parquet('<root>/**/*.parquet', hive_partitioning=true)`` read this
layout, so consumers that expect a bare file still work.  Reads go through
DuckDB (the house requirement per ``validation/wacc_gates.py``) which prunes on
the partition directory *before* opening a file.

PIT CONTRACT (frozen)
---------------------
* ``rebalance_date`` is the availability anchor for a row.
* Every ``*_filed_date`` is the ``filed_date`` of the filing that produced the
  value, and MUST satisfy ``*_filed_date <= rebalance_date``.  The reference
  producer enforces this by construction (``merge_asof`` direction='backward');
  ``MergedUniverseStore`` re-checks it at write time.
* Cross-sectional imputations (sector medians) are computed from the SAME
  rebalance_date cohort only — never from a later month.

FALLBACK LADDER (frozen, tier 0..3)
-----------------------------------
Tiers describe how much of a value's *lineage* we had to give up:

  tier 0  ``fundamentals_filed``  value computed from a PIT filing whose
          ``filed_date`` is within ``FRESH_DAYS`` of the rebalance.
  tier 1  ``forward_filled``      value carried forward from a PIT filing that
          is older than ``FRESH_DAYS`` but no older than ``MAX_STALE_DAYS``.
          Real filed data — merely stale.  (This is the ordinary
          "forward-filled to rebalance_date" state for a quiet filer.)
  tier 2  ``sector_median``       no usable filing for this ticker; imputed
          from the same-rebalance cross-sectional median of the tier 0/1
          cohort within ``sector_custom``.
  tier 3  ``constant_fallback``   not even a sector median was available
          (``>= SECTOR_MEDIAN_MIN_N`` rule failed) — a frozen constant.

``sector_source`` uses its own 3-value enum (``reconstitution`` | ``filing`` |
``constant_fallback``) mapped onto the same tier axis via
``SECTOR_TIER_SOURCE``.

COMPONENT-LEVEL CASCADES ARE AUDITED SEPARATELY
-----------------------------------------------
A WACC can be tier 0 (fresh filing) yet still have leaned on a sector median
for one *component*.  Suppressing that would hide risk, so the merged row
carries the top-level tier while ``fallback_tracking`` additionally logs every
component cascade (``wacc.cost_of_debt``, ``wacc.capital_structure``,
``icr.interest_expense``) with its own source/tier.

PROVENANCE (TradeAuditLedger v5 patterns, reused not reimplemented)
-------------------------------------------------------------------
* ``canonical_json`` / ``_sha256_hex`` / ``_uuid7_ns`` / ``GENESIS`` are
  imported from :mod:`ingestion.trade_audit` — the v5 ledger primitives.  If
  those are renamed there, this module fails loudly at import, which is the
  correct behaviour for an in-repo dependency.
* Two-layer chain, adapted to a row-wise table: ``record_hash = sha256(
  canonical_json(payload | {prev_chain_hash}))`` and ``sha256 = sha256(prev |
  record)``.  The table carries the *chained* value in ``sha256``; the stored
  hashes are never trusted during verification — they are recomputed from the
  non-provenance columns.
* The chain order is canonical and deterministic: ``(rebalance_date,
  cap_bucket, ticker)``.  Re-verification of an untouched dataset must return
  the same hashes, which is what makes the chain forensically meaningful.
* Canonical row order is also the canonical *partition* order, so an untouched
  dataset re-writes byte-identical chains.
* Writes are append-only: a ``(rebalance_date, ticker)`` pair already on disk
  is refused, and the new batch continues the chain from the existing head.

INTEGRATION
-----------
    Workstream A   MergedUniverseStore(root).write_merged(df)
                   MergedUniverseStore(root).write_fallback_tracking(df)
    Workstream B   MergedUniverseStore(root).read_merged(start, end)
                   MergedUniverseStore(root).write_ticker_usage(arm, win, df)
    validation     python -m validation.validate_merged_universe
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# --- TradeAuditLedger v5 primitives: REUSED, not reimplemented ---------------
from ingestion.trade_audit import (  # noqa: F401  (re-exported for callers)
    GENESIS,
    _sha256_hex,
    _uuid7_ns,
    canonical_json,
)

logger = logging.getLogger("ingestion.persistence.merged_universe")

try:  # pragma: no cover — DuckDB is the house I/O requirement
    import duckdb as _duckdb
except ImportError:  # pragma: no cover
    _duckdb = None


# ===========================================================================
# 1. FROZEN CONTRACT
# ===========================================================================
SCHEMA_VERSION = "1.0.0"
SCHEMA_FROZEN_ON = "2026-10-04"
DATASET_SOURCE = "MERGED_PIT_UNIVERSE"
DEFAULT_SOURCE_VERSION = "reconstitution_v2_fundamentals_v1"
UUID_VERSION = 7
TIMESTAMP_PRECISION = "nanosecond"
PARQUET_COMPRESSION = "zstd"

DEFAULT_UNIVERSE_ROOT = Path("data/universe")
DEFAULT_BACKTEST_ROOT = Path("data/backtests")

# --- entity taxonomies (frozen) --------------------------------------------
CAP_BUCKETS: Tuple[str, ...] = ("large", "mid", "micro")
# reconstitution v2 emits Mega/Large; normalised into the frozen 3-tier axis.
CAP_BUCKET_ALIASES: Dict[str, str] = {
    "mega": "large", "large": "mid", "mid": "mid", "micro": "micro",
    "small": "micro", "nano": "micro",
}
CUSTOM_SECTORS: Tuple[str, ...] = (
    "Hardware", "Software", "Consumer", "Industrial",
    "Healthcare", "Energy", "Financial",
)

# --- fallback axis ----------------------------------------------------------
TIER_FILED = 0            # fundamentals_filed
TIER_FORWARD = 1          # forward_filled
TIER_SECTOR_MEDIAN = 2    # sector_median
TIER_CONSTANT = 3         # constant_fallback
FALLBACK_TIERS: Tuple[int, ...] = (0, 1, 2, 3)

WACC_SOURCES: Tuple[str, ...] = (
    "fundamentals_filed", "forward_filled", "sector_median", "constant_fallback",
)
ICR_SOURCES: Tuple[str, ...] = WACC_SOURCES
ROIC_SOURCES: Tuple[str, ...] = WACC_SOURCES
SECTOR_SOURCES: Tuple[str, ...] = ("reconstitution", "filing", "constant_fallback")

TIER_SOURCE: Dict[int, str] = {
    TIER_FILED: "fundamentals_filed",
    TIER_FORWARD: "forward_filled",
    TIER_SECTOR_MEDIAN: "sector_median",
    TIER_CONSTANT: "constant_fallback",
}
SECTOR_TIER_SOURCE: Dict[int, str] = {
    TIER_FILED: "reconstitution",
    TIER_FORWARD: "filing",
    TIER_SECTOR_MEDIAN: "filing",
    TIER_CONSTANT: "constant_fallback",
}

#: fields whose lineage is embedded per-row in the merged table
TRACKED_FIELDS: Tuple[str, ...] = ("wacc", "icr", "sector", "roic")
#: component-level cascades, audited only in fallback_tracking
COMPONENT_FIELDS: Tuple[str, ...] = (
    "wacc.cost_of_debt", "wacc.capital_structure", "icr.interest_expense",
)

# --- freshness / imputation policy (frozen) ---------------------------------
FRESH_DAYS = 120           # a 10-Q/10-K within ~4 months is "current"
MAX_STALE_DAYS = 730       # ~2 years; beyond this we stop trusting a carry
SECTOR_MEDIAN_MIN_N = 5    # cohort size floor before a median is admissible

# --- arithmetic defence-in-depth bounds (frozen) ---------------------------
WACC_BOUNDS: Tuple[float, float] = (0.005, 0.35)
ICR_BOUNDS: Tuple[float, float] = (-50.0, 100.0)
ROIC_BOUNDS: Tuple[float, float] = (-0.99, 1.00)
KE_BOUNDS: Tuple[float, float] = (0.005, 0.50)
KD_BOUNDS: Tuple[float, float] = (0.0005, 0.50)
MAX_DEBT_WEIGHT = 0.60     # wd cap; junk debt tags cannot dominate the WACC
TAX_BOUNDS: Tuple[float, float] = (0.0, 0.35)
TAX_FALLBACK = 0.21        # matches phase8.trade_audit / valuation_schema

CONSTANT_FALLBACK_WACC = 0.0900
CONSTANT_FALLBACK_ICR = 5.0000
CONSTANT_FALLBACK_ROIC = 0.0500
CONSTANT_FALLBACK_KD = 0.0500

#: provenance columns — EXCLUDED from the row hash payload
PROVENANCE_COLUMNS: Tuple[str, ...] = ("sha256", "uuid", "timestamp_ns")
CHAIN_KEY = "prev_chain_hash"

#: The frozen schemas name the provenance triple differently on purpose: the
#: merged table *owns* the chain, so its columns are unprefixed; the fallback
#: audit log is a *referencing* table, so its columns carry an explicit
#: ``provenance_`` prefix.  Both are non-null.  This mapping is the only place
#: the two vocabularies are bridged.
PROVENANCE_NAME_MAP: Dict[str, str] = {
    "sha256": "provenance_sha256",
    "uuid": "provenance_uuid",
    "timestamp_ns": "provenance_timestamp_ns",
}


# ===========================================================================
# 2. ARROW / PARQUET SCHEMAS
# ===========================================================================
MERGED_UNIVERSE_SCHEMA = pa.schema([
    # -- identity ---------------------------------------------------------
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("cik", pa.string(), nullable=False),
    pa.field("rebalance_date", pa.date32(), nullable=False),

    # -- universe metadata ------------------------------------------------
    pa.field("market_cap", pa.float64(), nullable=False),
    pa.field("cap_bucket", pa.string(), nullable=False),
    pa.field("sector_gics", pa.string(), nullable=True),
    pa.field("sector_custom", pa.string(), nullable=False),

    # -- valuation fundamentals (forward-filled to rebalance_date) --------
    pa.field("roic", pa.float64(), nullable=True),
    pa.field("fcf_margin", pa.float64(), nullable=True),
    pa.field("wacc_estimate", pa.float64(), nullable=True),
    pa.field("cost_of_equity_est", pa.float64(), nullable=True),
    pa.field("roic_wacc_spread", pa.float64(), nullable=True),
    pa.field("ebit", pa.float64(), nullable=True),
    pa.field("interest_expense", pa.float64(), nullable=True),

    # -- COMPUTED fields --------------------------------------------------
    pa.field("wacc", pa.float64(), nullable=False),
    pa.field("icr", pa.float64(), nullable=True),

    # -- FALLBACK TRACKING (embedded per-field) ---------------------------
    pa.field("wacc_source", pa.string(), nullable=False),
    pa.field("wacc_filed_date", pa.date32(), nullable=True),
    pa.field("wacc_fallback_tier", pa.int8(), nullable=False),

    pa.field("icr_source", pa.string(), nullable=False),
    pa.field("icr_filed_date", pa.date32(), nullable=True),
    pa.field("icr_fallback_tier", pa.int8(), nullable=False),

    pa.field("sector_source", pa.string(), nullable=False),
    pa.field("sector_fallback_tier", pa.int8(), nullable=False),

    pa.field("roic_source", pa.string(), nullable=False),
    pa.field("roic_filed_date", pa.date32(), nullable=True),
    pa.field("roic_fallback_tier", pa.int8(), nullable=False),

    # -- provenance (per row) ---------------------------------------------
    pa.field("source", pa.string(), nullable=False),
    pa.field("retrieval_timestamp", pa.timestamp("ns"), nullable=False),
    pa.field("source_version", pa.string(), nullable=False),
    pa.field("sha256", pa.string(), nullable=False),
    pa.field("uuid", pa.string(), nullable=False),
    pa.field("timestamp_ns", pa.int64(), nullable=False),
])

FALLBACK_TRACKING_SCHEMA = pa.schema([
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("rebalance_date", pa.date32(), nullable=False),
    pa.field("field", pa.string(), nullable=False),
    pa.field("value", pa.float64(), nullable=True),
    pa.field("source", pa.string(), nullable=False),
    pa.field("filed_date", pa.date32(), nullable=True),
    pa.field("fallback_tier", pa.int8(), nullable=False),
    pa.field("provenance_sha256", pa.string(), nullable=False),
    pa.field("provenance_uuid", pa.string(), nullable=False),
    pa.field("provenance_timestamp_ns", pa.int64(), nullable=False),
])

TICKER_USAGE_SCHEMA = pa.schema([
    pa.field("ticker", pa.string(), nullable=False),
    pa.field("rebalance_date", pa.date32(), nullable=False),
    pa.field("arm", pa.string(), nullable=False),
    pa.field("weight", pa.float64(), nullable=False),
    pa.field("wacc_fallback_tier", pa.int8(), nullable=False),
    pa.field("icr_fallback_tier", pa.int8(), nullable=False),
    pa.field("sector_fallback_tier", pa.int8(), nullable=False),
    pa.field("any_fallback", pa.bool_(), nullable=False),
    pa.field("provenance_sha256", pa.string(), nullable=False),
    pa.field("provenance_uuid", pa.string(), nullable=False),
    pa.field("provenance_timestamp_ns", pa.int64(), nullable=False),
])

MERGED_PARTITION_COLS: Tuple[str, ...] = ("rebalance_date", "cap_bucket")
FALLBACK_PARTITION_COLS: Tuple[str, ...] = ("rebalance_date",)
USAGE_PARTITION_COLS: Tuple[str, ...] = ("rebalance_date", "arm")


def schema_contract(schema: pa.Schema, table: str) -> Dict[str, Any]:
    """Machine-readable JSON Schema contract for a table (A§8 data contracts)."""
    fields: List[Dict[str, Any]] = []
    for f in schema:
        fields.append({
            "name": f.name,
            "type": str(f.type),
            "nullable": bool(f.nullable),
            "required": not bool(f.nullable),
        })
    return {
        "table": table,
        "schema_version": SCHEMA_VERSION,
        "frozen_on": SCHEMA_FROZEN_ON,
        "owner": "bigpickle-worker",
        "source": DATASET_SOURCE,
        "source_version": DEFAULT_SOURCE_VERSION,
        "uuid_version": UUID_VERSION,
        "timestamp_precision": TIMESTAMP_PRECISION,
        "pit_timestamp_column": "rebalance_date",
        "entity_key": "ticker",
        "row_hash_excludes": list(PROVENANCE_COLUMNS),
        "chain_algorithm": (
            "record_hash=sha256(canonical_json(payload|{prev_chain_hash})); "
            "sha256=sha256(prev_chain_hash+'|'+record_hash); genesis='GENESIS'"
        ),
        # canonical chain order == partition order, then the entity key
        "chain_order": list(MERGED_PARTITION_COLS) + ["ticker"],
        "field_count": len(schema),
        "fields": fields,
    }


# ===========================================================================
# 3. PROVENANCE PRIMITIVES
# ===========================================================================
def _is_missing(v: Any) -> bool:
    if v is None or v is pd.NA or v is pd.NaT:
        return True
    return isinstance(v, float) and np.isnan(v)


def normalize_hashable(value: Any) -> Any:
    """Coerce a cell to a JSON-stable, parquet-round-trip-stable form.

    A row hash is only forensic if recomputing it from the *stored* parquet
    reproduces the *stored* hash.  pandas hands back ``NaN``/``NaT`` where the
    Arrow type says ``null``, and ``datetime.date`` where it says ``date32``;
    both must collapse onto the same canonical representation or every
    verification of a clean dataset would report a false tamper.
    """
    if _is_missing(value):
        return None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        f = float(value)
        return None if np.isnan(f) else f
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.date().isoformat()
    if isinstance(value, _date):
        return value.isoformat()
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        f = float(value)
        return None if np.isnan(f) else f
    return str(value)


def canonical_row_payload(row: Mapping[str, Any],
                          exclude: Sequence[str] = PROVENANCE_COLUMNS) -> Dict[str, Any]:
    """The hash pre-image for one row: every column except the provenance triple."""
    excluded = set(exclude)
    return {k: normalize_hashable(v) for k, v in row.items() if k not in excluded}


def compute_row_sha256(payload: Mapping[str, Any], prev_chain_hash: str) -> Tuple[str, str]:
    """TradeAuditLedger §6.2 two-layer hash, row-wise.

    Returns ``(record_hash, chain_hash)``.  ``record_hash`` covers the row plus
    the previous chain hash; ``chain_hash`` is the digest that the *next* row
    links against.  ``sha256`` in the table stores ``chain_hash`` — the value
    with tamper-evidence already folded in.
    """
    record = dict(payload)
    record[CHAIN_KEY] = prev_chain_hash
    record_hash = _sha256_hex(canonical_json(record))
    return record_hash, _sha256_hex(f"{prev_chain_hash}|{record_hash}")


def make_uuid7(ts_ns: int, seed: Optional[Union[str, bytes]] = None) -> str:
    """Monotonic, time-ordered UUIDv7 (v5 ledger primitive, content-addressed)."""
    return _uuid7_ns(int(ts_ns), seed=seed)


def time_ns_now() -> int:
    """Nanosecond wall clock (``time.time_ns`` — frozen precision contract)."""
    return int(time.time_ns())


def _canonical_row_order(df: pd.DataFrame) -> pd.DataFrame:
    """Deterministic chain order.  Order IS part of the contract."""
    return df.sort_values(
        ["rebalance_date", "cap_bucket", "ticker"], kind="mergesort"
    ).reset_index(drop=True)


def assign_provenance(df: pd.DataFrame,
                      prev_chain_hash: str = GENESIS,
                      source: str = DATASET_SOURCE,
                      source_version: str = DEFAULT_SOURCE_VERSION,
                      retrieval_ns: Optional[int] = None) -> Tuple[pd.DataFrame, str]:
    """Stamp the provenance triple + chain onto ``df`` in canonical order.

    UUIDv7 is seeded with the row's own content hash, so an identical row
    written twice yields an identical uuid — and the append-only guard (below)
    is what actually refuses the duplicate, by design: the uuid is evidence,
    not the lock.
    """
    out = _canonical_row_order(df)
    ns = time_ns_now() if retrieval_ns is None else int(retrieval_ns)
    retr = pd.Timestamp(ns, unit="ns", tz="UTC").tz_localize(None)

    out["source"] = source
    out["source_version"] = source_version
    out["retrieval_timestamp"] = retr
    out["timestamp_ns"] = np.int64(ns)

    prev = prev_chain_hash
    shas: List[str] = []
    uuids: List[str] = []
    for rec in out.to_dict("records"):
        payload = canonical_row_payload(rec)
        record_hash, chain_hash = compute_row_sha256(payload, prev)
        shas.append(chain_hash)
        # content-addressed uuid7: time-ordered AND reproducible for an
        # unchanged row, which makes spot-checks meaningful.
        uuids.append(make_uuid7(ns, seed=canonical_json([payload, chain_hash])))
        prev = chain_hash
    out["sha256"] = shas
    out["uuid"] = uuids
    return out, prev


# ===========================================================================
# 4. SCHEMA ENFORCEMENT
# ===========================================================================
class SchemaContractError(ValueError):
    """Raised when a frame violates a frozen Arrow contract."""


def _coerce_dates(series: pd.Series) -> pd.Series:
    """date32-ready column: NaT for missing, ``datetime.date`` otherwise."""
    s = pd.to_datetime(series, errors="coerce")
    if isinstance(s.dtype, pd.DatetimeTZDtype):
        s = s.dt.tz_localize(None)
    return s


def enforce_schema(df: pd.DataFrame, schema: pa.Schema, table: str) -> pd.DataFrame:
    """Coerce dtypes, then enforce presence / nullability / enum contracts.

    Raises :class:`SchemaContractError` rather than silently repairing a
    contract violation — a provenance-bearing table cannot carry silent fixes
    (same doctrine as ``trade_audit.validate_trigger_reason``).
    """
    out = df.copy()
    names = [f.name for f in schema]

    missing = [n for n in names if n not in out.columns]
    extra = [c for c in out.columns if c not in names]
    if missing:
        raise SchemaContractError(f"{table}: missing required columns {missing}")
    if extra:
        # Drop, but never silently: unknown columns break the hash pre-image.
        logger.warning("%s: dropping %d non-contract column(s): %s",
                       table, len(extra), extra)
        out = out[names]

    for f in schema:
        col = out[f.name]
        if pa.types.is_date32(f.type):
            out[f.name] = _coerce_dates(col)
        elif pa.types.is_timestamp(f.type):
            ts = pd.to_datetime(col, errors="coerce", utc=True)
            if isinstance(ts.dtype, pd.DatetimeTZDtype):
                ts = ts.dt.tz_localize(None)
            out[f.name] = ts
        elif pa.types.is_string(f.type):
            out[f.name] = col.astype("string").astype(object)
        elif pa.types.is_boolean(f.type):
            out[f.name] = col.astype("boolean").astype(object).where(col.notna(), None)
        elif pa.types.is_integer(f.type):
            out[f.name] = pd.to_numeric(col, errors="coerce").astype(f.type.to_pandas_dtype())
        elif pa.types.is_floating(f.type):
            out[f.name] = pd.to_numeric(col, errors="coerce").astype("float64")

    for f in schema:
        if not f.nullable:
            nulls = int(out[f.name].isna().sum())
            if nulls:
                bad = out.loc[out[f.name].isna(), names[0]].head(5).tolist()
                raise SchemaContractError(
                    f"{table}: column '{f.name}' is NOT NULL but carries "
                    f"{nulls} null(s) (e.g. {bad})")

    # -- semantic enums -----------------------------------------------------
    def _check_enum(col: str, allowed: Sequence[str], nullable: bool = False) -> None:
        s = out[col]
        if nullable:
            s = s.dropna()
        bad = sorted(set(map(str, s.unique())) - set(allowed))
        if bad:
            raise SchemaContractError(
                f"{table}: column '{col}' carries out-of-enum value(s) {bad}; "
                f"allowed {list(allowed)}")

    if "cap_bucket" in out:
        _check_enum("cap_bucket", CAP_BUCKETS)
    if "sector_custom" in out:
        _check_enum("sector_custom", CUSTOM_SECTORS)
    for col, allowed in (("wacc_source", WACC_SOURCES), ("icr_source", ICR_SOURCES),
                         ("sector_source", SECTOR_SOURCES), ("roic_source", ROIC_SOURCES)):
        if col in out:
            _check_enum(col, allowed)
    for col in ("wacc_fallback_tier", "icr_fallback_tier",
                "sector_fallback_tier", "roic_fallback_tier"):
        if col in out:
            _check_enum(col, [str(t) for t in FALLBACK_TIERS])

    return out


def validate_pit(frame: pd.DataFrame, table: str) -> None:
    """``*_filed_date <= rebalance_date`` — the no-lookahead gate."""
    cols = [c for c in frame.columns if c.endswith("_filed_date") or c == "filed_date"]
    for c in cols:
        if c not in frame.columns:
            continue
        fd = frame[c]
        rd = frame["rebalance_date"]
        both = fd.notna() & rd.notna()
        bad = both & (fd > rd)
        n = int(bad.sum())
        if n:
            ex = frame.loc[bad, ["ticker", "rebalance_date", c]].head(5).to_dict("records")
            raise SchemaContractError(
                f"{table}: PIT violation — {n} row(s) with {c} > rebalance_date: {ex}")


def validate_fallback_consistency(frame: pd.DataFrame, table: str) -> None:
    """``source`` must equal the source implied by ``fallback_tier``."""
    for col in ("wacc", "icr", "roic"):
        src, tier = f"{col}_source", f"{col}_fallback_tier"
        if src not in frame or tier not in frame:
            continue
        implied = frame[tier].map(TIER_SOURCE)
        bad = frame[src].astype("string") != implied.astype("string")
        if int(bad.sum()):
            raise SchemaContractError(
                f"{table}: {src}/{tier} inconsistent on {int(bad.sum())} row(s): "
                f"{frame.loc[bad, ['ticker', src, tier]].head(5).to_dict('records')}")
    if "sector_source" in frame and "sector_fallback_tier" in frame:
        implied = frame["sector_fallback_tier"].map(SECTOR_TIER_SOURCE)
        bad = frame["sector_source"].astype("string") != implied.astype("string")
        if int(bad.sum()):
            raise SchemaContractError(
                f"{table}: sector_source/sector_fallback_tier inconsistent on "
                f"{int(bad.sum())} row(s)")


# ===========================================================================
# 5. MERGED UNIVERSE STORE
# ===========================================================================
@dataclass(frozen=True)
class WriteReceipt:
    """What a write actually did — the audit trail for the audit trail."""
    table: str
    path: Path
    rows_written: int
    partitions_written: int
    rows_total: int
    chain_head: str
    manifest_path: Optional[Path] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table, "path": str(self.path),
            "rows_written": self.rows_written,
            "partitions_written": self.partitions_written,
            "rows_total": self.rows_total,
            "chain_head": self.chain_head,
            "manifest_path": str(self.manifest_path) if self.manifest_path else None,
        }


def _iso_date(d: Any) -> str:
    return pd.Timestamp(d).date().isoformat()


def _read_partition(path: Path, columns: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """Read exactly one partition file.

    Deliberately uses ``pq.ParquetFile`` rather than ``pq.read_table``: even
    given a single file path, ``read_table`` performs *dataset* discovery from
    the parent directory and merges the hive partition keys into the schema,
    which collides with the file's own ``date32`` ``rebalance_date`` and raises
    ``ArrowTypeError: ... date32[day] vs dictionary<values=string>``.
    ``ParquetFile`` bypasses discovery entirely — no partition inference, one
    file, column projection — which is both correct and faster.
    """
    return pq.ParquetFile(str(path)).read(columns=list(columns) if columns else None).to_pandas()


def _partition_relpaths(root: Path) -> List[Tuple[Tuple[str, ...], Path]]:
    """Discover hive partitions in canonical (rebalance_date, ...) order."""
    if not root.exists():
        return []
    found: List[Tuple[Tuple[str, ...], Path]] = []
    for p in sorted(root.rglob("*.parquet")):
        rel = p.relative_to(root)
        keys: List[str] = []
        ok = True
        for part in rel.parts[:-1]:
            if "=" not in part:
                ok = False
                break
            keys.append(part.split("=", 1)[1])
        if ok and keys:
            found.append((tuple(keys), p))
    # sort by the partition key tuple (ISO dates sort chronologically)
    return sorted(found, key=lambda x: x[0])


def _claim_dataset_root(path: Path, table: str, allow_replace: bool) -> None:
    """Guard the ``.parquet`` file-vs-directory hazard called out in the brief.

    Every dataset root in this module ends in ``.parquet`` but is a *directory*.
    An earlier draft of this work wrote flat single files at those paths, which
    makes ``mkdir`` fail deep inside a write with a confusing OS error.  Detect
    it here and say exactly what is wrong and how to clear it.
    """
    if path.is_file():
        if not allow_replace:
            raise SchemaContractError(
                f"{table}: {path} exists as a FILE but this table is a hive dataset "
                f"DIRECTORY (paths ending in .parquet hold rebalance_date=/cap_bucket= "
                f"subdirectories). Move or delete the file, then re-run with "
                f"--replace to overwrite it automatically.")
        logger.warning("%s: replacing stale FILE at %s with a hive dataset directory",
                       table, path)
        path.unlink()
    elif path.is_dir():
        entries = list(path.glob("*.parquet")) + list(path.glob("*="))
        if not entries and not (path / "_manifest.json").exists():
            raise SchemaContractError(
                f"{table}: {path} exists but is empty and contains no partitions or "
                f"_manifest.json - refusing to adopt it. Remove it or use --replace.")
    path.mkdir(parents=True, exist_ok=True)


def _write_hive(root: Path, out: pd.DataFrame, schema: pa.Schema,
                partition_cols: Sequence[str], table: str,
                entity_cols: Optional[Sequence[str]] = None) -> int:
    """Write ``out`` as an explicit hive dataset; returns partitions written.

    Partitions are append-safe: if a target partition directory already holds a
    ``part-0000.parquet``, the new rows are *merged* with the existing ones
    (deduplicated on ``entity_cols``) rather than clobbering the file, so an
    incremental write of one month cannot silently destroy the other tickers
    already in that month.

    ``entity_cols`` is the table's full logical primary key and **must** be
    supplied by the caller.  Guessing it as ``partition_cols + ["ticker"]`` is
    wrong for ``fallback_tracking``, whose key is
    ``(rebalance_date, ticker, field)``: one ticker legitimately has several
    rows per month (one per fallback component).  Under the guessed key an
    append keeps only the last of them and destroys the rest.  Guessing is only
    retained as a documented fallback for the merged/usage shapes, and a caller
    typo raises rather than silently degrading to a lossy key.
    """
    entity = list(entity_cols) if entity_cols is not None else \
        [c for c in (*list(partition_cols), "ticker") if c in out.columns]
    missing = [c for c in entity if c not in out.columns]
    if missing:
        raise SchemaContractError(
            f"{table}: entity_cols {entity} not all present in frame "
            f"(missing {missing}) - refusing to guess a lossy dedupe key")
    written = 0
    for key, grp in out.groupby(list(partition_cols), sort=True):
        key = key if isinstance(key, tuple) else (key,)
        d = root
        for col, val in zip(partition_cols, key):
            d = d / f"{col}={_iso_date(val) if col.endswith('_date') else val}"
        d.mkdir(parents=True, exist_ok=True)
        target = d / "part-0000.parquet"
        frame = grp.reset_index(drop=True)
        if target.exists():
            # merge rather than clobber: an incremental write of one month must
            # not destroy the other tickers already in that month's partition.
            prior = _read_partition(target)
            frame = pd.concat([prior, frame], ignore_index=True)
            # prior comes back from date32 as datetime.date objects while the
            # incoming frame holds Timestamps -- normalize before sorting or
            # pandas cannot order the mixed column.
            for c in [c for c in frame.columns if c.endswith("_date")]:
                frame[c] = _coerce_dates(frame[c])
            frame = (frame.drop_duplicates(subset=entity, keep="last")
                          .sort_values(entity, kind="mergesort")
                          .reset_index(drop=True))
        tbl = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
        pq.write_table(tbl, str(target), compression=PARQUET_COMPRESSION)
        written += 1
    if written == 0 and len(out):
        raise SchemaContractError(f"{table}: refusing to write {len(out)} rows "
                                  f"into zero partitions")
    return written


class MergedUniverseStore:
    """Validated, chained, partitioned persistence for the merged PIT universe.

    Parameters
    ----------
    root:
        Dataset root for the universe tables (default ``data/universe``).
    backtest_root:
        Root for backtest artifacts, i.e. where ``ticker_usage_*`` lands
        (default ``data/backtests``).
    """

    def __init__(self, root: Union[str, Path] = DEFAULT_UNIVERSE_ROOT,
                 backtest_root: Union[str, Path] = DEFAULT_BACKTEST_ROOT) -> None:
        self.root = Path(root)
        self.backtest_root = Path(backtest_root)
        self.root.mkdir(parents=True, exist_ok=True)
        # NOTE: a hive dataset root, not a file — see the module docstring.
        self.merged_path = self.root / "merged_pit_universe.parquet"
        self.fallback_path = self.root / "fallback_tracking.parquet"

    # -- manifest / provenance sidecars -------------------------------------
    @staticmethod
    def _write_manifest(dataset_root: Path, table: str, schema: pa.Schema,
                        partition_cols: Sequence[str], rows: int,
                        chain_head: str, source: str,
                        source_version: str) -> Path:
        """A§8 data-contract sidecar (mirrors the §9 provenance convention)."""
        payload = schema_contract(schema, table)
        payload.update({
            "source": source,
            "source_version": source_version,
            "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
            "timestamp_ns": time_ns_now(),
            "uuid_version": UUID_VERSION,
            "timestamp_precision": TIMESTAMP_PRECISION,
            "partition_columns": list(partition_cols),
            "row_count": int(rows),
            "chain_head_sha256": chain_head,
            "transformations": [f"MergedUniverseStore.{table}"],
            "mapping_confidence": "high",
            "schema_fields": payload["fields"],
        })
        path = dataset_root / "_manifest.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # merged_pit_universe
    # ------------------------------------------------------------------
    def write_merged(self, df: pd.DataFrame, *, source_version: str = DEFAULT_SOURCE_VERSION,
                     allow_replace: bool = False) -> WriteReceipt:
        """Validate, stamp provenance, extend the chain, write hive partitions.

        Append-only by default, with three explicit guards:

        1. a stale *file* sitting at the dataset path is refused unless
           ``allow_replace`` (see :func:`_claim_dataset_root`);
        2. a ``(rebalance_date, ticker)`` pair already on disk is refused unless
           ``allow_replace``;
        3. a batch whose earliest ``rebalance_date`` sorts *before* the latest
           partition already on disk is refused, because appending it would
           reorder canonical chain order and invalidate every hash already
           written.

        ``allow_replace=True`` waives 2 and 3 for the purpose of regenerating
        rows, but waiving 3 leaves the chain unverifiable across the rewritten
        range.  Rebuilding a partial range therefore means deleting the dataset
        directory and writing the full range in one call, not passing
        ``--replace``.
        """
        if df is None or len(df) == 0:
            raise SchemaContractError("write_merged: refusing to write an empty frame")
        out = enforce_schema(df, MERGED_UNIVERSE_SCHEMA, "merged_pit_universe")
        validate_pit(out, "merged_pit_universe")
        validate_fallback_consistency(out, "merged_pit_universe")

        wacc = out["wacc"].astype(float)
        if not bool(((wacc >= WACC_BOUNDS[0]) & (wacc <= WACC_BOUNDS[1])).all()):
            raise SchemaContractError(
                f"merged_pit_universe: wacc outside frozen bounds {WACC_BOUNDS} "
                f"(min {wacc.min()}, max {wacc.max()})")

        targets = out[["rebalance_date"]].drop_duplicates()
        _claim_dataset_root(self.merged_path, "merged_pit_universe", allow_replace)
        existing_keys = self._existing_keys(self.merged_path, MERGED_PARTITION_COLS,
                                            [_iso_date(x) for x in targets["rebalance_date"]])
        # BOTH sides must be ISO-normalised: _existing_keys yields date *strings*
        # (date32 read back) while `out` holds Timestamps, so comparing them raw
        # silently yields an empty intersection and the append-only guard never
        # fires.
        incoming = {(_iso_date(d), str(t)) for d, t in
                    out[["rebalance_date", "ticker"]].itertuples(index=False, name=None)}
        dupes = existing_keys & incoming
        if dupes and not allow_replace:
            raise SchemaContractError(
                f"merged_pit_universe: refusing to rewrite {len(dupes)} existing "
                f"(rebalance_date, ticker) row(s), e.g. {sorted(dupes)[:5]}")

        # Chain safety: appending a partition that sorts *before* the current
        # chain head would reorder canonical chain order and invalidate every
        # hash already written.  Refuse instead of writing an unverifiable chain.
        existing_dates = self._existing_dates(self.merged_path, MERGED_PARTITION_COLS)
        if existing_dates and len(stamped_dates := sorted({_iso_date(x) for x in out["rebalance_date"]})):
            if stamped_dates[0] < max(existing_dates) and not allow_replace:
                raise SchemaContractError(
                    f"merged_pit_universe: refusing to append rebalance_date="
                    f"{stamped_dates[0]} to a dataset whose latest partition is "
                    f"{max(existing_dates)} — that would break the SHA chain order. "
                    f"Rebuild the full range (delete the dataset) or pass --replace.")
        prev = self._chain_head(self.merged_path, MERGED_PARTITION_COLS)
        stamped, chain_head = assign_provenance(out, prev_chain_hash=prev,
                                                source=DATASET_SOURCE,
                                                source_version=source_version)
        n_part = _write_hive(self.merged_path, stamped, MERGED_UNIVERSE_SCHEMA,
                             MERGED_PARTITION_COLS, "merged_pit_universe",
                             entity_cols=["rebalance_date", "cap_bucket", "ticker"])
        rows_total = len(stamped) + max(0, len(existing_keys) - len(dupes))
        manifest = self._write_manifest(
            self.merged_path, "merged_pit_universe", MERGED_UNIVERSE_SCHEMA,
            MERGED_PARTITION_COLS, rows_total, chain_head, DATASET_SOURCE, source_version)
        return WriteReceipt("merged_pit_universe", self.merged_path, len(stamped),
                            n_part, rows_total, chain_head, manifest)

    def read_merged(self, start: Union[str, _date, pd.Timestamp],
                    end: Union[str, _date, pd.Timestamp],
                    columns: Optional[Sequence[str]] = None,
                    tickers: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Read the merged universe over ``[start, end]`` with predicate pushdown.

        Pushdown is real, not cosmetic: DuckDB prunes on the
        ``rebalance_date=`` directory *before* opening a file.  Falls back to a
        path-pruned pyarrow read if DuckDB is unavailable.
        """
        lo, hi = _iso_date(start), _iso_date(end)
        if lo > hi:
            raise ValueError(f"read_merged: start {lo} > end {hi}")
        cols = list(columns) if columns else [f.name for f in MERGED_UNIVERSE_SCHEMA]
        if not self.merged_path.exists():
            return pd.DataFrame({c: [] for c in cols})

        df = self._duckdb_read(self.merged_path, cols, lo, hi, tickers)
        if df is None:  # pragma: no cover — DuckDB is pinned
            df = self._pyarrow_read(self.merged_path, cols, lo, hi, tickers)
        df["rebalance_date"] = _coerce_dates(df["rebalance_date"])
        return _canonical_row_order(df) if "cap_bucket" in df.columns else df

    def fallback_distribution(self, start: Optional[Any] = None,
                              end: Optional[Any] = None) -> pd.DataFrame:
        """Tier histogram per tracked field — the headline health metric."""
        df = self.read_merged(start if start is not None else "0001-01-01",
                              end if end is not None else "9999-12-31")
        rows: List[Dict[str, Any]] = []
        for field in TRACKED_FIELDS:
            tcol = f"{field}_fallback_tier"
            if tcol not in df:
                continue
            counts = df[tcol].value_counts().to_dict()
            n = max(1, len(df))
            rows.append({"field": field, "n": int(len(df)),
                         **{f"tier_{t}": int(counts.get(t, 0)) for t in FALLBACK_TIERS},
                         "tier_0_1_frac": float((int(counts.get(0, 0)) + int(counts.get(1, 0))) / n),
                         "tier_3_frac": float(int(counts.get(3, 0)) / n)})
        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # fallback_tracking
    # ------------------------------------------------------------------
    def write_fallback_tracking(self, df: pd.DataFrame, *,
                                source_version: str = DEFAULT_SOURCE_VERSION) -> WriteReceipt:
        """Persist the fallback audit log (one row per tracked field event).

        The frozen schema carries a provenance triple on every row, but the
        producer cannot know those values yet: they are assigned to the merged
        table by :meth:`write_merged`, and a fallback event is only *caused by*
        a merged row.  So when the incoming frame lacks the triple we adopt the
        provenance of the merged row it points at (matching on
        ``(rebalance_date, ticker)``), which is the only defensible lineage: it
        makes a fallback event hash-linked to the very row whose value it
        explains.  Events with no matching merged row are a hard error rather
        than a silent orphan.

        ``fallback_tracking`` is not part of the merged SHA chain — it inherits
        the merged chain's ``sha256`` for cross-referencing rather than
        extending it, so its receipt reports the inherited head.
        """
        if df is None or len(df) == 0:
            return WriteReceipt("fallback_tracking", self.fallback_path, 0, 0, 0, GENESIS)
        _claim_dataset_root(self.fallback_path, "fallback_tracking", allow_replace=False)
        out = self._adopt_merged_provenance(df.copy())
        out = enforce_schema(out, FALLBACK_TRACKING_SCHEMA, "fallback_tracking")
        validate_pit(out, "fallback_tracking")
        if "field" in out:
            bad = sorted(set(out["field"].dropna().astype(str))
                         - set(TRACKED_FIELDS) - set(COMPONENT_FIELDS))
            if bad:
                raise SchemaContractError(
                    f"fallback_tracking: untracked field name(s) {bad}; expected a "
                    f"subset of {list(TRACKED_FIELDS) + list(COMPONENT_FIELDS)}")
        out = _canonical_row_order(out) if "cap_bucket" in out.columns else \
            out.sort_values(["rebalance_date", "ticker", "field"], kind="mergesort").reset_index(drop=True)
        out = out.drop_duplicates(subset=["ticker", "rebalance_date", "field"], keep="last")
        head = self._chain_head(self.merged_path, MERGED_PARTITION_COLS)
        n_part = _write_hive(self.fallback_path, out, FALLBACK_TRACKING_SCHEMA,
                             FALLBACK_PARTITION_COLS, "fallback_tracking",
                             entity_cols=["rebalance_date", "ticker", "field"])
        manifest = self._write_manifest(
            self.fallback_path, "fallback_tracking", FALLBACK_TRACKING_SCHEMA,
            FALLBACK_PARTITION_COLS, len(out), head, DATASET_SOURCE, source_version)
        return WriteReceipt("fallback_tracking", self.fallback_path, len(out),
                            n_part, len(out), head, manifest)

    def _adopt_merged_provenance(self, out: pd.DataFrame) -> pd.DataFrame:
        """Fill the fallback log's provenance triple from the merged table.

        Reads the merged chain's own `sha256`/`uuid`/`timestamp_ns` and
        re-emits them under the fallback schema's `provenance_*` names, so a
        fallback event is traceable to the exact merged row that caused it and
        to that row's position in the chain. Orphan events are a hard error,
        never a silently unlinked row.
        """
        want = list(PROVENANCE_NAME_MAP.values())
        if all(c in out.columns and not out[c].isna().all() for c in want):
            return out
        if not self.merged_path.exists():
            raise SchemaContractError(
                f"fallback_tracking: needs the provenance triple {want} but "
                f"{self.merged_path} does not exist - call write_merged() first so "
                f"fallback events can be hash-linked to the merged row that caused them.")
        merged = self.read_merged("0001-01-01", "9999-12-31",
                                  columns=["rebalance_date", "ticker", *PROVENANCE_COLUMNS])
        merged = merged.drop_duplicates(subset=["rebalance_date", "ticker"], keep="last")
        merged = merged.rename(columns={k: f"_m_{k}" for k in PROVENANCE_COLUMNS})
        merged["rebalance_date"] = merged["rebalance_date"].map(_iso_date)
        out = out.copy()
        out["rebalance_date"] = out["rebalance_date"].map(_iso_date)
        out = out.merge(merged, on=["rebalance_date", "ticker"], how="left")
        src_cols = [f"_m_{k}" for k in PROVENANCE_COLUMNS]
        orphan = out[src_cols].isna().all(axis=1)
        if int(orphan.sum()):
            keep = [c for c in ("ticker", "rebalance_date", "field") if c in out.columns]
            head = out.loc[orphan, keep].head(5)
            raise SchemaContractError(
                f"fallback_tracking: {int(orphan.sum())} event(s) reference a "
                f"(rebalance_date, ticker) absent from merged_pit_universe, e.g. "
                f"{head.to_dict('records')}")
        for merged_name, fb_name in PROVENANCE_NAME_MAP.items():
            out[fb_name] = out[f"_m_{merged_name}"]
        out = out.drop(columns=src_cols)
        out["rebalance_date"] = pd.to_datetime(out["rebalance_date"])
        return out

    def read_fallback_tracking(self, start: Optional[Any] = None,
                               end: Optional[Any] = None,
                               fields: Optional[Sequence[str]] = None) -> pd.DataFrame:
        if not self.fallback_path.exists():
            return pd.DataFrame({f.name: [] for f in FALLBACK_TRACKING_SCHEMA})
        lo = _iso_date(start) if start is not None else "0001-01-01"
        hi = _iso_date(end) if end is not None else "9999-12-31"
        df = self._duckdb_read(self.fallback_path, [f.name for f in FALLBACK_TRACKING_SCHEMA],
                               lo, hi, None)
        if df is None:  # pragma: no cover
            df = self._pyarrow_read(self.fallback_path,
                                    [f.name for f in FALLBACK_TRACKING_SCHEMA], lo, hi, None)
        df["rebalance_date"] = _coerce_dates(df["rebalance_date"])
        if "filed_date" in df:
            df["filed_date"] = _coerce_dates(df["filed_date"])
        if fields:
            df = df[df["field"].astype(str).isin(list(fields))]
        return df.sort_values(["rebalance_date", "ticker", "field"],
                              kind="mergesort").reset_index(drop=True)

    # ------------------------------------------------------------------
    # ticker_usage
    # ------------------------------------------------------------------
    def usage_path(self, arm: str, window: str) -> Path:
        """``data/backtests/ticker_usage_{arm}_{window}.parquet`` (hive dataset)."""
        a = str(arm).strip().lower()
        w = str(window).strip().lower()
        if not a or not w or "/" in a or "/" in w or "\\" in a or "\\" in w:
            raise ValueError(f"invalid arm/window for usage path: {arm!r}/{window!r}")
        return self.backtest_root / f"ticker_usage_{a}_{w}.parquet"

    def write_ticker_usage(self, arm: str, window: str, df: pd.DataFrame,
                           *, allow_replace: bool = False) -> WriteReceipt:
        """Persist per-rebalance ticker usage + fallback lineage for one arm/window.

        The frozen schema requires the provenance triple, and this table is
        derived from the merged universe rather than from raw filings, so the
        honest lineage is the merged row's chain hash: each usage row adopts
        the ``sha256``/``uuid``/``timestamp_ns`` of the
        ``(rebalance_date, ticker)`` merged row it selected, which is exactly
        what makes an Arm's selection auditable back to the input row. The
        column names differ again here (``provenance_*``), reusing the same
        single mapping as the fallback log.

        ``ticker_usage`` is not part of the merged SHA chain; it references it.
        """
        path = self.usage_path(arm, window)
        if df is None or len(df) == 0:
            return WriteReceipt("ticker_usage", path, 0, 0, 0, GENESIS)
        _claim_dataset_root(path, f"ticker_usage[{arm}/{window}]", allow_replace)
        a = str(arm).strip().lower()
        out = df.copy()
        out["arm"] = a
        out["any_fallback"] = (out[["wacc_fallback_tier", "icr_fallback_tier",
                                    "sector_fallback_tier"]] > TIER_FILED).any(axis=1)
        out = self._adopt_merged_provenance(out)
        out = enforce_schema(out, TICKER_USAGE_SCHEMA, "ticker_usage")
        out = out.drop_duplicates(subset=["rebalance_date", "arm", "ticker"], keep="last")
        out = out.sort_values(["rebalance_date", "arm", "ticker"],
                              kind="mergesort").reset_index(drop=True)
        head = self._chain_head(self.merged_path, MERGED_PARTITION_COLS)
        n_part = _write_hive(path, out, TICKER_USAGE_SCHEMA,
                             USAGE_PARTITION_COLS, "ticker_usage",
                             entity_cols=["rebalance_date", "arm", "ticker"])
        manifest = self._write_manifest(
            path, "ticker_usage", TICKER_USAGE_SCHEMA, USAGE_PARTITION_COLS,
            len(out), head, DATASET_SOURCE,
            f"{DEFAULT_SOURCE_VERSION}+three_arm_runner")
        return WriteReceipt("ticker_usage", path, len(out), n_part, len(out),
                            head, manifest)

    def read_ticker_usage(self, arm: str, window: str) -> pd.DataFrame:
        """Read one arm/window's ticker usage, tolerating both on-disk layouts.

        **Layout compatibility (deliberate).** Workstream A's ``three_arm_runner``
        writes ``ticker_usage_{arm}_{window}.parquet`` as a *flat single file*
        per monthly window -- 233 such files already exist in
        ``data/backtests/``.  The brief asks this table to be partitioned by
        ``rebalance_date``/``arm``, which only adds value when one dataset spans
        many months.  Rather than invalidate 233 existing artifacts, the reader
        accepts either layout:

        * flat file at the path  -> read directly;
        * hive dataset directory -> read partitions in range.

        The *writer* still refuses to silently replace a flat file with a
        directory (see ``_claim_dataset_root``), so the migration decision stays
        explicit and visible instead of being made by accident.
        """
        path = self.usage_path(arm, window)
        cols = [f.name for f in TICKER_USAGE_SCHEMA]
        if path.is_file():
            df = _read_partition(path, cols)
        elif path.exists():
            df = self._duckdb_read(path, cols, "0001-01-01", "9999-12-31", None)
            if df is None:  # pragma: no cover
                df = self._pyarrow_read(path, cols, "0001-01-01", "9999-12-31", None)
        else:
            return pd.DataFrame({c: [] for c in cols})
        df["rebalance_date"] = _coerce_dates(df["rebalance_date"])
        if "arm" in df.columns:
            df["arm"] = df["arm"].astype(str).str.strip().str.lower()
        return df.sort_values(["rebalance_date", "ticker"], kind="mergesort").reset_index(drop=True)

    # ------------------------------------------------------------------
    # provenance verification (forensic — stored hashes are never trusted)
    # ------------------------------------------------------------------
    def verify_merged_chain(self, *, verify_hashes: bool = True,
                            recompute_sources: bool = False,
                            source_version: Optional[str] = None) -> Dict[str, Any]:
        """Recompute every row hash and re-walk the chain across partitions.

        ``recompute_sources=True`` additionally rebuilds the provenance triple
        from the stored non-provenance columns at the stored ``timestamp_ns``,
        proving the ``sha256``/``uuid`` triple was derived from the data rather
        than copied alongside it.
        """
        parts = _partition_relpaths(self.merged_path)
        if not parts:
            return {"intact": True, "rows": 0, "partitions": 0,
                    "first_break": None, "chain_head": GENESIS}
        prev = GENESIS
        n_rows = 0
        n_break: Optional[int] = None
        for keys, p in parts:
            df = _read_partition(p)
            for rec in df.to_dict("records"):
                n_rows += 1
                if n_break is not None:
                    continue
                payload = canonical_row_payload(rec)
                record_hash, chain_hash = compute_row_sha256(payload, prev)
                if verify_hashes and chain_hash != rec.get("sha256"):
                    n_break = n_rows
                    logger.error("chain break at row %d (%s): stored=%s recomputed=%s",
                                 n_rows, p, rec.get("sha256"), chain_hash)
                    break
                if recompute_sources:
                    ns = int(rec["timestamp_ns"])
                    if rec.get("uuid") != make_uuid7(ns, seed=canonical_json([payload, chain_hash])):
                        n_break = n_rows
                        logger.error("uuid provenance break at row %d (%s)", n_rows, p)
                        break
                prev = chain_hash
        return {
            "intact": n_break is None,
            "rows": n_rows,
            "partitions": len(parts),
            "first_break": n_break,
            "chain_head": prev,
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _existing_keys(root: Path, partition_cols: Sequence[str],
                       wanted_dates: Sequence[str]) -> set:
        """(rebalance_date ISO string, ticker) keys already on disk.

        Dates are returned as ISO strings to match the partition directory
        naming; callers must normalize their own side the same way.
        """
        wanted = set(wanted_dates)
        keys: set = set()
        for pkeys, p in _partition_relpaths(root):
            if _iso_date(pkeys[0]) not in wanted:
                continue
            df = _read_partition(p, ["rebalance_date", "ticker"])
            keys |= {(_iso_date(d), str(t)) for d, t in
                     df[["rebalance_date", "ticker"]].itertuples(index=False, name=None)}
        return keys

    @staticmethod
    def _existing_dates(root: Path, partition_cols: Sequence[str]) -> set:
        """ISO ``rebalance_date`` partition values already on disk."""
        if not root.exists():
            return set()
        return {_iso_date(k[0]) for k, _ in _partition_relpaths(root)}

    @staticmethod
    def _chain_head(root: Path, partition_cols: Sequence[str]) -> str:
        """Last ``sha256`` in canonical partition order (disk-first)."""
        parts = _partition_relpaths(root)
        if not parts:
            return GENESIS
        df = _read_partition(parts[-1][1], ["sha256"])
        return str(df["sha256"].iloc[-1]) if len(df) else GENESIS

    @staticmethod
    def _duckdb_read(root: Path, columns: Sequence[str], lo: str, hi: str,
                     tickers: Optional[Sequence[str]]) -> Optional[pd.DataFrame]:
        """Primary read path: DuckDB over an explicitly partition-pruned file list.

        Two non-obvious constraints drove this shape:

        1. **No bound parameters.** DuckDB's prepared-statement parser rejects a
           parameter inside ``read_parquet(...)`` and does not accept the
           ``DATE ?`` cast form ("syntax error at or near \\"?\\""), which
           silently degraded every read to the pyarrow fallback. Values are
           inlined as escaped SQL literals instead (standard SQL doubling; the
           inputs are internally generated ISO dates, fixed column names and
           exchange tickers).

        2. **No ``hive_partitioning=true``.** The frozen schemas deliberately
           keep ``rebalance_date``/``cap_bucket``/``arm`` *inside* the file as
           well as in the directory path. Hive inference then collides with the
           file's own columns: ``date32`` vs the partition string raises in
           pyarrow, and for ``arm`` DuckDB silently returns NULL, quietly
           dropping a column. So partitioning is pruned by us -- we list only
           the ``rebalance_date=`` directories in range and hand DuckDB that
           explicit file list, which is real pruning *and* leaves no column
           ambiguity.
        """
        if _duckdb is None or not root.exists():  # pragma: no cover
            return None

        def lit(v: str) -> str:
            return "'" + str(v).replace("'", "''") + "'"

        parts = [(k, p) for k, p in _partition_relpaths(root) if lo <= k[0] <= hi]
        if not parts:
            return None
        file_list = ", ".join(lit(str(p).replace("\\", "/")) for _, p in parts)
        where = [f"rebalance_date BETWEEN DATE {lit(lo)} AND DATE {lit(hi)}"]
        if tickers:
            in_list = ", ".join(lit(t) for t in tickers)
            where.append(f"ticker IN ({in_list})")
        sql = (f"SELECT {', '.join(columns)} FROM "
               f"read_parquet([{file_list}]) "
               f"WHERE {' AND '.join(where)}")
        try:
            rel = _duckdb.execute(sql)
            df = rel.df()
            if len(df):
                # Timestamps, not .dt.date -- so this agrees with _pyarrow_read.
                # read_merged normalizes anyway, but a caller reaching for these
                # helpers directly should not have to care which engine ran.
                df["rebalance_date"] = _coerce_dates(df["rebalance_date"])
            return df
        except Exception as exc:  # noqa: BLE001
            logger.warning("duckdb read failed (%s); falling back to pyarrow", exc)
            return None

    @staticmethod
    def _pyarrow_read(root: Path, columns: Sequence[str], lo: str, hi: str,
                      tickers: Optional[Sequence[str]]) -> pd.DataFrame:
        """Path-pruned read (no hive partitioning — it collides on date32)."""
        parts = [(k, p) for k, p in _partition_relpaths(root) if lo <= k[0] <= hi]
        if not parts:
            return pd.DataFrame({c: [] for c in columns})
        df = pd.concat([_read_partition(p, columns) for _, p in parts],
                       ignore_index=True)
        df["rebalance_date"] = _coerce_dates(df["rebalance_date"])
        if tickers:
            df = df[df["ticker"].astype(str).isin(list(tickers))]
        return df[df["rebalance_date"].dt.strftime("%Y-%m-%d").between(lo, hi)]


# ===========================================================================
# 6. REFERENCE PRODUCER — PIT fallback ladder
# ===========================================================================
def normalize_cap_bucket(raw: Any, market_cap: Optional[float] = None) -> str:
    """Reconstitution ``Mega``/``Large`` -> the frozen ``large``/``mid``/``micro``."""
    key = str(raw).strip().lower()
    if key in CAP_BUCKET_ALIASES:
        return CAP_BUCKET_ALIASES[key]
    mc = float(market_cap) if market_cap is not None and not _is_missing(market_cap) else np.nan
    if np.isnan(mc):
        return "mid"
    if mc >= 2e11:
        return "large"
    if mc >= 2e9:
        return "mid"
    return "micro"


def _sector_median_broadcast(values: pd.Series, sectors: pd.Series,
                             eligible: np.ndarray, min_n: int) -> np.ndarray:
    """Same-rebalance cross-sectional median by ``sector_custom``.

    PIT-safe by construction: only rows in ``eligible`` (tier 0/1) of the SAME
    ``rebalance_date`` feed the median.  A ticker with no eligible peer keeps
    NaN and falls to the frozen constant (tier 3).
    """
    out = np.full(len(values), np.nan, dtype=float)
    if not eligible.any():
        return out
    tmp = pd.DataFrame({"v": np.where(eligible, values.astype(float), np.nan),
                        "s": sectors.astype(str)})
    g = tmp.groupby("s", observed=True)["v"].agg(["median", "count"])
    med = g["median"].where(g["count"] >= min_n)
    return sectors.astype(str).map(med).to_numpy(dtype=float)


def _classify_tiers(present: np.ndarray, staleness: np.ndarray) -> np.ndarray:
    """tier 0 = fresh filing, tier 1 = stale-but-valid carry, tier 3 = none."""
    tier = np.full(len(present), TIER_CONSTANT, dtype=np.int8)
    p = np.asarray(present, dtype=bool)
    s = np.asarray(staleness, dtype=float)
    usable = p & np.isfinite(s) & (s <= MAX_STALE_DAYS)
    tier[usable & (s <= FRESH_DAYS)] = TIER_FILED
    tier[usable & (s > FRESH_DAYS)] = TIER_FORWARD
    return tier


def _lift_tier3_to_sector_median(values: np.ndarray, tier: np.ndarray,
                                 sectors: pd.Series, min_n: int) -> np.ndarray:
    """tier 3 -> tier 2 when an admissible same-rebalance sector median exists."""
    v = pd.Series(values)
    med = _sector_median_broadcast(v, sectors, eligible=(tier <= TIER_FORWARD), min_n=min_n)
    lifted = (tier == TIER_CONSTANT) & np.isfinite(med)
    out_tier = tier.copy()
    out_tier[lifted] = TIER_SECTOR_MEDIAN
    out_val = v.to_numpy(dtype=float).copy()
    out_val[lifted] = med[lifted]
    return out_val, out_tier


def _clamp(values: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return np.clip(values, lo, hi)


def build_merged_pit_universe(
    reconstitution_path: Union[str, Path] = Path("data/universe/monthly_reconstitution.parquet"),
    fundamentals_path: Union[str, Path] = Path("data/pit_fundamentals/valuation_fundamentals.parquet"),
    start: Optional[Any] = None,
    end: Optional[Any] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Merge reconstitution v2 with PIT fundamentals under the frozen ladder.

    Returns ``(merged, fallback_tracking)`` — frames that satisfy all three
    Arrow contracts, ready for
    :meth:`MergedUniverseStore.write_merged` /
    :meth:`MergedUniverseStore.write_fallback_tracking`.

    WACC definition (frozen, documented so it can be audited):

        D_eff = total_debt                      if 0 <= total_debt
              = debt_to_equity * market_cap    if total_debt absent and D/E valid
              = 0.0                             otherwise  (no reported debt)
        wd    = min(D_eff / (D_eff + E), MAX_DEBT_WEIGHT)      E = market_cap (PIT)
        we    = 1 - wd
        kd    = cost_of_debt if KD_BOUNDS      else sector-median kd (same month) else None
        tau   = income_tax_expense / ebit      if TAX_BOUNDS     else TAX_FALLBACK
        wacc  = we*ke + wd*kd*(1 - tau)

    A firm with no reported debt has ``wd == 0`` and ``WACC == ke``.  That is a
    *determination*, not an imputation — the cascade that borrows a sector
    median (kd) or a D/E ratio is logged per row in ``fallback_tracking`` under
    ``wacc.cost_of_debt`` / ``wacc.capital_structure``.
    """
    recon = pd.read_parquet(reconstitution_path)
    recon["rebalance_date"] = _coerce_dates(recon["rebalance_date"])
    if start is not None:
        recon = recon[recon["rebalance_date"] >= pd.Timestamp(start)]
    if end is not None:
        recon = recon[recon["rebalance_date"] <= pd.Timestamp(end)]
    if recon.empty:
        raise ValueError("build_merged_pit_universe: empty reconstitution frame")
    anchor_max = recon["rebalance_date"].max()

    # ---- PIT gate: nothing filed after the last rebalance can be used -------
    fund = pd.read_parquet(fundamentals_path)
    fund["filed_date"] = _coerce_dates(fund["filed_date"])
    fund = fund[fund["filed_date"].notna() & (fund["filed_date"] <= anchor_max)]
    fund = fund.sort_values(["filed_date", "ticker"], kind="mergesort").reset_index(drop=True)

    left = recon.sort_values(["rebalance_date", "ticker"], kind="mergesort").reset_index(drop=True)
    n = len(left)
    sectors = (left["sector_custom"].astype(str) if "sector_custom" in left
               else left["sector"].map(_map_to_custom_sector).astype(str))

    # -----------------------------------------------------------------
    # FIELD-AWARE PIT ASOF — this is what "forward-filled to rebalance_date"
    # actually means.  For every field we do an INDEPENDENT asof join on
    # the filings that CARRY that field, so a value missing from last
    # quarter's 10-Q still resolves to the last filing that reported it --
    # inside the staleness budget.  Reading only the newest filing (the
    # naive join) discards that history and collapses to the sector median.
    #
    # Measured effect: filed-lineage coverage rises from ~26%/34%/48%
    # (naive newest-filing-only, 120/365/730-day windows) to 64.3% for
    # WACC.  That is the *ceiling* -- validation recomputes the same asof
    # against the raw corpus and confirms achieved == ceiling for every
    # tracked field.  The residual ~36% of the universe has no PIT
    # fundamentals at all, which no join strategy can recover.
    # -----------------------------------------------------------------
    _FIELD_ASOF: Dict[str, Tuple[np.ndarray, np.ndarray, np.ndarray]] = {}

    def field_asof(col: str) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """-> (value, filed_date, staleness_days) for the newest PIT filing
        that carries ``col``, searching back ``MAX_STALE_DAYS`` from the
        rebalance.  Never reads a filing dated after the rebalance.
        """
        if col not in fund.columns:
            empty = np.full(n, np.nan)
            return empty, np.full(n, np.datetime64("NaT"), dtype="datetime64[ns]"), empty
        sub = (fund[fund[col].notna()]
               .loc[:, ["ticker", "filed_date", col]]
               .drop_duplicates(["ticker", "filed_date"], keep="last"))
        if sub.empty:
            empty = np.full(n, np.nan)
            return empty, np.full(n, np.datetime64("NaT"), dtype="datetime64[ns]"), empty
        m = pd.merge_asof(
            left[["ticker", "rebalance_date"]], sub,
            left_on="rebalance_date", right_on="filed_date", by="ticker",
            direction="backward", allow_exact_matches=True,
        )
        value = pd.to_numeric(m[col], errors="coerce").astype(float).to_numpy()
        filed = m["filed_date"].to_numpy(dtype="datetime64[ns]")
        stale = (m["rebalance_date"] - m["filed_date"]).dt.days.to_numpy(dtype=float)
        _FIELD_ASOF[col] = (value, filed, stale)
        return value, filed, stale

    # ---- identity / universe metadata --------------------------------------
    out = pd.DataFrame(index=range(n))
    out["ticker"] = left["ticker"].astype(str).to_numpy()
    out["cik"] = left["cik"].astype(str).to_numpy()
    out["rebalance_date"] = left["rebalance_date"].to_numpy()
    out["market_cap"] = pd.to_numeric(left["market_cap"], errors="coerce").astype(float).to_numpy()
    out["cap_bucket"] = [
        normalize_cap_bucket(b, mc) for b, mc in
        zip(left["cap_bucket"] if "cap_bucket" in left else pd.Series(["mid"] * n),
            out["market_cap"])
    ]
    out["sector_gics"] = left["sector_gics"].to_numpy() if "sector_gics" in left.columns else None
    out["sector_custom"] = sectors.to_numpy()
    out["sector_source"] = SECTOR_TIER_SOURCE[TIER_FILED]
    out["sector_fallback_tier"] = np.int8(TIER_FILED)

    # ---- forward-filled fundamentals ----------------------------------------
    roic, roic_filed, roic_stale = field_asof("roic")
    fcf_margin, _, _ = field_asof("fcf_margin")
    wacc_estimate, _, _ = field_asof("wacc_estimate")
    ke, ke_filed, ke_stale = field_asof("cost_of_equity_est")
    ebit, ebit_filed, ebit_stale = field_asof("ebit")
    ie, ie_filed, ie_stale = field_asof("interest_expense")
    kd, kd_filed, kd_stale = field_asof("cost_of_debt")
    total_debt, td_filed, td_stale = field_asof("total_debt")
    debt_to_equity, de_filed, de_stale = field_asof("debt_to_equity")
    tax, _, _ = field_asof("income_tax_expense")

    out["fcf_margin"] = fcf_margin
    out["wacc_estimate"] = wacc_estimate
    out["cost_of_equity_est"] = ke
    out["ebit"] = ebit
    out["interest_expense"] = ie

    mcap = out["market_cap"].to_numpy()

    # =======================================================================
    # WACC  — see the frozen formula in this function's docstring
    # =======================================================================
    ke_ok = np.isfinite(ke) & (ke > KE_BOUNDS[0]) & (ke <= KE_BOUNDS[1]) & (ke_stale <= MAX_STALE_DAYS)

    # -- capital structure cascade (total_debt -> D/E -> no reported debt) --
    d_from_tag = np.isfinite(total_debt) & (total_debt >= 0) & (td_stale <= MAX_STALE_DAYS)
    d_from_ratio = ((~d_from_tag) & np.isfinite(debt_to_equity) & (debt_to_equity >= 0)
                    & np.isfinite(mcap) & (mcap > 0) & (de_stale <= MAX_STALE_DAYS))
    d_eff = np.where(d_from_tag, total_debt, 0.0)
    d_eff = np.where(d_from_ratio, debt_to_equity * mcap, d_eff)
    d_eff = np.where(np.isfinite(d_eff) & (d_eff >= 0), d_eff, 0.0)

    tot = d_eff + mcap
    wd = np.where(np.isfinite(tot) & (tot > 0), d_eff / tot, 0.0)
    wd = _clamp(wd, 0.0, MAX_DEBT_WEIGHT)
    we = 1.0 - wd

    # -- cost of debt cascade (sector median only when there IS debt to price)
    kd_ok = np.isfinite(kd) & (kd > KD_BOUNDS[0]) & (kd <= KD_BOUNDS[1]) & (kd_stale <= MAX_STALE_DAYS)
    needs_kd = (wd > 0) & (~kd_ok)
    kd_med = _sector_median_broadcast(
        pd.Series(np.where(kd_ok, kd, np.nan)), pd.Series(sectors),
        eligible=kd_ok, min_n=SECTOR_MEDIAN_MIN_N)
    kd_borrowed = needs_kd & np.isfinite(kd_med)
    kd_final = np.where(kd_borrowed, kd_med, np.where(kd_ok, kd, 0.0))

    tau = np.where(np.isfinite(ebit) & (ebit > 0) & np.isfinite(tax),
                   tax / np.where(ebit > 0, ebit, 1.0), np.nan)
    tau_ok = np.isfinite(tau) & (tau >= TAX_BOUNDS[0]) & (tau <= TAX_BOUNDS[1])
    tau_final = np.where(tau_ok, tau, TAX_FALLBACK)

    wacc_calc = we * np.where(ke_ok, ke, np.nan) + wd * kd_final * (1 - tau_final)
    # Tier anchors on the equity-cost input — the driver of the number.  The
    # debt-side cascades are determinations or logged borrows, not staleness of
    # the WACC itself, and are audited in fallback_tracking instead.
    wacc_tier = _classify_tiers(ke_ok & np.isfinite(wacc_calc), ke_stale)
    wacc_val, wacc_tier = _lift_tier3_to_sector_median(
        wacc_calc, wacc_tier, pd.Series(sectors), SECTOR_MEDIAN_MIN_N)
    wacc_t3 = (wacc_tier == TIER_CONSTANT)
    wacc_val = np.where(wacc_t3, CONSTANT_FALLBACK_WACC, wacc_val)
    out["wacc"] = _clamp(np.asarray(wacc_val, dtype=float), *WACC_BOUNDS)
    out["wacc_fallback_tier"] = wacc_tier
    out["wacc_filed_date"] = np.where(wacc_tier <= TIER_FORWARD, ke_filed, np.datetime64("NaT"))
    out["wacc_source"] = [TIER_SOURCE[int(t)] for t in wacc_tier]

    # =======================================================================
    # ICR  (ebit / interest_expense)
    # Both legs are required to form the ratio, so the tier anchors on the
    # OLDER of the two inputs — a ratio is only as fresh as its stalest leg.
    # =======================================================================
    ie_ok = np.isfinite(ie) & (ie > 0) & (ie_stale <= MAX_STALE_DAYS)
    ebit_ok = np.isfinite(ebit) & (ebit_stale <= MAX_STALE_DAYS)
    icr_calc = np.where(ie_ok & ebit_ok, ebit / np.where(ie_ok, ie, 1.0), np.nan)
    icr_anchor_stale = np.fmax(
        np.where(ebit_ok, ebit_stale, np.nan), np.where(ie_ok, ie_stale, np.nan))
    icr_tier = _classify_tiers(np.isfinite(icr_calc), icr_anchor_stale)
    icr_val, icr_tier = _lift_tier3_to_sector_median(
        icr_calc, icr_tier, pd.Series(sectors), SECTOR_MEDIAN_MIN_N)
    icr_t3 = (icr_tier == TIER_CONSTANT)
    icr_val = np.where(icr_t3, CONSTANT_FALLBACK_ICR, icr_val)
    out["icr"] = np.where(np.isfinite(icr_val), _clamp(icr_val, *ICR_BOUNDS), np.nan)
    out["icr_fallback_tier"] = icr_tier
    icr_anchor_filed = np.minimum(
        np.where(ebit_ok, ebit_filed, np.datetime64("NaT")),
        np.where(ie_ok, ie_filed, np.datetime64("NaT")))
    out["icr_filed_date"] = np.where(icr_tier <= TIER_FORWARD, icr_anchor_filed,
                                     np.datetime64("NaT"))
    out["icr_source"] = [TIER_SOURCE[int(t)] for t in icr_tier]

    # =======================================================================
    # ROIC
    # =======================================================================
    roic_present = np.isfinite(roic) & (roic_stale <= MAX_STALE_DAYS)
    roic_tier = _classify_tiers(roic_present, roic_stale)
    roic_val, roic_tier = _lift_tier3_to_sector_median(
        roic, roic_tier, pd.Series(sectors), SECTOR_MEDIAN_MIN_N)
    roic_t3 = (roic_tier == TIER_CONSTANT)
    roic_val = np.where(roic_t3, CONSTANT_FALLBACK_ROIC, roic_val)
    out["roic"] = np.where(np.isfinite(roic_val), _clamp(roic_val, *ROIC_BOUNDS), np.nan)
    out["roic_fallback_tier"] = roic_tier
    out["roic_filed_date"] = np.where(roic_tier <= TIER_FORWARD, roic_filed,
                                      np.datetime64("NaT"))
    out["roic_source"] = [TIER_SOURCE[int(t)] for t in roic_tier]

    out["roic_wacc_spread"] = np.where(np.isfinite(out["roic"].to_numpy()),
                                       out["roic"].to_numpy() - out["wacc"].to_numpy(), np.nan)

    # =======================================================================
    # provenance placeholders (stamped by the store)
    # =======================================================================
    out["source"] = DATASET_SOURCE
    out["source_version"] = DEFAULT_SOURCE_VERSION
    out["retrieval_timestamp"] = pd.Timestamp(time_ns_now(), unit="ns", tz="UTC").tz_localize(None)
    out["timestamp_ns"] = np.int64(0)
    out["sha256"] = ""
    out["uuid"] = ""

    merged = enforce_schema(out, MERGED_UNIVERSE_SCHEMA, "merged_pit_universe")

    # =======================================================================
    # fallback_tracking — top-level fields + component cascades
    #
    # One row per (ticker, rebalance_date, field) that gave up lineage.  Top-
    # level fields log when tier > 0; component cascades log whenever they
    # fire, because a sector-median kd can hide inside a tier-0 WACC row and
    # that is precisely the kind of borrowing a fallback audit exists to find.
    # =======================================================================
    idx = merged.index
    fb_frames: List[pd.DataFrame] = []

    def _as_series(arg: Any, mask: np.ndarray, dtype: Any) -> pd.Series:
        """Broadcast-or-select a per-row argument onto the masked subset.

        ``arg`` may be a scalar (broadcast), or a length-n array (selected by
        ``mask``).  Each argument is normalised independently — coupling them
        to one another's shape is how a mixed scalar/array call silently
        produces a list-of-arrays.
        """
        k = int(mask.sum())
        if arg is None or isinstance(arg, str):
            return pd.Series([arg] * k, index=idx[mask], dtype=dtype)
        arr = np.asarray(arg)
        if arr.ndim == 0:
            return pd.Series([arr.item()] * k, index=idx[mask], dtype=dtype)
        if arr.shape[0] != len(mask):
            raise ValueError(f"_emit: argument of length {arr.shape[0]} != row count {len(mask)}")
        return pd.Series(arr[mask], index=idx[mask], dtype=dtype)

    def _emit(field: str, mask: np.ndarray, value: Any, source: Any,
              filed: Any, tier: Any) -> None:
        m = np.asarray(mask, dtype=bool)
        if not m.any():
            return
        val = _as_series(value, m, "float64") if value is not None \
            else pd.Series(np.full(int(m.sum()), np.nan), index=idx[m], dtype="float64")
        fb_frames.append(pd.DataFrame({
            "ticker": merged.loc[idx[m], "ticker"].astype(str).to_numpy(),
            "rebalance_date": merged.loc[idx[m], "rebalance_date"].to_numpy(),
            "field": field,
            "value": val.to_numpy(),
            "source": _as_series(source, m, "object").to_numpy(),
            "filed_date": _as_series(filed, m, "datetime64[ns]").to_numpy(),
            "fallback_tier": _as_series(tier, m, "int8").to_numpy(),
        }))

    # top-level: only rows that actually gave up lineage
    for field, tier_col in (("wacc", "wacc_fallback_tier"), ("icr", "icr_fallback_tier"),
                            ("roic", "roic_fallback_tier")):
        tiers = merged[tier_col].to_numpy()
        filed_col = f"{field}_filed_date"
        filed_vals = (merged[filed_col].to_numpy() if filed_col in merged
                      else np.full(n, np.datetime64("NaT"), dtype=object))
        _emit(field, tiers > TIER_FILED, merged[field].astype(float).to_numpy(),
              [TIER_SOURCE[int(t)] for t in tiers], filed_vals, tiers)

    # sector: non-reconstitution lineage only
    s_tier = merged["sector_fallback_tier"].to_numpy()
    _emit("sector", s_tier > TIER_FILED, None,
          [SECTOR_TIER_SOURCE[int(t)] for t in s_tier], pd.NaT, s_tier)

    # component cascades (always logged — they hide inside a tier-0 row)
    _emit("wacc.cost_of_debt", kd_borrowed, kd_final,
          TIER_SOURCE[TIER_SECTOR_MEDIAN], ke_filed,
          np.full(n, TIER_SECTOR_MEDIAN, dtype=np.int8))
    _emit("wacc.capital_structure", d_from_ratio, d_eff,
          TIER_SOURCE[TIER_FORWARD], de_filed,
          np.full(n, TIER_FORWARD, dtype=np.int8))
    _emit("icr.interest_expense",
          merged["icr_fallback_tier"].to_numpy() == TIER_SECTOR_MEDIAN,
          merged["interest_expense"].astype(float).to_numpy(),
          TIER_SOURCE[TIER_SECTOR_MEDIAN], ie_filed,
          np.full(n, TIER_SECTOR_MEDIAN, dtype=np.int8))

    if fb_frames:
        fb = pd.concat(fb_frames, ignore_index=True)
        fb = fb.drop_duplicates(subset=["ticker", "rebalance_date", "field"], keep="last")
        fb = fb.sort_values(["rebalance_date", "ticker", "field"],
                            kind="mergesort").reset_index(drop=True)
    else:
        fb = pd.DataFrame({
            "ticker": pd.Series(dtype="object"), "rebalance_date": pd.Series(dtype="datetime64[ns]"),
            "field": pd.Series(dtype="object"), "value": pd.Series(dtype="float64"),
            "source": pd.Series(dtype="object"), "filed_date": pd.Series(dtype="datetime64[ns]"),
            "fallback_tier": pd.Series(dtype="int8"),
        })
    return merged, fb


def _map_to_custom_sector(raw: Any) -> str:
    """GICS L1 -> the frozen 7-sector axis (mirrors universe_builder)."""
    s = str(raw).strip().lower()
    if any(k in s for k in ("tech", "software", "information technology")):
        return "Software"
    if any(k in s for k in ("hardware", "semiconductor", "computer", "electronic")):
        return "Hardware"
    if any(k in s for k in ("health", "pharma", "bio", "medical")):
        return "Healthcare"
    if any(k in s for k in ("finan", "bank", "insurance")):
        return "Financial"
    if any(k in s for k in ("energy", "oil", "gas")):
        return "Energy"
    if any(k in s for k in ("consumer", "retail", "staples")):
        return "Consumer"
    if s in {v.lower() for v in CUSTOM_SECTORS}:
        return str(raw).strip().title()
    return "Industrial"


__all__ = [
    "MERGED_UNIVERSE_SCHEMA", "FALLBACK_TRACKING_SCHEMA", "TICKER_USAGE_SCHEMA",
    "MERGED_PARTITION_COLS", "FALLBACK_PARTITION_COLS", "USAGE_PARTITION_COLS",
    "CAP_BUCKETS", "CUSTOM_SECTORS", "WACC_SOURCES", "ICR_SOURCES", "SECTOR_SOURCES",
    "ROIC_SOURCES", "FALLBACK_TIERS", "TIER_SOURCE", "SECTOR_TIER_SOURCE",
    "TRACKED_FIELDS", "COMPONENT_FIELDS", "PROVENANCE_COLUMNS",
    "TIER_FILED", "TIER_FORWARD", "TIER_SECTOR_MEDIAN", "TIER_CONSTANT",
    "FRESH_DAYS", "MAX_STALE_DAYS", "SECTOR_MEDIAN_MIN_N",
    "WACC_BOUNDS", "ICR_BOUNDS", "ROIC_BOUNDS", "SCHEMA_VERSION",
    "DATASET_SOURCE", "DEFAULT_SOURCE_VERSION",
    "MergedUniverseStore", "WriteReceipt", "SchemaContractError",
    "assign_provenance", "canonical_row_payload", "compute_row_sha256",
    "make_uuid7", "enforce_schema", "validate_pit", "validate_fallback_consistency",
    "schema_contract", "normalize_cap_bucket", "build_merged_pit_universe",
    "migrate_ticker_usage_frame", "migrate_ticker_usage_datasets",
    "main",
]


# ===========================================================================
# 8. LEGACY MIGRATION — ticker_usage 9-col -> frozen 11-col
# ===========================================================================
def migrate_ticker_usage_frame(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Upgrade a Workstream A ``ticker_usage`` frame to the frozen schema.

    Workstream A shipped 233 flat ``ticker_usage_{arm}_{window}.parquet`` files
    on a 9-column contract: everything nullable, tiers as ``int64``, and a single
    opaque ``provenance`` JSON string.  The frozen P0A contract is 11 columns,
    non-nullable, tiers as ``int8``, and an explicit
    ``provenance_sha256``/``provenance_uuid``/``provenance_timestamp_ns`` triple.
    The legacy ``provenance`` blob already contains exactly those three keys, so
    the split is mechanical and lossless.

    Honest caveat on what is *not* preserved: the legacy ``uuid`` was a plain
    UUID shared across a whole batch, whereas the frozen contract requires a
    content-addressed uuid7 reproducible via
    ``make_uuid7(timestamp_ns, seed=canonical_json([payload, sha256]))``.  A
    migrated row therefore *carries* provenance rather than *reproducing* it --
    its ``verify``-style reproduction check will not hold.  Regenerating the
    merged dataset and re-deriving usage from it is the only way to get
    reproducible uuids, which is the preferred path when a re-run is cheap.

    Returns ``(migrated_frame, report)``.
    """
    report: Dict[str, Any] = {"rows": int(len(df)), "changes": [], "warnings": []}
    out = df.copy()

    if "provenance" in out.columns and not any(
            c in out.columns for c in PROVENANCE_NAME_MAP.values()):
        try:
            parsed = pd.Series(
                [json.loads(v) if isinstance(v, str) else (v or {}) for v in out["provenance"]],
                index=out.index)
        except json.JSONDecodeError as exc:
            raise SchemaContractError(
                f"migrate_ticker_usage: provenance column is not JSON ({exc})") from exc
        missing = [k for k in PROVENANCE_COLUMNS if k not in
                   set().union(*[set(p) for p in parsed])] if len(parsed) else list(PROVENANCE_COLUMNS)
        if missing:
            raise SchemaContractError(
                f"migrate_ticker_usage: legacy provenance blob lacks {missing}; "
                f"cannot migrate without losing provenance")
        for merged_name, fb_name in PROVENANCE_NAME_MAP.items():
            out[fb_name] = [p.get(merged_name) for p in parsed]
        out = out.drop(columns=["provenance"])
        report["changes"].append("split provenance JSON -> provenance_{sha256,uuid,timestamp_ns}")
        report["warnings"].append(
            "migrated uuid is carried from the legacy writer and is NOT a "
            "content-addressed uuid7; provenance is carried, not reproducible")

    for tier_col in ("wacc_fallback_tier", "icr_fallback_tier", "sector_fallback_tier"):
        if tier_col in out.columns and out[tier_col].dtype != np.int8:
            out[tier_col] = out[tier_col].astype("int8")
            report["changes"].append(f"{tier_col}: int64 -> int8")

    if "arm" in out.columns:
        out["arm"] = out["arm"].astype(str).str.strip().str.lower()
        report["changes"].append("arm lowercased (matches partition/usage_path convention)")

    enforce_schema(out, TICKER_USAGE_SCHEMA, "ticker_usage (migrated)")
    report["changes"].append("enforced non-nullable frozen contract")
    return out.reset_index(drop=True), report


def migrate_ticker_usage_datasets(backtest_root: Union[str, Path] = DEFAULT_BACKTEST_ROOT,
                                  *, apply: bool = False) -> Dict[str, Any]:
    """Scan (and optionally rewrite) legacy flat ``ticker_usage_*`` files.

    Defaults to a **dry run**: it reports what would change and touches nothing.
    Rewriting another workstream's artifacts is not a side effect this module
    should perform implicitly.
    """
    root = Path(backtest_root)
    want = set(TICKER_USAGE_SCHEMA.names)
    results: List[Dict[str, Any]] = []
    for p in sorted(root.glob("ticker_usage_*")):
        if not p.is_file():
            continue
        try:
            df = pq.ParquetFile(str(p)).read().to_pandas()
            already = set(df.columns) == want
            migrated, rep = migrate_ticker_usage_frame(df)
        except Exception as exc:  # noqa: BLE001
            results.append({"path": str(p), "status": "error", "error": str(exc)})
            continue
        entry = {"path": str(p), "status": "already_frozen" if already else "legacy",
                 "rows": rep["rows"], "changes": rep["changes"], "warnings": rep["warnings"]}
        if not already and apply:
            tmp = p.with_suffix(".parquet.tmp")
            pq.write_table(pa.Table.from_pandas(migrated, schema=TICKER_USAGE_SCHEMA,
                                                preserve_index=False), str(tmp))
            tmp.replace(p)
            entry["status"] = "migrated"
        results.append(entry)
    legacy = [r for r in results if r["status"] == "legacy"]
    return {
        "backtest_root": str(root),
        "applied": bool(apply),
        "datasets_scanned": len(results),
        "legacy": len(legacy),
        "already_frozen": sum(1 for r in results if r["status"] == "already_frozen"),
        "errors": [r for r in results if r["status"] == "error"],
        "dry_run_hint": "re-run with apply=True to rewrite" if legacy and not apply else None,
        "datasets": results,
    }


# ===========================================================================
# 7. CLI — build + verify the dataset
# ===========================================================================
def main(argv: Optional[List[str]] = None) -> int:
    """``python -m ingestion.persistence.merged_universe --build``

    Builds the merged universe + fallback tracking from reconstitution v2 and
    PIT fundamentals, writes both tables through the store, and verifies the
    provenance chain before reporting success.
    """
    import argparse

    ap = argparse.ArgumentParser(
        prog="ingestion.persistence.merged_universe",
        description="Build / verify the merged PIT universe (P0A, Workstream B)")
    ap.add_argument("--build", action="store_true", help="build + write both tables")
    ap.add_argument("--verify", action="store_true", help="verify the chain only")
    ap.add_argument("--root", default=str(DEFAULT_UNIVERSE_ROOT))
    ap.add_argument("--backtest-root", default=str(DEFAULT_BACKTEST_ROOT))
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--reconstitution",
                    default="data/universe/monthly_reconstitution.parquet")
    ap.add_argument("--fundamentals", default="data/pit_fundamentals/valuation_fundamentals.parquet")
    ap.add_argument("--replace", action="store_true",
                    help="allow overwriting existing (rebalance_date, ticker) rows")
    ns = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    store = MergedUniverseStore(root=ns.root, backtest_root=ns.backtest_root)

    if ns.build:
        t0 = time.time()
        merged, fb = build_merged_pit_universe(
            reconstitution_path=ns.reconstitution, fundamentals_path=ns.fundamentals,
            start=ns.start, end=ns.end)
        r1 = store.write_merged(merged, allow_replace=ns.replace)
        r2 = store.write_fallback_tracking(fb)
        print(f"merged_pit_universe : {r1.rows_written:,} rows -> {r1.partitions_written} "
              f"partitions in {time.time() - t0:.1f}s")
        print(f"fallback_tracking   : {r2.rows_written:,} rows -> {r2.partitions_written} partitions")
        print(f"chain head          : {r1.chain_head}")

    if ns.build or ns.verify:
        v = store.verify_merged_chain(verify_hashes=True, recompute_sources=True)
        status = "INTACT" if v["intact"] else f"BROKEN @ row {v['first_break']}"
        print(f"chain verify        : {status} ({v['rows']:,} rows / "
              f"{v['partitions']} partitions)")
        return 0 if v["intact"] else 1

    print("nothing to do — pass --build and/or --verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())