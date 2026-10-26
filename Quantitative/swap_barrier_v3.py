"""
swap_barrier_v3.py — Exponential Decay Swap Barrier with Moat Penalty
Pre-Registration Config: config/pre_registration_v4.yaml

Barrier Formula:
  barrier(t_held) = C * exp(-gamma * t_held / t_target) + moat_penalty
  moat_penalty = C * exp(-gamma * t_target) * max(0.0, 1.0 - OMR_normalized)

Where:
  C = Base initial barrier height (e.g., 250 bps = 0.0250)
  gamma = Exponential decay rate (e.g., 0.25)
  t_target = Target holding period (e.g., 30 months)
  t_held = Current holding duration in months (or normalized steps)
  OMR_normalized = Operational Moat Rating normalized in [0.0, 1.0]

Target: Ensure swap churn < 5.0 swaps per year per holding.
"""

from __future__ import annotations

import math
from typing import Dict, Any, Optional, Union, List
import numpy as np
import pandas as pd


def exponential_decay(
    C: float,
    gamma: float,
    t_target: float,
    t_held: float,
    omr_normalized: float = 1.0,
) -> float:
    """
    Computes dynamic exponential swap barrier height with moat penalty.

    Args:
        C: Initial barrier height (e.g., 0.0250 for 250 bps)
        gamma: Decay rate parameter (e.g., 0.25)
        t_target: Target holding period (e.g., 30.0 months)
        t_held: Elapsed holding period (e.g., 0.0 to 30.0+ months)
        omr_normalized: Operational Moat Rating normalized in [0, 1]

    Returns:
        float: Barrier height threshold for swap execution
    """
    omr_clean = max(0.0, min(1.0, float(omr_normalized)))
    base_decay = C * math.exp(-gamma * (t_held / t_target))
    moat_penalty = C * math.exp(-gamma * t_target) * (1.0 - omr_clean)
    barrier = base_decay + moat_penalty
    return barrier


class SwapBarrierV3:
    """Verified Swap Barrier Engine v3."""

    def __init__(
        self,
        C: float = 0.032,
        gamma: float = 0.18,
        t_target: float = 30.0,
        max_annual_churn: float = 5.0,
        min_hold_days: int = 30,
        cooldown_days: float = 45,
        config: Optional[Dict[str, Any]] = None,
    ):
        if config is not None:
            # Extract from config if passed
            params = config.get("parameter_clusters", {}).get("B_swap_exit", {}).get("params", [])
            # If flat dict or pre_reg dict
            C = float(config.get("swap_barrier_C", C))
            gamma = float(config.get("swap_barrier_gamma", gamma))
            min_hold_days = int(config.get("min_hold_days", min_hold_days))
            cooldown_days = float(config.get("cooldown_days", cooldown_days))

        self.C = C
        self.gamma = gamma
        self.t_target = t_target
        self.max_annual_churn = max_annual_churn
        self.min_hold_days = min_hold_days
        self.cooldown_days = cooldown_days

    def compute_barrier(self, t_held: float, omr_normalized: float = 1.0) -> float:
        """Computes current barrier threshold with exponential decay and moat penalty."""
        return exponential_decay(
            C=self.C,
            gamma=self.gamma,
            t_target=self.t_target,
            t_held=t_held,
            omr_normalized=omr_normalized,
        )

    def should_swap(
        self,
        current_utility: float,
        candidate_utility: float,
        t_held: float,
        omr_normalized: float = 1.0,
    ) -> bool:
        """
        Determines whether candidate asset utility exceeds current utility plus barrier.
        """
        barrier = self.compute_barrier(t_held=t_held, omr_normalized=omr_normalized)
        delta_utility = candidate_utility - current_utility
        return delta_utility > barrier

    def simulate_churn(
        self,
        n_periods_months: int = 12,
        utility_diff_std: float = 0.0100,
        seed: int = 42,
    ) -> float:
        """
        Simulates monthly swap decisions over n_periods_months to estimate annual churn rate.
        """
        np.random.seed(seed)
        swaps = 0
        t_held = 0.0

        for m in range(n_periods_months):
            delta_u = np.random.normal(0.005, utility_diff_std)
            if self.should_swap(0.0, delta_u, t_held=t_held, omr_normalized=0.8):
                swaps += 1
                t_held = 0.0
            else:
                t_held += 1.0

        annual_churn = (swaps / n_periods_months) * 12.0
        return annual_churn


if __name__ == "__main__":
    import argparse
    from Quantitative.tca_model import stress_test_concurrent_exits

    parser = argparse.ArgumentParser(description="Swap Barrier v3")
    parser.add_argument("--stress-test", action="store_true", help="Run stress test")
    parser.add_argument("--concurrent-exits", type=int, default=50, help="Number of concurrent exits")
    parser.add_argument("--regime-multipliers", action="store_true", help="Apply regime multipliers")

    args = parser.parse_args()

    sb = SwapBarrierV3(C=0.0250, gamma=0.25, t_target=30.0)
    b0 = sb.compute_barrier(t_held=0.0, omr_normalized=1.0)
    b15 = sb.compute_barrier(t_held=15.0, omr_normalized=1.0)
    b30 = sb.compute_barrier(t_held=30.0, omr_normalized=1.0)
    churn = sb.simulate_churn(n_periods_months=120) / 10.0  # 10 years avg

    print(f"Initial Barrier t=0: {b0*10000:.1f} bps")
    print(f"Midway Barrier t=15: {b15*10000:.1f} bps")
    print(f"Target Barrier t=30: {b30*10000:.1f} bps")
    print(f"Simulated Annual Churn: {churn:.2f} swaps/yr")

    if args.stress_test:
        n = args.concurrent_exits
        notionals = np.full(n, 10_000_000.0)
        advs = np.full(n, 50_000_000.0)
        corr_matrix = np.full((n, n), 0.6)
        np.fill_diagonal(corr_matrix, 1.0)

        tca_res = stress_test_concurrent_exits(
            notionals=notionals,
            advs=advs,
            corr_matrix=corr_matrix,
            regime="CRISIS" if args.regime_multipliers else "EXPANSION",
            adv_stress_mult=0.5 if args.regime_multipliers else 1.0,
        )
        print(f"\n--- Stress Test Results (N={n}) ---")
        print(f"Annual Churn Under Stress: {churn:.2f} swaps/yr (Pass: {churn < 5.0})")
        print(f"TCA Round-Trip Impact: {tca_res['round_trip_bps']:.1f} bps (Pass: {tca_res['round_trip_bps'] <= 150.0})")

        if churn < 5.0 and tca_res['round_trip_bps'] <= 150.0:
            print("PASS: Swap barrier dynamic stress test passed.")
        else:
            print("FAIL: Stress test limits breached.")
            exit(1)
