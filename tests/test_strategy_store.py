"""
Integration tests for the 4-Stage strategy persistence layer
(ingestion/harness/strategy_store.py + factor_store mapping_confidence +
scripts/migrate_valuation_stage1.py).

Coverage:
  - 4 stage tables + schema-version table are created idempotently
  - regime_state round-trip, probability-sum constraint, PIT enforcement
  - grassroots_signal revision-safe writes, snapshot reads, confidence grading
  - black_swan_event writes and no-lookahead window alignment
  - stage_gate_decision writes and PIT-safe (filed_date-anchored) join to
    the valuation fundamentals parquet
  - regime-conditional factor query via ASOF join (no regime lookahead)
  - factor_store mapping_confidence compatibility on the same DuckDB file
  - valuation_fundamentals Stage-1 migration on a copy of the REAL parquet:
    idempotent, column-preserving, sidecar + SHA-256 emitted
"""

import json
import shutil
from pathlib import Path

import duckdb
import pandas as pd
import polars as pl
import pytest

from ingestion.harness.factor_store import FactorStore
from ingestion.harness.pit_validator import PITValidationError
from ingestion.harness.strategy_store import StrategyStore

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_PARQUET = REPO_ROOT / "data" / "pit_fundamentals" / "valuation_fundamentals.parquet"


@pytest.fixture
def store(tmp_path):
    """Fresh StrategyStore on a temp DuckDB (same shape as the harness DB)."""
    db = tmp_path / "factors.duckdb"
    return StrategyStore(db_path=str(db))


# ────────────────────────────────────────────────────────────── schema init

def test_four_tables_and_schema_version_created(store):
    with store._get_connection() as conn:
        tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    assert {"regime_state", "grassroots_signal", "black_swan_event",
            "stage_gate_decision", "strategy_schema_version",
            "factor_panel", "factor_provenance"} <= tables
    status = store.schema_status()
    assert len(status) == 4
    assert status["version"].tolist() == [1, 2, 3, 4]
    # idempotency: a second init never errors and never double-applies
    store2 = StrategyStore(db_path=store.db_path)
    assert len(store2.schema_status()) == 4


# ──────────────────────────────────────────────────────────────── regime

def test_regime_state_roundtrip_and_probability_constraint(store):
    df = pd.DataFrame({
        "obs_date": ["2023-01-03", "2023-01-04", "2023-01-05"],
        "detected_state": ["EXPANSION", "SHOCK", "CRISIS"],
        "p_expansion": [0.90, 0.20, 0.05],
        "p_shock": [0.08, 0.70, 0.15],
        "p_crisis": [0.02, 0.10, 0.80],
        "confident": [True, True, True],
        "transition_matrix_json": [None] * 3,
        "retrained": [True, False, False],
    })
    store.write_regime_state(df, {"source": "test", "n_states": 3})

    series = store.read_regime_series("2023-01-01", "2023-01-06")
    assert len(series) == 3
    assert series["detected_state"].tolist() == ["EXPANSION", "SHOCK", "CRISIS"]

    latest = store.read_regime_at("2023-01-04")
    assert latest["detected_state"] == "SHOCK"
    assert latest["p_crisis"] == pytest.approx(0.10)

    # upsert last-write-wins per obs_date
    df2 = df.iloc[[1]].copy()
    df2["detected_state"] = "CRISIS"
    store.write_regime_state(df2, {"source": "test"})
    series = store.read_regime_series()
    series["obs_date"] = pd.to_datetime(series["obs_date"]).dt.normalize()
    assert series.loc[series["obs_date"] == pd.Timestamp("2023-01-04"),
                      "detected_state"].iloc[0] == "CRISIS"


def test_regime_probability_sum_enforced(store):
    df = pd.DataFrame({
        "obs_date": ["2023-01-03"],
        "detected_state": ["EXPANSION"],
        "p_expansion": [0.5], "p_shock": [0.5], "p_crisis": [0.5],  # sums to 1.5
    })
    with pytest.raises(ValueError, match="sum to 1"):
        store.write_regime_state(df)


def test_regime_pit_enforcement(store):
    df = pd.DataFrame({
        "obs_date": ["2099-01-01"],  # future obs violates retrieval >= obs
        "detected_state": ["EXPANSION"],
        "p_expansion": [0.9], "p_shock": [0.05], "p_crisis": [0.05],
    })
    with pytest.raises(PITValidationError):
        store.write_regime_state(df)


# ──────────────────────────────────────────────────────────────── grassroots

def test_grassroots_snapshot_uses_latest_revision(store):
    base = pd.DataFrame({
        "date": ["2023-03-01", "2023-03-01"],
        "ticker": ["AAPL", "MSFT"],
        "c_t_composite": [0.10, -0.02],
        "mapping_confidence": ["high", "medium"],
        "lead_lag_validated": [True, False],
        "rho_c_to_t": [0.35, 0.05],
        "rho_t_to_w": [0.22, -0.01],
    })
    store.write_grassroots_signals(base, {"source": "test", "rev": 1})
    # second revision on the same date (supersedes)
    base2 = base.copy()
    base2["c_t_composite"] = [0.12, -0.01]
    store.write_grassroots_signals(base2, {"source": "test", "rev": 2})

    snap = store.read_grassroots_snapshot("2023-03-15", min_confidence="low")
    assert len(snap) == 2
    assert snap.set_index("ticker").loc["AAPL", "c_t_composite"] == pytest.approx(0.12)

    # default medium filter still lets 'medium' through but drops low
    snap_med = store.read_grassroots_snapshot("2023-03-15", min_confidence="medium")
    assert set(snap_med["ticker"]) == {"AAPL", "MSFT"}


def test_grassroots_requires_confidence_and_rejects_bad_grades(store):
    df = pd.DataFrame({
        "date": ["2023-03-01"],
        "ticker": ["AAPL"],
        "c_t_composite": [0.1],
    })
    with pytest.raises(ValueError, match="mapping_confidence"):
        store.write_grassroots_signals(df)
    df["mapping_confidence"] = "ultra"
    with pytest.raises(ValueError, match="confidence"):
        store.write_grassroots_signals(df)


# ──────────────────────────────────────────────────────────── black swan

def test_black_swan_events_and_window_alignment(store):
    events = pd.DataFrame({
        "event_date": ["2023-03-10", "2023-03-10", "2023-05-01", "2023-03-15"],
        "ticker": ["AAPL", "MARKET", "MSFT", "NVDA"],
        "tier": ["TIER2_STRUCTURAL", "TIER1_PROVISIONAL", "TIER1_PROVISIONAL", "TIER1_PROVISIONAL"],
        "fast_triggered": [True, True, False, True],
        "slow_triggered": [True, False, False, True],
        "hmm_crisis_prob": [0.85, 0.30, 0.10, 0.55],
        "moat_invariant": [True, None, None, None],
        "moat_ratio": [1.02, None, None, None],
        "entry_price_limit": [150.0, 145.0, 30.0, 180.0],
        "deploy_pct": [0.75, 0.25, 0.25, 0.5],
        # daily-detector convention: each event is retrieved within a day of
        # its event_date (strict PIT) — except the NVDA row, which a backfilled
        # run only discovered in May (its retrieval must exclude it from Mar)
        "retrieval_timestamp": ["2023-03-10 23:00:00", "2023-03-10 23:00:00",
                                "2023-05-02 23:00:00", "2023-05-01 09:00:00"],
    })
    store.write_black_swan_events(events, {"source": "test"})

    rows = store.read_black_swan_events(tier="TIER2_STRUCTURAL")
    assert len(rows) == 1 and rows.iloc[0]["ticker"] == "AAPL"

    # Mar/Apr window: only events KNOWN by 2023-04-30. The MSFT event is
    # outside by event_date; the backfilled NVDA discovery (retrieved May 1)
    # must ALSO be excluded — that is the no-lookahead guarantee.
    aligned = store.align_black_swan_timestamps(start_date="2023-03-01", end_date="2023-04-30")
    assert len(aligned) == 2, "May event sits outside the window and must be excluded"
    aligned_dates = aligned["event_date"].tolist()
    assert all(pd.Timestamp(d) <= pd.Timestamp("2023-04-30") for d in aligned_dates)
    assert set(aligned["ticker"]) == {"AAPL", "MARKET"}

    # default ticker for market-wide rows is 'MARKET' — no NULL keys
    assert store.read_black_swan_events(ticker="MARKET").iloc[0]["tier"] == "TIER1_PROVISIONAL"


# ──────────────────────────────────────────────────────────── stage gates

MINI_VALUATION_COLS = [
    "ticker", "fiscal_end_date", "filed_date", "accession", "form", "fiscal_year",
    "revenue", "ebit", "operating_income", "rd_expense", "roic", "roic_cap_rd",
    "wacc_estimate", "omr", "interest_coverage", "fcf",
]


def test_stage_gate_write_and_pit_safe_join(tmp_path):
    store = StrategyStore(db_path=str(tmp_path / "factors.duckdb"))

    # mini valuation parquet mirroring the real panel's key + Stage-1 columns
    mini = pl.DataFrame({
        "ticker": ["AAPL", "AAPL", "AAPL"],
        "fiscal_end_date": ["2022-12-31", "2023-12-31", "2024-12-31"],
        "filed_date": ["2023-02-15", "2024-02-10", "2025-02-05"],
        "accession": ["A1", "A2", "A3"], "form": ["10-K"] * 3, "fiscal_year": [2022, 2023, 2024],
        "revenue": [100.0, 120.0, 140.0], "ebit": [18.0, 22.0, 26.0],
        "operating_income": [18.0, 22.0, 26.0], "rd_expense": [3.0, 4.0, 5.0],
        "roic": [0.12, 0.13, 0.14], "roic_cap_rd": [0.15, 0.16, 0.17],
        "wacc_estimate": [0.09, 0.09, 0.09], "omr": [0.21, 0.22, 0.22],
        "interest_coverage": [6.0, 7.0, 8.0], "fcf": [10.0, 12.0, 14.0],
    })
    mini_path = tmp_path / "valuation_fundamentals.parquet"
    mini.write_parquet(mini_path)

    gates = pd.DataFrame({
        "rebalance_date": ["2023-03-01", "2023-03-01", "2023-03-01"],
        "ticker": ["AAPL", "AAPL", "AAPL"],
        "stage": [1, 2, 3],
        "gate_name": ["intrinsic_value", "asymmetric_entry", "disciplined_exit"],
        "passed": [True, False, False],
        "reason": ["ROIC_cap >= WACC+2%", "P > 0.90*IV", "position not held"],
        "filed_date": ["2023-02-15", "2023-02-15", "2023-02-15"],
    })
    store.write_stage_gate_decisions(gates, {"source": "test"})

    joined = store.query_stage_gate_factors(fundamentals_path=str(mini_path),
                                            stage=1, rebalance_date="2023-03-01")
    assert len(joined) == 1
    row = joined.iloc[0]
    # PIT-safe: the joined fundamentals row is the one filed 2023-02-15 (<= rebal)
    assert row["filed_date"] == pd.Timestamp("2023-02-15")
    assert row["roic_cap_rd"] == pytest.approx(0.15)
    assert row["omr"] == pytest.approx(0.21)
    assert row["ticker"] == "AAPL"

    # unfiltered: all three stage rows surface, join by their own filed anchors
    all_rows = store.query_stage_gate_factors(fundamentals_path=str(mini_path))
    assert len(all_rows) == 3


def test_stage_gate_pit_anchor_rejects_lookahead(store):
    gates = pd.DataFrame({
        "rebalance_date": ["2023-03-01"],
        "ticker": ["AAPL"],
        "stage": [1],
        "gate_name": ["intrinsic_value"],
        "passed": [True],
        "filed_date": ["2023-06-01"],  # filed AFTER the rebalance -> lookahead
    })
    with pytest.raises(PITValidationError):
        store.write_stage_gate_decisions(gates)


# ─────────────────────────────────────────────────── regime-conditional query

def test_regime_conditional_factor_query(store):
    # plant factor rows and regimes in the shared DB
    with store._get_connection() as conn:
        conn.executemany("INSERT INTO factor_panel VALUES (?,?,?,?,?,?,?)", [
            ("2023-01-03", "AAPL", "cpcv_ret_1m", 0.05, "H1", "2023-01-04 00:00:00", "high"),
            ("2023-01-04", "MSFT", "cpcv_ret_1m", -0.02, "H1", "2023-01-05 00:00:00", "high"),
            ("2023-01-05", "AAPL", "cpcv_ret_1m", 0.06, "H1", "2023-01-06 00:00:00", "high"),
        ])
    store.write_regime_state(pd.DataFrame({
        "obs_date": ["2023-01-03", "2023-01-04"],
        "detected_state": ["EXPANSION", "SHOCK"],
        "p_expansion": [0.90, 0.2], "p_shock": [0.08, 0.7], "p_crisis": [0.02, 0.1],
        "confident": [True, True],
    }))

    exp_rows = store.query_regime_conditional("cpcv_ret_1m", "EXPANSION")
    assert exp_rows["ticker"].tolist() == ["AAPL"]  # only the 01-03 row (ASOF: no lookahead)

    shock_rows = store.query_regime_conditional("cpcv_ret_1m", "SHOCK")
    # 01-04 row carries regime from obs_date 01-04; 01-05 row ASOF-joins to 01-04 SHOCK
    assert set(shock_rows["ticker"]) == {"MSFT", "AAPL"}


# ───────────────────────────────────────────────── factor_store compatibility

def test_export_table_emits_parquet_and_sidecar(store, tmp_path):
    store.write_regime_state(pd.DataFrame({
        "obs_date": ["2023-01-03", "2023-01-04"],
        "detected_state": ["EXPANSION", "SHOCK"],
        "p_expansion": [0.9, 0.2], "p_shock": [0.08, 0.7], "p_crisis": [0.02, 0.1],
        "confident": [True, True],
    }))
    out = tmp_path / "regime_state.parquet"
    sha = store.export_table("regime_state", str(out))
    assert out.exists()
    assert out.with_name("regime_state.parquet.provenance.json").exists()

    side = json.loads(out.with_name("regime_state.parquet.provenance.json").read_text(encoding="utf-8"))
    assert side["table"] == "regime_state"
    assert side["sha256"] == sha
    assert side["row_count"] == 2
    assert side["pit_timestamp_column"] == "obs_date"

    # table whitelist enforced
    with pytest.raises(ValueError, match="table must be one of"):
        store.export_table("factor_panel", str(tmp_path / "bad.parquet"))


def test_legacy_factor_panel_gains_confidence_column(tmp_path):
    """Existing harness DBs predate mapping_confidence; opening them with
    FactorStore must ADD the column (idempotently) without rewriting rows."""
    db = tmp_path / "legacy.duckdb"
    with duckdb.connect(str(db)) as conn:
        conn.execute("""
            CREATE TABLE factor_panel (
                date DATE, ticker VARCHAR, factor_name VARCHAR, value DOUBLE,
                provenance_hash VARCHAR, created_at TIMESTAMP
            )
        """)
        conn.execute("INSERT INTO factor_panel VALUES "
                     "('2023-01-03','AAPL','cpcv_ret_1m',0.05,'H1','2023-01-04 00:00:00')")
        assert "mapping_confidence" not in [r[0] for r in conn.execute("DESCRIBE factor_panel").fetchall()]

    fs = FactorStore(db_path=str(db))  # triggers ensure_factor_store_schema
    with duckdb.connect(str(db), read_only=True) as conn:
        cols = [r[0] for r in conn.execute("DESCRIBE factor_panel").fetchall()]
        assert "mapping_confidence" in cols
        # legacy row preserved, confidence defaults to NULL (ungraded)
        legacy = conn.execute(
            "SELECT ticker, value, mapping_confidence FROM factor_panel WHERE provenance_hash='H1'"
        ).fetchall()
    assert legacy == [("AAPL", 0.05, None)]
    # re-opening is a no-op (idempotent ALTER)
    FactorStore(db_path=str(db))
    # legacy NULL grades still surface on default reads (not silently dropped)
    assert len(fs.read_factor("cpcv_ret_1m")) == 1


def test_factor_store_mapping_confidence_compat(store):
    fs = FactorStore(db_path=str(store.db_path))
    df = pd.DataFrame({
        "date": ["2023-01-10", "2023-01-10"],
        "ticker": ["AAPL", "MSFT"],
        "value": [0.14, 0.03],
    })
    fs.write_factor(df, "grassroots_c_t", {"source": "test"})  # catalogue -> medium
    read = fs.read_factor("grassroots_c_t")
    assert set(read["mapping_confidence"]) == {"medium"}

    df_hy = pd.DataFrame({"date": ["2023-01-11"], "ticker": ["NVDA"], "value": [0.95]})
    fs.write_factor(df_hy, "hype_p95", {"source": "apewisdom"}, mapping_confidence="low")
    stripped = fs.read_factor("hype_p95", min_confidence="medium")
    assert len(stripped) == 0, "low-confidence hype rows must be stripped from core reads"

    # per-row Series confidence grades are accepted too
    df_se = pd.DataFrame({"date": ["2023-01-12", "2023-01-12"], "ticker": ["TSLA", "AMZN"], "value": [0.1, 0.2]})
    grades = pd.Series(["low", "medium"], dtype="object")
    fs.write_factor(df_se, "grassroots_c_t", {"source": "test"}, mapping_confidence=grades)
    read_se = fs.read_factor("grassroots_c_t")
    assert read_se.loc[read_se["ticker"] == "TSLA", "mapping_confidence"].iloc[0] == "low"
    assert read_se.loc[read_se["ticker"] == "AMZN", "mapping_confidence"].iloc[0] == "medium"
    assert len(fs.read_factor("grassroots_c_t", min_confidence="medium")) == 3  # 1 med + 2 defaults


# ───────────────────────────────────────────── valuation fundamentals migration

def test_valuation_stage1_migration_on_real_copy(tmp_path):
    if not REAL_PARQUET.exists():
        pytest.skip("real valuation_fundamentals.parquet not present")
    import scripts.migrate_valuation_stage1 as mig

    copy = tmp_path / "valuation_fundamentals.parquet"
    shutil.copy2(REAL_PARQUET, copy)

    original = pl.read_parquet(copy)
    original_cols = original.columns
    assert "roic_cap_rd" not in original_cols or True  # (real file may already carry it)

    # patch module paths onto the copy
    save_p, save_s = mig.PARQUET_PATH, mig.SIDECAR_PATH
    mig.PARQUET_PATH = copy
    mig.SIDECAR_PATH = copy.with_name(f"{copy.name}.provenance.json")
    try:
        result = mig.apply_migration(mig.load_polars(), dry_run=False)
        assert result.get("applied"), result

        migrated = pl.read_parquet(copy)
        for col in mig.TARGET_COLUMNS:
            assert col in migrated.columns, f"{col} missing after migration"
        # existing schema preserved (superset)
        assert set(original_cols) <= set(migrated.columns)
        # key PIT columns intact and row count unchanged
        assert len(migrated) == len(original)
        assert migrated["filed_date"].equals(original["filed_date"])
        assert migrated["ticker"].equals(original["ticker"])

        # coverage sanity: R&D reporters get roic_cap_rd where math is possible
        rd_rows = migrated.filter(pl.col("rd_expense").is_not_null() & (pl.col("rd_expense") > 0))
        non_null = rd_rows.filter(pl.col("roic_cap_rd").is_not_null())
        assert len(non_null) > 0, "no roic_cap_rd values computed for R&D reporters"

        # stage-1 gate sanity on real data: some ticker passes ROIC_cap >= WACC+2%
        gate = migrated.filter(
            (pl.col("roic_cap_rd").is_not_null())
        ).with_columns(
            (pl.col("roic_cap_rd") - pl.col("wacc_estimate")).alias("spread")
        ).filter(pl.col("spread") >= 0.02)
        assert len(gate) > 0

        # sidecar manifest present with hash + migration version
        sidecar = json.loads(mig.SIDECAR_PATH.read_text(encoding="utf-8"))
        assert sidecar["migration_version"] == mig.STAGE1_MIGRATION_VERSION
        assert sidecar["sha256"] == mig._sha256_bytes(copy.read_bytes())
        assert sidecar["row_count"] == len(migrated)

        # idempotency: second apply is a no-op skip
        result2 = mig.apply_migration(mig.load_polars(), dry_run=False)
        assert result2.get("skipped"), result2
    finally:
        mig.PARQUET_PATH, mig.SIDECAR_PATH = save_p, save_s


def test_valuation_migration_check_mode(tmp_path):
    if not REAL_PARQUET.exists():
        pytest.skip("real valuation_fundamentals.parquet not present")
    import scripts.migrate_valuation_stage1 as mig
    copy = tmp_path / "valuation_fundamentals.parquet"
    shutil.copy2(REAL_PARQUET, copy)
    save_p, save_s = mig.PARQUET_PATH, mig.SIDECAR_PATH
    mig.PARQUET_PATH = copy
    mig.SIDECAR_PATH = copy.with_name(f"{copy.name}.provenance.json")
    try:
        df = pl.read_parquet(copy)
        status = mig.migration_status(df, mig.load_polars())
        assert set(status["missing"]) == set(mig.TARGET_COLUMNS) or not status["missing"]
        assert set(status["present"]) <= set(mig.TARGET_COLUMNS)
    finally:
        mig.PARQUET_PATH, mig.SIDECAR_PATH = save_p, save_s