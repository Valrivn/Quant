#!/usr/bin/env python3
"""
credit_spread_monitor.py — FRED Credit Spread Monitoring

Monitors the credit yield spread between corporate bonds and equivalent-maturity
U.S. Treasuries to detect hidden market-priced default risk.

Uses FRED series:
  - BAA10Y: Moody's Seasoned Baa Corporate Bond Yield Relative to 10-Year Treasury
  - BAMLC0A4CBBB: ICE BofA BBB US Corporate Index Effective Yield
  - DGS10: Market Yield on 10-Year Treasury Securities

Damodaran Principle: If credit spreads widen while agency ratings remain unchanged,
override the rating and flag hidden market-priced default risk. The market prices
risk faster than rating agencies update their assessments.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

from Quantitative.shared.fred_scraper import FREDScraper, FRED_SERIES_REGISTRY

logger = logging.getLogger(__name__)


class SpreadRegime(Enum):
    """Credit spread regime classification based on BAA10Y spread levels."""
    NORMAL = "NORMAL"
    WIDENING = "WIDENING"
    CRISIS = "CRISIS"
    UNKNOWN = "UNKNOWN"


class SpreadDirection(Enum):
    """Directional movement of credit spreads over the lookback window."""
    TIGHTENING = "TIGHTENING"
    STABLE = "STABLE"
    WIDENING = "WIDENING"


@dataclass
class SpreadSnapshot:
    """Point-in-time credit spread measurement."""
    baa_yield: Optional[float] = None
    treasury_10y: Optional[float] = None
    baa10y_spread: Optional[float] = None
    bofa_bbb_yield: Optional[float] = None
    spread_bps: Optional[float] = None
    date: Optional[str] = None


@dataclass
class SpreadRegimeResult:
    """Complete credit spread regime analysis."""
    current_spread_bps: Optional[float]
    regime: SpreadRegime
    direction: SpreadDirection
    spread_history_bps: List[float]
    baa10y_latest: Optional[Tuple[str, float]] = None
    bofa_bbb_latest: Optional[Tuple[str, float]] = None
    treasury_10y_latest: Optional[Tuple[str, float]] = None
    regime_change_alert: bool = False
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict:
        return {
            "current_spread_bps": self.current_spread_bps,
            "regime": self.regime.value,
            "direction": self.direction.value,
            "spread_history_bps": self.spread_history_bps[-10:],
            "regime_change_alert": self.regime_change_alert,
            "baa10y_latest": self.baa10y_latest,
            "bofa_bbb_latest": self.bofa_bbb_latest,
            "treasury_10y_latest": self.treasury_10y_latest,
            "evaluated_at": self.evaluated_at,
        }


class CreditSpreadMonitor:
    """
    Monitors credit spreads between corporate bonds and U.S. Treasuries.

    Regime thresholds (in basis points):
      - NORMAL:    < 200 bps (healthy credit environment)
      - WIDENING:  200-300 bps (elevated stress, monitor closely)
      - CRISIS:    > 300 bps (systemic credit risk, flight to safety)

    These thresholds are configurable to allow pivot/tuning.
    """

    # Default regime thresholds in basis points
    THRESHOLD_WIDENING_BPS = 200.0
    THRESHOLD_CRISIS_BPS = 300.0

    # Direction detection: lookback window for trend analysis
    DIRECTION_LOOKBACK_DAYS = 30

    # FRED series keys
    SERIES_BAA10Y = "BAA10Y"
    SERIES_BOFA_BBB = "BAMLC0A4CBBB"
    SERIES_TREASURY_10Y = "DGS10"

    def __init__(
        self,
        fred_scraper: Optional[FREDScraper] = None,
        widening_threshold_bps: float = 200.0,
        crisis_threshold_bps: float = 300.0,
        direction_lookback: int = 30,
    ):
        self._scraper = fred_scraper or FREDScraper()
        self._widening_threshold = widening_threshold_bps
        self._crisis_threshold = crisis_threshold_bps
        self._lookback = direction_lookback

    def _compute_spread_series(
        self,
        baa_observations: List,
        treasury_observations: List,
    ) -> List[float]:
        """
        Compute daily credit spread in basis points from matched date pairs.

        Spread = (BAA Corporate Yield - 10Y Treasury Yield) * 100
        Only includes dates where both observations exist.
        """
        baa_by_date = {obs.date: obs.value for obs in baa_observations}
        treasury_by_date = {obs.date: obs.value for obs in treasury_observations}

        common_dates = sorted(set(baa_by_date.keys()) & set(treasury_by_date.keys()))

        spreads_bps = []
        for date in common_dates:
            spread = (baa_by_date[date] - treasury_by_date[date]) * 100.0
            spreads_bps.append(spread)

        return spreads_bps

    def _classify_regime(self, spread_bps: float) -> SpreadRegime:
        """Classify current spread level into a regime bucket."""
        if spread_bps > self._crisis_threshold:
            return SpreadRegime.CRISIS
        elif spread_bps > self._widening_threshold:
            return SpreadRegime.WIDENING
        else:
            return SpreadRegime.NORMAL

    def _detect_direction(self, spreads_bps: List[float]) -> SpreadDirection:
        """
        Detect directional movement using linear regression slope over lookback window.

        A positive slope indicates widening (deteriorating credit conditions).
        A negative slope indicates tightening (improving credit conditions).
        """
        if len(spreads_bps) < 2:
            return SpreadDirection.STABLE

        recent = spreads_bps[-self._lookback:]
        n = len(recent)

        x_mean = (n - 1) / 2.0
        y_mean = sum(recent) / n

        numerator = sum((i - x_mean) * (recent[i] - y_mean) for i in range(n))
        denominator = sum((i - x_mean) ** 2 for i in range(n))

        if denominator == 0:
            return SpreadDirection.STABLE

        slope = numerator / denominator

        # Classify: slope > 0.5 bps/day = widening, < -0.5 bps/day = tightening
        if slope > 0.5:
            return SpreadDirection.WIDENING
        elif slope < -0.5:
            return SpreadDirection.TIGHTENING
        else:
            return SpreadDirection.STABLE

    def fetch_and_classify(self, force_refresh: bool = False) -> SpreadRegimeResult:
        """
        Fetch FRED credit spread data and classify the current regime.

        Returns:
            SpreadRegimeResult with current spread, regime, direction, and history.
        """
        logger.info("CreditSpreadMonitor: Fetching FRED credit spread data...")

        results = self._scraper.fetch_multiple(
            [self.SERIES_BAA10Y, self.SERIES_BOFA_BBB, self.SERIES_TREASURY_10Y],
            force_refresh=force_refresh,
        )

        baa_result = results.get(self.SERIES_BAA10Y)
        bofa_result = results.get(self.SERIES_BOFA_BBB)
        treasury_result = results.get(self.SERIES_TREASURY_10Y)

        # Extract latest values
        baa10y_latest = None
        if baa_result and baa_result.observations:
            obs = baa_result.observations[-1]
            baa10y_latest = (obs.date, obs.value)

        bofa_bbb_latest = None
        if bofa_result and bofa_result.observations:
            obs = bofa_result.observations[-1]
            bofa_bbb_latest = (obs.date, obs.value)

        treasury_10y_latest = None
        if treasury_result and treasury_result.observations:
            obs = treasury_result.observations[-1]
            treasury_10y_latest = (obs.date, obs.value)

        # Compute spread series from BAA10Y (direct spread series from FRED)
        # BAA10Y is already a spread: Moody's Baa yield minus 10-year Treasury
        spread_series_bps = []
        current_spread_bps = None

        if baa_result and baa_result.observations:
            for obs in baa_result.observations:
                spread_bps = obs.value * 100.0
                spread_series_bps.append(spread_bps)

            if spread_series_bps:
                current_spread_bps = spread_series_bps[-1]

        # If BAA10Y not available, compute from raw yields
        if current_spread_bps is None and bofa_bbb_latest and treasury_10y_latest:
            current_spread_bps = (bofa_bbb_latest[1] - treasury_10y_latest[1]) * 100.0

            # Build history from matched observations
            if bofa_result and treasury_result:
                spreads = self._compute_spread_series(
                    bofa_result.observations,
                    treasury_result.observations,
                )
                spread_series_bps = spreads

        regime = self._classify_regime(current_spread_bps) if current_spread_bps is not None else SpreadRegime.UNKNOWN
        direction = self._detect_direction(spread_series_bps)

        # Check for regime change alert (transition to CRISIS or from NORMAL to WIDENING)
        regime_change_alert = False
        if len(spread_series_bps) >= 2:
            prev_spread = spread_series_bps[-2]
            prev_regime = self._classify_regime(prev_spread)
            if regime != prev_regime:
                regime_change_alert = True
                logger.warning(
                    f"Credit spread regime change: {prev_regime.value} -> {regime.value} "
                    f"(spread: {prev_spread:.1f} -> {current_spread_bps:.1f} bps)"
                )

        result = SpreadRegimeResult(
            current_spread_bps=round(current_spread_bps, 2) if current_spread_bps else None,
            regime=regime,
            direction=direction,
            spread_history_bps=spread_series_bps[-90:],
            baa10y_latest=baa10y_latest,
            bofa_bbb_latest=bofa_bbb_latest,
            treasury_10y_latest=treasury_10y_latest,
            regime_change_alert=regime_change_alert,
        )

        logger.info(
            f"CreditSpreadMonitor: regime={regime.value}, "
            f"spread={current_spread_bps:.1f} bps" if current_spread_bps else
            "CreditSpreadMonitor: insufficient data for classification"
        )

        return result

    def is_crisis(self, result: Optional[SpreadRegimeResult] = None) -> bool:
        """Quick check: is the credit market in crisis mode?"""
        if result is None:
            result = self.fetch_and_classify()
        return result.regime == SpreadRegime.CRISIS

    def get_spread_bps(self, result: Optional[SpreadRegimeResult] = None) -> Optional[float]:
        """Get the current credit spread in basis points."""
        if result is None:
            result = self.fetch_and_classify()
        return result.current_spread_bps


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s %(message)s")

    monitor = CreditSpreadMonitor()
    result = monitor.fetch_and_classify()

    print(f"\n{'='*60}")
    print("CREDIT SPREAD MONITOR REPORT")
    print(f"{'='*60}")
    print(f"  Current Spread: {result.current_spread_bps:.1f} bps" if result.current_spread_bps else "  Current Spread: N/A")
    print(f"  Regime: {result.regime.value}")
    print(f"  Direction: {result.direction.value}")
    print(f"  Regime Change Alert: {'YES' if result.regime_change_alert else 'No'}")
    if result.baa10y_latest:
        print(f"  BAA10Y Latest: {result.baa10y_latest[0]} = {result.baa10y_latest[1]:.2f}%")
    if result.bofa_bbb_latest:
        print(f"  BofA BBB Latest: {result.bofa_bbb_latest[0]} = {result.bofa_bbb_latest[1]:.2f}%")
    if result.treasury_10y_latest:
        print(f"  10Y Treasury: {result.treasury_10y_latest[0]} = {result.treasury_10y_latest[1]:.2f}%")
    print(f"\n  Regime Thresholds: Normal < {monitor._widening_threshold:.0f} bps | "
          f"Widening {monitor._widening_threshold:.0f}-{monitor._crisis_threshold:.0f} bps | "
          f"Crisis > {monitor._crisis_threshold:.0f} bps")
