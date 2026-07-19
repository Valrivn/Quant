#!/usr/bin/env python3
"""
gold_macro_valuation.py — Real Rates + M2 Expansion Monitor

Implements Damodaran-style gold valuation using two macroeconomic indicators:

1. Real Interest Rates (FRED: DFII10 - 10-Year TIPS Constant Maturity)
   - When real rates rise above +2.0%, gold becomes fundamentally cheap
     (opportunity cost of holding non-yielding asset increases)
   - When real rates fall below 0%, gold is fundamentally expensive
     (negative real yields make zero-yield gold attractive)

2. M2 Money Supply Growth (FRED: M2SL)
   - When M2 growth rate exceeds +10% YoY, gold serves as a
     monetary debasement hedge
   - Accelerating M2 → inflationary pressure → gold appreciation

The macro valuation produces a composite signal:
  - UNDERVALUED: Gold is cheap relative to real rates and/or M2 expansion
  - FAIR_VALUE: Gold is priced appropriately for current macro conditions
  - OVERVALUED: Gold is expensive relative to macro fundamentals
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

from Quantitative.shared.fred_scraper import FREDScraper

logger = logging.getLogger(__name__)


class GoldMacroSignal(Enum):
    """Macro valuation signal for gold."""
    UNDERVALUED = "UNDERVALUED"
    FAIR_VALUE = "FAIR_VALUE"
    OVERVALUED = "OVERVALUED"
    UNKNOWN = "UNKNOWN"


@dataclass
class MacroMetrics:
    """Raw macro metrics for gold valuation."""
    real_rate_10y: Optional[float] = None
    m2_latest: Optional[float] = None
    m2_year_ago: Optional[float] = None
    m2_yoy_growth: Optional[float] = None
    real_rate_date: Optional[str] = None
    m2_date: Optional[str] = None


@dataclass
class GoldMacroValuationResult:
    """Complete gold macro valuation analysis."""
    signal: GoldMacroSignal
    metrics: MacroMetrics
    real_rate_component: Optional[float] = None
    m2_component: Optional[float] = None
    composite_score: Optional[float] = None
    explanation: str = ""
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict:
        return {
            "signal": self.signal.value,
            "real_rate_10y": self.metrics.real_rate_10y,
            "m2_yoy_growth": self.metrics.m2_yoy_growth,
            "real_rate_component": self.real_rate_component,
            "m2_component": self.m2_component,
            "composite_score": self.composite_score,
            "explanation": self.explanation,
            "evaluated_at": self.evaluated_at,
        }


class GoldMacroValuation:
    """
    Computes gold's macro valuation using real interest rates and M2 expansion.

    Configurable thresholds allow pivoting the valuation logic:
      - Real rate thresholds: above_high / below_low define the cheap/expensive zones
      - M2 growth threshold: above this level signals monetary debasement hedging
      - Component weights: how much each signal contributes to composite
    """

    # FRED series
    SERIES_DFII10 = "DFII10"
    SERIES_M2SL = "M2SL"

    # M2 YoY calculation window (months back)
    M2_LAG_MONTHS = 12

    def __init__(
        self,
        fred_scraper: Optional[FREDScraper] = None,
        real_rate_high_threshold: float = 2.0,
        real_rate_low_threshold: float = 0.0,
        m2_growth_threshold: float = 10.0,
        real_rate_weight: float = 0.5,
        m2_weight: float = 0.5,
    ):
        self._scraper = fred_scraper or FREDScraper()
        self._real_rate_high = real_rate_high_threshold
        self._real_rate_low = real_rate_low_threshold
        self._m2_threshold = m2_growth_threshold
        self._w_real = real_rate_weight
        self._w_m2 = m2_weight

    def _compute_real_rate_component(self, real_rate: float) -> float:
        """
        Compute the real-rate valuation component for gold.

        Damodaran Logic:
          - High real rates (> +2%) → gold is cheap → high component (opportunity to buy)
          - Low/negative real rates → gold is expensive → low component

        Maps real rate to [0, 1] where 1.0 = gold is most undervalued.
        """
        if real_rate >= self._real_rate_high:
            return 1.0
        elif real_rate <= self._real_rate_low:
            return 0.0
        else:
            return (real_rate - self._real_rate_low) / (self._real_rate_high - self._real_rate_low)

    def _compute_m2_component(self, m2_yoy_growth: float) -> float:
        """
        Compute the M2 expansion valuation component for gold.

        Monetary debasement logic:
          - High M2 growth (> 10% YoY) → gold is cheap hedge → high component
          - Low M2 growth → less debasement pressure → lower component

        Maps M2 growth to [0, 1] where 1.0 = gold is most undervalued as hedge.
        """
        if m2_yoy_growth >= self._m2_threshold:
            return 1.0
        elif m2_yoy_growth <= 0.0:
            return 0.0
        else:
            return m2_yoy_growth / self._m2_threshold

    def _classify_signal(self, composite: float) -> GoldMacroSignal:
        """Classify composite score into a valuation signal."""
        if composite >= 0.6:
            return GoldMacroSignal.UNDERVALUED
        elif composite <= 0.3:
            return GoldMacroSignal.OVERVALUED
        else:
            return GoldMacroSignal.FAIR_VALUE

    def fetch_and_valuate(self, force_refresh: bool = False) -> GoldMacroValuationResult:
        """
        Fetch FRED macro data and compute gold's macro valuation signal.

        Returns:
            GoldMacroValuationResult with raw metrics, component scores,
            composite valuation, and explanation.
        """
        logger.info("GoldMacroValuation: Fetching FRED macro data...")

        results = self._scraper.fetch_multiple(
            [self.SERIES_DFII10, self.SERIES_M2SL],
            force_refresh=force_refresh,
        )

        metrics = MacroMetrics()

        # Extract real rate
        dfii_result = results.get(self.SERIES_DFII10)
        if dfii_result and dfii_result.observations:
            latest = dfii_result.observations[-1]
            metrics.real_rate_10y = latest.value
            metrics.real_rate_date = latest.date

        # Extract M2 and compute YoY growth
        m2_result = results.get(self.SERIES_M2SL)
        if m2_result and m2_result.observations:
            observations = m2_result.observations
            latest_m2 = observations[-1]
            metrics.m2_latest = latest_m2.value
            metrics.m2_date = latest_m2.date

            # Find observation approximately 12 months ago
            if len(observations) >= 2:
                target_idx = max(0, len(observations) - self.M2_LAG_MONTHS)
                year_ago_m2 = observations[target_idx]
                metrics.m2_year_ago = year_ago_m2.value

                if metrics.m2_year_ago and metrics.m2_year_ago > 0:
                    metrics.m2_yoy_growth = (
                        (metrics.m2_latest - metrics.m2_year_ago) / metrics.m2_year_ago
                    ) * 100.0

        # Compute components
        real_rate_component = None
        m2_component = None
        composite_score = None
        explanation_parts = []

        if metrics.real_rate_10y is not None:
            real_rate_component = self._compute_real_rate_component(metrics.real_rate_10y)
            explanation_parts.append(
                f"10Y real rate = {metrics.real_rate_10y:.2f}% "
                f"(component: {real_rate_component:.2f})"
            )

        if metrics.m2_yoy_growth is not None:
            m2_component = self._compute_m2_component(metrics.m2_yoy_growth)
            explanation_parts.append(
                f"M2 YoY growth = {metrics.m2_yoy_growth:.1f}% "
                f"(component: {m2_component:.2f})"
            )

        # Weighted composite
        if real_rate_component is not None and m2_component is not None:
            composite_score = (
                self._w_real * real_rate_component + self._w_m2 * m2_component
            )
        elif real_rate_component is not None:
            composite_score = real_rate_component
        elif m2_component is not None:
            composite_score = m2_component

        signal = GoldMacroSignal.UNKNOWN
        if composite_score is not None:
            signal = self._classify_signal(composite_score)
            explanation_parts.insert(0, f"Signal: {signal.value} (composite: {composite_score:.2f})")

        explanation = "; ".join(explanation_parts) if explanation_parts else "Insufficient macro data for valuation"

        result = GoldMacroValuationResult(
            signal=signal,
            metrics=metrics,
            real_rate_component=real_rate_component,
            m2_component=m2_component,
            composite_score=round(composite_score, 4) if composite_score else None,
            explanation=explanation,
        )

        logger.info(
            f"GoldMacroValuation: signal={signal.value}, "
            f"real_rate={metrics.real_rate_10y}, "
            f"M2_growth={metrics.m2_yoy_growth}"
        )

        return result

    def is_undervalued(self, result: Optional[GoldMacroValuationResult] = None) -> bool:
        """Quick check: is gold currently undervalued by macro metrics?"""
        if result is None:
            result = self.fetch_and_valuate()
        return result.signal == GoldMacroSignal.UNDERVALUED


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s %(message)s")

    valuation = GoldMacroValuation()
    result = valuation.fetch_and_valuate()

    print(f"\n{'='*60}")
    print("GOLD MACRO VALUATION REPORT")
    print(f"{'='*60}")
    print(f"  Signal: {result.signal.value}")
    print(f"  Composite Score: {result.composite_score}" if result.composite_score else "  Composite Score: N/A")
    print(f"  Real Rate Component: {result.real_rate_component}" if result.real_rate_component else "  Real Rate Component: N/A")
    print(f"  M2 Component: {result.m2_component}" if result.m2_component else "  M2 Component: N/A")
    print(f"\n  Metrics:")
    print(f"    10Y Real Rate: {result.metrics.real_rate_10y:.2f}%" if result.metrics.real_rate_10y else "    10Y Real Rate: N/A")
    print(f"    M2 YoY Growth: {result.metrics.m2_yoy_growth:.1f}%" if result.metrics.m2_yoy_growth else "    M2 YoY Growth: N/A")
    print(f"    M2 Latest: ${result.metrics.m2_latest:,.1f}B" if result.metrics.m2_latest else "    M2 Latest: N/A")
    print(f"\n  {result.explanation}")
