"""
test_quality_composite_v8.py — Unit tests for QualityCompositeEngine & PeakThresholdTracker (Phase 8 Redesign)
"""

import numpy as np
import pandas as pd
import pytest

from Quantitative.quality_composite import QualityCompositeEngine, mad_zscore, winsorize_series
from Quantitative.peak_threshold_tracker import PeakThresholdTracker, process_peak_thresholds_panel


def test_mad_zscore_computation():
    s = pd.Series([10.0, 12.0, 11.0, 13.0, 100.0])  # 100.0 is an outlier
    z = mad_zscore(s)
    
    # Median is 12.0, MAD is median(|-2, 0, -1, 1, 88|) = 1.0
    # Outlier 100.0 receives high robust Z-score without blowing up standard deviation
    assert z.iloc[-1] > 3.0
    assert abs(z.iloc[1]) == 0.0


def test_winsorize_series():
    s = pd.Series(range(100), dtype=float)
    w = winsorize_series(s, pctl=(0.05, 0.95))
    
    assert w.min() == s.quantile(0.05)
    assert w.max() == s.quantile(0.95)


def test_premium_entry_eligibility():
    engine = QualityCompositeEngine()

    # Panel where ticker QUAL1 is high QCS (>2.0), Price/IV > 1.10, OMR >= 0.50
    records = [
        {
            "asof_date": "2024-01-01",
            "ticker": "QUAL1",
            "sector": "Tech",
            "roic_wacc_spread": 0.50,
            "fcf_margin": 0.50,
            "icr": 15.0,
            "accruals_ratio": -0.10,
            "cash_conversion": 1.5,
            "de_ratio": 0.05,
            "cash_runway": 60.0,
            "gh_velocity_90d": 1.00,
            "ats_hiring_velocity_90d": 1.00,
            "patent_citation_accel_1y": 1.00,
            "rpo_growth_yoy": 1.00,
            "mda_tone_score": 1.00,
            "price": 120.0,
            "iv_p25": 100.0,  # Price / IV = 1.20 > 1.10
            "omr": 0.80,
        }
    ]
    # Add 10 normal tickers to establish a distribution baseline
    for i in range(10):
        records.append({
            "asof_date": "2024-01-01",
            "ticker": f"NORM{i}",
            "sector": "Tech",
            "roic_wacc_spread": 0.05,
            "fcf_margin": 0.10,
            "icr": 3.0,
            "accruals_ratio": 0.05,
            "cash_conversion": 0.9,
            "de_ratio": 0.8,
            "cash_runway": 12.0,
            "gh_velocity_90d": 0.0,
            "ats_hiring_velocity_90d": 0.0,
            "patent_citation_accel_1y": 0.0,
            "rpo_growth_yoy": 0.0,
            "mda_tone_score": 0.5,
            "price": 90.0,
            "iv_p25": 100.0,
            "omr": 0.30,
        })

    df = pd.DataFrame(records)
    res = engine.compute_qcs_monthly(df)
    qual1_row = res[res["ticker"] == "QUAL1"].iloc[0]
    norm0_row = res[res["ticker"] == "NORM0"].iloc[0]

    assert qual1_row["qcs_composite"] > norm0_row["qcs_composite"]
    assert bool(qual1_row["premium_entry_eligible"]) is True
    assert bool(norm0_row["premium_entry_eligible"]) is False


def test_sector_qual_coverage_override():
    engine = QualityCompositeEngine()
    engine.qual_coverage_threshold = 0.50

    # Sector where qual coverage is 0% (all missing qual metrics)
    df = pd.DataFrame([
        {
            "asof_date": "2024-01-01",
            "ticker": "NOQUAL1",
            "sector": "Energy",
            "roic_wacc_spread": 0.10,
            "fcf_margin": 0.15,
            "icr": 5.0,
            "accruals_ratio": 0.01,
            "cash_conversion": 1.0,
            "de_ratio": 0.5,
            "cash_runway": 24.0,
            "gh_velocity_90d": np.nan,
            "ats_hiring_velocity_90d": np.nan,
            "patent_citation_accel_1y": np.nan,
            "rpo_growth_yoy": np.nan,
            "mda_tone_score": np.nan,
            "price": 100.0,
            "iv_p25": 100.0,
            "omr": 0.50,
        }
    ])

    res = engine.compute_qcs_monthly(df)
    row = res.iloc[0]
    assert row["qual_coverage_pct"] == 0.0
    # When qual coverage < threshold, qcs_composite should equal quant_pillar (100% quant weight)
    assert abs(row["qcs_composite"] - row["quant_pillar"]) < 1e-5


def test_peak_threshold_tracker():
    tracker = PeakThresholdTracker()

    np.random.seed(42)
    dates = pd.bdate_range("2020-01-01", periods=600)
    vals = np.random.normal(1.0, 0.05, 600)
    vals[-1] = 1.35  # Trigger trim alert

    s = pd.Series(vals, index=dates)
    p90, p95, trim = tracker.compute_peaks_for_series(s)

    assert p95 > p90
    assert trim is True
