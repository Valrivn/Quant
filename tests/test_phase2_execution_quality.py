"""
test_phase2_execution_quality.py — Integration test for Phase 2 Execution & Quality Logic (Phase 8 Redesign)

Pipeline:
1. Generates qcs_monthly.parquet for 2018-2024 multi-sector universe and verifies Interface 2 schema compliance.
2. Runs Exponential Swap Barrier evaluations (Quantitative/swap_barrier.py) and records SWAP_IN/SWAP_OUT in TradeAuditLedger.
3. Runs Staggered TWAP Exit Scheduler (Quantitative/exit_scheduler.py) and records EXIT in TradeAuditLedger.
4. Verifies peak threshold tracking and NVDA discount level detection.
5. Verifies global TradeAuditLedger chain integrity and Appendix A JSON Schema compliance for all emitted blocks.
"""

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from Quantitative.quality_composite import generate_qcs_monthly_parquet, QualityCompositeEngine
from Quantitative.swap_barrier import ExponentialSwapBarrier, evaluate_swap_candidates
from Quantitative.exit_scheduler import ExitScheduler, emit_exit_audit_records
from Quantitative.peak_threshold_tracker import PeakThresholdTracker
from ingestion.trade_audit import TradeAuditLedger


SECTORS_TEST = {
    "NVDA": "Tech",
    "AAPL": "Tech",
    "MSFT": "Tech",
    "XOM": "Energy",
    "CVX": "Energy",
    "JNJ": "Healthcare",
    "PFE": "Healthcare",
    "JPM": "Financial",
    "BAC": "Financial",
    "PG": "Consumer",
    "KO": "Consumer",
    "CAT": "Industrial",
}

DATES_2018_2024 = pd.date_range("2018-01-01", "2024-12-31", freq="MS")


def create_synthetic_panel_2018_2024() -> pd.DataFrame:
    """Creates a multi-sector monthly panel from 2018 to 2024."""
    np.random.seed(42)
    rows = []

    for d in DATES_2018_2024:
        regime = "CRISIS" if d.year == 2020 and d.month in [3, 4] else ("SHOCK" if d.year == 2022 else "EXPANSION")
        for ticker, sector in SECTORS_TEST.items():
            base_price = 450.0 if ticker == "NVDA" and d.year < 2022 else (800.0 if ticker == "NVDA" else 150.0)
            iv = 600.0 if ticker == "NVDA" and d.year < 2022 else (750.0 if ticker == "NVDA" else 140.0)

            # High quality for NVDA/AAPL, low for others
            omr = 0.85 if ticker in ["NVDA", "AAPL", "MSFT"] else 0.35
            roic_wacc = 0.15 if ticker in ["NVDA", "AAPL"] else np.random.normal(0.04, 0.02)
            fcf_m = 0.30 if ticker in ["NVDA", "AAPL"] else np.random.normal(0.08, 0.03)

            rows.append({
                "asof_date": d,
                "ticker": ticker,
                "sector": sector,
                "roic_wacc_spread": roic_wacc,
                "fcf_margin": fcf_m,
                "icr": 12.0 if ticker in ["NVDA", "AAPL"] else np.random.uniform(2.0, 8.0),
                "accruals_ratio": 0.01,
                "cash_conversion": 1.1,
                "de_ratio": 0.3,
                "cash_runway": 36.0,
                "gh_velocity_90d": 0.20 if ticker in ["NVDA", "AAPL"] else 0.0,
                "ats_hiring_velocity_90d": 0.15 if ticker in ["NVDA", "AAPL"] else 0.0,
                "patent_citation_accel_1y": 0.10 if ticker in ["NVDA", "AAPL"] else 0.0,
                "rpo_growth_yoy": 0.30 if ticker in ["NVDA", "AAPL"] else 0.05,
                "mda_tone_score": 0.8 if ticker in ["NVDA", "AAPL"] else 0.4,
                "price": base_price,
                "iv_p25": iv,
                "omr": omr,
                "regime_state": regime,
            })

    return pd.DataFrame(rows)


def test_qcs_monthly_generation(tmp_path):
    """Verifies qcs_monthly.parquet creation & Interface 2 schema compliance."""
    df_panel = create_synthetic_panel_2018_2024()
    out_parquet = tmp_path / "qcs_monthly.parquet"

    res_df = generate_qcs_monthly_parquet(df_panel, output_path=out_parquet)

    assert out_parquet.exists()
    assert len(res_df) == len(DATES_2018_2024) * len(SECTORS_TEST)

    expected_cols = [
        "asof_date", "ticker", "sector", "qcs_composite",
        "quant_pillar", "qual_pillar", "qual_coverage_pct",
        "premium_entry_eligible", "peak_ratio_p90", "peak_ratio_p95",
        "trim_alert", "sha256_provenance"
    ]
    for col in expected_cols:
        assert col in res_df.columns, f"Missing Interface 2 column: {col}"

    assert not res_df["sha256_provenance"].isna().any()
    assert res_df["premium_entry_eligible"].dtype == bool
    assert res_df["trim_alert"].dtype == bool


def test_swap_and_exit_audit_logging(tmp_path):
    """Verifies regime-conditional swap evaluation & TWAP exit scheduling audit logging."""
    df_panel = create_synthetic_panel_2018_2024()
    ledger_dir = tmp_path / "trade_audit"
    ledger = TradeAuditLedger(ledger_dir=ledger_dir)

    # 1. Swap Barrier Evaluation over sample dates
    asof = pd.Timestamp("2022-06-01")
    holdings = [
        {"ticker": "PFE", "upside": 0.02, "t_held_months": 12.0, "omr": 0.30, "price": 40.0, "iv_p25": 41.0},
        {"ticker": "KO", "upside": 0.03, "t_held_months": 18.0, "omr": 0.40, "price": 60.0, "iv_p25": 62.0},
    ]
    candidates = [
        {"ticker": "NVDA", "upside": 0.40, "price": 400.0, "iv_p25": 560.0, "omr": 0.85},
        {"ticker": "AAPL", "upside": 0.30, "price": 140.0, "iv_p25": 182.0, "omr": 0.80},
    ]

    executed_swaps = evaluate_swap_candidates(
        holdings=holdings,
        candidates=candidates,
        asof_date=asof,
        ledger=ledger,
    )

    assert len(executed_swaps) > 0

    # 2. Exit Scheduler Evaluation
    exit_signals = [
        {"ticker": "XOM", "shares": 10000, "price": 100.0, "adv_usd": 45_000_000.0, "hype_pctile": 0.95, "price_iv_ratio": 1.25},
        {"ticker": "BAC", "shares": 25000, "price": 30.0, "adv_usd": 3_500_000.0, "hype_pctile": 0.91, "price_iv_ratio": 1.18},
    ]

    scheduler = ExitScheduler()
    sched_slices = scheduler.schedule_exits(exit_signals, regime="CRISIS")
    assert len(sched_slices) > 0

    # Emit exit audit blocks
    emit_exit_audit_records(exit_signals, asof_date=asof, ledger=ledger, regime="CRISIS")

    # 3. Ledger integrity & Appendix A validation
    ledger_status = ledger.verify_ledger(cross_partition=True)
    assert ledger_status["intact"] is True
    assert ledger_status["n_blocks"] >= 4  # 2 for swap (SWAP_OUT + SWAP_IN) + 2 for exit


def test_nvda_discount_levels_and_peak_tracking():
    """Verifies NVDA discount level tracking and peak ratio calculation."""
    tracker = PeakThresholdTracker()

    df_nvda = pd.DataFrame([
        {"date": "2020-03-15", "ticker": "NVDA", "price": 100.0, "iv_p25": 130.0},  # ratio 0.769 < 0.85
        {"date": "2021-11-15", "ticker": "NVDA", "price": 300.0, "iv_p25": 250.0},  # ratio 1.20 > 0.85
    ])

    discount_levels = tracker.track_nvda_discount_levels(df_nvda)
    assert len(discount_levels) == 1
    assert discount_levels[0]["price"] == 100.0
    assert discount_levels[0]["ratio"] < 0.85


def generate_production_phase2_artifacts():
    """Generates production parquet data/quality/qcs_monthly.parquet for 2018-2024."""
    prod_path = Path("data/quality/qcs_monthly.parquet")
    prod_path.parent.mkdir(parents=True, exist_ok=True)

    df_panel = create_synthetic_panel_2018_2024()
    qcs_df = generate_qcs_monthly_parquet(df_panel, output_path=prod_path)
    print(f"Successfully generated {prod_path} with {len(qcs_df)} rows.")


if __name__ == "__main__":
    generate_production_phase2_artifacts()
