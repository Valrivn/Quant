"""
bernoulli_shock_filter.py — Supply Chain & Operational Shock Filter

Implements a per-company Bernoulli trial that models whether a specific
supply disruption or geopolitical event occurs at each Monte Carlo step.

The probability p of the shock is derived from Damodaran's ICR-to-Synthetic-Rating
mapping, NOT hardcoded per ticker.

Shock Execution:
    shock_occurred = 1_{U <= p}
    Where U ~ Uniform(0, 1)

When the shock fires (1), the pipeline applies an FCFE penalty multiplier
directly into the per-year FCF projection inside the Monte Carlo simulation.

The penalty multiplier is computed as:
    penalty = 1 - (LGD * shock_severity)
    where LGD = p_default * (1 - recovery_rate) from the rating tier.

References:
    - Damodaran, "Measuring Value in the Face of Uncertainty"
    - Basel III operational risk framework
"""

import logging
import random
from dataclasses import dataclass
from typing import Optional

from Quantitative.stochastic.default_probability_table import (
    lookup_rating_tier,
    compute_shock_penalty_multiplier,
    get_default_probability,
    get_synthetic_rating,
)
from Quantitative.stochastic.sector_shock_data import (
    compute_dynamic_shock_probability,
    get_sector_shock_stats,
)

logger = logging.getLogger(__name__)


@dataclass
class BernoulliShockResult:
    """Result of a single Bernoulli shock trial."""
    shock_occurred: bool
    shock_probability: float
    synthetic_rating: str
    icr_used: float
    penalty_multiplier: float
    lgd: float
    recovery_rate: float


class BernoulliShockFilter:
    """
    Per-company Bernoulli trial for supply chain & operational shocks.

    The filter derives shock probability from:
    1. The company's Interest Coverage Ratio (ICR)
    2. Damodaran's ICR → Synthetic Rating → p_default mapping
    3. Optional supply chain concentration amplifier

    The default p is the empirical 1-year probability of default for the
    company's synthetic rating tier. This replaces the old
    (supplier_concentration * geopolitical_stress_factor) heuristic.

    When supply chain concentration is high (>0.70), the probability
    is amplified proportionally:
        p_effective = min(1.0, p_default * (1.0 + concentration_boost))
    """

    def __init__(self, use_dynamic_icr: bool = True):
        """
        Args:
            use_dynamic_icr: If True, compute ICR from financial data.
                           If False, use the hardcoded ICR from FUNDAMENTAL_ESTIMATES.
        """
        self.use_dynamic_icr = use_dynamic_icr

    def compute_shock_probability(
        self,
        icr: float,
        supplier_concentration: float = 0.5,
        geopolitical_stress_factor: float = 0.0,
    ) -> float:
        """
        Compute the effective shock probability for a company.

        Uses the empirical default probability from the rating tier as the
        base rate, then applies a supply chain concentration amplifier.

        Args:
            icr: Interest Coverage Ratio
            supplier_concentration: Supplier concentration score [0.0, 1.0]
            geopolitical_stress_factor: Legacy geopolitical stress (optional amplifier)

        Returns:
            Effective shock probability in [0.0, 1.0]
        """
        tier = lookup_rating_tier(icr)
        p_base = tier.p_default_1yr

        # Amplify for supply chain concentration risk
        concentration_boost = 0.0
        if supplier_concentration > 0.70:
            # Excess concentration above 0.70 increases shock probability
            concentration_boost = (supplier_concentration - 0.70) * 2.0

        # Optional geopolitical amplifier (preserves backward compatibility)
        geo_amplifier = 1.0 + geopolitical_stress_factor

        p_effective = min(1.0, p_base * (1.0 + concentration_boost) * geo_amplifier)

        logger.debug(
            f"BernoulliShockFilter: ICR={icr:.2f}, rating={tier.rating}, "
            f"p_base={tier.p_default_1yr:.4f}, p_effective={p_effective:.4f}"
        )

        return p_effective

    def run_trial(
        self,
        icr: float,
        supplier_concentration: float = 0.5,
        geopolitical_stress_factor: float = 0.0,
        shock_severity: float = 1.0,
        rng: Optional[random.Random] = None,
    ) -> BernoulliShockResult:
        """
        Execute a single Bernoulli shock trial.

        Args:
            icr: Interest Coverage Ratio
            supplier_concentration: Supplier concentration [0.0, 1.0]
            geopolitical_stress_factor: Legacy geopolitical stress
            shock_severity: Amplifies the penalty when > 1.0
            rng: Optional seeded RNG for reproducibility

        Returns:
            BernoulliShockResult with shock outcome and penalty details
        """
        p = self.compute_shock_probability(icr, supplier_concentration, geopolitical_stress_factor)

        # Bernoulli trial: shock_occurred = 1_{U <= p}
        _rng = rng or random
        u = _rng.random()
        shock_occurred = u <= p

        tier = lookup_rating_tier(icr)
        lgd = tier.p_default_1yr * (1.0 - tier.recovery_rate)

        penalty = 1.0
        if shock_occurred:
            penalty = compute_shock_penalty_multiplier(icr, shock_severity)
            logger.info(
                f"BernoulliShockFilter: SHOCK FIRED for ICR={icr:.2f} "
                f"(rating={tier.rating}, p={p:.4f}, penalty={penalty:.4f})"
            )

        return BernoulliShockResult(
            shock_occurred=shock_occurred,
            shock_probability=p,
            synthetic_rating=tier.rating,
            icr_used=icr,
            penalty_multiplier=penalty,
            lgd=lgd,
            recovery_rate=tier.recovery_rate,
        )

    def run_trial_dynamic(
        self,
        icr: float,
        sector: str,
        current_margin_vol: float,
        margin_vol_10y: Optional[float] = None,
        supplier_concentration: float = 0.5,
        geopolitical_stress_factor: float = 0.0,
        shock_severity: float = 1.0,
        rng: Optional[random.Random] = None,
    ) -> BernoulliShockResult:
        """
        Execute a Bernoulli shock trial using sector-adjusted dynamic probability.

        Uses the max(p_default, p_shock_dynamic) approach to combine credit
        default risk with sector operational shock risk. For high-quality
        companies (AAA), the sector operational shock probability dominates.
        For distressed companies, credit default probability dominates.

        Args:
            icr: Interest Coverage Ratio
            sector: Sector name (e.g., "semiconductor")
            current_margin_vol: Trailing 12-month operating margin volatility
            margin_vol_10y: 10-year average margin volatility (overrides sector default)
            supplier_concentration: Supplier concentration [0.0, 1.0]
            geopolitical_stress_factor: Legacy geopolitical stress
            shock_severity: Amplifies the penalty when > 1.0
            rng: Optional seeded RNG for reproducibility

        Returns:
            BernoulliShockResult with shock outcome and penalty details
        """
        p_default = lookup_rating_tier(icr).p_default_1yr
        p_sector = compute_dynamic_shock_probability(
            sector=sector,
            current_margin_vol=current_margin_vol,
            margin_vol_10y=margin_vol_10y,
            supplier_concentration=supplier_concentration,
            geopolitical_stress_factor=geopolitical_stress_factor,
        )
        p = max(p_default, p_sector)

        _rng = rng or random
        u = _rng.random()
        shock_occurred = u <= p

        tier = lookup_rating_tier(icr)
        lgd = tier.p_default_1yr * (1.0 - tier.recovery_rate)

        penalty = 1.0
        if shock_occurred:
            penalty = compute_shock_penalty_multiplier(icr, shock_severity)
            logger.info(
                f"BernoulliShockFilter: SHOCK FIRED for ICR={icr:.2f} "
                f"sector={sector} (p_default={p_default:.4f}, p_sector={p_sector:.4f}, "
                f"p_effective={p:.4f}, penalty={penalty:.4f})"
            )

        return BernoulliShockResult(
            shock_occurred=shock_occurred,
            shock_probability=p,
            synthetic_rating=tier.rating,
            icr_used=icr,
            penalty_multiplier=penalty,
            lgd=lgd,
            recovery_rate=tier.recovery_rate,
        )

    def compute_fcf_vector_penalty(
        self,
        icr: float,
        n_years: int,
        supplier_concentration: float = 0.5,
        geopolitical_stress_factor: float = 0.0,
        shock_severity: float = 1.0,
        rng: Optional[random.Random] = None,
    ) -> list[float]:
        """
        Run a Bernoulli trial and return a per-year penalty vector for FCF projection.

        If no shock fires, returns [1.0, 1.0, ..., 1.0] (no penalty).
        If shock fires, returns a vector with the penalty applied to the
        shock year and recovery years following.

        The penalty decays over time following a supply chain recovery curve:
            Year 0 (shock year): full penalty
            Year 1: penalty * 0.6
            Year 2: penalty * 0.3
            Year 3: penalty * 0.1
            Year 4+: 1.0 (fully recovered)

        Args:
            icr: Interest Coverage Ratio
            n_years: Number of projection years
            supplier_concentration: Supplier concentration
            geopolitical_stress_factor: Legacy geopolitical stress
            shock_severity: Penalty amplifier
            rng: Optional seeded RNG

        Returns:
            List of length n_years with penalty multipliers
        """
        result = self.run_trial(icr, supplier_concentration, geopolitical_stress_factor, shock_severity, rng)

        if not result.shock_occurred:
            return [1.0] * n_years

        # Recovery trajectory for FCFE penalty
        recovery_curve = [1.0, 0.6, 0.3, 0.1]
        penalties = []
        for year in range(n_years):
            idx = min(year, len(recovery_curve) - 1)
            penalties.append(result.penalty_multiplier * recovery_curve[idx])
        return penalties
