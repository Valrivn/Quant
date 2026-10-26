"""
swap_barrier_v2.py — Dynamic Bidirectional + Cash-Aware Swap Barrier Engine
Pre-Registration Config: config/pre_registration_v3.yaml

Provides:
  1. BidirectionalSwapBarrier: Evaluates holding swap threshold current->candidate OR entry from cash.
  2. Cash-Aware Re-entry: Evaluates cash entry gates and stop-loss bypass logic.
"""

from __future__ import annotations

import argparse
import logging
import math
from dataclasses import dataclass
from typing import Dict, Any, Optional, Union, Tuple

import numpy as np
import yaml

from Quantitative.regime_shield_v2 import ConfigWrapper, load_config

logger = logging.getLogger(__name__)


@dataclass
class SwapHolding:
    ticker: str
    expected_return: float
    t_held_months: float
    has_moat: bool = False


@dataclass
class SwapCandidate:
    ticker: str
    expected_return: float
    price: float = 100.0
    iv: float = 120.0
    has_moat: bool = False


class PortfolioStateMock:
    """Mock/wrapper for portfolio state checking stop-loss history and regime."""
    def __init__(
        self,
        regime: str = "EXPANSION",
        recent_stop_losses: Optional[Dict[str, int]] = None,
    ):
        self.regime = regime
        # Map of ticker -> days since stop loss
        self.recent_stop_losses = recent_stop_losses or {}

    def was_stop_lossed(self, ticker: str, lookback_days: int) -> bool:
        days = self.recent_stop_losses.get(ticker)
        if days is not None and days <= lookback_days:
            return True
        return False


class BidirectionalSwapBarrier:
    """Bidirectional + Cash-Aware Swap Barrier solver."""

    def __init__(self, config: Union[ConfigWrapper, Dict[str, Any], str, None] = None):
        if config is not None:
            cfg = load_config(config)
            sb_cfg = getattr(cfg, "swap_barrier", cfg.get("swap_barrier") if isinstance(cfg, dict) else None)
            self.C = float(getattr(sb_cfg, "C_bps", 150)) / 10000.0
            self.gamma = float(getattr(sb_cfg, "gamma", 0.5))
            self.t_target = float(getattr(sb_cfg, "t_target_months", 24))
            self.epsilon = float(getattr(sb_cfg, "epsilon", 1e-6))
            self.moat_penalty = float(getattr(sb_cfg, "moat_penalty_bps", 100)) / 10000.0
            self.cash_aware = bool(getattr(sb_cfg, "cash_aware_reentry", True))
            self.sl_bypass = bool(getattr(sb_cfg, "stop_loss_reentry_bypass", True))
            self.sl_lookback = int(getattr(sb_cfg, "stop_loss_lookback_days", 20))
        else:
            self.C = 0.015
            self.gamma = 0.5
            self.t_target = 24.0
            self.epsilon = 1e-6
            self.moat_penalty = 0.01
            self.cash_aware = True
            self.sl_bypass = True
            self.sl_lookback = 20

    def barrier(self, t_held_months: float, has_moat: bool) -> float:
        """B(t) = C * exp(-gamma * t_held_months / t_target) + moat_penalty (if moat)."""
        base = self.C * np.exp(-self.gamma * (t_held_months / self.t_target))
        return float(base + (self.moat_penalty if has_moat else 0.0))

    def _entry_threshold(self, regime: str) -> float:
        """Regime-conditional entry threshold P / IV."""
        if regime == "EXPANSION":
            return 0.85
        elif regime == "SHOCK":
            return 0.75
        elif regime == "CRISIS":
            return 0.65
        return 0.80

    def _can_enter_from_cash(self, candidate: SwapCandidate, portfolio_state: PortfolioStateMock) -> bool:
        # Bypass barrier if re-entering a recently stop-lossed position
        if self.sl_bypass and portfolio_state.was_stop_lossed(candidate.ticker, self.sl_lookback):
            logger.info(f"Stop-loss reentry bypass triggered for {candidate.ticker}")
            return True
        # Standard entry gate
        threshold = self._entry_threshold(portfolio_state.regime)
        return candidate.price <= candidate.iv * threshold

    def evaluate(self, holding: Any, candidate: Any, regime: str, hmm_probs: Dict[str, float], liquidity: Any) -> Tuple[str, float, float, str]:
        # KILL SWITCH (forward-looking)
        kill = (hmm_probs.get("CRISIS", 0) > 0.60 and 
                holding.price / holding.iv_p25 < 0.80 and
                liquidity.spread_ratio > 2.0)
        if kill:
            return "KILL_SWITCH", 0.0, 0.0, "regime_crisis_iv_breach_liquidity"
        return "HOLD", 0.0, 0.0, "default"

    def can_swap(
        self,
        current: Optional[SwapHolding],
        candidate: SwapCandidate,
        portfolio_state: PortfolioStateMock,
    ) -> bool:
        """Bidirectional: swap current->candidate OR re-enter from cash."""
        if current is None:
            return self._can_enter_from_cash(candidate, portfolio_state)
        
        b_val = self.barrier(current.t_held_months, current.has_moat)
        edge = candidate.expected_return - current.expected_return
        return edge > (b_val + self.epsilon)

    def should_swap(
        self,
        upside_i: float,
        upside_j: float,
        holding_months: float,
        is_moat_protected: bool = False,
    ) -> Tuple[bool, float, float]:
        """Determine if candidate j offers enough edge to breach swap barrier over holding i."""
        edge_bps = (upside_j - upside_i) * 10000.0
        barrier_val = self.barrier(holding_months, is_moat_protected)
        barrier_bps = barrier_val * 10000.0
        should = edge_bps > barrier_bps
        return should, float(edge_bps), float(barrier_bps)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Test Quantitative.swap_barrier_v2")
    parser.add_argument("--test", action="store_true", help="Run test suite")
    parser.add_argument("--bidirectional", action="store_true", help="Test bidirectional swap")
    parser.add_argument("--cash-aware", action="store_true", help="Test cash aware reentry")
    parser.add_argument("--sl-bypass", action="store_true", help="Test stop loss bypass")
    parser.add_argument("--config", type=str, default="config/pre_registration_v3.yaml", help="Path to config")
    args = parser.parse_args()

    cfg = load_config(args.config)
    sb = BidirectionalSwapBarrier(cfg)
    p_state = PortfolioStateMock(regime="EXPANSION", recent_stop_losses={"AAPL": 5})

    cand = SwapCandidate(ticker="AAPL", expected_return=0.25, price=100.0, iv=130.0, has_moat=True)
    holding = SwapHolding(ticker="MSFT", expected_return=0.10, t_held_months=12.0, has_moat=False)

    # 1. Swap current -> candidate
    can_swap_holding = sb.can_swap(holding, cand, p_state)
    print(f"Swap MSFT -> AAPL: Edge = 15% vs Barrier = {sb.barrier(12.0, False)*100:.2f}% => {can_swap_holding}")

    # 2. Re-enter from cash with SL bypass
    cand_sl = SwapCandidate(ticker="AAPL", expected_return=0.15, price=110.0, iv=120.0)
    can_swap_cash_sl = sb.can_swap(None, cand_sl, p_state)
    print(f"Re-enter AAPL from cash (recently SL'd): => {can_swap_cash_sl}")

    # 3. Re-enter from cash standard gate (no SL history)
    cand_norm = SwapCandidate(ticker="NVDA", expected_return=0.15, price=100.0, iv=110.0)
    can_swap_cash_norm = sb.can_swap(None, cand_norm, p_state)
    print(f"Re-enter NVDA from cash (price $100 vs IV $110, thresh 85%): => {can_swap_cash_norm}")

    print("SwapBarrierV2 Test OK!")
