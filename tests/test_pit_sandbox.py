"""Counter-tests from the B-20260823-001 council, as executable gates.

DA-1  timestamp coverage >= 80% per source (else quarantine, mechanism = theater)
DA-2  oracle transfer check: benchmark F1 vs held-out REAL inputs, >15pp drop = miscalibrated
B-1   scoring-instrument provenance: training cutoff <= window start OR rule-based
B-2   cross-domain divergence check (same harness as DA-2)
SG-1  frozen-hash regression replay: mutating post-date rows must yield bit-identical outputs
Plus mechanism tests: quarantine routing, clock-step filtering incl. JOIN bypass.
"""
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.schema_pit import SANDBOX_TABLES, create_pit_tables, ingest_pit_rows  # noqa: E402
from db.pit_reader import pit_query  # noqa: E402

BARS = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "config" / "weights_sentinel_bars.yaml").read_text()
)


@pytest.fixture
def pit_db():
    conn = sqlite3.connect(":memory:")
    create_pit_tables(conn)
    return conn


# ---------------------------------------------------------------- mechanism

def test_undated_rows_quarantined_not_dropped(pit_db):
    result = ingest_pit_rows(pit_db, "pit_transcripts", [
        {"symbol": "AAPL", "event_ts": "2010-02-01 16:30:00", "available_as_of": "2010-02-01", "content": "dated"},
        {"symbol": "ORPH", "event_ts": "", "available_as_of": None, "content": "undated"},
    ])
    assert result == {"accepted": 1, "quarantined": 1}
    assert pit_db.execute("SELECT COUNT(*) FROM pit_transcripts").fetchone()[0] == 1
    # quarantined row survives in _excluded — auditable, never silently dropped
    assert pit_db.execute("SELECT symbol FROM pit_transcripts_excluded").fetchone()[0] == "ORPH"
    log = pit_db.execute("SELECT detail_json FROM pit_audit_log ORDER BY audit_id DESC").fetchone()[0]
    assert json.loads(log)["quarantined"] == 1


def test_clock_step_hides_future_rows(pit_db):
    ingest_pit_rows(pit_db, "pit_transcripts", [
        {"symbol": "OLD", "event_ts": "2009-06-01 09:00:00", "available_as_of": "2009-06-01"},
        {"symbol": "NEW", "event_ts": "2024-06-01 09:00:00", "available_as_of": "2024-06-01"},
    ])
    old = pit_query(pit_db, "2009-12-31", "SELECT symbol FROM v_pit_transcripts")
    assert [r[0] for r in old] == ["OLD"]
    both = pit_query(pit_db, "2025-12-31", "SELECT symbol FROM v_pit_transcripts")
    assert sorted(r[0] for r in both) == ["NEW", "OLD"]


def test_join_through_views_still_filters(pit_db):
    """sim-guardian hole: complex joins must not bypass temporal filtering."""
    ingest_pit_rows(pit_db, "pit_transcripts", [
        {"symbol": "FUT", "event_ts": "2023-03-01 09:00:00", "available_as_of": "2023-03-01"},
    ])
    ingest_pit_rows(pit_db, "pit_market_labels", [
        {"row_id": 1, "horizon_days": 5, "forward_return": 0.05, "available_as_of": "2023-04-01"},
    ])
    rows = pit_query(
        pit_db,
        "2018-01-01",
        "SELECT t.symbol, m.forward_return FROM v_pit_transcripts t "
        "JOIN v_pit_market_labels m ON m.row_id = t.row_id",
    )
    assert rows == []  # both rows are future at 2018; join must leak nothing


def test_raw_table_access_blocked(pit_db):
    with pytest.raises(PermissionError):
        pit_query(pit_db, "2009-01-01", "SELECT * FROM pit_transcripts")
    with pytest.raises(PermissionError):
        pit_query(pit_db, "2009-01-01", "DELETE FROM v_pit_transcripts")


# ------------------------------------------------------------------ DA-1

def test_da1_timestamp_coverage_gate():
    from scripts.pit_phase0_audit import coverage_pct_for_source
    # fixture manifest: 4 dated + 1 undated = 80% -> exactly at bar
    manifest = {"pit_transcripts": [
        {"symbol": "A", "date": "2009-02-01"}, {"symbol": "B", "date": "2009-05-01"},
        {"symbol": "C", "date": "2010-08-01"}, {"symbol": "D", "date": "2011-02-01"},
        {"symbol": "E", "date": ""},
    ]}
    pct = coverage_pct_for_source(manifest, "pit_transcripts")
    assert pct == pytest.approx(80.0)
    gate = BARS["phase0"]["timestamp_coverage_min_pct"]
    assert pct >= gate  # passes AT the bar
    # one more undated -> 66.7% -> fails, source must be quarantined wholesale
    manifest["pit_transcripts"].append({"symbol": "F", "date": ""})
    assert coverage_pct_for_source(manifest, "pit_transcripts") < gate


# ------------------------------------------------------- DA-2 / B-2 harness

def _f1(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return 2 * p * r / (p + r) if p + r else 0.0


def test_da2_oracle_transfer_detects_miscalibrated_oracle():
    """Benchmark F1 fine on PhraseBank-style input but collapses on real transcripts."""
    from scripts.pit_phase0_audit import oracle_transfer_verdict
    benchmark_f1 = _f1(62, 18, 20)      # ~0.766 on news-domain sentences
    real_input_f1 = _f1(30, 45, 55)     # ~0.376 on held-out REAL pipeline inputs
    verdict = oracle_transfer_verdict(benchmark_f1, real_input_f1, BARS)
    assert not verdict["passed"] and verdict["drop_pp"] > 15.0
    ok = oracle_transfer_verdict(benchmark_f1, 0.68, BARS)
    assert ok["passed"]


# ------------------------------------------------------------------- B-1

def test_b1_instrument_provenance_registry():
    from scripts.pit_phase0_audit import instrument_provenance_ok
    cutoff = BARS["phase0"]["provenance_rule"]
    assert instrument_provenance_ok("loughran_mcdonald_dict", trained=False, cutoff=None, rule=cutoff)
    assert instrument_provenance_ok("vader_lexicon", trained=False, cutoff=None, rule=cutoff)
    # a transformer finetuned in 2023 is contaminated for a 2009 window start
    assert not instrument_provenance_ok("finbert_2023", trained=True, cutoff="2023-06-01", rule=cutoff)


# ------------------------------------------------------------------- SG-1

def _snapshot_hash(conn, as_of):
    queries = [
        "SELECT symbol, event_ts, available_as_of FROM v_pit_transcripts ORDER BY symbol",
        "SELECT row_id, horizon_days, forward_return, available_as_of FROM v_pit_market_labels ORDER BY row_id",
    ]
    blob = json.dumps([pit_query(conn, as_of, q) for q in queries], sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()


def test_sg1_frozen_hash_replay_bit_identical_under_future_mutation(pit_db):
    ingest_pit_rows(pit_db, "pit_transcripts", [
        {"symbol": "PAST", "event_ts": "2010-01-15 09:30:00", "available_as_of": "2010-01-15"},
        {"symbol": "FUTURE", "event_ts": "2025-03-10 09:30:00", "available_as_of": "2025-03-10"},
    ])
    ingest_pit_rows(pit_db, "pit_market_labels", [
        {"row_id": 1, "horizon_days": 5, "forward_return": 0.02, "available_as_of": "2010-01-22"},
        {"row_id": 2, "horizon_days": 5, "forward_return": -0.07, "available_as_of": "2025-03-17"},
    ])
    as_of = "2012-01-01"
    before = _snapshot_hash(pit_db, as_of)

    # adversary mutates AND deletes post-date rows
    pit_db.execute("UPDATE pit_transcripts SET content='tampered' WHERE symbol='FUTURE'")
    pit_db.execute("DELETE FROM pit_market_labels WHERE row_id=2")
    pit_db.execute("INSERT INTO pit_scores (row_id, instrument, score, computed_as_of) VALUES (2,'x',9.9,'2026-01-01')")
    pit_db.commit()

    after = _snapshot_hash(pit_db, as_of)
    assert before == after, "look-ahead leak: past-as-of output changed when future rows mutated"


def test_sg1_replay_catches_actual_leak(pit_db):
    """Negative control: if a query DOES read raw tables, replay must fail loudly."""
    ingest_pit_rows(pit_db, "pit_transcripts", [
        {"symbol": "PAST", "event_ts": "2010-01-15 09:30:00", "available_as_of": "2010-01-15"},
        {"symbol": "FUTURE", "event_ts": "2025-03-10 09:30:00", "available_as_of": "2025-03-10"},
    ])

    def leaky_snapshot(c, as_of):
        return hashlib.sha256(
            str(c.execute("SELECT symbol FROM pit_transcripts WHERE event_ts <= ?",
                          (as_of + " 23:59:59",)).fetchall()).encode()
        ).hexdigest()

    before = leaky_snapshot(pit_db, "2012-01-01")
    pit_db.execute("UPDATE pit_transcripts SET event_ts='1999-01-01 00:00:00' WHERE symbol='FUTURE'")
    pit_db.commit()
    assert leaky_snapshot(pit_db, "2012-01-01") != before  # raw access leaks; only v_* path is clean
