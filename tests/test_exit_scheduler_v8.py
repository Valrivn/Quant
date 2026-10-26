"""
test_exit_scheduler_v8.py — Unit tests for ExitScheduler (Phase 8 Redesign)
"""

import numpy as np
import pandas as pd
import pytest

from Quantitative.exit_scheduler import ExitScheduler, emit_exit_audit_records
from ingestion.trade_audit import TradeAuditLedger


def test_determine_twap_days():
    scheduler = ExitScheduler()

    # Liquid name ($50M ADV)
    d_exp = scheduler.determine_twap_days(adv_usd=50_000_000.0, regime="EXPANSION")
    d_cri = scheduler.determine_twap_days(adv_usd=50_000_000.0, regime="CRISIS")
    assert d_exp == 5
    assert d_cri == 2

    # Illiquid name ($2M ADV < $5M threshold) -> extended by 2 days
    d_cri_ill = scheduler.determine_twap_days(adv_usd=2_000_000.0, regime="CRISIS")
    assert d_cri_ill == 4  # 2 + 2 = 4 days


def test_schedule_exits_adv_limit():
    scheduler = ExitScheduler()

    # Position: 100,000 shares @ $10 = $1M notional. ADV = $1M ($100k shares/day).
    # 5% ADV daily limit = 5,000 shares/day.
    signals = [
        {"ticker": "SMALL", "shares": 100_000, "price": 10.0, "adv_usd": 1_000_000.0}
    ]

    slices = scheduler.schedule_exits(signals, regime="SHOCK")
    # Base TWAP days for SHOCK is 3 days (33k shares/day), but 5% ADV cap forces extension
    total_shares_scheduled = sum(s["shares_slice"] for s in slices)
    assert abs(total_shares_scheduled - 100_000) < 1e-3
    assert len(slices) >= 5  # extended to respect ADV cap


def test_emit_exit_audit_records(tmp_path):
    ledger = TradeAuditLedger(ledger_dir=tmp_path / "trade_audit")

    signals = [
        {"ticker": "NVDA", "shares": 500, "price": 800.0, "adv_usd": 100_000_000.0, "hype_pctile": 0.96, "price_iv_ratio": 1.30}
    ]

    blocks = emit_exit_audit_records(signals, asof_date="2024-05-15", ledger=ledger, regime="EXPANSION")
    assert len(blocks) == 1
    blk = blocks[0]
    assert blk.ticker == "NVDA"
    assert blk.action == "EXIT"
    assert blk.stage_gate == 3
    assert blk.trigger_reason["exit"]["hype_pctile"] == 0.96

    # Verify ledger integrity
    res = ledger.verify_ledger()
    assert res["intact"] is True
