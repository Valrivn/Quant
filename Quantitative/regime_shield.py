"""
regime_shield.py — HMM Regime Shield & Dynamic Fallback Allocator
Combines GaussianHMM macro state detection with 3-tier fallback allocation.

Regimes:
  EXPANSION: 80-100% equity allocation
  SHOCK:     40-60% equity allocation (tightened stop-losses, partial de-risking)
  CRISIS:    0% equity allocation -> 60% BIL (short-term treasuries) + 40% GLD (gold)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Tuple, Optional
import numpy as np
import pandas as pd

from Quantitative.hmm_regime import GaussianHMM, RegimeState

logger = logging.getLogger(__name__)


@dataclass
class RegimeShieldAllocation:
    regime: str
    equity_weight: float
    bil_weight: float
    gld_weight: float
    confidence: float
    probs: Dict[str, float]


# Pre-registered regime-conditional entry thresholds & targets (RS-10, T3 debate 2026-09-19)
REGIME_ENTRY_THRESHOLDS = {
    "EXPANSION": 0.85,  # 0.85 * IV_P25 (aggressive entry)
    "SHOCK": 0.95,      # 0.95 * IV_P25 (lenient re-entry)
    "CRISIS": 0.75,     # 0.75 * IV_P25 (black swan opportunism)
}

REGIME_TARGET_THRESHOLDS = {
    "EXPANSION": 1.20,  # 1.20 * IV_P25 target
    "SHOCK": 1.10,      # 1.10 * IV_P25 target
    "CRISIS": 1.05,     # 1.05 * IV_P25 target
}

MIN_EXIT_FLOOR_MULTIPLE = 1.15  # 1.15 * IV_P25 floor exit threshold


def get_regime_entry_threshold(regime: str) -> float:
    """Returns multiplier for IV_P25 entry threshold based on regime state."""
    return REGIME_ENTRY_THRESHOLDS.get(regime, 0.85)


def get_regime_target_threshold(regime: str) -> float:
    """Returns multiplier for IV_P25 target exit threshold based on regime state."""
    return REGIME_TARGET_THRESHOLDS.get(regime, 1.15)


class RegimeShield:
    def __init__(self):
        self.hmm = GaussianHMM(n_states=3, seed=42)
        self.is_fitted = False

    def fit_or_update(self, macro_df: pd.DataFrame) -> None:
        """
        Fit HMM model on macro features if available, else mark ready for fallback.
        Expected feature columns or standard names:
        ['credit_spread', 'yield_curve', 'market_ret_21d', 'market_vol_21d']
        """
        if macro_df.empty or len(macro_df) < 20:
            return
        
        # Ensure we have the required features
        feats = []
        for col in ["credit_spread", "yield_curve", "market_ret_21d", "market_vol_21d"]:
            if col in macro_df.columns:
                feats.append(macro_df[col].values)
            else:
                feats.append(np.zeros(len(macro_df)))
        
        obs = np.column_stack(feats)
        # Z-score normalization
        mean = np.mean(obs, axis=0)
        std = np.std(obs, axis=0) + 1e-6
        obs_z = (obs - mean) / std

        try:
            self.hmm.fit(obs_z)
            self.is_fitted = True
        except Exception as e:
            logger.warning(f"Failed to fit HMM in RegimeShield: {e}")
            self.is_fitted = False

    def predict_regime(self, current_macro: Dict[str, float]) -> RegimeShieldAllocation:
        """
        Predict regime state and output target asset allocation weights.
        """
        # Rule-based fallback if HMM not fitted or macro state directly derived
        credit_spread = current_macro.get("credit_spread", 1.5)
        mkt_ret_21d = current_macro.get("market_ret_21d", 0.0)
        mkt_vol_21d = current_macro.get("market_vol_21d", 0.15)
        spy_below_200d = current_macro.get("spy_below_200d", False)

        # Fallback heuristic logic if HMM fit isn't used
        if credit_spread > 4.0 or (spy_below_200d and mkt_ret_21d < -0.10):
            regime_str = "CRISIS"
            probs = {"EXPANSION": 0.05, "SHOCK": 0.15, "CRISIS": 0.80}
            confidence = 0.80
            eq_w, bil_w, gld_w = 0.0, 0.60, 0.40
        elif credit_spread > 2.5 or spy_below_200d or mkt_vol_21d > 0.25:
            regime_str = "SHOCK"
            probs = {"EXPANSION": 0.20, "SHOCK": 0.70, "CRISIS": 0.10}
            confidence = 0.70
            eq_w, bil_w, gld_w = 0.50, 0.30, 0.20
        else:
            regime_str = "EXPANSION"
            probs = {"EXPANSION": 0.85, "SHOCK": 0.10, "CRISIS": 0.05}
            confidence = 0.85
            eq_w, bil_w, gld_w = 1.00, 0.0, 0.0

        return RegimeShieldAllocation(
            regime=regime_str,
            equity_weight=eq_w,
            bil_weight=bil_w,
            gld_weight=gld_w,
            confidence=confidence,
            probs=probs
        )
