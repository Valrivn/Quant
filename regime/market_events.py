"""
Stage 1: Market Event Detector (Persistence Gate)
"""

from typing import Dict, List, Optional, Tuple
import pandas as pd


class MarketEventDetector:
    """
    Stage 1: Persistence Gate
    - Spread Velocity: ΔSpread > 0.5 bps/day for 3 consecutive days
    - BOCPD: P(r=0) > 0.85 for 2 consecutive days  
    - SPY Trend: Close < 200d SMA - 1.5% for 3 consecutive days
    
    Stage 2: Graded De-Risking (handled in derisking.py)
    Stage 3: Re-Entry Embargo (handled in embargo.py)
    """
    
    def __init__(self, config: Optional[Dict] = None):
        config = config or {}
        triggers_cfg = config.get("market_event_detection", {}).get("triggers", {})
        
        sv_cfg = triggers_cfg.get("spread_velocity", {})
        self.sv_threshold = sv_cfg.get("threshold_bps_per_day", 0.5)
        self.sv_days = sv_cfg.get("persistence_days", 3)
        
        bocpd_cfg = triggers_cfg.get("bocpd_regime_shift", {})
        self.bocpd_threshold = bocpd_cfg.get("posterior_threshold", 0.85)
        self.bocpd_days = bocpd_cfg.get("persistence_days", 2)
        
        spy_cfg = triggers_cfg.get("spy_trend_break", {})
        self.spy_buffer = spy_cfg.get("buffer_pct", 0.015)
        self.spy_days = spy_cfg.get("persistence_days", 3)

    def _check_spread_velocity(self, history: List[float]) -> Tuple[bool, int]:
        """Return (triggered, consecutive_days)."""
        if len(history) < 2:
            return False, 0
        
        consecutive = 0
        # Iterate backwards through history for consecutive daily diffs > threshold
        for i in range(len(history) - 1, 0, -1):
            diff = history[i] - history[i - 1]
            if diff > self.sv_threshold:
                consecutive += 1
            else:
                break
                
        triggered = consecutive >= self.sv_days
        return triggered, consecutive

    def _check_bocpd(self, posterior: float, history: List[float]) -> Tuple[bool, int]:
        """Return (triggered, consecutive_days). history = trailing posteriors including current."""
        full_hist = history if history else [posterior]
        if full_hist and full_hist[-1] != posterior:
            full_hist = list(full_hist) + [posterior]
            
        consecutive = 0
        for val in reversed(full_hist):
            if val > self.bocpd_threshold:
                consecutive += 1
            else:
                break
                
        triggered = consecutive >= self.bocpd_days
        return triggered, consecutive

    def _check_spy_trend(self, close: float, sma_200: float, history: List[Tuple[float, float]]) -> Tuple[bool, int]:
        """Return (triggered, consecutive_days). history = [(close, sma_200), ...]"""
        full_hist = history if history else [(close, sma_200)]
        if full_hist and full_hist[-1] != (close, sma_200):
            full_hist = list(full_hist) + [(close, sma_200)]
            
        consecutive = 0
        for c, sma in reversed(full_hist):
            barrier = sma * (1.0 - self.spy_buffer)
            if c < barrier:
                consecutive += 1
            else:
                break
                
        triggered = consecutive >= self.spy_days
        return triggered, consecutive

    def update(self, market_data: Dict) -> Dict:
        """
        Input: {
            'credit_spread': float,           # current spread
            'credit_spread_history': List[float],  # last N days
            'bocpd_posterior': float,         # P(r=0) from BOCPD
            'bocpd_history': Optional[List[float]],
            'spy_close': float,
            'spy_sma_200': float,
            'spy_history': Optional[List[Tuple[float, float]]],
            'date': pd.Timestamp
        }
        Output: {
            'triggered': bool,
            'trigger_type': 'spread_velocity' | 'bocpd' | 'spy_trend' | None,
            'confirmation_day': int,          # 0 if not confirmed, 1+ if confirmed
            'days_since_confirmation': int
        }
        """
        cs_hist = market_data.get('credit_spread_history', [])
        if not cs_hist and 'credit_spread' in market_data:
            cs_hist = [market_data['credit_spread']]
            
        sv_triggered, sv_count = self._check_spread_velocity(cs_hist)
        
        bocpd_post = market_data.get('bocpd_posterior', 0.0)
        bocpd_hist = market_data.get('bocpd_history', [bocpd_post])
        bocpd_triggered, bocpd_count = self._check_bocpd(bocpd_post, bocpd_hist)
        
        spy_c = market_data.get('spy_close', 0.0)
        spy_sma = market_data.get('spy_sma_200', 0.0)
        spy_hist = market_data.get('spy_history', [(spy_c, spy_sma)])
        spy_triggered, spy_count = self._check_spy_trend(spy_c, spy_sma, spy_hist)
        
        triggered = False
        trigger_type = None
        confirmation_day = 0
        
        if sv_triggered:
            triggered = True
            trigger_type = 'spread_velocity'
            confirmation_day = sv_count
        elif bocpd_triggered:
            triggered = True
            trigger_type = 'bocpd'
            confirmation_day = bocpd_count
        elif spy_triggered:
            triggered = True
            trigger_type = 'spy_trend'
            confirmation_day = spy_count
            
        days_since_confirmation = (confirmation_day - 1) if triggered else 0
        
        emergency = bocpd_triggered or sv_triggered
        
        return {
            'triggered': triggered,
            'trigger_type': trigger_type,
            'confirmation_day': confirmation_day,
            'days_since_confirmation': days_since_confirmation,
            'emergency': emergency,
        }
