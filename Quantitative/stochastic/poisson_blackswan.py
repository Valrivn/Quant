"""
poisson_blackswan.py — Monte Carlo Black Swan Counts via Poisson Process

Estimates the total frequency of independent tail-risk events across the
entire portfolio over a fixed time frame using a Poisson distribution.

Instead of simulating just one price drop, this module calculates how many
systemic structural drops hit simultaneously across holdings within a
simulated year.

The intensity parameter (lambda) is NOT static. It scales dynamically
using FRED macro data spreads:

    lambda_stress = lambda_base * (Current_Spread / Historical_Mean_Spread)

When credit spreads widen into a CRISIS regime, the expected shock arrival
intensity increases proportionally.

References:
  - Poisson Process: Norris, "Applied Stochastic Processes"
  - Tail risk modeling: Taleb, "The Black Swan"
  - Credit spread dynamics: Damodaran, "Risk Premiums"
"""

import logging
import math
import random
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class PoissonShockResult:
    """Result of a Poisson black swan simulation."""
    n_shocks: int
    lambda_base: float
    lambda_stress: float
    spread_ratio: float
    current_spread_bps: Optional[float]
    historical_mean_spread_bps: float
    regime: str
    shock_magnitudes: List[float]
    portfolio_impact: float


# ---------------------------------------------------------------------------
# Historical baseline parameters
# ---------------------------------------------------------------------------

# Base expected number of severe market disruptions per year
# Historically: large macro corrections or inflation shocks occur at
# a long-term base rate of ~0.25 to 0.35 per year.
DEFAULT_LAMBDA_BASE = 0.30

# Historical mean BAA10Y credit spread (in basis points)
# Long-term average from 1986-2024: approximately 220 bps
HISTORICAL_MEAN_SPREAD_BPS = 220.0

# Shock magnitude distribution parameters
# When a black swan hits, the severity follows a LogNormal distribution
# calibrated to historical tail events (2008, 2020, 2022)
SHOCK_MEAN_MAGNITUDE = -0.15  # Average 15% drawdown
SHOCK_STD_MAGNITUDE = 0.08    # Standard deviation of severity


class PoissonBlackSwan:
    """
    Poisson process model for systemic black swan events.

    The model:
    1. Computes baseline intensity lambda_base from historical data
    2. Scales lambda using current credit spread from CreditSpreadMonitor
    3. Samples from Poisson(lambda_stress) to determine shock count
    4. For each shock, samples severity from LogNormal distribution
    5. Returns portfolio-level impact vector

    Integration with CreditSpreadMonitor:
        When the credit spread monitor reports CRISIS regime, the lambda
        is automatically boosted, increasing the expected number of
        simultaneous systemic shocks.
    """

    def __init__(
        self,
        lambda_base: float = DEFAULT_LAMBDA_BASE,
        historical_mean_spread_bps: float = HISTORICAL_MEAN_SPREAD_BPS,
        shock_mean: float = SHOCK_MEAN_MAGNITUDE,
        shock_std: float = SHOCK_STD_MAGNITUDE,
    ):
        self.lambda_base = lambda_base
        self.historical_mean_spread_bps = historical_mean_spread_bps
        self.shock_mean = shock_mean
        self.shock_std = shock_std

    def compute_stress_lambda(
        self,
        current_spread_bps: Optional[float] = None,
        regime: str = "NORMAL",
    ) -> float:
        """
        Compute the stress-adjusted intensity parameter.

        Formula:
            lambda_stress = lambda_base * (Current_Spread / Historical_Mean_Spread)

        When spread data is unavailable, regime-based multipliers are used:
            NORMAL:     1.0x base
            WIDENING:   1.5x base
            CRISIS:     2.5x base

        Args:
            current_spread_bps: Current BAA10Y credit spread in basis points
            regime: Credit spread regime (NORMAL/WIDENING/CRISIS)

        Returns:
            Stress-adjusted lambda >= 0
        """
        if current_spread_bps is not None and current_spread_bps > 0:
            spread_ratio = current_spread_bps / self.historical_mean_spread_bps
            lambda_stress = self.lambda_base * spread_ratio
            logger.info(
                f"PoissonBlackSwan: spread={current_spread_bps:.1f}bps, "
                f"mean={self.historical_mean_spread_bps:.1f}bps, "
                f"ratio={spread_ratio:.3f}, lambda_stress={lambda_stress:.4f}"
            )
        else:
            # Fallback to regime-based multipliers
            regime_multipliers = {
                "NORMAL": 1.0,
                "WIDENING": 1.5,
                "CRISIS": 2.5,
                "UNKNOWN": 1.0,
            }
            multiplier = regime_multipliers.get(regime, 1.0)
            lambda_stress = self.lambda_base * multiplier
            logger.info(
                f"PoissonBlackSwan: no spread data, regime={regime}, "
                f"multiplier={multiplier:.1f}, lambda_stress={lambda_stress:.4f}"
            )

        return max(0.0, lambda_stress)

    def sample_shock_count(
        self,
        lambda_stress: float,
        rng: Optional[random.Random] = None,
    ) -> int:
        """
        Sample the number of independent black swan events from
        Poisson(lambda_stress).

        Uses numpy's Poisson sampler for accuracy.
        """
        return int(np.random.poisson(lambda_stress))

    def sample_shock_magnitudes(
        self,
        n_shocks: int,
        rng: Optional[random.Random] = None,
    ) -> List[float]:
        """
        Sample the severity of each black swan event from a
        LogNormal distribution calibrated to historical tail events.

        Returns a list of negative multipliers (drawdowns).
        e.g., [-0.12, -0.25] means two shocks of 12% and 25%.
        """
        if n_shocks <= 0:
            return []

        magnitudes = []
        for _ in range(n_shocks):
            # LogNormal ensures magnitude is always negative (drawdown)
            raw = np.random.lognormal(
                mean=self.shock_mean,
                sigma=self.shock_std,
            )
            # Clamp to reasonable bounds [-50%, -1%]
            magnitude = max(-0.50, min(-0.01, raw))
            magnitudes.append(round(magnitude, 4))

        return magnitudes

    def compute_portfolio_impact(
        self,
        shock_magnitudes: List[float],
        portfolio_weights: Optional[List[float]] = None,
    ) -> float:
        """
        Compute the aggregate portfolio impact from multiple simultaneous
        black swan events.

        If portfolio_weights are provided, the impact is weighted by
        each holding's weight. Otherwise, equal weighting is assumed.

        The total impact is the sum of individual shock impacts, capped
        at -100% (total loss).
        """
        if not shock_magnitudes:
            return 0.0

        if portfolio_weights is None:
            # Equal weight: each shock affects the whole portfolio equally
            total_impact = sum(shock_magnitudes)
        else:
            # Weighted: each shock affects a different portion
            total_impact = sum(
                m * w for m, w in zip(shock_magnitudes, portfolio_weights)
            )

        return max(-1.0, min(0.0, total_impact))

    def simulate(
        self,
        current_spread_bps: Optional[float] = None,
        regime: str = "NORMAL",
        portfolio_weights: Optional[List[float]] = None,
        n_portfolios: int = 1,
        rng: Optional[random.Random] = None,
    ) -> PoissonShockResult:
        """
        Full Poisson black swan simulation.

        1. Compute stress-adjusted lambda
        2. Sample shock count from Poisson(lambda_stress)
        3. Sample shock magnitudes from LogNormal
        4. Compute portfolio impact

        Args:
            current_spread_bps: Current BAA10Y spread from CreditSpreadMonitor
            regime: Credit spread regime
            portfolio_weights: Optional portfolio weights
            n_portfolios: Number of independent portfolio simulations
            rng: Optional seeded RNG

        Returns:
            PoissonShockResult with shock count, magnitudes, and impact
        """
        lambda_stress = self.compute_stress_lambda(current_spread_bps, regime)
        spread_ratio = (
            current_spread_bps / self.historical_mean_spread_bps
            if current_spread_bps and current_spread_bps > 0
            else 1.0
        )

        # Simulate across n_portfolios and aggregate
        all_shock_counts = []
        all_magnitudes = []
        all_impacts = []

        for _ in range(n_portfolios):
            n_shocks = self.sample_shock_count(lambda_stress, rng)
            magnitudes = self.sample_shock_magnitudes(n_shocks, rng)
            impact = self.compute_portfolio_impact(magnitudes, portfolio_weights)

            all_shock_counts.append(n_shocks)
            all_magnitudes.extend(magnitudes)
            all_impacts.append(impact)

        # Aggregate results
        total_shocks = sum(all_shock_counts)
        mean_impact = sum(all_impacts) / len(all_impacts) if all_impacts else 0.0

        logger.info(
            f"PoissonBlackSwan: lambda_stress={lambda_stress:.4f}, "
            f"total_shocks={total_shocks}, mean_impact={mean_impact:.4f}"
        )

        return PoissonShockResult(
            n_shocks=total_shocks,
            lambda_base=self.lambda_base,
            lambda_stress=lambda_stress,
            spread_ratio=spread_ratio,
            current_spread_bps=current_spread_bps,
            historical_mean_spread_bps=self.historical_mean_spread_bps,
            regime=regime,
            shock_magnitudes=all_magnitudes,
            portfolio_impact=mean_impact,
        )

    def sample_per_simulation_shocks(
        self,
        lambda_stress: float,
    ) -> List[float]:
        """
        Sample black swan shock magnitudes for a SINGLE Monte Carlo pass.

        This is the interface used by monte_carlo.py's _simulate_single.
        Returns a list of negative multipliers (may be empty if no shocks
        in this simulation step).

        Returns:
            List of shock magnitudes (negative floats) for this simulation
        """
        n_shocks = self.sample_shock_count(lambda_stress)
        return self.sample_shock_magnitudes(n_shocks)
