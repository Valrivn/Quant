"""
Unified Regime Detection Engine — BOCPD + IO-HMM + Micro-States
Pre-Registration Config: config/pre_registration_v5.yaml

Unified architecture combining:
- BOCPD (Seismic Shock Alarm): Instant structural break detection
- IO-HMM (Wind-Vane): Tier 1 macro states using continuous market velocity
- Micro-States (Economic Engines): Tier 2 specific threat identification

Output: Unified regime state with actionable pipeline directives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from Quantitative.bocpd import BOCPD, BOCPDConfig, build_bocpd
from Quantitative.io_hmm import IOHMM, IOHMMConfig, build_io_hmm
from Quantitative.micro_states import MicroStateClassifier, MicroStateConfig, build_micro_classifier
from Quantitative.hmm_regime import load_fred_cache, build_macro_features

logger = logging.getLogger(__name__)


@dataclass
class UnifiedRegimeConfig:
    """Unified regime detection configuration."""
    # BOCPD config
    bocpd_hazard: float = 0.01
    bocpd_threshold: float = 0.95
    
    # IO-HMM config
    io_hmm_states: Tuple[str, ...] = ("EXPANSION", "CHOPPY", "CRISIS")
    
    # Micro-state config
    micro_enabled: bool = True
    
    # Integration logic
    bocpd_override_weight: float = 0.8      # Weight for BOCPD alarm override
    io_hmm_weight: float = 1.0              # Weight for IO-HMM belief
    micro_weight: float = 1.0               # Weight for micro-state override
    
    # Decision thresholds
    crisis_threshold: float = 0.7           # P(CRISIS) threshold
    choppy_threshold: float = 0.4           # P(CHOPPY) threshold
    
    # Regime persistence
    min_dwell_days: int = 10                # Minimum days in regime
    max_transitions_per_year: int = 6       # Max transitions/year


@dataclass
class UnifiedRegimeState:
    """Complete unified regime state."""
    # Tier 1: Macro state
    macro_belief: np.ndarray                # p(s_t | data) for 3 macro states
    macro_map: int                          # MAP macro state
    macro_name: str                         # State name
    
    # Tier 2: Micro state
    micro_belief: Dict                      # p(micro | macro, data)
    micro_map: str                          # MAP micro state
    micro_name: str                         # Micro state name
    
    # BOCPD
    bocpd_alarm: bool
    bocpd_p_r0: float
    bocpd_run_length: int
    
    # IO-HMM
    io_hmm_belief: np.ndarray
    io_hmm_map: int
    
    # Micro-state
    micro_scores: Dict
    micro_map: str
    micro_name: str
    
    # Final unified regime
    regime_name: str                        # Full regime descriptor
    confidence: float                       # Overall confidence
    
    # Pipeline actions
    actions: Dict                           # Pipeline directives


class UnifiedRegimeEngine:
    """
    Unified Regime Detection Engine: BOCPD + IO-HMM + Micro-States.
    
    Architecture:
    1. BOCPD (Seismic Alarm): Detects r_t = 0 → forces CRISIS
    2. IO-HMM (Wind-Vane): Tier 1 macro states using market velocity
    3. Micro-States: Tier 2 economic threat identification
    
    Decision Logic:
    - If BOCPD alarm: FORCE CRISIS (circuit breaker)
    - Else: Use IO-HMM belief for Tier 1
    - Then: Use Micro-states for Tier 2 within macro context
    """
    
    def __init__(self, config: Optional["UnifiedRegimeConfig"] = None):
        from dataclasses import dataclass
        from typing import Optional
        
        @dataclass
        class _Config:
            bocpd_hazard: float = 0.01
            bocpd_threshold: float = 0.95
            io_hmm_states: Tuple[str, ...] = ("EXPANSION", "CHOPPY", "CRISIS")
            micro_enabled: bool = True
            bocpd_override_weight: float = 0.8
            io_hmm_weight: float = 1.0
            micro_weight: float = 1.0
            crisis_threshold: float = 0.7
            choppy_threshold: float = 0.4
            min_dwell_days: int = 10
            max_transitions_per_year: int = 6
        
        self.config = config or _Config()
        self.bocpd: Optional[BOCPD] = None
        self.io_hmm: Optional[IOHMM] = None
        self.micro: Optional[MicroStateClassifier] = None
        
        # State
        self._initialized: bool = False
        self._last_macro_state: int = -1
        self._dwell_counter: int = 0
        self._transitions_this_year: int = 0
        self._year_start: int = 0
        
        # Components (initialized on demand)
        self._bocpd: Optional[BOCPD] = None
        self._io_hmm: Optional[IOHMM] = None
        self._micro: Optional[MicroStateClassifier] = None
        
    def initialize(self) -> None:
        """Initialize all components."""
        # Components are built from the (module-level imported) config classes.
        # NOTE: these names must not be re-imported inside this function, or
        # Python scopes them as function-locals and the first use below raises
        # UnboundLocalError.
        self._bocpd = build_bocpd(
            BOCPDConfig(
                hazard_rate=self.config.bocpd_hazard,
                threshold=self.config.bocpd_threshold,
            )
        )

        # IO-HMM
        self._io_hmm = build_io_hmm(
            IOHMMConfig(
                n_states=len(self.config.io_hmm_states),
                state_names=tuple(self.config.io_hmm_states),
                input_features=(
                    "credit_spread_velocity",
                    "yield_curve_momentum",
                    "realized_vol_21d",
                    "credit_spread_level",
                    "yield_curve_slope",
                    "vix_level",
                ),
            )
        )

        # Micro-states
        self._micro = build_micro_classifier(MicroStateConfig())
        
        self._initialized = True
        logger.info("Unified Regime Engine initialized")
    
    def _load_market_data(self) -> Dict[str, pd.Series]:
        """Load all market data from FRED cache."""
        return {
            "credit_spread": load_fred_cache("BAA10Y") / 100.0,
            "yield_curve": (load_fred_cache("DGS10") - load_fred_cache("DGS2")) / 100.0,
            "realized_vol": None,  # Will be computed from market
            "vix": load_fred_cache("VIXCLS"),
            "stl_fsi": load_fred_cache("STLFSI2"),
            "hy_oas": load_fred_cache("BAMLH0A0HYM2") / 100.0,
        }
    
    def _compute_io_hmm_inputs(self, data: Optional[Dict[str, pd.Series]] = None) -> pd.DataFrame:
        """Compute IO-HMM input features from raw market data.

        ``data`` is accepted for call-site compatibility but is not consumed:
        the feature legs are rebuilt directly from the FRED cache. The value
        returned here is currently NOT used by :meth:`update` (the live path
        uses :meth:`_get_latest_io_inputs`), so this is a batch helper.
        """
        if data is None:
            data = self._load_market_data()
        credit_spread = load_fred_cache("BAA10Y") / 100.0
        dgs10 = load_fred_cache("DGS10") / 100.0
        dgs2 = load_fred_cache("DGS2") / 100.0
        vix = load_fred_cache("VIXCLS")
        
        # Credit spread velocity (21-day)
        spread_vel = credit_spread.diff(21) / 21
        
        # Yield curve momentum
        curve = (dgs10 - dgs2).reindex(spread_vel.index).ffill()
        curve_mom = curve.diff(21) / 21
        
        # Realized vol from SPY
        ff5 = pd.read_parquet("data/fred_cache/ff5_factors_daily.parquet")
        market_ret = ff5["Mkt-RF"] + ff5["RF"]
        realized_vol = market_ret.rolling(21, min_periods=10).std() * np.sqrt(252)
        
        # Levels
        spread_level = load_fred_cache("BAA10Y") / 100.0
        curve_level = (load_fred_cache("DGS10") - load_fred_cache("DGS2")) / 100.0
        vix_level = load_fred_cache("VIXCLS")
        
        df = pd.DataFrame({
            "credit_spread_velocity": spread_vel,
            "yield_curve_momentum": curve.diff(21) / 21,
            "realized_vol_21d": pd.read_parquet("data/fred_cache/ff5_factors_daily.parquet")["Mkt-RF"].rolling(21).std() * np.sqrt(252),
            "credit_spread_level": spread_level,
            "yield_curve_slope": curve_level,
            "vix_level": vix_level,
        }).dropna()
        
        return df
    
    def update(self, as_of: Optional[pd.Timestamp] = None) -> Dict:
        """
        Update unified regime engine with latest data.
        Returns complete unified regime state.
        """
        if as_of is None:
            as_of = pd.Timestamp.now()
        
        # Lazy initialization
        if not hasattr(self, '_bocpd') or self._bocpd is None:
            self.initialize()
        
        # Load latest market data
        data = self._load_market_data()
        io_inputs = self._compute_io_hmm_inputs()
        
        # Get latest input vector for IO-HMM
        latest_io = self._get_latest_io_inputs()
        
        # 1. BOCPD: Seismic Shock Alarm
        # Use credit spread velocity as BOCPD observation
        bocpd_obs = self._get_bocpd_observation()
        bocpd_diag = self._bocpd.update(bocpd_obs)
        
        # 2. IO-HMM: Wind-Vane (Tier 1)
        io_input = self._get_latest_io_inputs()
        io_diag = self._io_hmm.update(io_input)
        
        # 3. Micro-states (Economic Engines)
        if self.config.micro_enabled:
            micro_state, micro_scores = self._micro.classify_current()
        else:
            micro_state = None
            micro_scores = {}
        
        # 4. Decision Logic: Unified Regime Decision
        unified = self._decide_unified_regime()
        
        return unified
    
    def _get_bocpd_observation(self) -> float:
        """Get BOCPD observation: credit spread velocity."""
        credit_spread = load_fred_cache("BAA10Y") / 100.0
        spread_vel = credit_spread.diff(21) / 21
        return spread_vel.iloc[-1] if len(spread_vel) > 0 else 0.0
    
    def _get_latest_io_inputs(self) -> np.ndarray:
        """Get latest IO-HMM input vector (6 features)."""
        data = self._load_market_data()
        credit_spread = load_fred_cache("BAA10Y") / 100.0
        dgs10 = load_fred_cache("DGS10") / 100.0
        dgs2 = load_fred_cache("DGS2") / 100.0
        
        spread_vel = (credit_spread.diff(21) / 21).iloc[-1]
        curve = (dgs10 - dgs2).reindex(load_fred_cache("BAA10Y").index).ffill()
        curve_mom = (curve.diff(21) / 21).iloc[-1]
        
        ff5 = pd.read_parquet("data/fred_cache/ff5_factors_daily.parquet")
        market_ret = ff5["Mkt-RF"] + ff5["RF"]
        realized_vol = market_ret.rolling(21, min_periods=10).std().iloc[-1] * np.sqrt(252)
        
        spread_level = load_fred_cache("BAA10Y").iloc[-1] / 100.0
        # DGS10 and DGS2 have different last-observation dates, so the raw
        # subtraction aligns on the index UNION and leaves NaN past the shorter
        # leg. Take the last date where BOTH legs are known (no ffill across a
        # stale leg, so this stays PIT-correct).
        curve_level = (load_fred_cache("DGS10") - load_fred_cache("DGS2")).dropna().iloc[-1] / 100.0
        vix_level = load_fred_cache("VIXCLS").iloc[-1]
        
        return np.array([
            spread_vel, curve_mom, realized_vol,
            spread_level, curve_level, vix_level
        ])
    
    def _decide_unified_regime(self) -> Dict:
        """Unified regime decision logic."""
        # Get component states
        bocpd_alarm = self._bocpd.state.alarm_triggered if self._bocpd.state else False
        bocpd_p_r0 = self._bocpd.state.p_r0 if self._bocpd.state else 0.0
        
        io_belief = self._io_hmm.state.current_belief if self._io_hmm.state else np.array([1/3, 1/3, 1/3])
        io_map = self._io_hmm.state.current_state_map if self._io_hmm.state else 0
        
        micro_state = getattr(self._micro, "_last_state", None) if self._micro else None
        micro_name = micro_state.value if micro_state else "NONE"
        micro_scores = self._micro.history[-1]["scores"] if self._micro and self._micro.history else {}
        
        # Decision logic
        # 1. BOCPD Circuit Breaker: If alarm, FORCE CRISIS
        if self._bocpd.state and self._bocpd.state.alarm_triggered:
            macro_state = 2  # CRISIS
            regime_name = "CRISIS (BOCPD FORCED)"
            confidence = 0.95
            macro_map = 2
        else:
            # 2. IO-HMM Wind-Vane for Tier 1
            raw_macro_map = int(np.argmax(self._io_hmm.state.current_belief))
            
            # Apply dwell constraint
            if hasattr(self, '_last_macro_state') and self._last_macro_state >= 0 and raw_macro_map != self._last_macro_state:
                if self._dwell_counter < self.config.min_dwell_days:
                    macro_map = self._last_macro_state
                else:
                    self._dwell_counter = 0
                    self._last_macro_state = raw_macro_map
                    macro_map = raw_macro_map
            else:
                self._dwell_counter += 1
                self._last_macro_state = raw_macro_map
                macro_map = raw_macro_map

            macro_name = self.config.io_hmm_states[macro_map]
            macro_belief = self._io_hmm.state.current_belief[macro_map]
            
            # Tier 2: Micro-states within macro context
            micro_actions = {}
            if self._micro and getattr(self._micro, "_last_state", None):
                micro_name = self._micro._last_state.value
                micro_actions = self._micro.get_pipeline_actions(self._micro._last_state)
            
            # Compose regime name
            if macro_map == 2:  # CRISIS
                regime_name = "CRISIS"
                confidence = 0.8
            elif macro_map == 1:  # CHOPPY
                regime_name = f"CHOPPY/{self._micro._last_state.value if self._micro and self._micro._last_state else 'UNKNOWN'}"
                confidence = 0.7
            else:  # EXPANSION
                regime_name = f"EXPANSION/{self._micro._last_state.value if self._micro and self._micro._last_state else 'GOLDILOCKS'}"
                confidence = 0.75
            
            macro_map = macro_map
        
        # Pipeline actions
        actions = self._compile_actions(macro_map, micro_name)
        
        return {
            "regime_name": regime_name,
            "confidence": confidence,
            "macro_state": self.config.io_hmm_states[macro_map] if macro_map < 3 else "CRISIS",
            "macro_belief": self._io_hmm.state.current_belief.tolist() if self._io_hmm.state else [0.33, 0.33, 0.34],
            "macro_map": macro_map,
            "micro_state": micro_name,
            "micro_scores": {k.value: v for k, v in micro_scores.items()} if micro_scores else {},
            "bocpd_alarm": bocpd_alarm,
            "bocpd_p_r0": bocpd_p_r0,
            "io_hmm_belief": io_belief.tolist(),
            "io_hmm_map": io_map,
            "micro_state": micro_name,
            "micro_scores": {k.value: v for k, v in micro_scores.items()} if micro_scores else {},
            "regime_name": regime_name,
            "confidence": confidence,
            "actions": actions,
            "bocpd_alarm": bocpd_alarm,
            "bocpd_p_r0": bocpd_p_r0,
            "bocpd_run_length": self._bocpd.state.map_run_length if self._bocpd.state else -1,
            "io_hmm_belief": io_belief,
            "io_hmm_map": io_map,
            "micro_belief": {k.value: v for k, v in micro_scores.items()} if micro_scores else {},
            "micro_map": micro_name,
            "micro_name": micro_name,
            "regime_name": regime_name,
            "confidence": confidence,
            "actions": actions,
        }
    
    def _compile_actions(self, macro_map: int, micro_name: str) -> Dict:
        """Compile pipeline actions from macro + micro states."""
        # Resolve the macro NAME from the state index; callers pass macro_map (int).
        states = tuple(self.config.io_hmm_states)
        macro_name = states[macro_map] if 0 <= macro_map < len(states) else "CRISIS"
        actions = {
            "macro_action": "HOLD",
            "micro_action": "NONE",
            "wacc_adjustment_bps": 0,
            "allocation_shifts": {},
            "risk_limits": {},
        }
        
        if macro_name == "CRISIS" or "CRISIS" in macro_name:
            actions["macro_action"] = "CRISIS_MODE"
            actions["wacc_adjustment_bps"] = 100
            actions["allocation_shifts"]["equity"] = -0.3
            actions["allocation_shifts"]["bonds"] = 0.2
            actions["allocation_shifts"]["cash"] = 0.1
            actions["risk_limits"]["max_drawdown"] = 0.10
        elif macro_name == "CHOPPY":
            actions["macro_action"] = "DEFENSIVE"
            actions["wacc_adjustment_bps"] = 25
            actions["allocation_shifts"]["equity"] = -0.15
            actions["risk_limits"]["max_drawdown"] = 0.15
        
        # Micro-state actions
        if "GOLDILOCKS" in str(micro_name):
            actions["micro_action"] = "LOWER_HURDLE_RATES"
            actions["wacc_adjustment_bps"] = actions.get("wacc_adjustment_bps", 0) - 50
        elif "LATE_CYCLE" in str(micro_name):
            actions["micro_action"] = "TIGHTEN_ENTRY"
        elif "LIQUIDITY_CRUNCH" in str(micro_name):
            actions["micro_action"] = "DOWN_FILTER_SHIFT_TO_BIL"
        elif "STAGFLATION" in str(micro_name):
            actions["micro_action"] = "ROUTE_CASH_TO_GOLD"
        
        return actions
    
    def get_current_state(self) -> Dict:
        """Get current unified regime state."""
        if not hasattr(self, '_initialized') or not self._initialized:
            self.initialize()
        return self._decide_unified_regime()


def build_unified_engine(config: Optional[UnifiedRegimeConfig] = None) -> UnifiedRegimeEngine:
    """Factory function to create unified regime engine."""
    engine = UnifiedRegimeEngine(config)
    engine.initialize()
    return engine


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    engine = build_unified_engine()
    state = engine.update()
    
    print(f"Regime: {state['regime_name']}")
    print(f"Confidence: {state['confidence']:.2f}")
    print(f"Macro: {state['macro_state']}")
    print(f"Micro: {state['micro_state']}")
    print(f"BOCPD Alarm: {state['bocpd_alarm']} (P(r=0)={state['bocpd_p_r0']:.3f})")
    print(f"IO-HMM: {state['io_hmm_belief']}")
    print(f"Micro: {state['micro_state']}")
    print(f"Actions: {state['actions']}")