"""
research/two_state_regime/cooldown_buffer.py

Hysteria Cooldown Transition Buffer for 2-State (Bull/Bear) Regime Framework.
Decoupled research module.

Mechanics:
- States: "BULL", "BEAR", and transitional "COOLDOWN" (Hysteria Buffer).
- Cooldown Length: T_cooldown = 25 to 35 trading days (empirically derived from stagnant periods).
- State Transition Rules:
  * BULL -> COOLDOWN_TO_BEAR: Triggers when P(BEAR) >= enter_threshold (0.60) for confirmation_days (3d).
    During COOLDOWN, strategy remains in neutral/protective posture.
    If P(BEAR) holds across T_cooldown trading days -> locks into BEAR.
    If P(BEAR) falls back -> cancels cooldown and reverts to BULL (false alarm averted).
  * BEAR -> COOLDOWN_TO_BULL: Triggers when P(BULL) >= 0.60 for 5d.
    Requires full T_cooldown days of sustained bull probability before locking into BULL.
  * Asymmetric Emergency Bypass: Market crash (return <= -3.5 sigma or 5d drop <= -8%)
    bypasses cooldown immediately into BEAR.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class CooldownBufferConfig:
    cooldown_days: int = 25              # Empirically derived duration (~5-6 weeks of stagnant consolidation)
    confirmation_trigger_days: int = 3   # Days of signal agreement to enter cooldown
    enter_threshold: float = 0.60        # Probability threshold to initiate transition
    min_dwell_days: int = 15             # Minimum days locked in state before permitting new transition
    emergency_vol_sigma: float = -3.5    # Crash bypass threshold
    emergency_5d_drop: float = -0.08     # 5-day drop crash bypass threshold


@dataclass
class CooldownTransitionRecord:
    date: pd.Timestamp
    from_state: str
    to_state: str
    trigger_type: str
    dwell_days: int


class TwoStateCooldownBuffer:
    """
    Manages 2-State market regime classification with an empirical hysteria cooldown buffer.
    """

    def __init__(self, config: Optional[CooldownBufferConfig] = None):
        self.config = config or CooldownBufferConfig()

        self.active_regime: str = "BULL"          # "BULL" or "BEAR"
        self.days_in_active: int = 0

        # Cooldown state: None, "COOLDOWN_TO_BEAR", "COOLDOWN_TO_BULL"
        self.in_cooldown: bool = False
        self.cooldown_target: Optional[str] = None
        self.cooldown_timer: int = 0

        # Candidate trigger counter
        self.candidate_counter: int = 0
        self.candidate_target: Optional[str] = None

        self.transition_history: List[CooldownTransitionRecord] = []
        self.daily_log: List[Dict[str, any]] = []

    def update(
        self,
        date: pd.Timestamp,
        probs: Dict[str, float],
        daily_return: float = 0.0,
        trailing_5d_return: float = 0.0,
    ) -> str:
        """
        Processes day t observables and returns the effective regime classification:
        "BULL", "BEAR", or "COOLDOWN" (Hysteria Corridor).
        """
        self.days_in_active += 1

        p_bull = probs.get("BULL", 0.5)
        p_bear = probs.get("BEAR", 0.5)
        raw_winner = "BEAR" if p_bear >= p_bull else "BULL"

        # -------------------------------------------------------------
        # 1. Asymmetric Crash Emergency Circuit Breaker
        # -------------------------------------------------------------
        if (
            daily_return <= self.config.emergency_vol_sigma * 0.015 or
            trailing_5d_return <= self.config.emergency_5d_drop
        ) and self.active_regime != "BEAR":
            self._execute_immediate_lock(date, "BEAR", "EMERGENCY_BYPASS")
            self._log_state(date, probs, raw_winner)
            return self.active_regime

        # -------------------------------------------------------------
        # 2. Inside Active Cooldown Phase
        # -------------------------------------------------------------
        if self.in_cooldown:
            self.cooldown_timer += 1

            # Check if signal remains supportive of target
            target_prob = p_bear if self.cooldown_target == "BEAR" else p_bull
            opp_prob = p_bull if self.cooldown_target == "BEAR" else p_bear

            # Cancel cooldown if signal completely collapsed (false alarm avoided)
            if opp_prob >= 0.65:
                logger.info(f"[{date.strftime('%Y-%m-%d')}] Cooldown to {self.cooldown_target} cancelled; reverted to {self.active_regime}.")
                self.in_cooldown = False
                self.cooldown_target = None
                self.cooldown_timer = 0
                self.candidate_counter = 0
                self._log_state(date, probs, raw_winner)
                return self.active_regime

            # If sustained throughout the entire empirical cooldown period -> LOCK IN
            if self.cooldown_timer >= self.config.cooldown_days:
                self._execute_immediate_lock(date, self.cooldown_target, "COOLDOWN_CONFIRMED")
                self._log_state(date, probs, raw_winner)
                return self.active_regime

            self._log_state(date, probs, raw_winner)
            return "COOLDOWN"

        # -------------------------------------------------------------
        # 3. Minimum Dwell Time in Locked State
        # -------------------------------------------------------------
        if self.days_in_active < self.config.min_dwell_days:
            self.candidate_counter = 0
            self._log_state(date, probs, raw_winner)
            return self.active_regime

        # -------------------------------------------------------------
        # 4. Evaluating Triggers to Enter Cooldown
        # -------------------------------------------------------------
        desired_transition = "BEAR" if self.active_regime == "BULL" else "BULL"
        signal_strength = p_bear if desired_transition == "BEAR" else p_bull

        if signal_strength >= self.config.enter_threshold:
            if self.candidate_target == desired_transition:
                self.candidate_counter += 1
            else:
                self.candidate_target = desired_transition
                self.candidate_counter = 1

            # If signal holds across confirmation window -> Enter Cooldown Corridor
            if self.candidate_counter >= self.config.confirmation_trigger_days:
                self.in_cooldown = True
                self.cooldown_target = desired_transition
                self.cooldown_timer = 0
                self.candidate_counter = 0
                self._log_state(date, probs, raw_winner)
                return "COOLDOWN"
        else:
            self.candidate_counter = 0
            self.candidate_target = None

        self._log_state(date, probs, raw_winner)
        return self.active_regime

    def _execute_immediate_lock(self, date: pd.Timestamp, new_state: str, trigger_type: str) -> None:
        rec = CooldownTransitionRecord(
            date=date,
            from_state=self.active_regime,
            to_state=new_state,
            trigger_type=trigger_type,
            dwell_days=self.days_in_active,
        )
        self.transition_history.append(rec)
        self.active_regime = new_state
        self.days_in_active = 0
        self.in_cooldown = False
        self.cooldown_target = None
        self.cooldown_timer = 0
        self.candidate_counter = 0

    def _log_state(self, date: pd.Timestamp, probs: Dict[str, float], raw_winner: str) -> None:
        effective_output = "COOLDOWN" if self.in_cooldown else self.active_regime
        self.daily_log.append({
            "date": date,
            "effective_regime": effective_output,
            "active_locked": self.active_regime,
            "in_cooldown": self.in_cooldown,
            "cooldown_target": self.cooldown_target,
            "cooldown_timer": self.cooldown_timer,
            "p_bull": probs.get("BULL", 0.0),
            "p_bear": probs.get("BEAR", 0.0),
            "raw_winner": raw_winner,
        })

    def get_summary_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.daily_log).set_index("date")
