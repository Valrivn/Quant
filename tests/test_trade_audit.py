"""Phase 1 (Schemas & WACC Engine) — unit tests for the immutable chained
trade_audit ledger (ingestion/trade_audit.py, analysis §6.1-§6.4, frozen
config/pre_registration_phase8.yaml trade_audit block).

Covers: two-layer hashing oracle, tamper-evidence, immutability (no block
rewrite), trigger_reason Appendix A JSON Schema, stage_gate enum {1..5},
no back-append into sealed months, cross-partition chaining, single-writer
lock, retention guard, and all-or-nothing batch appends.
"""

import json
import time

import pandas as pd
import pytest

from ingestion.trade_audit import (
    GENESIS,
    TradeAuditLedger,
    _WriteLock,
    validate_trigger_reason,
)


def _valid_trigger(ts="2026-09-14T12:00:00.000000000"):
    return {
        "valuation": {
            "iv_p25": 112.4,
            "price": 118.0,
            "discount_pct": 0.0474,
            "iv_method": "dcf_v2_nowcast",
            "iv_basis_filed_date": "2026-09-12",
            "iv_nowcast_ts": ts,
        }
    }


def _payload(asof="2026-09-14", ticker="AAPL", trigger=None, **kw):
    base = dict(
        ticker=ticker,
        asof=pd.Timestamp(asof),
        wacc_v2=0.088,
        ke_v2=0.095,
        kd_pre_tax_v2=0.045,
        erp_blend=0.045,
        rf_10y=0.042,
        beta_source="sector_bottom_up",
        retrieval_ts=pd.Timestamp("2026-09-14T12:00:00.000000000"),
        action="ENTRY",
        gate_name="VALUATION",
        stage_gate=1,
        trigger_reason=trigger if trigger is not None else _valid_trigger(),
    )
    base.update(kw)
    return base


@pytest.fixture
def ledger(tmp_path):
    return TradeAuditLedger(ledger_dir=tmp_path)


# ---------------------------------------------------------------------------
# Chaining + verification oracle
# ---------------------------------------------------------------------------

def test_basic_chain_and_verify(ledger):
    b1 = ledger.append(**_payload())
    b2 = ledger.append(**_payload(ticker="MSFT"))
    assert b1.prev_chain_hash == GENESIS
    assert b2.prev_chain_hash == b1.chain_hash
    ok, n, brk = ledger.verify_chain("2026-09", verify_hashes=True)
    assert ok and n == 2 and brk is None
    gres = ledger.verify_ledger(cross_partition=True)
    assert gres["intact"] is True and gres["n_blocks"] == 2


def test_record_hash_is_frozen_and_recomputable(ledger):
    """§6.2 oracle: recompute(record, chain) must reproduce stored hashes."""
    b = ledger.append(**_payload())
    rec, chn = b.recompute()
    assert rec == b.record_hash
    assert chn == b.chain_hash
    assert len(b.record_hash) == 64 and len(b.chain_hash) == 64


def test_tamper_evidence_detected(ledger):
    """Retro-edit of a persisted block must break the chain at that block."""
    ledger.append(**_payload())
    ledger.append(**_payload(ticker="MSFT"))
    path = ledger._partition_path("2026-09")
    import pandas as _pd
    df = _pd.read_parquet(path)
    df.loc[0, "erp_blend"] = 0.999  # tamper
    df.to_parquet(path, index=False)
    ok, n, brk = ledger.verify_chain("2026-09", verify_hashes=True)
    assert ok is False and brk == 1  # recompute oracle flags the tampered block


def test_immutability_duplicate_block_refused(ledger):
    ledger.append(**_payload())
    with pytest.raises(ValueError):
        ledger.append(**_payload())  # same (asof, ticker, action) -> same block_id


def test_chain_seed_cross_partition(ledger):
    """First block of month P+1 chains to the last block of month P."""
    ledger.append(**_payload(asof="2026-09-14"))
    ledger.append(**_payload(asof="2026-10-14"))
    months = ledger._existing_months()
    assert months == ["2026-09", "2026-10"]
    b_last_sep = ledger._last_chain_hash_of("2026-09")
    oct_blocks = ledger._load_partition("2026-10")
    assert oct_blocks[0].prev_chain_hash == b_last_sep
    gres = ledger.verify_ledger(cross_partition=True)
    assert gres["intact"] is True and gres["n_blocks"] == 2


# ---------------------------------------------------------------------------
# Validation gates
# ---------------------------------------------------------------------------

def test_trigger_reason_json_schema_rejects_malformed():
    good = _valid_trigger()
    validate_trigger_reason(good)  # no raise
    with pytest.raises(ValueError):
        validate_trigger_reason({"valuation": {"iv_p25": 1.0}})  # missing props
    with pytest.raises(ValueError):
        validate_trigger_reason({"grassroots": {}})              # wrong section
    with pytest.raises(ValueError):
        validate_trigger_reason({})                              # no valuation
    with pytest.raises(ValueError):
        validate_trigger_reason(None)
    with pytest.raises(ValueError):
        # frozen Appendix A: additionalProperties:false is TOP-LEVEL only
        # (extra keys inside "valuation" are permitted by the frozen schema)
        validate_trigger_reason({"valuation": good["valuation"], "extra_toplevel": 1})


def test_append_rejects_bad_trigger_reason(ledger):
    with pytest.raises(ValueError):
        ledger.append(**_payload(trigger={"valuation": {"iv_p25": 1.0}}))
    assert len(ledger._load_partition("2026-09")) == 0  # nothing persisted


def test_stage_gate_enum_enforced(ledger):
    for g in (1, 2, 3, 4, 5):
        ledger.append(**_payload(ticker=f"T{g}E", stage_gate=g))
    with pytest.raises((ValueError, AssertionError)):
        ledger.append(**_payload(ticker="BAD", stage_gate=9))


def test_back_append_into_sealed_month_refused(ledger):
    ledger.append(**_payload(asof="2026-10-14"))       # newest month: 2026-10
    with pytest.raises((ValueError, AssertionError)):
        ledger.append(**_payload(asof="2026-09-14"))   # older -> sealed


def test_retention_guard_refuses_outside_10y_window(ledger):
    with pytest.raises(ValueError):
        ledger.append(**_payload(asof="2000-01-03"))   # 26 years ago


def test_batch_all_or_nothing(ledger):
    """One invalid payload rejects the whole batch (nothing persisted)."""
    good1 = _payload(ticker="NVDA")
    bad = _payload(ticker="AMD", trigger={"valuation": {"iv_p25": 1.0}})
    good2 = _payload(ticker="INTC")
    with pytest.raises(ValueError):
        ledger.append_batch([good1, bad, good2])
    assert ledger._load_partition("2026-09") == []


def test_batch_sorted_and_chained(ledger):
    """Batch with shuffled dates persists in (month, asof, retrieval) order."""
    blocks = ledger.append_batch([
        _payload(asof="2026-09-15", ticker="B"),
        _payload(asof="2026-09-14", ticker="A"),
        _payload(asof="2026-09-15", ticker="C"),
    ])
    assert blocks[0].ticker == "A" and blocks[1].ticker == "B"
    ok, n, brk = ledger.verify_chain("2026-09", verify_hashes=True)
    assert ok and n == 3 and brk is None


# ---------------------------------------------------------------------------
# Locking, probe, persisted read-back
# ---------------------------------------------------------------------------

def test_single_writer_lock_round_trip(ledger):
    with _WriteLock(ledger.ledger_dir):
        pass  # acquired + released
    with _WriteLock(ledger.ledger_dir):
        pass  # re-acquirable after release


def test_lock_refuses_concurrent_writer(ledger):
    lock = _WriteLock(ledger.ledger_dir)
    lock.acquire()
    try:
        with pytest.raises(OSError):
            _WriteLock(ledger.ledger_dir).acquire()  # held by same live pid
    finally:
        lock.release()


def test_probe_reports_frozen_contract(ledger):
    pr = ledger.probe()
    assert set(pr["config"]["stage_gates"]) == {1, 2, 3, 4, 5}
    assert pr["config"]["retention_years"] == 10
    assert pr["config"]["timestamp_precision"] == "nanosecond"


def test_persisted_read_back_matches_fields(ledger):
    blk = ledger.append(**_payload(
        exec_slippage_bps=3.5, exec_impact_bps=1.2, exec_fill_pct=0.995,
        pnl_attribution={"slippage": {"realized_bps": 3.5}}))
    loaded = ledger.load_month("2026-09")
    assert len(loaded) == 1
    b = loaded[0]
    assert b.block_id == blk.block_id
    assert b.exec_slippage_bps == pytest.approx(3.5)
    assert b.pnl_attribution == {"slippage": {"realized_bps": 3.5}}
    assert b.trigger_reason["valuation"]["iv_method"] == "dcf_v2_nowcast"
    rec, chn = b.recompute()
    assert rec == b.record_hash and chn == b.chain_hash


def test_uuid7_time_ordering(ledger):
    """block_id PK starts with a time-ordered ns prefix (C2.2)."""
    b1 = ledger.append(**_payload())
    time.sleep(0.002)
    b2 = ledger.append(**_payload(ticker="MSFT"))
    assert b1.block_id < b2.block_id  # lexicographic ns ordering
    assert len(b1.block_id) == 36  # uuid string


def test_manifest_written_with_boundary_hashes(ledger):
    ledger.append(**_payload())
    ledger.append(**_payload(ticker="MSFT"))
    mf = json.loads(ledger._manifest_path("2026-09").read_text(encoding="utf-8"))
    blocks = ledger._load_partition("2026-09")
    assert mf["first_chain_hash"] == blocks[0].chain_hash
    assert mf["last_chain_hash"] == blocks[-1].chain_hash
    assert mf["n_blocks"] == 2
    assert mf["sealed_at"]  # ISO ts present
# ---------------------------------------------------------------------------
# B4 — replay idempotence, cross-partition continuity, fault injection
# ---------------------------------------------------------------------------

def test_replay_is_idempotent_and_chain_stays_intact(ledger):
    """
    Replaying the SAME logical trade must not fork the chain or double count.
    block_id is a content-derived UUIDv7, so an identical payload reproduces the
    same id and the ledger REFUSES the write — replay is a no-op, not a second
    block.
    """
    p = _payload()
    b1 = ledger.append(**p)
    with pytest.raises(ValueError, match="refusing rewrite"):
        ledger.append(**p)              # same ticker/asof/payload -> replay
    res = ledger.verify_ledger(cross_partition=True)
    assert res["intact"] is True
    assert res["n_blocks"] == 1         # the replay added nothing
    assert ledger.load_month("2026-09")[0].block_id == b1.block_id


def test_chain_spans_month_partitions_continuously(ledger):
    """
    A partition boundary must not reset the hash chain: block N+1 in the next
    month still carries block N's chain hash, so verify_ledger can walk the
    whole history rather than each month in isolation.
    """
    sept = ledger.append(**_payload(asof=pd.Timestamp("2026-09-30")))
    octo = ledger.append(**_payload(asof=pd.Timestamp("2026-10-01"),
                                    ticker="MSFT"))
    assert octo.prev_chain_hash == sept.chain_hash
    res = ledger.verify_ledger(cross_partition=True)
    assert res["intact"] is True
    assert res["n_blocks"] == 2


def test_verify_ledger_detects_mid_chain_tamper(ledger):
    """
    Fault injection: rewrite a persisted block's payload in place and confirm
    the chain verification FAILS rather than silently recomputing a clean
    chain from the tampered bytes.
    """
    a = ledger.append(**_payload())
    b = ledger.append(**_payload(ticker="MSFT"))
    c = ledger.append(**_payload(ticker="NVDA"))
    assert ledger.verify_ledger(cross_partition=True)["intact"] is True

    part = ledger._load_partition("2026-09")
    path = ledger._partition_path("2026-09")
    assert len(part) == 3
    # tamper the middle block's stored record hash in the persisted partition
    stored = pd.read_parquet(path)
    mid = stored["block_id"].tolist().index(b.block_id)
    stored.loc[mid, "record_hash"] = "0" * 64
    stored.to_parquet(path, index=False)
    res = ledger.verify_ledger(cross_partition=True)
    assert res["intact"] is False
    ok, n, brk = ledger.verify_chain("2026-09", verify_hashes=True)
    assert ok is False
    assert brk is not None


def test_ledger_survives_reopen_and_keeps_chain(ledger, tmp_path):
    """A fresh ledger object on the same directory continues the chain."""
    b1 = ledger.append(**_payload())
    reopened = TradeAuditLedger(ledger_dir=ledger.ledger_dir)
    b2 = reopened.append(**_payload(ticker="MSFT"))
    assert b2.prev_chain_hash == b1.chain_hash
    assert reopened.verify_ledger(cross_partition=True)["intact"] is True
