"""
test_provenance_audit.py — tests for the trade-audit ledger chain verification
added to validation/provenance_audit.py (RS-07 + WS-2 C2.5).

Exercises check_trade_ledger on a synthetic two-partition ledger built through
the real ingestion.trade_audit.TradeAuditLedger producer, covers the forensic
tamper path, the empty-ledger SKIP path, CLI exit codes, and the ledger-dir
classification heuristic.
"""

import json

import pandas as pd
import pytest

from validation import provenance_audit as pa

TRIGGER = {
    "valuation": {
        "iv_p25": 1.15, "price": 1.0, "discount_pct": 0.15,
        "iv_method": "dcf_v2_nowcast", "iv_basis_filed_date": "2026-08-20",
        "iv_nowcast_ts": "2026-09-01T00:00:00Z",
    }
}


def _build_ledger(dirpath, n_aug=1, n_sep=2):
    """Build a 2-partition ledger (2026-08, 2026-09) via the real producer."""
    from ingestion.trade_audit import TradeAuditLedger
    ld = TradeAuditLedger(ledger_dir=dirpath)
    items = []
    for i in range(n_aug):
        items.append({
            "ticker": f"A{i}", "asof": "2026-08-31", "wacc_v2": 0.08,
            "ke_v2": 0.10, "kd_pre_tax_v2": 0.05, "erp_blend": 0.04,
            "rf_10y": 0.04, "beta_source": "bloomberg_60m", "action": "ENTRY",
            "stage_gate": 1, "trigger_reason": TRIGGER,
            "retrieval_ts": "2026-09-01T00:00:00Z",
        })
    for i in range(n_sep):
        items.append({
            "ticker": f"B{i}", "asof": "2026-09-15", "wacc_v2": 0.09,
            "ke_v2": 0.11, "kd_pre_tax_v2": 0.05, "erp_blend": 0.045,
            "rf_10y": 0.04, "beta_source": "bloomberg_60m",
            "action": "REBALANCE" if i else "QUARANTINE", "stage_gate": 2 + i,
            "trigger_reason": TRIGGER, "retrieval_ts": "2026-09-16T00:00:00Z",
        })
    ld.append_batch(items)
    return ld


def test_ledger_chain_pass_two_partitions(tmp_path):
    _build_ledger(tmp_path / "ledger")
    rpt = pa.check_trade_ledger(tmp_path / "ledger")
    assert rpt["status"] == "PASS", rpt["violations"]
    assert rpt["global"]["n_blocks"] == 3
    assert rpt["global"]["partitions"] == 2
    assert set(rpt["partitions"].keys()) == {"2026-08", "2026-09"}
    assert rpt["config_contract"]["uuid_version"] == 7
    assert rpt["config_contract"]["chain_verification"] is True
    for m, p in rpt["partitions"].items():
        assert p["chain_intact"] is True
        assert p["issues"] == []


def test_ledger_chain_fail_on_disk_tamper(tmp_path):
    _build_ledger(tmp_path / "ledger")
    p = tmp_path / "ledger" / "month=2026-08" / "trade_audit_2026-08.parquet"
    df = pd.read_parquet(p)
    df.loc[0, "ke_v2"] = 0.999                       # violates frozen wacc bounds
    df.to_parquet(p, engine="pyarrow", index=False)
    rpt = pa.check_trade_ledger(tmp_path / "ledger")
    assert rpt["status"] == "FAIL"
    assert any("chain_verification failed" in i
               for part in rpt["violations"] for i in part["issues"])


def test_ledger_skips_empty_dir(tmp_path):
    (tmp_path / "empty").mkdir()
    rpt = pa.check_trade_ledger(tmp_path / "empty")
    assert rpt["status"] == "SKIP"
    assert rpt["reason"].startswith("no month=")


def test_ledger_detects_decreasing_timestamps(tmp_path):
    """Out-of-order retrieval_ts inside a month must be flagged."""
    from ingestion.trade_audit import TradeAuditLedger
    db = tmp_path / "ledger_oo"
    ld = TradeAuditLedger(ledger_dir=db)
    items = [
        {"ticker": "X", "asof": "2026-09-10", "wacc_v2": 0.08, "ke_v2": 0.10,
         "kd_pre_tax_v2": 0.05, "erp_blend": 0.04, "rf_10y": 0.04,
         "beta_source": "b", "trigger_reason": TRIGGER,
         "retrieval_ts": "2026-09-11T10:00:00Z"},
        {"ticker": "Y", "asof": "2026-09-11", "wacc_v2": 0.08, "ke_v2": 0.10,
         "kd_pre_tax_v2": 0.05, "erp_blend": 0.04, "rf_10y": 0.04,
         "beta_source": "b", "trigger_reason": TRIGGER,
         "retrieval_ts": "2026-09-11T09:00:00Z"},   # earlier than block 1
    ]
    ld.append_batch(items)
    rpt = pa.check_trade_ledger(db)
    assert rpt["status"] == "FAIL"
    assert any("retrieval_ts decreasing" in i
               for part in rpt["violations"] for i in part["issues"])


def test_cmd_ledger_check_exit_codes_and_report(tmp_path):
    _build_ledger(tmp_path / "ledger")
    out = tmp_path / "reports" / "provenance_audit_report.json"
    rc = pa.cmd_ledger_check(tmp_path / "ledger", out)
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["status"] == "PASS"
    assert doc["provenance"]["source"] == "validation.provenance_audit"
    assert len(doc["provenance"]["sha256"]) == 64
    assert doc["provenance"]["row_count"] == 3

    # tamper -> exit 1
    p = tmp_path / "ledger" / "month=2026-08" / "trade_audit_2026-08.parquet"
    df = pd.read_parquet(p)
    df.loc[0, "wacc_v2"] = 0.9
    df.to_parquet(p, engine="pyarrow", index=False)
    assert pa.cmd_ledger_check(tmp_path / "ledger", out) == 1


def test_cmd_ledger_check_skip_path_exit_zero(tmp_path):
    """Unbuilt ledger: cmd reports SKIP and exits 0 (never a crash)."""
    out = tmp_path / "reports" / "prov_skip.json"
    (tmp_path / "not_built").mkdir()
    rc = pa.cmd_ledger_check(tmp_path / "not_built", out)
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["status"] == "SKIP"
    assert doc["provenance"]["row_count"] == 0


def test_is_trade_ledger_dir_classification(tmp_path):
    _build_ledger(tmp_path / "ledger")
    assert pa._is_trade_ledger_dir(tmp_path / "ledger") is True
    (tmp_path / "plain").mkdir()
    assert pa._is_trade_ledger_dir(tmp_path / "plain") is False