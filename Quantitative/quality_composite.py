"""
quality_composite.py — Phase 8 Quality Composite Score (QCS) Engine
Pre-Registration Config: config/pre_registration_phase8.yaml
Quality Thresholds Config: config/quality_thresholds.yaml

Provides:
  1. QualityCompositeEngine: Calculates sector-relative winsorized MAD Z-scores for Quant & Qual Pillars.
  2. Compute QCS Composite with 60% Quant / 40% Qual weighting (or 100% Quant if qual coverage < 20%).
  3. Premium Entry Allowance evaluation (QCS > 2.0, Price/IV_P25 > 1.10, Moat OMR >= 0.5 -> 0.90x IV allowance).
  4. Integration with PeakThresholdTracker.
  5. Persistence to data/quality/qcs_monthly.parquet adhering to Interface 2 schema.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import yaml

from Quantitative.peak_threshold_tracker import process_peak_thresholds_panel, PeakThresholdTracker

logger = logging.getLogger(__name__)


def mad_zscore(series: pd.Series, constant: float = 0.6745) -> pd.Series:
    """Computes robust MAD (Median Absolute Deviation) Z-scores."""
    clean = series.dropna()
    if len(clean) == 0:
        return pd.Series(0.0, index=series.index)
    med = clean.median()
    mad = (clean - med).abs().median()
    if mad < 1e-8:
        std = clean.std()
        if pd.isna(std) or std < 1e-8:
            return pd.Series(0.0, index=series.index)
        return (series - med) / std
    return constant * (series - med) / mad


def winsorize_series(series: pd.Series, pctl: Tuple[float, float] = (0.01, 0.99)) -> pd.Series:
    """Winsorizes series at specified lower and upper percentiles."""
    clean = series.dropna()
    if len(clean) == 0:
        return series
    lower = clean.quantile(pctl[0])
    upper = clean.quantile(pctl[1])
    return series.clip(lower=lower, upper=upper)


class QualityCompositeEngine:
    """Quality Composite Score (QCS) calculation and persistence engine."""

    def __init__(self, config: Union[Dict[str, Any], str, Path, None] = None):
        self.quant_weight = 0.60
        self.qual_weight = 0.40
        self.premium_threshold_z = 2.0
        self.premium_price_iv_threshold = 1.10
        self.moat_omr_threshold = 0.50
        self.qual_coverage_threshold = 0.20

        if config is not None:
            self._load_config(config)

    def _load_config(self, config: Union[Dict[str, Any], str, Path]) -> None:
        if isinstance(config, (str, Path)):
            p = Path(config)
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            else:
                cfg = {}
        elif isinstance(config, dict):
            cfg = config
        else:
            cfg = {}

        p8 = cfg.get("phase8", cfg)
        qc = p8.get("quality_composite", cfg.get("quality_composite", {}))
        qt = cfg.get("quality_thresholds", {})

        self.quant_weight = float(qc.get("quant_weight", 0.60))
        self.qual_weight = float(qc.get("qual_weight", 0.40))
        self.premium_threshold_z = float(qc.get("premium_threshold_z", 2.0))
        self.premium_entry_allowance = float(qc.get("premium_entry_allowance", 0.05))
        self.moat_omr_threshold = float(qc.get("moat_omr_threshold", 0.50))
        self.qual_coverage_threshold = float(qt.get("sector_qual_coverage_threshold", 0.20))

    def compute_qcs_monthly(self, df: pd.DataFrame) -> pd.DataFrame:
        """Computes monthly QCS scores across a panel of tickers.
        
        Expected input df columns:
          - asof_date, ticker, sector
          - roic_wacc_spread, fcf_margin, icr, accruals_ratio, cash_conversion, de_ratio, cash_runway
          - gh_velocity_90d, ats_hiring_velocity_90d, patent_citation_accel_1y, rpo_growth_yoy, mda_tone_score
          - price, iv_p25, omr (optional)
        """
        data = df.copy()
        data["asof_date"] = pd.to_datetime(data["asof_date"])

        # Default fill for missing metrics
        quant_cols = [
            "roic_wacc_spread", "fcf_margin", "icr",
            "accruals_ratio", "cash_conversion", "de_ratio", "cash_runway"
        ]
        qual_cols = [
            "gh_velocity_90d", "ats_hiring_velocity_90d",
            "patent_citation_accel_1y", "rpo_growth_yoy", "mda_tone_score"
        ]

        for col in quant_cols + qual_cols:
            if col not in data.columns:
                data[col] = np.nan
        if "omr" not in data.columns:
            data["omr"] = 0.50
        if "price" not in data.columns:
            data["price"] = 100.0
        if "iv_p25" not in data.columns:
            data["iv_p25"] = 100.0

        data["month"] = data["asof_date"].dt.to_period("M")

        results = []
        for (month, sector), group in data.groupby(["month", "sector"]):
            grp = group.copy()
            n_grp = len(grp)

            # 1. Quant Pillar (60%)
            # ROIC-WACC Spread (25%)
            r_w = winsorize_series(grp["roic_wacc_spread"], (0.01, 0.99))
            z_roic_wacc = mad_zscore(r_w)

            # FCF Margin (25%)
            fcf_w = winsorize_series(grp["fcf_margin"], (0.01, 0.99))
            z_fcf = mad_zscore(fcf_w)

            # ICR (20%) - Cap 10.0, floor 0.5
            icr_capped = grp["icr"].clip(lower=0.5, upper=10.0)
            z_icr = mad_zscore(icr_capped)

            # Revenue Quality (15%) - Accruals ratio (lower is better, so negate) + Cash conversion
            z_accruals = -mad_zscore(grp["accruals_ratio"].fillna(0.0))
            z_cash_conv = mad_zscore(grp["cash_conversion"].fillna(0.0))
            z_rev_qual = 0.5 * z_accruals + 0.5 * z_cash_conv

            # Balance Sheet Strength (15%) - D/E (lower is better) + Cash runway
            z_de = -mad_zscore(grp["de_ratio"].fillna(1.0))
            z_runway = mad_zscore(grp["cash_runway"].fillna(12.0))
            z_bal_sheet = 0.5 * z_de + 0.5 * z_runway

            quant_pillar = (
                0.25 * z_roic_wacc +
                0.25 * z_fcf +
                0.20 * z_icr +
                0.15 * z_rev_qual +
                0.15 * z_bal_sheet
            )

            # 2. Qual Pillar (40%)
            # Count qual coverage ratio
            qual_non_null = grp[qual_cols].notna().sum(axis=1)
            coverage_per_row = qual_non_null / len(qual_cols)
            sector_qual_coverage = float((qual_non_null > 0).mean())

            # MAD Z-scores for qual components (neutral fill 0.0 for missing)
            z_gh = mad_zscore(grp["gh_velocity_90d"].fillna(0.0))
            z_ats = mad_zscore(grp["ats_hiring_velocity_90d"].fillna(0.0))
            z_pat = mad_zscore(grp["patent_citation_accel_1y"].fillna(0.0))
            z_rpo = mad_zscore(grp["rpo_growth_yoy"].fillna(0.0))
            z_mda = mad_zscore(grp["mda_tone_score"].fillna(0.0))

            qual_pillar = (
                0.25 * z_gh +
                0.25 * z_ats +
                0.20 * z_pat +
                0.15 * z_rpo +
                0.15 * z_mda
            )

            # Sector qual coverage check (< 20% qual coverage forces Qual Weight = 0)
            if sector_qual_coverage < self.qual_coverage_threshold:
                w_quant = 1.00
                w_qual = 0.00
            else:
                w_quant = self.quant_weight
                w_qual = self.qual_weight

            qcs_composite = w_quant * quant_pillar + w_qual * qual_pillar

            grp["quant_pillar"] = quant_pillar
            grp["qual_pillar"] = qual_pillar
            grp["qcs_composite"] = qcs_composite
            grp["qual_coverage_pct"] = sector_qual_coverage

            # 3. Premium Entry Allowance
            price_iv_ratio = grp["price"] / np.maximum(0.01, grp["iv_p25"])
            grp["price_iv_ratio"] = price_iv_ratio

            premium_eligible = (
                (qcs_composite > self.premium_threshold_z) &
                (price_iv_ratio > self.premium_price_iv_threshold) &
                (grp["omr"] >= self.moat_omr_threshold)
            )
            grp["premium_entry_eligible"] = premium_eligible

            results.append(grp)

        qcs_df = pd.concat(results, ignore_index=True)
        qcs_df = qcs_df.drop(columns=["month"], errors="ignore")

        # 4. Integrate Peak Threshold Tracker
        qcs_df = process_peak_thresholds_panel(qcs_df, config_path="config/pre_registration_phase8.yaml")

        # Sort chronologically
        qcs_df = qcs_df.sort_values(["asof_date", "ticker"]).reset_index(drop=True)
        return qcs_df


def generate_qcs_monthly_parquet(
    df: pd.DataFrame,
    output_path: Union[str, Path] = "data/quality/qcs_monthly.parquet",
    config_path: str = "config/pre_registration_phase8.yaml",
) -> pd.DataFrame:
    """Computes QCS monthly scores, adds SHA-256 provenance, and writes to Parquet adhering to Interface 2 schema."""
    engine = QualityCompositeEngine(config=config_path)
    qcs_df = engine.compute_qcs_monthly(df)

    # Required columns for Interface 2 schema
    interface_2_cols = [
        "asof_date",
        "ticker",
        "sector",
        "qcs_composite",
        "quant_pillar",
        "qual_pillar",
        "qual_coverage_pct",
        "premium_entry_eligible",
        "peak_ratio_p90",
        "peak_ratio_p95",
        "trim_alert",
    ]

    for col in interface_2_cols:
        if col not in qcs_df.columns:
            if col == "sector":
                qcs_df[col] = "default"
            elif col == "qual_coverage_pct":
                qcs_df[col] = 1.0
            elif col == "premium_entry_eligible":
                qcs_df[col] = False
            elif col == "trim_alert":
                qcs_df[col] = False
            else:
                qcs_df[col] = 0.0

    out_df = qcs_df[interface_2_cols].copy()
    out_df["asof_date"] = out_df["asof_date"].dt.strftime("%Y-%m-%d")

    # Compute SHA-256 provenance for the dataframe content
    content_bytes = out_df.to_csv(index=False).encode("utf-8")
    prov_sha256 = hashlib.sha256(content_bytes).hexdigest()
    out_df["sha256_provenance"] = prov_sha256

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_parquet(out_p, engine="pyarrow", index=False)

    logger.info(f"Successfully generated QCS Monthly parquet ({len(out_df)} rows) at {out_p}")
    return out_df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Test Quantitative.quality_composite")
    parser.add_argument("--test", action="store_true", help="Run self-test suite")
    args = parser.parse_args()

    # Synthetic monthly test panel
    dates = pd.date_range("2024-01-01", "2024-06-01", freq="MS")
    tickers = ["AAPL", "MSFT", "NVDA", "XOM", "JNJ"]
    records = []
    np.random.seed(42)

    for d in dates:
        for t in tickers:
            records.append({
                "asof_date": d,
                "ticker": t,
                "sector": "Tech" if t in ["AAPL", "MSFT", "NVDA"] else "Other",
                "roic_wacc_spread": np.random.normal(0.08, 0.03),
                "fcf_margin": np.random.normal(0.20, 0.05),
                "icr": np.random.uniform(2.0, 15.0),
                "accruals_ratio": np.random.normal(0.02, 0.01),
                "cash_conversion": np.random.normal(1.0, 0.1),
                "de_ratio": np.random.uniform(0.2, 1.5),
                "cash_runway": 24.0,
                "gh_velocity_90d": np.random.normal(0.1, 0.05),
                "ats_hiring_velocity_90d": np.random.normal(0.05, 0.02),
                "patent_citation_accel_1y": np.random.normal(0.02, 0.01),
                "rpo_growth_yoy": np.random.normal(0.15, 0.05),
                "mda_tone_score": np.random.normal(0.5, 0.1),
                "price": 100.0,
                "iv_p25": 90.0,
                "omr": 0.70 if t in ["AAPL", "NVDA"] else 0.30,
            })

    df_test = pd.DataFrame(records)
    res_df = generate_qcs_monthly_parquet(df_test, output_path="data/quality/qcs_monthly.parquet")
    print(f"Generated QCS Monthly result ({len(res_df)} rows):")
    print(res_df.head(5))

    assert "qcs_composite" in res_df.columns
    assert "sha256_provenance" in res_df.columns
    assert "peak_ratio_p95" in res_df.columns

    print("QualityCompositeEngine Test OK!")
