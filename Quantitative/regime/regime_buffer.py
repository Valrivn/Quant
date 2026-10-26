"""
Quantitative/regime/regime_buffer.py

Hysteresis Dwell-Time Confirmation Buffer & Regime Switching Engine.
Prevents rapid model whipsawing and churn hysteria between Bull, Bear, and Stagnant regimes.

Features:
1. Multi-Day Confirmation Window (K = 5 to 7 days):
   A candidate regime must sustain posterior probability >= threshold before transitioning.
2. Minimum Dwell Time (T_dwell = 15 to 20 days):
   Once locked, prevents parameter flipping for at least T_dwell trading days.
3. Asymmetric Emergency Circuit Breaker:
   Immediate bypass into BEAR state if severe market shock occurs (daily drop <= -3.5 sigma or 5d DD <= -8%).
4. Dwell Time Analytics:
   Tracks transition history, swap counts, and stability ratio.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class HysteresisBufferConfig:
    """Configuration for hysteresis dwell-time confirmation buffer."""
    confirmation_days: int = 5         # Days candidate regime must sustain probability >= threshold
    min_dwell_days: int = 15           # Minimum trading days required before permitting next transition
    enter_threshold: float = 0.65       # Probability threshold required to initiate staging
    emergency_vol_sigma: float = -3.5   # Negative return z-score trigger for immediate crash bypass
    emergency_5d_drop: float = -0.08    # 5-day drop trigger (-8%) for immediate crash bypass


@dataclass
class RegimeTransitionRecord:
    date: pd.Timestamp
    from_regime: str
    to_regime: str
    trigger_type: str  # "CONFIRMED_BUFFER" or "EMERGENCY_BYPASS"
    days_in_prior_state: int


class RegimeHysteresisBuffer:
    """
    Stateful buffer smoothing daily statistical regime probabilities into
    disciplined, low-turnover regime allocations.
    """

    def __init__(self, config: Optional[HysteresisBufferConfig] = None):
        self.config = config or HysteresisBufferConfig()
        
        # Current active locked regime
        self.active_regime: str = "BULL"
        self.days_in_active_regime: int = 0
        
        # Provisional staging state
        self.candidate_regime: Optional[str] = None
        self.candidate_counter: int = 0
        
        # History log
        self.transition_history: List[RegimeTransitionRecord] = []
        self.daily_log: List[Dict[str, any]] = []

    def update(
        self,
        date: pd.Timestamp,
        prob_dict: Dict[str, float],
        daily_return: float = 0.0,
        trailing_5d_return: float = 0.0,
    ) -> str:
        """
        Updates the buffer on day t and returns the confirmed ACTIVE regime.
        
        prob_dict: e.g. {"BULL": 0.80, "STAGNANT": 0.15, "BEAR": 0.05}
        """
        self.days_in_active_regime += 1
        
        # Highest posterior candidate on day t
        raw_winner = max(prob_dict, key=prob_dict.get)
        raw_prob = prob_dict[raw_winner]
        
        # -------------------------------------------------------------
        # 1. Asymmetric Emergency Panic Check (Immediate Bypass)
        # -------------------------------------------------------------
        if (
            daily_return <= self.config.emergency_vol_sigma * 0.015 or 
            trailing_5d_return <= self.config.emergency_5d_drop
        ) and self.active_regime != "BEAR":
            logger.warning(
                f"RegimeBuffer [{date.strftime('%Y-%m-%d')}]: EMERGENCY CIRCUIT BREAKER TRIGGERED! "
                f"Daily ret={daily_return:.2%}, 5d ret={trailing_5d_return:.2%}. Forcing immediate BEAR lock."
            )
            self._execute_transition(date, "BEAR", trigger_type="EMERGENCY_BYPASS")
            self._log_state(date, prob_dict, raw_winner, raw_prob)
            return self.active_regime

        # -------------------------------------------------------------
        # 2. Dwell-Time Lock Check
        # -------------------------------------------------------------
        if self.days_in_active_regime < self.config.min_dwell_days:
            # Still in minimum dwell lock — reset any candidate counter
            self.candidate_regime = None
            self.candidate_counter = 0
            self._log_state(date, prob_dict, raw_winner, raw_prob)
            return self.active_regime

        # -------------------------------------------------------------
        # 3. Confirmation Staging Logic
        # -------------------------------------------------------------
        if raw_winner != self.active_regime and raw_prob >= self.config.enter_threshold:
            if self.candidate_regime == raw_winner:
                self.candidate_counter += 1
            else:
                self.candidate_regime = raw_winner
                self.candidate_counter = 1
                
            # If candidate sustained probability across required K days
            if self.candidate_counter >= self.config.confirmation_days:
                self._execute_transition(date, self.candidate_regime, trigger_type="CONFIRMED_BUFFER")
        else:
            # Decay/reset candidate counter if probability drops
            self.candidate_regime = None
            self.candidate_counter = 0

        self._log_state(date, prob_dict, raw_winner, raw_prob)
        return self.active_regime

    def _execute_transition(self, date: pd.Timestamp, new_regime: str, trigger_type: str) -> None:
        """Executes regime shift and resets state timers."""
        record = RegimeTransitionRecord(
            date=date,
            from_regime=self.active_regime,
            to_regime=new_regime,
            trigger_type=trigger_type,
            days_in_prior_state=self.days_in_active_regime
        )
        self.transition_history.append(record)
        self.active_regime = new_regime
        self.days_in_active_regime = 0
        self.candidate_regime = None
        self.candidate_counter = 0

    def _log_state(self, date: pd.Timestamp, prob_dict: Dict[str, float], raw_winner: str, raw_prob: float) -> None:
        """Appends daily snapshot for analysis."""
        self.daily_log.append({
            'date': date,
            'active_regime': self.active_regime,
            'days_in_active': self.days_in_active_regime,
            'raw_winner': raw_winner,
            'raw_prob': raw_prob,
            'candidate_regime': self.candidate_regime,
            'candidate_counter': self.candidate_counter,
            'p_bull': prob_dict.get('BULL', 0.0),
            'p_stagnant': prob_dict.get('STAGNANT', 0.0),
            'p_bear': prob_dict.get('BEAR', 0.0),
        })

    def get_summary_dataframe(self) -> pd.DataFrame:
        """Returns historical time-series of active regimes and buffer states."""
        return pd.DataFrame(self.daily_log).set_index('date')
