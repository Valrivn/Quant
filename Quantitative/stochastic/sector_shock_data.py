"""
sector_shock_data.py — Sector Operational Shock Data Loader

Loads pre-computed sector shock probabilities from data/sector_shock_probs.json
and provides them for the dynamic Bernoulli shock filter.

Uses Bayesian shrinkage to pull small-sample estimates toward a prior,
preventing extreme p_base values from short history windows.

The dynamic shock probability formula (per user specification):
    p_shock_dynamic(t) = p_base × (σ_margin_TTM / σ_margin_10Y)

When σ_margin_TTM > σ_margin_10Y (margin volatility expanding),
p_shock increases — modeling rising operational risk during cyclical peaks.

References:
    - Lynch, "One Up on Wall Street": Hardware cyclicals face severe
      industry supply gluts even when their balance sheet cash is high.
    - Damodaran (Session 7 & 8): Operational shocks compress after-tax
      EBIT and reduce reinvestment efficiency.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
DATA_FILE = ROOT / "data" / "sector_shock_probs.json"


@dataclass(frozen=True)
class SectorShockStats:
    """Sector-level operational shock statistics."""
    sector: str
    p_base_raw: float
    p_base: float
    n_firms: int
    n_years: int
    n_shocks: int
    margin_vol_10y: float


# Hardcoded fallback defaults (long-run tech sector averages)
# Used when JSON data file is unavailable
DEFAULTS = {
    "semiconductor": SectorShockStats(
        sector="semiconductor",
        p_base_raw=0.08,
        p_base=0.08,
        n_firms=30,
        n_years=300,
        n_shocks=24,
        margin_vol_10y=0.06,
    ),
    "platform_software": SectorShockStats(
        sector="platform_software",
        p_base_raw=0.02,
        p_base=0.02,
        n_firms=30,
        n_years=300,
        n_shocks=6,
        margin_vol_10y=0.03,
    ),
    "hardware_oem": SectorShockStats(
        sector="hardware_oem",
        p_base_raw=0.06,
        p_base=0.06,
        n_firms=30,
        n_years=300,
        n_shocks=18,
        margin_vol_10y=0.08,
    ),
}

# Bayesian prior: Beta(alpha=2, beta=98) centered at 0.02
# This pulls small-sample estimates toward 2% long-run average
_PRIOR_ALPHA = 2.0
_PRIOR_BETA = 98.0

# Floor and ceiling for p_base after shrinkage
_P_BASE_FLOOR = 0.005   # 0.5% minimum — even stable sectors have some risk
_P_BASE_CEILING = 0.12  # 12% maximum — cap from small-sample noise


def _bayesian_shrinkage(k: int, n: int) -> float:
    """
    Compute Bayesian posterior mean for p_base using Beta-Binomial conjugate.

    Prior: Beta(alpha=2, beta=98) — centered at 0.02
    Likelihood: Binomial(k shocks in n firm-years)
    Posterior: Beta(alpha + k, beta + n - k)
    Posterior mean: (alpha + k) / (alpha + beta + n)
    """
    posterior_mean = (_PRIOR_ALPHA + k) / (_PRIOR_ALPHA + _PRIOR_BETA + n)
    return max(_P_BASE_FLOOR, min(_P_BASE_CEILING, posterior_mean))


def _load_json_data() -> Optional[dict]:
    """Load sector shock data from JSON file."""
    if not DATA_FILE.exists():
        return None
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load {DATA_FILE}: {e}")
        return None


_CACHE: Optional[dict] = None


def _get_data() -> dict:
    """Get sector shock data, loading from JSON or using defaults."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    raw = _load_json_data()
    if raw is None:
        logger.info("Using hardcoded sector shock defaults (no JSON data file)")
        _CACHE = {k: v.__dict__ for k, v in DEFAULTS.items()}
        return _CACHE

    result = {}
    for sector, stats in raw.items():
        p_base_raw = stats.get("p_base", 0.02)
        n_shocks = stats.get("n_shocks", 0)
        n_years = stats.get("n_years", 0)
        p_base = _bayesian_shrinkage(n_shocks, n_years)
        result[sector] = {
            "sector": sector,
            "p_base_raw": p_base_raw,
            "p_base": p_base,
            "n_firms": stats.get("n_firms", 0),
            "n_years": n_years,
            "n_shocks": n_shocks,
            "margin_vol_10y": stats.get("margin_vol_10y", 0.05),
        }

    _CACHE = result
    return result


def get_sector_shock_stats(sector: str) -> SectorShockStats:
    """
    Get operational shock statistics for a sector.

    Returns a SectorShockStats with Bayesian-shrunk p_base and margin volatility.
    Falls back to hardcoded defaults for unknown sectors.
    """
    data = _get_data()
    stats = data.get(sector)
    if stats is None:
        logger.warning(f"Unknown sector '{sector}', using platform_software defaults")
        stats = data.get("platform_software", DEFAULTS["platform_software"].__dict__)
    return SectorShockStats(**stats)


def compute_dynamic_shock_probability(
    sector: str,
    current_margin_vol: float,
    margin_vol_10y: Optional[float] = None,
    supplier_concentration: float = 0.5,
    geopolitical_stress_factor: float = 0.0,
) -> float:
    """
    Compute the dynamic sector-adjusted shock probability.

    Formula (per user specification):
        p_shock_dynamic(t) = p_base × (σ_margin_TTM / σ_margin_10Y)

    When current margin volatility exceeds the 10-year average,
    operational shock probability increases proportionally.

    Args:
        sector: Sector name (e.g., "semiconductor", "platform_software")
        current_margin_vol: Trailing 12-month operating margin volatility
        margin_vol_10y: 10-year average margin volatility (overrides sector default if provided)
        supplier_concentration: Supply chain concentration [0.0, 1.0]
        geopolitical_stress_factor: Geopolitical stress amplifier

    Returns:
        Effective shock probability in [0.0, 1.0]
    """
    stats = get_sector_shock_stats(sector)
    p_base = stats.p_base

    ref_vol = margin_vol_10y if margin_vol_10y is not None else stats.margin_vol_10y
    if ref_vol <= 0:
        ref_vol = 0.05

    vol_ratio = current_margin_vol / ref_vol
    vol_ratio = max(0.5, min(3.0, vol_ratio))

    p_shock = p_base * vol_ratio

    concentration_boost = 0.0
    if supplier_concentration > 0.70:
        concentration_boost = (supplier_concentration - 0.70) * 2.0

    geo_amplifier = 1.0 + geopolitical_stress_factor

    p_effective = min(1.0, p_shock * (1.0 + concentration_boost) * geo_amplifier)

    return p_effective
