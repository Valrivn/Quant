"""
default_probability_table.py — Damodaran ICR-to-Default Probability Mapping

Maps a company's Interest Coverage Ratio (ICR) to a synthetic credit rating
using Damodaran's published mapping, then derives the empirical 1-year
probability of default (p_default) for that rating.

These probabilities serve as the canonical input for the Bernoulli shock
filter, enabling a data-driven catastrophe probability rather than
hardcoded estimates.

References:
  - Aswath Damodaran, "Measuring Value in the Face of Uncertainty"
  - Damodaran's ICR-to-Synthetic-Rating table (spread_table.xls)
  - Moody's/S&P historical default rate studies (1920-2023)
"""

import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RatingTier:
    """A single credit rating tier with its ICR threshold and default stats."""
    icr_threshold: float
    rating: str
    spread: float
    p_default_1yr: float
    p_default_5yr: float
    recovery_rate: float


# ---------------------------------------------------------------------------
# Damodaran's canonical ICR → Synthetic Rating → Default Probability table
#
# p_default_1yr: Empirical 1-year probability of default for this rating.
#   Source: Moody's Annual Default Study, S&P Global Ratings Direct,
#   and Damodaran's published tables. These are long-run averages across
#   economic cycles.
#
# p_default_5yr: Empirical cumulative 5-year probability of default.
#
# recovery_rate: Historical average recovery rate for this rating tier
#   in the event of default (used for LGD calculations).
# ---------------------------------------------------------------------------

RATING_TABLE: list[RatingTier] = [
    RatingTier(icr_threshold=12.5, rating="AAA", spread=0.0063, p_default_1yr=0.0001, p_default_5yr=0.0004, recovery_rate=0.60),
    RatingTier(icr_threshold=9.5,  rating="AA",  spread=0.0075, p_default_1yr=0.0003, p_default_5yr=0.0015, recovery_rate=0.58),
    RatingTier(icr_threshold=7.5,  rating="A+",  spread=0.0090, p_default_1yr=0.0005, p_default_5yr=0.0025, recovery_rate=0.56),
    RatingTier(icr_threshold=6.0,  rating="A",   spread=0.0105, p_default_1yr=0.0008, p_default_5yr=0.0040, recovery_rate=0.55),
    RatingTier(icr_threshold=4.5,  rating="A-",  spread=0.0120, p_default_1yr=0.0012, p_default_5yr=0.0060, recovery_rate=0.53),
    RatingTier(icr_threshold=4.0,  rating="BBB", spread=0.0150, p_default_1yr=0.0018, p_default_5yr=0.0090, recovery_rate=0.50),
    RatingTier(icr_threshold=3.5,  rating="BB+", spread=0.0200, p_default_1yr=0.0035, p_default_5yr=0.0180, recovery_rate=0.45),
    RatingTier(icr_threshold=3.0,  rating="BB",  spread=0.0250, p_default_1yr=0.0070, p_default_5yr=0.0350, recovery_rate=0.42),
    RatingTier(icr_threshold=2.5,  rating="B+",  spread=0.0325, p_default_1yr=0.0120, p_default_5yr=0.0600, recovery_rate=0.38),
    RatingTier(icr_threshold=2.0,  rating="B",   spread=0.0400, p_default_1yr=0.0180, p_default_5yr=0.0900, recovery_rate=0.35),
    RatingTier(icr_threshold=1.5,  rating="B-",  spread=0.0525, p_default_1yr=0.0300, p_default_5yr=0.1400, recovery_rate=0.32),
    RatingTier(icr_threshold=1.0,  rating="CCC", spread=0.0650, p_default_1yr=0.0500, p_default_5yr=0.2200, recovery_rate=0.28),
    RatingTier(icr_threshold=0.5,  rating="CC",  spread=0.0850, p_default_1yr=0.0800, p_default_5yr=0.3500, recovery_rate=0.22),
    RatingTier(icr_threshold=0.0,  rating="C",   spread=0.1000, p_default_1yr=0.1200, p_default_5yr=0.5000, recovery_rate=0.15),
]

# Distressed default tier (ICR <= 0)
DISTRESSED_TIER = RatingTier(
    icr_threshold=-1.0, rating="D", spread=0.1200,
    p_default_1yr=0.2500, p_default_5yr=0.7000, recovery_rate=0.10,
)


def lookup_rating_tier(icr: float) -> RatingTier:
    """
    Look up the synthetic credit rating tier for a given ICR.

    Iterates the Damodaran table from highest to lowest ICR threshold.
    Returns the first tier where icr >= threshold.
    """
    if icr <= 0:
        return DISTRESSED_TIER
    for tier in RATING_TABLE:
        if icr >= tier.icr_threshold:
            return tier
    return DISTRESSED_TIER


def get_default_probability(icr: float, horizon: int = 1) -> float:
    """
    Get the empirical probability of default for a given ICR.

    Args:
        icr: Interest Coverage Ratio
        horizon: 1 for 1-year probability, 5 for 5-year cumulative

    Returns:
        Probability of default in [0.0, 1.0]
    """
    tier = lookup_rating_tier(icr)
    if horizon <= 1:
        return tier.p_default_1yr
    elif horizon <= 5:
        return tier.p_default_5yr
    else:
        # Extrapolate conservatively (cap at 5-year value)
        return tier.p_default_5yr


def get_synthetic_rating(icr: float) -> str:
    """Return the synthetic credit rating string for a given ICR."""
    return lookup_rating_tier(icr).rating


def get_credit_spread(icr: float) -> float:
    """Return the credit spread above risk-free rate for a given ICR."""
    return lookup_rating_tier(icr).spread


def get_recovery_rate(icr: float) -> float:
    """Return the historical recovery rate for a given ICR's rating tier."""
    return lookup_rating_tier(icr).recovery_rate


def compute_shock_penalty_multiplier(
    icr: float,
    shock_severity: float = 1.0,
) -> float:
    """
    Compute the FCFE penalty multiplier when a Bernoulli shock fires.

    Uses the rating tier's default probability and recovery rate to
    estimate the expected loss given default (LGD):
        LGD = p_default * (1 - recovery_rate)

    The penalty multiplier scales the FCFE downward:
        penalty = 1.0 - (LGD * shock_severity)

    For a BB+ rated company (p_default=1.8%, recovery=45%):
        LGD = 0.018 * 0.55 = 0.0099 → penalty ≈ 0.99 (mild)
    For a CCC rated company (p_default=5.0%, recovery=28%):
        LGD = 0.050 * 0.72 = 0.036 → penalty ≈ 0.964

    When shock_severity > 1.0 (e.g., for concentrated supply chains),
    the penalty amplifies proportionally.
    """
    tier = lookup_rating_tier(icr)
    lgd = tier.p_default_1yr * (1.0 - tier.recovery_rate)
    raw_penalty = 1.0 - (lgd * shock_severity)
    return max(0.0, min(1.0, raw_penalty))


def build_default_probability_map() -> Dict[str, float]:
    """
    Build a lookup dictionary mapping synthetic rating → p_default_1yr.
    Useful for bulk lookups and caching.
    """
    return {tier.rating: tier.p_default_1yr for tier in RATING_TABLE}
