#!/usr/bin/env python3
"""
spot_gold_tracker.py — FRED Spot Gold Price Feed

Fetches the London Bullion Market Association (LBMA) Gold Fixing Price from
FRED and computes daily log returns for tracking-error and correlation analysis.

FRED Series: GOLDPMGBD228NLBM (Gold Fixing Price 3:00 P.M. London Time)

The tracker produces a return series that is date-aligned to ETF return series,
enabling the gold_etf_screener to compute:
  - Tracking Error (standard deviation of return differential)
  - Pearson Correlation Coefficient (r)
  - Annualized Tracking Difference (directional drift)
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from Quantitative.shared.fred_scraper import FREDScraper, FRED_SERIES_REGISTRY

logger = logging.getLogger(__name__)


@dataclass
class SpotGoldObservation:
    """Single spot gold price observation with computed log return."""
    date: str
    price: float
    log_return: Optional[float] = None


@dataclass
class SpotGoldResult:
    """Complete spot gold price and return series."""
    observations: List[SpotGoldObservation]
    latest_price: Optional[float] = None
    latest_date: Optional[str] = None
    daily_returns: Optional[List[float]] = None
    daily_dates: Optional[List[str]] = None
    annualized_return: Optional[float] = None
    annualized_volatility: Optional[float] = None
    series_id: str = "GOLDPMGBD228NLBM"
    cache_hit: bool = False
    evaluated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict:
        return {
            "latest_price": self.latest_price,
            "latest_date": self.latest_date,
            "observation_count": len(self.observations),
            "annualized_return": self.annualized_return,
            "annualized_volatility": self.annualized_volatility,
            "series_id": self.series_id,
            "cache_hit": self.cache_hit,
            "evaluated_at": self.evaluated_at,
        }


class SpotGoldTracker:
    """
    Fetches spot gold prices from FRED and computes daily log returns.

    The return series is designed to be date-aligned with ETF return series
    from ETFDataFetcher, enabling direct comparison for tracking error
    and correlation analysis.
    """

    SERIES_ID = "GOLDPMGBD228NLBM"
    TRADING_DAYS_PER_YEAR = 252

    def __init__(self, fred_scraper: Optional[FREDScraper] = None):
        self._scraper = fred_scraper or FREDScraper()

    def _compute_log_returns(self, prices: List[float]) -> List[Optional[float]]:
        """
        Compute daily log returns from a price series.

        r_t = ln(P_t / P_{t-1})

        First observation has no return (returns None).
        """
        if not prices:
            return []
        returns = [None]
        for i in range(1, len(prices)):
            if prices[i - 1] > 0 and prices[i] > 0:
                ret = math.log(prices[i] / prices[i - 1])
                returns.append(ret)
            else:
                returns.append(None)
        return returns

    def fetch(self, force_refresh: bool = False) -> SpotGoldResult:
        """
        Fetch spot gold price history and compute daily log returns.

        Returns:
            SpotGoldResult with price observations, daily returns, and
            annualized metrics for correlation/tracking-error analysis.
        """
        logger.info("SpotGoldTracker: Fetching FRED spot gold price data...")

        result = self._scraper.fetch_series(self.SERIES_ID, force_refresh=force_refresh)

        if not result.observations:
            logger.warning("SpotGoldTracker: No observations returned from FRED")
            return SpotGoldResult(observations=[], series_id=self.SERIES_ID, cache_hit=result.cache_hit)

        # Build price series
        observations = []
        prices = []
        for obs in result.observations:
            observations.append(SpotGoldObservation(date=obs.date, price=obs.value))
            prices.append(obs.value)

        # Compute log returns
        log_returns = self._compute_log_returns(prices)

        for i, ret in enumerate(log_returns):
            observations[i].log_return = ret

        # Build aligned return series (excluding first None)
        daily_returns = [r for r in log_returns if r is not None]
        daily_dates = [
            observations[i].date
            for i in range(len(log_returns))
            if log_returns[i] is not None
        ]

        # Compute annualized metrics
        annualized_return = None
        annualized_volatility = None
        if daily_returns:
            n = len(daily_returns)
            mean_daily = sum(daily_returns) / n
            annualized_return = mean_daily * self.TRADING_DAYS_PER_YEAR

            if n > 1:
                variance = sum((r - mean_daily) ** 2 for r in daily_returns) / (n - 1)
                daily_vol = math.sqrt(variance)
                annualized_volatility = daily_vol * math.sqrt(self.TRADING_DAYS_PER_YEAR)

        latest = observations[-1] if observations else None

        spot_result = SpotGoldResult(
            observations=observations,
            latest_price=latest.price if latest else None,
            latest_date=latest.date if latest else None,
            daily_returns=daily_returns,
            daily_dates=daily_dates,
            annualized_return=round(annualized_return, 6) if annualized_return is not None else None,
            annualized_volatility=round(annualized_volatility, 6) if annualized_volatility is not None else None,
            series_id=self.SERIES_ID,
            cache_hit=result.cache_hit,
        )

        logger.info(
            f"SpotGoldTracker: {len(observations)} observations, "
            f"latest=${latest.price:.2f} ({latest.date})" if latest else
            "SpotGoldTracker: no data available"
        )

        return spot_result

    def get_returns_aligned(
        self,
        etf_dates: List[str],
        force_refresh: bool = False,
    ) -> Tuple[List[float], List[str]]:
        """
        Get spot gold returns aligned to the given ETF date series.

        Only returns data points where both spot gold and ETF have observations
        on the same date, enabling direct correlation computation.

        Args:
            etf_dates: List of date strings from the ETF return series

        Returns:
            Tuple of (aligned_spot_returns, aligned_dates)
        """
        result = self.fetch(force_refresh=force_refresh)

        spot_by_date = {
            obs.date: obs.log_return
            for obs in result.observations
            if obs.log_return is not None
        }

        etf_date_set = set(etf_dates)
        aligned_returns = []
        aligned_dates = []

        for date in etf_dates:
            if date in spot_by_date and spot_by_date[date] is not None:
                aligned_returns.append(spot_by_date[date])
                aligned_dates.append(date)

        logger.info(
            f"SpotGoldTracker: {len(aligned_dates)} aligned data points "
            f"out of {len(etf_dates)} ETF dates"
        )

        return aligned_returns, aligned_dates


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(name)s %(message)s")

    tracker = SpotGoldTracker()
    result = tracker.fetch()

    print(f"\n{'='*60}")
    print("SPOT GOLD TRACKER REPORT")
    print(f"{'='*60}")
    print(f"  Latest Price: ${result.latest_price:.2f}" if result.latest_price else "  Latest Price: N/A")
    print(f"  Latest Date: {result.latest_date}")
    print(f"  Observations: {len(result.observations)}")
    print(f"  Annualized Return: {result.annualized_return:.4%}" if result.annualized_return else "  Annualized Return: N/A")
    print(f"  Annualized Volatility: {result.annualized_volatility:.4%}" if result.annualized_volatility else "  Annualized Volatility: N/A")
    print(f"  Cache Hit: {result.cache_hit}")
