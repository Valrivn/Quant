"""
Quantitative/tca_model.py — Almgren-Chriss TCA with Regime Multipliers & Concurrent Stress

TASK 2 Requirement:
- Base: Almgren-Chriss permanent + temporary impact
- Regime multipliers: EXP 1.0x, SHOCK 1.25x (or 2.0x per pre-reg v3), CRISIS 1.5x (or 3.0x per pre-reg v3)
- Budget: 150 bps per round-trip (CEO frozen)
- Concurrent exit stress test: Portfolio-wide correlation clustering
"""

from __future__ import annotations

import numpy as np
from typing import Dict, List, Any, Optional

# CEO / Pre-registration frozen parameters
BUDGET_BPS: float = 150.0
DEFAULT_SIGMA_DAILY: float = 0.015
TWAP_DAYS: float = 3.0
MAX_DAILY_PCT: float = 0.05
MIN_ADV_USD: float = 5_000_000.0

# Regime multipliers for TCA (EXP 1.0x, SHOCK 1.25x/2.0x, CRISIS 1.5x/3.0x)
REGIME_MULTIPLIERS: Dict[str, float] = {
    "EXP": 1.0,
    "EXPANSION": 1.0,
    "SHOCK": 1.25,
    "CRISIS": 1.5,
}

# RS-27 piecewise / Almgren-Chriss parameters
AC_GAMMA: float = 0.1  # Permanent impact coefficient
AC_ETA: float = 0.1    # Temporary impact coefficient


def calculate_almgren_chriss_impact(
    trade_size_usd: float,
    adv_usd: float,
    sigma_daily: float = DEFAULT_SIGMA_DAILY,
    regime: str = "EXPANSION",
    twap_days: float = TWAP_DAYS,
) -> float:
    """
    Computes round-trip TCA cost in bps using Almgren-Chriss framework with regime multipliers.
    
    Round-trip cost (bps) = 2 * (Permanent Impact + Temporary Impact + Timing Slippage)
    """
    adv = max(float(adv_usd), 1e-9)
    size = max(float(trade_size_usd), 0.0)
    
    if size == 0.0:
        return 0.0

    # Participation rate over TWAP horizon
    daily_size = size / max(float(twap_days), 1.0)
    participation = daily_size / adv

    regime_key = str(regime).upper()
    mult = REGIME_MULTIPLIERS.get(regime_key, 1.0)

    # Base volatility impact in bps
    base_bps = float(sigma_daily) * 10_000.0

    # Almgren-Chriss permanent impact ~ gamma * (size / ADV) * sigma
    perm_impact_bps = AC_GAMMA * participation * base_bps * mult

    # Almgren-Chriss temporary impact ~ eta * (participation)^0.5 * sigma
    temp_impact_bps = AC_ETA * np.sqrt(max(participation, 1e-6)) * base_bps * mult

    # ADV crush penalty if participation > 10%
    if participation > 0.10:
        crush_factor = 1.0 + 2.0 * (participation - 0.10)
        temp_impact_bps *= crush_factor

    # Timing / opportunity cost (linear in participation, scaled by regime)
    timing_slippage_bps = 2.0 * participation * mult * (3.0 / max(twap_days, 0.25)) + 0.5

    # One-side total bps
    one_side_bps = perm_impact_bps + temp_impact_bps + timing_slippage_bps

    # Round-trip cost
    round_trip_bps = 2.0 * one_side_bps
    return float(round_trip_bps)


def stress_test_concurrent_exits(
    notionals: np.ndarray,
    advs: np.ndarray,
    corr_matrix: np.ndarray,
    regime: str = "EXPANSION",
    adv_stress_mult: float = 1.0,
    twap_days: float = TWAP_DAYS,
) -> Dict[str, Any]:
    """
    Concurrent exit stress test accounting for portfolio-wide correlation clustering.
    Higher correlation clusters force liquidity bottlenecking.
    """
    notionals = np.asarray(notionals, dtype=np.float64)
    advs = np.asarray(advs, dtype=np.float64) * max(float(adv_stress_mult), 1e-9)
    n = len(notionals)

    if n == 0:
        return {
            "round_trip_bps": 0.0,
            "budget_bps": BUDGET_BPS,
            "over_budget": False,
            "n_names": 0,
        }

    # Calculate correlation penalty for clustered liquidation
    avg_corr = float(np.mean(corr_matrix)) if n > 1 else 0.0
    cluster_penalty = 1.0 + max(0.0, avg_corr - 0.5) * 0.5  # Up to 25% inflation for high correlation

    per_name_bps = []
    for i in range(n):
        effective_adv = advs[i] / cluster_penalty
        cost = calculate_almgren_chriss_impact(
            trade_size_usd=notionals[i],
            adv_usd=effective_adv,
            regime=regime,
            twap_days=twap_days,
        )
        per_name_bps.append(cost)

    portfolio_avg_cost = float(np.mean(per_name_bps))
    
    return {
        "engine": "Quantitative.tca_model.AlmgrenChriss",
        "regime": regime,
        "n_names": int(n),
        "gross_notional_usd": float(notionals.sum()),
        "average_correlation": round(avg_corr, 4),
        "cluster_penalty": round(cluster_penalty, 4),
        "round_trip_bps": round(portfolio_avg_cost, 4),
        "budget_bps": BUDGET_BPS,
        "over_budget": bool(portfolio_avg_cost > BUDGET_BPS),
        "per_name_bps": [round(x, 4) for x in per_name_bps],
    }


if __name__ == "__main__":
    # Quick sanity check
    print("Testing Quantitative.tca_model...")
    cost = calculate_almgren_chriss_impact(1_000_000, 50_000_000, regime="EXP")
    print(f"Sample round-trip cost (1M in 50M ADV, EXP): {cost:.2f} bps")
    assert cost < BUDGET_BPS, f"Cost {cost} exceeded budget {BUDGET_BPS}"
    print("Quantitative/tca_model.py check passed.")
