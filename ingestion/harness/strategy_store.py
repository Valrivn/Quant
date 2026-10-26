"""
Strategy Store — Persistence layer for the 4-Stage Dynamic Value & Regime
Shield Strategy (D-20260915 mandate, docs/STRATEGY_VIABILITY_BRIEFING.md).

Adds four versioned, PIT-safe tables to the shared harness DB
(data/harness/factors.duckdb — same DB as ingestion/harness/factor_store.py):

    1. regime_state          — HMM macro state per obs date (EXPANSION/SHOCK/
                               CRISIS), posterior probabilities, transition
                               matrix, retrain window, model provenance.
    2. grassroots_signal     — Stage-2 Grassroots C_t panel per ticker/date:
                               composite velocity, lead-lag validation metadata
                               (rho C->T, T->W), mapping_confidence grade.
    3. black_swan_event      — Stage-4 Tier 1/2 events: fast/slow trigger
                               state, moat invariance flags, re-entry limits,
                               disengage bookkeeping.
    4. stage_gate_decision   — per ticker per rebalance, one row per stage
                               (1..4): passed/blocked + reason + the filed_date
                               anchor of the fundamentals used (PIT-safe joins).

PIT rules enforced on write (PITValidator, orientation "X <= Y"):

    - regime_state:        obs_date <= retrieval_timestamp (a regime "for"
                           obs_date is only knowable at/after that date; bulk
                           backfills are written after the fact and are NOT
                           lookahead)
    - grassroots_signal:   date <= retrieval_timestamp; revision-safe PK keeps
                           every retrieval, snapshots pick the latest revision
    - black_swan_event:    event_date <= retrieval_timestamp; callers may pass
                           a per-row retrieval_timestamp (daily detector)
    - stage_gate_decision: filed_date <= rebalance_date (fundamentals known
                           when the gate ran — no lookahead)

Actual no-lookahead is additionally enforced on every READ surface:
read_regime_at / read_grassroots_snapshot select only rows dated <= trade_date,
and align_black_swan_timestamps drops events whose retrieval postdates the
window end.

Mapping confidence (Data_Strategy.md §6/§8): every qualitative row in
grassroots_signal carries a {high, medium, low} grade; 'low' rows are dropped
from core-arm queries unless explicitly requested.

SHA-256 provenance: every write batches a provenance hash; every parquet
export writes a .provenance.json sidecar (sha256 / row_count / date_range /
source / pit_timestamp_column / mapping_confidence distribution) matching the
house manifest conventions.

This module implements persistence ONLY. Strategy logic (gates, allocation,
HMM math) lives in Quantitative/* and real_backtest.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import duckdb
import pandas as pd

from config.logging_config import college_event
from ingestion.harness.factor_store import (
    CONFIDENCE_LEVELS,
    QUAL_FACTOR_CATALOG,
    ensure_factor_store_schema,
)
from ingestion.harness.pit_validator import PITValidationError, PITValidator

logger = logging.getLogger(__name__)

DEFAULT_STRATEGY_STORE = "data/harness/factors.duckdb"

REGIME_STATES = ("EXPANSION", "SHOCK", "CRISIS")
BLACK_SWAN_TIERS = ("NONE", "TIER1_PROVISIONAL", "TIER2_STRUCTURAL", "DISENGAGED")

# ---------------------------------------------------------------------------
# DDL — the four strategy tables (all CREATE TABLE IF NOT EXISTS, idempotent)
# ---------------------------------------------------------------------------

DDL_REGIME_STATE = """
CREATE TABLE IF NOT EXISTS regime_state (
    obs_date               DATE    NOT NULL,
    detected_state         VARCHAR NOT NULL CHECK (detected_state IN ('EXPANSION','SHOCK','CRISIS')),
    p_expansion            DOUBLE  NOT NULL,
    p_shock                DOUBLE  NOT NULL,
    p_crisis               DOUBLE  NOT NULL,
    confident              BOOLEAN NOT NULL,
    log_likelihood         DOUBLE,
    transition_matrix_json VARCHAR,
    train_window_start     DATE,
    train_window_end       DATE,
    retrained              BOOLEAN NOT NULL,
    coverage_json          VARCHAR,
    model_hash             VARCHAR NOT NULL,
    retrieval_timestamp    TIMESTAMP NOT NULL,
    PRIMARY KEY (obs_date),
    CHECK (abs(p_expansion + p_shock + p_crisis - 1.0) < 0.01)
)
"""

DDL_GRASSROOTS_SIGNAL = """
CREATE TABLE IF NOT EXISTS grassroots_signal (
    date                        DATE    NOT NULL,
    ticker                      VARCHAR NOT NULL,
    c_t_composite               DOUBLE,
    accelerating                BOOLEAN,
    c_t_baseline                DOUBLE,
    n_sources_available         INTEGER,
    sources_json                VARCHAR,
    lead_lag_validated          BOOLEAN NOT NULL DEFAULT FALSE,
    rho_c_to_t                  DOUBLE,
    rho_t_to_w                  DOUBLE,
    max_lag_used                INTEGER,
    validation_window_start     DATE,
    validation_window_end       DATE,
    mapping_confidence          VARCHAR NOT NULL CHECK (mapping_confidence IN ('high','medium','low')),
    retrieval_timestamp         TIMESTAMP NOT NULL,
    provenance_hash             VARCHAR NOT NULL,
    PRIMARY KEY (date, ticker, retrieval_timestamp)
)
"""

DDL_BLACK_SWAN_EVENT = """
CREATE TABLE IF NOT EXISTS black_swan_event (
    event_date            DATE    NOT NULL,
    ticker                VARCHAR NOT NULL DEFAULT 'MARKET',
    tier                  VARCHAR NOT NULL CHECK (tier IN ('NONE','TIER1_PROVISIONAL','TIER2_STRUCTURAL','DISENGAGED')),
    fast_triggered        BOOLEAN,
    slow_triggered        BOOLEAN,
    drawdown_5d           DOUBLE,
    sigma_threshold       DOUBLE,
    hmm_crisis_prob       DOUBLE,
    moat_invariant        BOOLEAN,
    moat_ratio            DOUBLE,
    structural_impairment BOOLEAN,
    sector_crisis         BOOLEAN,
    deploy_pct            DOUBLE,
    entry_price_limit     DOUBLE,
    reentry_eligible      BOOLEAN,
    reentry_trigger_date  DATE,
    disengage_reason      VARCHAR,
    retrieval_timestamp   TIMESTAMP NOT NULL,
    provenance_hash       VARCHAR NOT NULL,
    PRIMARY KEY (event_date, ticker, tier, retrieval_timestamp)
)
"""

DDL_STAGE_GATE_DECISION = """
CREATE TABLE IF NOT EXISTS stage_gate_decision (
    rebalance_date      DATE    NOT NULL,
    ticker              VARCHAR NOT NULL,
    stage               INTEGER NOT NULL CHECK (stage BETWEEN 1 AND 4),
    gate_name           VARCHAR NOT NULL,
    passed              BOOLEAN NOT NULL,
    reason              VARCHAR,
    detail_json         VARCHAR,
    filed_date          DATE,
    retrieval_timestamp TIMESTAMP NOT NULL,
    provenance_hash     VARCHAR NOT NULL,
    PRIMARY KEY (rebalance_date, ticker, stage)
)
"""

# Versioned migrations: (version, name, DDL/DDLs). strategy_schema_version
# records which versions have been applied; --migrate re-runs are no-ops.
STRATEGY_SCHEMA_MIGRATIONS: List[Dict[str, Any]] = [
    {"version": 1, "name": "regime_state (HMM macro states)", "ddl": [DDL_REGIME_STATE]},
    {"version": 2, "name": "grassroots_signal (Stage-2 C_t panel)", "ddl": [DDL_GRASSROOTS_SIGNAL]},
    {"version": 3, "name": "black_swan_event (Stage-4 tier events)", "ddl": [DDL_BLACK_SWAN_EVENT]},
    {"version": 4, "name": "stage_gate_decision (stage pass/fail log)", "ddl": [DDL_STAGE_GATE_DECISION]},
]

DDL_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS strategy_schema_version (
    version     INTEGER PRIMARY KEY,
    name        VARCHAR NOT NULL,
    applied_at  TIMESTAMP NOT NULL,
    migration_hash VARCHAR NOT NULL
)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(payload) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _hash_provenance(provenance: dict) -> str:
    return _sha256(json.dumps(provenance, sort_keys=True, default=str))


def _now() -> datetime:
    # naive UTC wall-clock: keeps PITValidator (naive-vs-naive) comparisons
    # safe while encoding the same instant the tz-aware form would.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_date(value) -> Optional[pd.Timestamp]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    return pd.Timestamp(value)


class StrategyStore:
    """DuckDB persistence for the 4-stage strategy tables.

    Uses the shared harness DB path by default (the same file factor_store.py
    writes) so regime / C_t / black-swan / stage-gate state can be joined to
    factor rows and the valuation fundamentals panel without ETL.
    """

    def __init__(self, db_path: str = DEFAULT_STRATEGY_STORE, ensure_fundamental_schema: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db(ensure_fundamental_schema=ensure_fundamental_schema)

    def _get_connection(self):
        return duckdb.connect(str(self.db_path))

    # ------------------------------------------------------------------ DDL

    def _init_db(self, ensure_fundamental_schema: bool = True) -> None:
        with self._get_connection() as conn:
            if ensure_fundamental_schema:
                # factor_panel mapping_confidence guard (shared table)
                ensure_factor_store_schema(conn)
            conn.execute(DDL_SCHEMA_VERSION)
            for mig in STRATEGY_SCHEMA_MIGRATIONS:
                self._apply_migration(conn, mig)

    def _apply_migration(self, conn: duckdb.DuckDBPyConnection, mig: dict) -> None:
        applied = conn.execute(
            "SELECT 1 FROM strategy_schema_version WHERE version = ?", [mig["version"]]
        ).fetchone()
        if applied:
            return
        mig_hash = _hash_provenance({"version": mig["version"], "name": mig["name"], "ddl": mig["ddl"]})
        for ddl in mig["ddl"]:
            conn.execute(ddl)
        conn.execute(
            "INSERT INTO strategy_schema_version VALUES (?, ?, ?, ?)",
            [mig["version"], mig["name"], _now(), mig_hash],
        )
        logger.info("strategy_store: applied schema migration v%d (%s)", mig["version"], mig["name"])

    def migrate(self) -> List[str]:
        """Idempotent schema migration runner (safe on the production DB)."""
        applied = []
        with self._get_connection() as conn:
            if not conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = 'strategy_schema_version'"
            ).fetchone():
                conn.execute(DDL_SCHEMA_VERSION)
            for mig in STRATEGY_SCHEMA_MIGRATIONS:
                self._apply_migration(conn, mig)
                applied.append(f"v{mig['version']}: {mig['name']}")
        return applied

    def schema_status(self) -> pd.DataFrame:
        with self._get_connection() as conn:
            return self._finalize(
                conn.execute("SELECT version, name, applied_at FROM strategy_schema_version ORDER BY version").df()
            )

    @staticmethod
    def _finalize(df: pd.DataFrame) -> pd.DataFrame:
        """DuckDB 1.5 returns TIMESTAMP as tz-aware datetime64[us, UTC] in
        .df(); strip the tz so downstream pandas comparisons never raise
        aware-vs-naive TypeErrors. Wall-clock time is preserved (all writes
        are UTC)."""
        for c in df.columns:
            if isinstance(df[c].dtype, pd.DatetimeTZDtype):
                df[c] = df[c].dt.tz_localize(None)
        return df

    # ------------------------------------------------------------- writes

    def write_regime_state(self, df: pd.DataFrame, provenance: dict | None = None) -> str:
        """Upsert HMM regime state for observation dates (last-write-wins per obs_date).

        df columns: obs_date, detected_state, p_expansion, p_shock, p_crisis,
        confident (optional extras: log_likelihood, transition_matrix_json,
        train_window_start/end, retrained, coverage_json, model_hash).
        """
        required = {"obs_date", "detected_state", "p_expansion", "p_shock", "p_crisis"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"regime_state rows missing columns: {missing}")

        df = df.copy()
        df["obs_date"] = pd.to_datetime(df["obs_date"]).dt.date
        for col in ("p_expansion", "p_shock", "p_crisis"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # probabilities must sum to 1 (within tolerance, lifted from DDL CHECK)
        prob_sum = df["p_expansion"] + df["p_shock"] + df["p_crisis"]
        bad = df[~prob_sum.between(0.99, 1.01)]
        if not bad.empty:
            raise ValueError(f"regime posterior probabilities must sum to 1.0; {len(bad)} bad rows")

        bad_state = df[~df["detected_state"].isin(REGIME_STATES)]
        if not bad_state.empty:
            raise ValueError(f"detected_state must be one of {REGIME_STATES}")

        now = _now()
        for col, default in (("confident", False), ("retrained", False),
                             ("log_likelihood", None), ("transition_matrix_json", None),
                             ("train_window_start", None), ("train_window_end", None),
                             ("coverage_json", None)):
            if col not in df.columns:
                df[col] = default
        df["retrieval_timestamp"] = now
        if "model_hash" not in df.columns:
            df["model_hash"] = _hash_provenance(provenance or {"source": "hmm_regime_detector"})
        cols = [
            "obs_date", "detected_state", "p_expansion", "p_shock", "p_crisis",
            "confident", "log_likelihood", "transition_matrix_json",
            "train_window_start", "train_window_end", "retrained",
            "coverage_json", "model_hash", "retrieval_timestamp",
        ]
        for c in cols:
            if c not in df.columns:
                df[c] = None

        # PIT: retrieval can never precede the observation date (a regime "for"
        # obs_date is only knowable at/after that date). Validation orientation:
        # obs_date <= retrieval_timestamp (never the reverse — bulk regime
        # backfills are written after the fact, which is NOT lookahead).
        PITValidator.validate_pit(
            df, timestamp_col="obs_date", trade_date_col="retrieval_timestamp",
            required_cols=None,
        )

        prov_hash = _hash_provenance(provenance or {"source": "hmm_regime_detector", "rows": len(df)})
        with self._get_connection() as conn:
            conn.register("regime_write", df[cols])
            conn.execute(
                "INSERT OR REPLACE INTO regime_state SELECT * FROM regime_write"
            )
        college_event("strategy_store_regime_written", rows=len(df), prov=prov_hash[:10])
        return prov_hash

    def write_grassroots_signals(self, df: pd.DataFrame, provenance: dict | None = None) -> str:
        """Append Grassroots C_t signal rows (revision-safe: PK includes retrieval).

        df columns: date, ticker, c_t_composite (nullable when degraded),
        mapping_confidence (REQUIRED — forced confidence grade). Lead-lag
        metadata optional: lead_lag_validated, rho_c_to_t, rho_t_to_w,
        max_lag_used, validation_window_start/end, accelerating, c_t_baseline,
        n_sources_available, sources_json.
        """
        required = {"date", "ticker", "c_t_composite", "mapping_confidence"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"grassroots_signal rows missing columns: {missing}")

        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        bad_conf = df[~df["mapping_confidence"].isin(CONFIDENCE_LEVELS)]
        if not bad_conf.empty:
            raise ValueError(f"mapping_confidence must be one of {CONFIDENCE_LEVELS}")

        now = _now()
        prov = provenance or {"source": "consumer_signal", "rows": len(df)}
        prov_hash = _hash_provenance(prov)
        df["retrieval_timestamp"] = now
        df["provenance_hash"] = prov_hash
        if "lead_lag_validated" not in df.columns:
            df["lead_lag_validated"] = False

        # PIT: the signal for `date` is only knowable at/after that date
        # (date <= retrieval_timestamp). Actual no-lookahead is enforced on the
        # READ side: snapshots select the last signal date <= trade_date.
        PITValidator.validate_pit(
            df, timestamp_col="date", trade_date_col="retrieval_timestamp",
            required_cols=None,
        )

        cols = [
            "date", "ticker", "c_t_composite", "accelerating", "c_t_baseline",
            "n_sources_available", "sources_json", "lead_lag_validated",
            "rho_c_to_t", "rho_t_to_w", "max_lag_used", "validation_window_start",
            "validation_window_end", "mapping_confidence", "retrieval_timestamp",
            "provenance_hash",
        ]
        for c in cols:
            if c not in df.columns:
                df[c] = None
        with self._get_connection() as conn:
            conn.register("c_t_write", df[cols])
            conn.execute("INSERT INTO grassroots_signal SELECT * FROM c_t_write")
        college_event("strategy_store_grassroots_written", rows=len(df), prov=prov_hash[:10])
        return prov_hash

    def write_black_swan_events(self, df: pd.DataFrame, provenance: dict | None = None) -> str:
        """Append black swan events (Tier 1/2 / disengage), revision-safe.

        df columns: event_date, tier (required); ticker optional -> 'MARKET'.
        Trigger bookkeeping: fast_triggered, slow_triggered, drawdown_5d,
        sigma_threshold, hmm_crisis_prob, moat_invariant, moat_ratio,
        structural_impairment, sector_crisis, deploy_pct, entry_price_limit,
        reentry_eligible, reentry_trigger_date, disengage_reason.
        """
        required = {"event_date", "tier"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"black_swan_event rows missing columns: {missing}")

        df = df.copy()
        df["event_date"] = pd.to_datetime(df["event_date"]).dt.date
        bad_tier = df[~df["tier"].isin(BLACK_SWAN_TIERS)]
        if not bad_tier.empty:
            raise ValueError(f"tier must be one of {BLACK_SWAN_TIERS}")
        if "ticker" not in df.columns:
            df["ticker"] = "MARKET"
        df["ticker"] = df["ticker"].fillna("MARKET")

        now = _now()
        prov = provenance or {"source": "black_swan_engine", "rows": len(df)}
        prov_hash = _hash_provenance(prov)
        if "retrieval_timestamp" not in df.columns:
            df["retrieval_timestamp"] = now
        df["retrieval_timestamp"] = pd.to_datetime(df["retrieval_timestamp"])
        df["provenance_hash"] = prov_hash

        # PIT: an event "on event_date" can only be known at/after that date
        # (event_date <= retrieval_timestamp). Window alignment on the read
        # side guarantees no lookahead.
        PITValidator.validate_pit(
            df, timestamp_col="event_date", trade_date_col="retrieval_timestamp",
            required_cols=None,
        )

        cols = [
            "event_date", "ticker", "tier", "fast_triggered", "slow_triggered",
            "drawdown_5d", "sigma_threshold", "hmm_crisis_prob", "moat_invariant",
            "moat_ratio", "structural_impairment", "sector_crisis", "deploy_pct",
            "entry_price_limit", "reentry_eligible", "reentry_trigger_date",
            "disengage_reason", "retrieval_timestamp", "provenance_hash",
        ]
        for c in cols:
            if c not in df.columns:
                df[c] = None
        with self._get_connection() as conn:
            conn.register("bs_write", df[cols])
            conn.execute("INSERT INTO black_swan_event SELECT * FROM bs_write")
        college_event("strategy_store_blackswan_written", rows=len(df), prov=prov_hash[:10])
        return prov_hash

    def write_stage_gate_decisions(self, df: pd.DataFrame, provenance: dict | None = None) -> str:
        """Upsert stage-gate decisions (last-write-wins per rebalance+ticker+stage).

        df columns: rebalance_date, ticker, stage (1..4), gate_name, passed,
        reason (optional), detail_json (optional). filed_date (optional) is the
        PIT anchor of the fundamentals row used — required for the PIT-safe
        stage-gate join against valuation_fundamentals.parquet.
        """
        required = {"rebalance_date", "ticker", "stage", "gate_name", "passed"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"stage_gate_decision rows missing columns: {missing}")

        df = df.copy()
        df["rebalance_date"] = pd.to_datetime(df["rebalance_date"]).dt.date
        df["stage"] = pd.to_numeric(df["stage"], errors="coerce").astype("Int64")
        bad_stage = df[~df["stage"].between(1, 4)]
        if not bad_stage.empty:
            raise ValueError(f"stage must be between 1 and 4")

        if "filed_date" in df.columns and df["filed_date"].notna().any():
            # Core PIT rule: fundamentals must have been filed before/at rebalance.
            pit_df = pd.DataFrame({
                "filed_date": pd.to_datetime(df["filed_date"]),
                "rebalance_date": pd.to_datetime(df["rebalance_date"]),
                "ticker": df["ticker"],
            }).dropna(subset=["filed_date"])
            PITValidator.validate_pit(
                pit_df, timestamp_col="filed_date", trade_date_col="rebalance_date",
                required_cols=None,
            )

        now = _now()
        prov = provenance or {"source": "stage_gate_engine", "rows": len(df)}
        prov_hash = _hash_provenance(prov)
        df["retrieval_timestamp"] = now
        df["provenance_hash"] = prov_hash

        cols = [
            "rebalance_date", "ticker", "stage", "gate_name", "passed", "reason",
            "detail_json", "filed_date", "retrieval_timestamp", "provenance_hash",
        ]
        for c in cols:
            if c not in df.columns:
                df[c] = None
        with self._get_connection() as conn:
            conn.register("gate_write", df[cols])
            conn.execute(
                "INSERT OR REPLACE INTO stage_gate_decision SELECT * FROM gate_write"
            )
        college_event("strategy_store_stage_gates_written", rows=len(df), prov=prov_hash[:10])
        return prov_hash

    # ------------------------------------------------------------- reads

    def read_regime_series(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> pd.DataFrame:
        q = "SELECT * FROM regime_state"
        params: List[Any] = []
        if start_date:
            q += " WHERE obs_date >= ?"; params.append(start_date)
        if end_date:
            q += (" AND " if start_date else " WHERE ") + " obs_date <= ?"; params.append(end_date)
        q += " ORDER BY obs_date"
        with self._get_connection() as conn:
            return self._finalize(conn.execute(q, params).df())

    def read_regime_at(self, trade_date: str) -> Optional[Dict[str, Any]]:
        """Latest regime state known at/ before trade_date (PIT, no lookahead)."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM regime_state WHERE obs_date <= ? ORDER BY obs_date DESC LIMIT 1",
                [trade_date],
            ).fetchdf()
        if row.empty:
            return None
        return row.iloc[0].to_dict()

    def read_grassroots_snapshot(self, trade_date: str, min_confidence: str = "medium") -> pd.DataFrame:
        """Latest C_t revision per ticker for the last signal date <= trade_date.

        Core-arm semantics: confidence grades below min_confidence are dropped
        (per Data_Strategy §6 — 'low' excluded from core).
        """
        conf_filter = {"high": "g.mapping_confidence = 'high'", "medium": "g.mapping_confidence != 'low'",
                       "low": "1=1"}.get(min_confidence)
        if conf_filter is None:
            raise ValueError("min_confidence must be one of high/medium/low")
        with self._get_connection() as conn:
            return self._finalize(conn.execute(f"""
                WITH last_date AS (
                    SELECT max(date) AS md FROM grassroots_signal WHERE date <= ?
                ),
                last_rev AS (
                    SELECT ticker, max(retrieval_timestamp) AS rt
                    FROM grassroots_signal g JOIN last_date ON g.date = last_date.md
                    GROUP BY ticker
                )
                SELECT g.*
                FROM grassroots_signal g
                JOIN last_rev   ON g.ticker = last_rev.ticker AND g.retrieval_timestamp = last_rev.rt
                JOIN last_date  ON g.date = last_date.md
                WHERE {conf_filter}
                ORDER BY g.ticker
            """, [trade_date]).df())

    def read_grassroots_panel(self, ticker: Optional[str] = None, start_date: Optional[str] = None,
                              end_date: Optional[str] = None, min_confidence: str = "medium") -> pd.DataFrame:
        q = "SELECT * FROM grassroots_signal g WHERE 1=1"
        params: List[Any] = []
        if ticker:
            q += " AND g.ticker = ?"; params.append(ticker)
        if start_date:
            q += " AND g.date >= ?"; params.append(start_date)
        if end_date:
            q += " AND g.date <= ?"; params.append(end_date)
        q += " AND " + {"high": "g.mapping_confidence = 'high'",
                        "medium": "g.mapping_confidence != 'low'",
                        "low": "1=1"}.get(min_confidence, "1=1")
        q += " ORDER BY g.date, g.ticker"
        with self._get_connection() as conn:
            return self._finalize(conn.execute(q, params).df())

    def read_black_swan_events(self, ticker: Optional[str] = None, tier: Optional[str] = None,
                               start_date: Optional[str] = None, end_date: Optional[str] = None) -> pd.DataFrame:
        q = "SELECT * FROM black_swan_event WHERE 1=1"
        params: List[Any] = []
        if ticker:
            q += " AND ticker = ?"; params.append(ticker)
        if tier:
            q += " AND tier = ?"; params.append(tier)
        if start_date:
            q += " AND event_date >= ?"; params.append(start_date)
        if end_date:
            q += " AND event_date <= ?"; params.append(end_date)
        q += " ORDER BY event_date, ticker"
        with self._get_connection() as conn:
            return self._finalize(conn.execute(q, params).df())

    def read_stage_gates(self, rebalance_date: Optional[str] = None, ticker: Optional[str] = None,
                         stage: Optional[int] = None, passed: Optional[bool] = None) -> pd.DataFrame:
        q = "SELECT * FROM stage_gate_decision WHERE 1=1"
        params: List[Any] = []
        if rebalance_date:
            q += " AND rebalance_date = ?"; params.append(rebalance_date)
        if ticker:
            q += " AND ticker = ?"; params.append(ticker)
        if stage:
            q += " AND stage = ?"; params.append(stage)
        if passed is not None:
            q += " AND passed = ?"; params.append(passed)
        q += " ORDER BY rebalance_date, ticker, stage"
        with self._get_connection() as conn:
            return self._finalize(conn.execute(q, params).df())

    # ------------------------------------------------ verification queries

    def query_regime_conditional(self, factor_name: str, regime: str,
                                 start_date: Optional[str] = None, end_date: Optional[str] = None,
                                 min_confidence: Optional[str] = None) -> pd.DataFrame:
        """Regime-conditional factor query (dispatch item 4.1).

        ASOF-joins factor_panel to regime_state so every factor row inherits
        the latest regime known ON that date (obs_date <= factor.date) — the
        regime is never read from the future. Returns factor rows only for the
        requested detected_state.
        """
        if regime not in REGIME_STATES:
            raise ValueError(f"regime must be one of {REGIME_STATES}")
        q = """
            SELECT f.date, f.ticker, f.factor_name, f.value, f.mapping_confidence,
                   r.detected_state, r.p_expansion, r.p_shock, r.p_crisis, r.confident
            FROM factor_panel f
            ASOF JOIN regime_state r ON f.date >= r.obs_date
            WHERE f.factor_name = ? AND r.detected_state = ?
        """
        params: List[Any] = [factor_name, regime]
        if start_date:
            q += " AND f.date >= ?"; params.append(start_date)
        if end_date:
            q += " AND f.date <= ?"; params.append(end_date)
        if min_confidence == "high":
            q += " AND f.mapping_confidence = 'high'"
        elif min_confidence == "medium":
            q += " AND f.mapping_confidence != 'low'"
        q += " ORDER BY f.date, f.ticker"
        with self._get_connection() as conn:
            return self._finalize(conn.execute(q, params).df())

    def query_stage_gate_factors(self, fundamentals_path: str = "data/pit_fundamentals/valuation_fundamentals.parquet",
                                 stage: Optional[int] = None, rebalance_date: Optional[str] = None,
                                 passed: Optional[bool] = None, extra_cols: Sequence[str] = ()) -> pd.DataFrame:
        """Stage-gate filtered fundamentals (dispatch item 4.2).

        Joins stage_gate_decision to the valuation fundamentals panel on
        (ticker, filed_date) — the exact PIT snapshot the gate consumed. The
        join key is recorded at gate time, so re-running this query is immune
        to later parquet revisions (no lookahead, exact provenance).
        """
        if not Path(fundamentals_path).exists():
            raise FileNotFoundError(fundamentals_path)
        extra = list(dict.fromkeys(["stage", "gate_name", "passed", "reason", "filed_date", "retrieval_timestamp",
                                    "roic_cap_rd", "roic", "wacc_estimate", "omr", "interest_coverage",
                                    "revenue", "fcf", "rd_expense"] + list(extra_cols)))
        select_list = ", ".join(f"d.{c}" if c in ("stage", "gate_name", "passed", "reason",
                                                  "filed_date", "retrieval_timestamp") else f"v.{c}" for c in extra)
        q = f"""
            SELECT d.ticker, d.rebalance_date, {select_list}
            FROM stage_gate_decision d
            LEFT JOIN read_parquet(?) v
              ON v.ticker = d.ticker AND v.filed_date = d.filed_date
            WHERE 1=1
        """
        params: List[Any] = [str(fundamentals_path)]
        if stage is not None:
            q += " AND d.stage = ?"; params.append(stage)
        if rebalance_date:
            q += " AND d.rebalance_date = ?"; params.append(rebalance_date)
        if passed is not None:
            q += " AND d.passed = ?"; params.append(passed)
        q += " ORDER BY d.rebalance_date, d.ticker, d.stage"
        with self._get_connection() as conn:
            return self._finalize(conn.execute(q, params).df())

    def align_black_swan_timestamps(self, tier: Optional[str] = None, start_date: Optional[str] = None,
                                    end_date: Optional[str] = None) -> pd.DataFrame:
        """Black-swan event timestamp alignment (dispatch item 4.3).

        Returns events whose detection date sits in [start_date, end_date]
        AND whose retrieval happened no later than end_date — i.e. only events
        that were knowable inside the window (no lookahead from a later run).
        """
        q = """
            SELECT event_date, ticker, tier, fast_triggered, slow_triggered,
                   hmm_crisis_prob, moat_invariant, structural_impairment,
                   deploy_pct, entry_price_limit, reentry_eligible,
                   reentry_trigger_date, disengage_reason, retrieval_timestamp,
                   provenance_hash
            FROM black_swan_event
            WHERE 1=1
        """
        params: List[Any] = []
        if tier:
            q += " AND tier = ?"; params.append(tier)
        if start_date:
            q += " AND event_date >= ?"; params.append(start_date)
        if end_date:
            q += " AND event_date <= ?"; params.append(end_date)
            q += " AND retrieval_timestamp <= ?"; params.append(f"{end_date} 23:59:59")
        q += " ORDER BY event_date, ticker"
        with self._get_connection() as conn:
            return self._finalize(conn.execute(q, params).df())

    # ---------------------------------------------------------- parquet export

    def export_table(self, table: str, out_path: str, where: str = "", params: Sequence[Any] = ()) -> str:
        """Write a strategy table to parquet with a .provenance.json sidecar.

        Provenance sidecar matches the house manifest schema (source / sha256 /
        row_count / date_range / pit_timestamp_column / mapping_confidence).
        Returns the SHA-256 of the exported parquet bytes.
        """
        allowed = {"regime_state", "grassroots_signal", "black_swan_event", "stage_gate_decision"}
        if table not in allowed:
            raise ValueError(f"table must be one of {allowed}")
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        q = f"SELECT * FROM {table}"
        if where:
            q += " WHERE " + where
        q += " ORDER BY 1"
        with self._get_connection() as conn:
            df = conn.execute(q, list(params)).df()
        df.to_parquet(out, index=False)
        sha = _sha256(out.read_bytes())
        pit_anchor = {
            "regime_state": "obs_date", "grassroots_signal": "date",
            "black_swan_event": "event_date", "stage_gate_decision": "rebalance_date",
        }[table]
        conf_dist = {}
        if "mapping_confidence" in df.columns:
            conf_dist = df["mapping_confidence"].value_counts().to_dict()
        sidecar = {
            "source": "STRATEGY_STORE",
            "source_version": f"strategy_store_schema_v{max(m['version'] for m in STRATEGY_SCHEMA_MIGRATIONS)}",
            "table": table,
            "pit_timestamp_column": pit_anchor,
            "retrieval_timestamp": _now().isoformat(),
            "sha256": sha,
            "row_count": len(df),
            "date_range": {
                "min": str(df.iloc[0, 0]) if len(df) else None,
                "max": str(df.iloc[-1, 0]) if len(df) else None,
            },
            "mapping_confidence": conf_dist,
            "transformations": ["strategy_store_export"],
        }
        (out.parent / f"{out.name}.provenance.json").write_text(
            json.dumps(sidecar, indent=2, default=str), encoding="utf-8"
        )
        return sha


def main():
    import argparse
    parser = argparse.ArgumentParser(description="4-Stage Strategy persistence layer harness")
    parser.add_argument("--migrate", action="store_true", help="Apply idempotent schema migrations")
    parser.add_argument("--status", action="store_true", help="Show schema migration status")
    parser.add_argument("--db", default=DEFAULT_STRATEGY_STORE, help="DuckDB path")
    parser.add_argument("--test", action="store_true", help="Run self-test on a temp DB")
    args = parser.parse_args()

    if args.test:
        import tempfile
        tmp = Path(tempfile.mkdtemp()) / "strategy_store_test.duckdb"
        store = StrategyStore(db_path=str(tmp))
        df = pd.DataFrame({
            "obs_date": ["2023-01-03", "2023-01-04"],
            "detected_state": ["EXPANSION", "SHOCK"],
            "p_expansion": [0.9, 0.2], "p_shock": [0.08, 0.7], "p_crisis": [0.02, 0.1],
            "confident": [True, True],
        })
        store.write_regime_state(df, {"source": "selftest"})
        print(store.read_regime_series().to_string())
        print("strategy_store self-test OK")
        return

    if args.migrate:
        store = StrategyStore(db_path=args.db)
        applied = store.migrate()
        print("Applied (or already present):")
        for a in applied:
            print("  ", a)
        print(store.schema_status().to_string(index=False))
        logger.info("strategy_store migrate complete — idempotent, safe to re-run")
        return

    if args.status:
        store = StrategyStore(db_path=args.db)
        print(store.schema_status().to_string(index=False))
        return

    parser.print_help()


if __name__ == "__main__":
    main()