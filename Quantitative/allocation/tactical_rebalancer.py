import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

class TacticalRebalancer:
    """
    Tactical shifting logic based on macro states, strictly bounded by a permitted asset universe parameter.
    """
    def __init__(self):
        self.macro_states_config = {
            "Inflation > 4%": {
                "description": "Persistent high inflation environment.",
                "trigger_conditions": {"inflation_rate": ">4%"}
            },
            "Bearish & Inflation <= 2%": {
                "description": "Recessionary concerns with low inflation (deflationary pressures).",
                "trigger_conditions": {"inflation_rate": "<=2%", "market_outlook": "Bearish"}
            },
            "Systemic Crash": {
                "description": "Extreme market dislocation, 'black swan' event.",
                "trigger_conditions": {"event": "Systemic Crash"}
            },
            "Neutral": {
                "description": "Balanced economic outlook, within target inflation range.",
                "trigger_conditions": {"inflation_rate": "2-4%", "market_outlook": "Neutral/Bullish"}
            },
        }

    def get_macro_state(self, inflation_rate: float, market_outlook: str) -> str:
        if inflation_rate > 4.0:
            return "Inflation > 4%"
        elif market_outlook.lower() == "bearish" and inflation_rate <= 2.0:
            return "Bearish & Inflation <= 2%"
        else:
            return "Neutral"

    def get_tactical_adjustments(self, macro_state: str, permitted_universe: List[str]) -> List[Dict[str, Any]]:
        """
        Provides tactical allocation adjustments strictly restricted to the permitted_universe.
        
        Args:
            macro_state: The current macro state.
            permitted_universe: Explicit list of allowed asset tickers.
            
        Returns:
            List[Dict[str, Any]]: adjustments records.
        """
        permitted_set = set(permitted_universe)
        adjustments = []

        # Map adjustments to only allowed tickers in permitted_universe
        raw_adjs = {}
        reason = ""

        if macro_state == "Inflation > 4%":
            reason = "Inflation > 4% trigger: Shift from traditional bonds to gold."
            raw_adjs = {
                "VCIT": -0.15,
                "IEF": -0.15,
                "IAU": 0.15,
                "GLDM": 0.15
            }
        elif macro_state == "Bearish & Inflation <= 2%":
            reason = "Bearish and Inflation <= 2% trigger: Shift from corporate bonds to short-term government cash/treasuries."
            raw_adjs = {
                "VCIT": -0.10,
                "VCSH": -0.10,
                "IEF": -0.10,
                "BIL": 0.15,
                "SHY": 0.15
            }
        elif macro_state == "Systemic Crash":
            reason = "Systemic Crash trigger: Drastic safety flight (Short-term U.S. government paper & gold)."
            raw_adjs = {
                "VCIT": -0.20,
                "VCSH": -0.20,
                "IEF": -0.10,
                "BIL": 0.20,
                "SHY": 0.10,
                "IAU": 0.10,
                "GLDM": 0.10
            }

        # Filter out anything not in permitted_universe
        filtered_adjs = {}
        for ticker, change in raw_adjs.items():
            if ticker in permitted_set:
                filtered_adjs[ticker] = change
            else:
                logger.warning(f"TacticalRebalancer: Ticker '{ticker}' is not in permitted_universe. Skipping.")

        # Ensure the adjustments balance out (sum is approximately 0) to maintain allocation
        total_adjustment = sum(filtered_adjs.values())
        if abs(total_adjustment) > 1e-6 and len(filtered_adjs) > 0:
            # Distribute residual to balance the sum to 0
            pos_keys = [k for k, v in filtered_adjs.items() if v > 0]
            if pos_keys:
                adjustment_share = total_adjustment / len(pos_keys)
                for k in pos_keys:
                    filtered_adjs[k] -= adjustment_share

        if filtered_adjs:
            adjustments.append({
                "reason": reason,
                "adjustments": filtered_adjs
            })

        return adjustments
