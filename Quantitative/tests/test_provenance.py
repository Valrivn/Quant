"""
Unit Tests for Data Provenance & Zero-Synthetic Policy Enforcement
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import pandas as pd
import pytest

from Quantitative.contracts.feature_contract import MANDATORY_FRED_SERIES, assert_no_synthetic_data
from ingestion.trade_audit import TradeAuditLedger, _uuid7_ns, time_ns_now
from research.ml_parameter_optimization import (
    GENESIS_HASH,
    TrialTracker,
    chain_hash,
    new_trial_uuid7,
)


def test_fred_cache_completeness():
    cache_dir = Path("data/fred_cache")
    assert cache_dir.exists(), "data/fred_cache directory must exist"

    for series_id in MANDATORY_FRED_SERIES:
        parquet_file = cache_dir / f"{series_id}.parquet"
        assert parquet_file.exists(), f"Mandatory FRED series parquet missing: {series_id}"
        
        df = pd.read_parquet(parquet_file)
        assert "date" in df.columns
        assert "value" in df.columns
        assert not df["value"].dropna().empty, f"FRED cache series {series_id} contains no valid data"


def test_assert_no_synthetic_data_guard():
    # Valid dataframe
    df_valid = pd.DataFrame({"CREDIT_SPREAD_VELOCITY_21D": [0.1, 0.2, 0.15]})
    assert_no_synthetic_data(df_valid)

    # Constant / synthetic data dataframe
    df_constant = pd.DataFrame({"CREDIT_SPREAD_VELOCITY_21D": [1.0, 1.0, 1.0]})
    with pytest.raises(RuntimeError, match="Synthetic/fallback data detected"):
        assert_no_synthetic_data(df_constant)

    # All zero dataframe
    df_zeros = pd.DataFrame({"CREDIT_SPREAD_VELOCITY_21D": [0.0, 0.0, 0.0]})
    with pytest.raises(RuntimeError, match="Synthetic/fallback data detected"):
        assert_no_synthetic_data(df_zeros)


def test_uuid7_monotonic_generation():
    u1, ts1, i1 = new_trial_uuid7()
    u2, ts2, i2 = new_trial_uuid7()
    assert i2 >= i1
    assert str(u1) != str(u2)
    # Time-ordered ns prefix check
    u1_ns = _uuid7_ns(time_ns_now())
    u2_ns = _uuid7_ns(time_ns_now() + 1000)
    assert u1_ns != u2_ns


def test_timestamp_ns_monotonic():
    tracker = TrialTracker()
    ts1 = tracker._next_timestamp_ns()
    ts2 = tracker._next_timestamp_ns()
    assert ts2 > ts1


def test_trial_tracker_chain_sha256():
    tracker = TrialTracker()
    assert tracker.chain_head == GENESIS_HASH

    t1 = tracker.log_trial(trial_id=0, params={"param1": 0.5}, metric_value=0.8)
    assert "chain_sha256" in t1
    assert len(t1["chain_sha256"]) == 64
    assert tracker.chain_head == t1["chain_sha256"]

    t2 = tracker.log_trial(trial_id=1, params={"param1": 0.6}, metric_value=0.85)
    assert t2["chain_sha256"] != t1["chain_sha256"]
    assert tracker.chain_head == t2["chain_sha256"]


def test_trial_tracker_513_trials_enumeration():
    tracker = TrialTracker(target_n=513)
    for i in range(513):
        tracker.log_trial(trial_id=i, params={"alpha": i * 0.001}, metric_value=0.5 + i * 0.0001)

    assert len(tracker.trials) == 513
    verify_res = tracker.verify_chain()
    assert verify_res["ok"] is True
    assert verify_res["n_trials"] == 513


def test_trial_tracker_evaluated_metric_status():
    tracker = TrialTracker(metric_status="EVALUATED")
    assert tracker.metric_status == "EVALUATED"
    entry = tracker.log_trial(trial_id=0, params={"x": 1}, metric_value=0.92)
    assert "trial_uuid" in entry
    assert "chain_sha256" in entry


def test_trial_tracker_genesis_hash():
    assert len(GENESIS_HASH) == 64
    assert GENESIS_HASH == "c80a47bf6ae8d1c595f3b5e607c2588478bec0c47c4f0f97eb6c374b277a1685"


def test_trade_audit_ledger_chain_verification(tmp_path):
    ledger = TradeAuditLedger(ledger_dir=tmp_path / "audit")
    d = pd.Timestamp("2026-09-20")
    tr = {"valuation": {"iv_p25": 100.0, "price": 90.0, "discount_pct": -0.10,
                        "iv_method": "dcf_v2_nowcast", "iv_basis_filed_date": "2026-09-15",
                        "iv_nowcast_ts": "2026-09-20T00:00:00Z"}}

    blk = ledger.append(ticker="AAPL", asof=d, wacc_v2=0.10, ke_v2=0.12,
                        kd_pre_tax_v2=0.04, erp_blend=0.04, rf_10y=0.04,
                        beta_source="sector", action="ENTRY", gate_name="VALUATION",
                        stage_gate=1, trigger_reason=tr)

    assert blk.prev_chain_hash == "GENESIS"
    assert len(blk.chain_hash) == 64
    ok, n_blocks, break_idx = ledger.verify_chain("2026-09")
    assert ok is True
    assert n_blocks == 1
    assert break_idx is None


def test_trade_audit_ledger_single_writer_lock(tmp_path):
    ledger = TradeAuditLedger(ledger_dir=tmp_path / "audit")
    lock_file = tmp_path / "audit" / ".ledger.lock"
    assert not lock_file.exists()
    d = pd.Timestamp("2026-09-20")
    tr = {"valuation": {"iv_p25": 100.0, "price": 90.0, "discount_pct": -0.10,
                        "iv_method": "dcf_v2_nowcast", "iv_basis_filed_date": "2026-09-15",
                        "iv_nowcast_ts": "2026-09-20T00:00:00Z"}}

    ledger.append(ticker="MSFT", asof=d, wacc_v2=0.10, ke_v2=0.12,
                  kd_pre_tax_v2=0.04, erp_blend=0.04, rf_10y=0.04,
                  beta_source="sector", action="ENTRY", gate_name="VALUATION",
                  stage_gate=1, trigger_reason=tr)
    # Lock is acquired and released atomically during append
    assert not lock_file.exists()

