"""Phase 1 (Schemas & WACC Engine) — end-to-end integration test.

Drives ingestion/wacc_engine_v2.py over a synthetic 12-ticker multi-sector
universe, 2018-01-02 .. 2024-12-31 (daily), through persistence
(month=YYYY-MM parquet + .provenance.json sidecars with SHA-256 and
degraded-flag aggregates) and the immutable trade_audit ledger (one chained
block per panel row, stage_gate=1, Appendix A trigger_reason).

Assertions (pre-registered coverage gate):
  * 15-column panel matches the frozen valuation_schema WACC_PANEL contract
    and passes validate_wacc_panel (PIT: filed_date_ref <= asof_date, bounds).
  * beta_source  100% non-null per sidecar (100% coverage);
    degraded_pct == 0.0 (all mapped sectors + rho present -> gate >= 99%).
  * ke_v2 per-ticker cross-regime sigma > 50bps (2018-2024 regime variation).
  * 84 month partitions (2018-01..2024-12) each with a valid SHA-256 sidecar.
  * audit ledger: global chain intact, n_blocks == panel rows; disabled parts
    of the config are NOT silently dropped — bounds clamping keeps every
    written block inside frozen erp/wacc bounds.
"""

import hashlib
import json
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ingestion.trade_audit import TradeAuditLedger
from ingestion.wacc_engine_v2 import WACC_PANEL_COLUMNS, WACCEngineV2
from ingestion.valuation_schema import validate_wacc_panel

T0 = pd.Timestamp("2018-01-02")
T1 = pd.Timestamp("2024-12-31")

# 12 tickers, all with MAPPED GICS L2 sectors (every lookup -> sector_bottom_up,
# so beta_degraded == False everywhere) covering High/Medium/Low stress tiers +
# one Financials carve-out.
SECTORS = {
    "SFT": "451020",   # Software (Internet)  beta 1.55  -> High stress
    "SEM": "453010",   # Semiconductor        beta 1.49  -> High
    "HLT": "501010",   # Health Care Equip    beta ~ Low
    "MAT": "151020",   # beta 0.93            -> Medium
    "BKY": "401010",   # Financials (carve-out)
    "ENE": "101010",   # Energy
    "UTL": "601010",   # Utilities
    "FOD": "302010",   # Food
    "RET": "255030",   # Retail (Building)    beta 1.31 -> High
    "TLM": "501020",   # Telecom               -> Medium/Low
    "CHM": "151030",   # Chemicals
    "IND": "201020",   # Industrial Machinery
}

RF_BY_YEAR = {2018: 0.028, 2019: 0.024, 2020: 0.009, 2021: 0.015,
              2022: 0.040, 2023: 0.039, 2024: 0.042}
VIX_BY_YEAR = {2018: 16.0, 2019: 15.0, 2020: 30.0, 2021: 18.0,
               2022: 25.0, 2023: 16.0, 2024: 13.0}
# Damodaran historical ERP (config/damodaran_erp.yaml — frozen inputs)
ERP_BY_YEAR = {2018: 0.0596, 2019: 0.0520, 2020: 0.0472, 2021: 0.0424,
               2022: 0.0594, 2023: 0.0460, 2024: 0.0460}


def _business_days() -> pd.DatetimeIndex:
    return pd.bdate_range(T0, T1)


def _load_market_series(eng: WACCEngineV2) -> None:
    """PIT market inputs, anchored annually (as-of each Jan 3)."""
    for y, rf in RF_BY_YEAR.items():
        eng.load_rf({pd.Timestamp(f"{y}-01-03"): rf})
    for y, vix in VIX_BY_YEAR.items():
        eng.load_vix({pd.Timestamp(f"{y}-01-03"): vix})
    for y, erp in ERP_BY_YEAR.items():
        eng.load_erp({pd.Timestamp(f"{y}-01-03"): erp})


def _load_filings(eng: WACCEngineV2, seed: int = 7) -> None:
    """Deterministic per-ticker filing stream (2 filings/yr, PIT-anchored)."""
    rng = np.random.default_rng(seed)
    for i, (ticker, gics) in enumerate(SECTORS.items()):
        fin = gics == "401010"
        base_d_e = rng.uniform(0.1, 1.2)
        base_cash = rng.uniform(0.0, 0.25)
        fluct = rng.uniform(0.6, 1.6)
        icr0 = rng.uniform(1.5, 14.0)
        rows = []
        for year in range(2018, 2025):
            for half, day in (("a", "03-25"), ("b", "09-25")):
                filed = pd.Timestamp(f"{year}-{day}")
                # regime-shaped ICR: 2020 shock for 2 tickers (burn), 2022 tightening
                shock = 0.55 if (year == 2020 and i % 5 == 0) else (
                    0.75 if (year == 2022 and i % 7 == 0) else 1.0)
                icr = max(0.4, icr0 * fluct * shock)
                rev = rng.uniform(2e8, 8e9)
                ocf = rev * rng.uniform(0.02, 0.25) * (1.0 if shock == 1.0 else -0.4)
                ebit = rev * rng.uniform(0.01, 0.18) * (1.0 if shock == 1.0 else -0.5)
                rows.append({
                    "filed": filed,
                    "d_e_market_net": base_d_e * rng.uniform(0.8, 1.25),
                    "cash_ratio": base_cash * rng.uniform(0.8, 1.2),
                    "icr_v2": icr,
                    "operating_cash_flow": ocf,
                    "revenue": rev,
                    "ebit": ebit,
                    "tax_rate_applied": 0.21,
                    "is_financial_kd": fin,
                    "iv_baseline": rng.uniform(80.0, 400.0),
                    "duration_wacc": rng.uniform(4.0, 11.0),
                    "wacc_estimate": rng.uniform(0.06, 0.13),
                })
        rows.sort(key=lambda r: r["filed"])   # PIT: chronological insert order
        for r in rows:
            eng.update_on_filing(ticker, r.pop("filed"), r)
        # rho anchored at t0 (no missing-rho -> rho_degraded False)
        eng.load_rho_60m({(ticker, T0): rng.uniform(0.3, 0.85)})


def _load_firm_regressions(eng: WACCEngineV2) -> None:
    """Two tickers get a firm 60-month regression for 2022-2024 (>=36 obs)."""
    for ticker, beta in (("SFT", 1.62), ("SEM", 1.31)):
        for d in pd.bdate_range("2022-01-03", "2024-12-31", freq="180D"):
            eng.load_beta_60m({(ticker, d): beta},
                              n_obs={(ticker, d): 48},
                              rho_map={(ticker, d): 0.55})


def _build_panel(eng: WACCEngineV2, dates) -> tuple:
    df, provs = eng.build_daily_panel_with_prov(
        tickers=list(SECTORS),
        dates=dates,
        gics_l2_map=SECTORS,
        # ebit/icr passed per row below via maps are for distress shortcuts;
        # the PIT filing values anchored in _load_filings carry by default.
        ebit_map={t: None for t in SECTORS},
        icr_map={t: None for t in SECTORS},
        iv_baseline_map={t: None for t in SECTORS},
        d_wacc_map={t: None for t in SECTORS},
        retrieval_ts=pd.Timestamp("2024-12-31T12:00:00.000000000"),
    )
    return df, provs


# ---------------------------------------------------------------------------

_PANEL_CACHE = None


def _panel_snapshot():
    """Lazy module-level cache: the daily panel build is ~25s, both big tests
    consume the SAME snapshot (pytest forbids returning values from tests)."""
    global _PANEL_CACHE
    if _PANEL_CACHE is None:
        eng = WACCEngineV2()
        _load_market_series(eng)
        _load_filings(eng)
        _load_firm_regressions(eng)
        dates = _business_days()
        df, provs = _build_panel(eng, dates)
        _PANEL_CACHE = df, provs, eng, dates
    return _PANEL_CACHE


def test_panel_schema_and_bounds(tmp_path):
    df, provs, eng, dates = _panel_snapshot()

    # 1. column contract == frozen schema WACC_PANEL
    assert list(df.columns) == list(WACC_PANEL_COLUMNS)
    assert len(df) == len(SECTORS) * len(dates)

    # 2. frozen schema validator passes (PIT + bounds + coverage)
    violations = validate_wacc_panel(df)
    assert violations == [], violations[:5]
    assert eng.validate_panel(df) == []

    # 3. beta_source coverage 100% + firm override present on the sub-period
    assert df["beta_source"].notna().all()
    assert (df["beta_source"] == "sector_bottom_up").any()
    assert (df["beta_source"] == "firm_regression_60m").any()

    # 4. ke per-ticker cross-regime sigma > 50bps (2018 VIX 16 -> 2020 VIX 30
    #    -> 2022 rates 4%: cost of capital moves materially)
    for ticker in SECTORS:
        ke = df.loc[df["ticker"] == ticker, "ke_v2"]
        assert ke.std() > 0.005, (ticker, ke.std())

    # 5. bounds on every frozen output column
    assert df["wacc_v2"].between(0.0, 0.5).all()
    assert df["ke_v2"].between(0.0, 0.5).all()
    assert df["kd_pre_tax_v2"].between(0.0, 0.5).all()
    assert df["erp_blend"].between(0.005, 0.08).all()
    assert df["erp_blend"].max() <= 0.08 and df["erp_blend"].min() >= 0.005
    assert df["iv_nowcast"].min() >= 0.01

    # 6. PIT: every row's filing anchor precedes (or equals) the as-of date
    assert (df["filed_date_ref"].dt.normalize() <= df["asof_date"].dt.normalize()).all()


def test_partitioned_write_with_sidecars_and_audit(tmp_path):
    """Full pipeline: build → partition → sidecar provenance → audit ledger."""
    df, provs, eng, dates = _panel_snapshot()

    out_dir = tmp_path / "wacc_panel"
    ledger_dir = tmp_path / "trade_audit"
    ledger = TradeAuditLedger(ledger_dir=ledger_dir)

    paths = eng.write_panel_partitioned(df, out_dir=out_dir, provs=provs,
                                        audit=ledger)

    # 7. exactly 84 month partitions (2018-01 .. 2024-12), one parquet each
    partitions = sorted(p.name for p in out_dir.glob("month=*"))
    assert len(partitions) == 84
    assert partitions[0] == "month=2018-01" and partitions[-1] == "month=2024-12"
    assert len(paths) == 84

    # 8. every sidecar: sha256 of its parquet + coverage-gate aggregates
    total_rows = 0
    for mdir in out_dir.glob("month=*"):
        pq = mdir / "wacc_panel.parquet"
        sc = json.loads((mdir / "wacc_panel.parquet.provenance.json").read_text(encoding="utf-8"))
        import hashlib
        assert sc["sha256"] == hashlib.sha256(pq.read_bytes()).hexdigest()
        assert sc["table"] == "wacc_panel_daily"
        assert sc["pk"] == ["asof_date", "ticker"]
        assert sc["beta_source_100pct"] is True
        assert sc["wacc_bounds_ok"] is True and sc["erp_bounds_ok"] is True
        assert sc["filed_date_ref_le_asof"] is True
        assert sc["beta_degraded_rows"] == 0
        assert sc["degraded_pct"] == 0.0   # all mapped + rho present -> gate ok
        total_rows += sc["rows"]
    assert total_rows == len(df)

    # 9. audit ledger: one chained block per panel row, global chain intact
    gres = ledger.verify_ledger(cross_partition=True)
    assert gres["intact"] is True
    assert gres["n_blocks"] == len(df)
    assert gres["partitions"] == 84
    for m in range(1, 13):
        for y in (2018, 2020, 2024):
            month = f"{y}-{m:02d}"
            ok, n, brk = ledger.verify_chain(month, verify_hashes=True)
            assert ok and brk is None, month
            assert n > 0

    # 10. spot-check a persisted block: stage_gate=1, Appendix A trigger, bounds
    blocks = ledger.load_month("2024-12")
    assert blocks and all(b.stage_gate == 1 for b in blocks)
    sample = blocks[len(blocks) // 2]
    assert sample.trigger_reason["valuation"]["iv_method"] == "dcf_v2_nowcast"
    assert 0.0 <= sample.wacc_v2 <= 0.5
    assert 0.005 <= sample.erp_blend <= 0.08
    assert sample.beta_source in ("sector_bottom_up", "firm_regression_60m")
    rec, chn = sample.recompute()
    assert rec == sample.record_hash and chn == sample.chain_hash


def test_audit_clamps_hostile_bounds():
    """Bounds clamping keeps hostile payloads inside frozen erp/wacc ranges."""
    from ingestion.trade_audit import DEFAULT_ERP_BOUNDS, DEFAULT_WACC_BOUNDS
    with tempfile.TemporaryDirectory() as td:
        ledger = TradeAuditLedger(ledger_dir=Path(td))
        ledger.append_batch([{
            "ticker": "X", "asof": pd.Timestamp("2026-09-14"),
            "wacc_v2": 99.0, "ke_v2": 99.0, "kd_pre_tax_v2": 99.0,
            "erp_blend": 9.9, "rf_10y": 0.042, "beta_source": "sector_bottom_up",
            "retrieval_ts": pd.Timestamp("2026-09-14T12:00:00.000000000"),
            "action": "PANEL_WRITE", "gate_name": "WACC_PANEL", "stage_gate": 1,
            "trigger_reason": {
                "valuation": {"iv_p25": 1.0, "price": 1.0, "discount_pct": 0.0,
                              "iv_method": "dcf_v2_nowcast",
                              "iv_basis_filed_date": "2026-09-12",
                              "iv_nowcast_ts": "2026-09-14T12:00:00.000000000"}}}])
        blk = ledger.load_month("2026-09")[0]
        elo, ehi = DEFAULT_ERP_BOUNDS
        assert elo <= blk.erp_blend <= ehi
        wlo, whi = DEFAULT_WACC_BOUNDS
        assert all(wlo <= getattr(blk, k) <= whi
                   for k in ("wacc_v2", "ke_v2", "kd_pre_tax_v2"))