"""
peak_threshold_tracker.py — Per-Ticker Peak Threshold Registry & Trim Alert Engine
Pre-Registration Config: config/pre_registration_phase8.yaml
Quality Thresholds Config: config/quality_thresholds.yaml

Provides:
  1. PeakThresholdTracker: Tracks rolling 5-year Price / IV_P25 peak percentiles (P90, P95).
  2. Sector blending: Blends 50% ticker + 50% GICS sector peak quantiles for 2-5 year histories.
  3. Trim Alert generator: Triggers when current Price / IV_P25 exceeds 95th percentile historical peak.
  4. NVDA discount tracker: Special tracking for NVDA price levels when Price / IV_P25 < 0.85.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger(__name__)


class PeakThresholdTracker:
    """Per-ticker peak threshold tracker and trim alert calculator."""

    def __init__(self, config: Union[Dict[str, Any], str, Path, None] = None):
        self.lookback_years = 5
        self.lookback_days = 5 * 252  # 1260 trading days
        self.min_trading_days = 500
        self.alert_pctl = 0.90
        self.trim_alert_pctl = 0.95
        self.sector_blend_weight = 0.50

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
        pt = p8.get("peak_tracking", cfg.get("peak_tracking", {}))

        self.lookback_years = int(pt.get("lookback_years", 5))
        self.lookback_days = self.lookback_years * 252
        self.alert_pctl = float(pt.get("alert_pctl", 0.90))
        self.trim_alert_pctl = float(pt.get("trim_alert_pctl", 0.95))
        self.min_trading_days = int(pt.get("min_price_history_days", 500))
        self.sector_blend_weight = float(pt.get("sector_blend_weight", 0.50))

    def compute_peaks_for_series(
        self,
        ratio_series: pd.Series,
        sector_ratio_series: Optional[pd.Series] = None,
    ) -> Tuple[float, float, bool]:
        """Compute P90 and P95 rolling 5-year peak ratios for a ticker.
        
        Blends 50% ticker + 50% GICS sector peak quantile if history is between 500 and 1260 trading days.
        Returns: (p90_threshold, p95_threshold, trim_alert_triggered)
        """
        clean_series = ratio_series.dropna()
        n_obs = len(clean_series)

        if n_obs < 30:
            # Insufficient ticker data: rely on sector if available, else standard fallback
            if sector_ratio_series is not None and len(sector_ratio_series.dropna()) >= 30:
                s_clean = sector_ratio_series.dropna().tail(self.lookback_days)
                p90 = float(s_clean.quantile(self.alert_pctl))
                p95 = float(s_clean.quantile(self.trim_alert_pctl))
            else:
                p90, p95 = 1.15, 1.25
            current_val = float(clean_series.iloc[-1]) if n_obs > 0 else 1.0
            return p90, p95, current_val > p95

        window_series = clean_series.tail(self.lookback_days)
        ticker_p90 = float(window_series.quantile(self.alert_pctl))
        ticker_p95 = float(window_series.quantile(self.trim_alert_pctl))
        current_val = float(window_series.iloc[-1])

        # Sector blending for 2-5 year histories (500 <= n_obs < 1260)
        if self.min_trading_days <= n_obs < self.lookback_days and sector_ratio_series is not None:
            s_clean = sector_ratio_series.dropna().tail(self.lookback_days)
            if len(s_clean) >= 30:
                sec_p90 = float(s_clean.quantile(self.alert_pctl))
                sec_p95 = float(s_clean.quantile(self.trim_alert_pctl))
                
                w = self.sector_blend_weight
                p90 = float(w * ticker_p90 + (1.0 - w) * sec_p90)
                p95 = float(w * ticker_p95 + (1.0 - w) * sec_p95)
            else:
                p90, p95 = ticker_p90, ticker_p95
        else:
            p90, p95 = ticker_p90, ticker_p95

        trim_alert = current_val > p95
        return p90, p95, trim_alert

    def track_nvda_discount_levels(self, price_iv_df: pd.DataFrame) -> List[Dict[str, Any]]:
        """Track NVDA-specific $Price levels where Price / IV_P25 < 0.85 (deep discount).
        
        Expects price_iv_df with columns ['date', 'ticker', 'price', 'iv_p25'].
        """
        nvda_df = price_iv_df[price_iv_df["ticker"] == "NVDA"].copy()
        if nvda_df.empty:
            return []

        nvda_df["ratio"] = nvda_df["price"] / np.maximum(0.01, nvda_df["iv_p25"])
        discount_df = nvda_df[nvda_df["ratio"] < 0.85].copy()

        results = []
        for _, row in discount_df.iterrows():
            results.append({
                "date": str(pd.Timestamp(row["date"]).date()),
                "ticker": "NVDA",
                "price": float(row["price"]),
                "iv_p25": float(row["iv_p25"]),
                "ratio": float(row["ratio"]),
                "discount_pct": float(1.0 - row["ratio"]),
            })
        return results


def process_peak_thresholds_panel(
    panel_df: pd.DataFrame,
    config_path: str = "config/pre_registration_phase8.yaml",
) -> pd.DataFrame:
    """Computes peak percentiles (p90, p95) and trim alerts per ticker & sector across time.
    
    panel_df columns required: ['asof_date', 'ticker', 'sector', 'price', 'iv_p25']
    Returns panel_df enriched with ['peak_ratio_p90', 'peak_ratio_p95', 'trim_alert'].
    """
    df = panel_df.copy()
    df["asof_date"] = pd.to_datetime(df["asof_date"])
    df["price_iv_ratio"] = df["price"] / np.maximum(0.01, df["iv_p25"])

    tracker = PeakThresholdTracker(config=config_path)

    # Compute daily sector average ratios for sector blending
    sector_daily = df.groupby(["asof_date", "sector"])["price_iv_ratio"].mean().unstack()

    results = []
    # Process ticker by ticker chronologically
    for ticker, t_df in df.groupby("ticker"):
        t_df = t_df.sort_values("asof_date").reset_index(drop=True)
        sector = t_df["sector"].iloc[0] if "sector" in t_df.columns else "default"
        sec_series = sector_daily[sector] if sector in sector_daily.columns else None

        ratios = t_df["price_iv_ratio"].values
        dates = t_df["asof_date"].values

        p90_list = []
        p95_list = []
        trim_list = []

        for i in range(len(t_df)):
            sub_ratio = pd.Series(ratios[: i + 1], index=dates[: i + 1])
            sub_sec = sec_series.loc[: dates[i]] if sec_series is not None else None

            p90, p95, trim = tracker.compute_peaks_for_series(sub_ratio, sub_sec)
            p90_list.append(p90)
            p95_list.append(p95)
            trim_list.append(trim)

        t_df["peak_ratio_p90"] = p90_list
        t_df["peak_ratio_p95"] = p95_list
        t_df["trim_alert"] = trim_list
        results.append(t_df)

    out_df = pd.concat(results, ignore_index=True).sort_values(["asof_date", "ticker"]).reset_index(drop=True)
    return out_df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Test Quantitative.peak_threshold_tracker")
    parser.add_argument("--test", action="store_true", help="Run self-test suite")
    args = parser.parse_args()

    tracker = PeakThresholdTracker()

    # Synthetic series
    np.random.seed(42)
    dates = pd.bdate_range("2020-01-01", periods=600)
    base_ratio = np.random.normal(0.90, 0.10, 600)
    base_ratio[-5:] = 1.30  # Spike at the end

    series = pd.Series(base_ratio, index=dates)
    p90, p95, trim = tracker.compute_peaks_for_series(series)

    print(f"P90: {p90:.4f}, P95: {p95:.4f}, Trim Alert: {trim}")
    assert trim is True, "Trim alert should be triggered for ratio 1.30 > P95"

    # Test NVDA discount tracker
    df_nvda = pd.DataFrame([
        {"date": "2024-01-15", "ticker": "NVDA", "price": 450.0, "iv_p25": 600.0},  # ratio 0.75 < 0.85
        {"date": "2024-02-15", "ticker": "NVDA", "price": 700.0, "iv_p25": 650.0},  # ratio 1.07
    ])
    nvda_levels = tracker.track_nvda_discount_levels(df_nvda)
    print(f"NVDA Discount Levels (< 0.85x IV): {nvda_levels}")
    assert len(nvda_levels) == 1 and nvda_levels[0]["price"] == 450.0

    print("PeakThresholdTracker Test OK!")
