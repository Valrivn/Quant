from typing import Dict, List, Any

class WeightingProcessAudit:
    def get_logs(self, baseline_weights: Dict[str, float], tactical_adjustments: List[Dict[str, Any]]) -> Dict[str, Any]:
        current_weights = baseline_weights.copy()
        for adj_record in tactical_adjustments:
            for etf, change in adj_record.get('adjustments', {}).items():
                current_weights[etf] = current_weights.get(etf, 0.0) + change
                if current_weights[etf] < 0:
                    current_weights[etf] = 0.0
        total_weight = sum(current_weights.values())
        if abs(total_weight) > 1e-9:
            final_calculated_weights = {k: v / total_weight for k, v in current_weights.items()}
        else:
            final_calculated_weights = current_weights
        return {
            "baseline_weights": baseline_weights,
            "tactical_adjustments": tactical_adjustments,
            "final_calculated_weights": final_calculated_weights
        }
