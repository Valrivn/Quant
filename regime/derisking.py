"""
Stage 2: Graded De-Risking State Machine
"""

from typing import Dict, List, Optional


class GradedDerisking:
    """
    Step 1: Day 3  → Equity cap 70%
    Step 2: Day 5  → Equity cap 50%
    Step 3: Day 10 → Equity cap 40%
    """
    
    def __init__(self, config: Optional[Dict] = None):
        config = config or {}
        steps_cfg = config.get("market_event_detection", {}).get("graded_derisking", {}).get("steps", None)
        if steps_cfg:
            self.steps = sorted(steps_cfg, key=lambda x: x['day'])
        else:
            self.steps = [
                {'day': 3, 'equity_cap': 0.70},
                {'day': 5, 'equity_cap': 0.50},
                {'day': 10, 'equity_cap': 0.40},
            ]
        self.cash_instruments = config.get("market_event_detection", {}).get("graded_derisking", {}).get("cash_allocation", ["BIL", "SHY"])

    def is_active(self, days_since_confirmation: int) -> bool:
        """True if any de-risking step applies."""
        if days_since_confirmation < 0:
            return False
        first_step_day = self.steps[0]['day']
        return days_since_confirmation >= (first_step_day - 1)

    def get_equity_cap(self, days_since_confirmation: int) -> float:
        """Return current equity cap based on days since trigger confirmation.
        Returns 1.0 if no active trigger or days_since_confirmation < 0."""
        if days_since_confirmation < 0:
            return 1.0
        
        # Confirmation day count begins at 1 (which corresponds to days_since_confirmation = 0)
        # So confirmation day = days_since_confirmation + 1
        conf_day = days_since_confirmation + 1
        
        current_cap = 1.0
        for step in self.steps:
            if conf_day >= step['day']:
                current_cap = float(step['equity_cap'])
                
        return current_cap

    def apply_derisking(self, target_weights: Dict[str, float], 
                        days_since_confirmation: int,
                        emergency: bool = False,
                        portfolio_valuation: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        """
        1. Get equity cap for current day
        2. Scale equity weights to fit within cap
        3. Allocate excess to BIL/SHY
        4. Return adjusted weights
        """
        equity_cap = self.get_equity_cap(days_since_confirmation)
        
        if emergency:
            val = portfolio_valuation or {}
            z_val = float(val.get('z_val', 0.0))
            icr = float(val.get('icr', 10.0))
            
            if z_val <= -1.0 and icr >= 5.0:
                # SUPPRESS emergency exit (hold, log suppression)
                pass
            elif z_val > 0.0 or icr < 3.0:
                # Apply aggressive de-risking ladder immediately (max applicable day step)
                max_step_day = max(s['day'] for s in self.steps)
                emergency_days = max_step_day - 1 # ensures conf_day >= max_step_day
                equity_cap = self.get_equity_cap(emergency_days)
        
        cash_set = set(self.cash_instruments)
        equity_weights = {k: v for k, v in target_weights.items() if k not in cash_set}
        total_equity_w = sum(equity_weights.values())
        
        adjusted_weights: Dict[str, float] = {}
        
        if total_equity_w > equity_cap and total_equity_w > 0:
            scale_factor = equity_cap / total_equity_w
            scaled_equity_sum = 0.0
            for k, v in equity_weights.items():
                scaled_w = v * scale_factor
                adjusted_weights[k] = scaled_w
                scaled_equity_sum += scaled_w
            excess = 1.0 - scaled_equity_sum
        else:
            for k, v in equity_weights.items():
                adjusted_weights[k] = v
            excess = 1.0 - total_equity_w
            
        if excess < 0.0:
            excess = 0.0
            
        num_cash = len(self.cash_instruments)
        if num_cash > 0:
            cash_per_inst = excess / num_cash
            for cash_inst in self.cash_instruments:
                adjusted_weights[cash_inst] = adjusted_weights.get(cash_inst, 0.0) + cash_per_inst
                
        total_w = sum(adjusted_weights.values())
        if total_w > 0:
            adjusted_weights = {k: v / total_w for k, v in adjusted_weights.items()}
            
        return adjusted_weights
