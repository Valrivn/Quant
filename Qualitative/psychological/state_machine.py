from typing import Tuple, Optional
from config import load_hybrid_config
from psychological.interfaces import RegimeOutput
from psychological.scrapers.validation_gate import CrossValidationGate


class PsychologicalStateMachine:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config().get("psychological", {})
        self.thresholds = self.config.get("regime_thresholds", {})
        self.validation_gate = CrossValidationGate(config_dict)
        
    def evaluate(self, bull_bear_ratio: float, velocity_sigma: float, 
                 employee_sentiment_proxy: float = None, 
                 dev_velocity_sigma: float = None,
                 glassdoor_raw: float = None,
                 comparably_badge: float = None,
                 validation_result: Optional[dict] = None) -> RegimeOutput:
        
        panic_ratio = self.thresholds.get("panic_ratio", 0.25)
        panic_velocity_sigma = self.thresholds.get("panic_velocity_sigma", 2.0)
        euphoria_ratio = self.thresholds.get("euphoria_ratio", 4.0)
        euphoria_velocity_sigma = self.thresholds.get("euphoria_velocity_sigma", 2.5)
        apathy_ratio_min = self.thresholds.get("apathy_ratio_min", 0.8)
        apathy_ratio_max = self.thresholds.get("apathy_ratio_max", 1.5)
        apathy_velocity_sigma = self.thresholds.get("apathy_velocity_sigma", 0.5)
        asymmetric_employee_sigma = self.thresholds.get("asymmetric_employee_sigma", -1.5)
        asymmetric_git_velocity_sigma = self.thresholds.get("asymmetric_git_velocity_sigma", 1.0)
        glassdoor_divergence_threshold = self.thresholds.get("glassdoor_divergence_threshold", 0.3)
        
        validation_override = False
        validation_penalty = 1.0
        
        if validation_result:
            validation_override = validation_result.get("override_triggered", False)
            validation_penalty = validation_result.get("penalty_multiplier", 1.0)
        elif glassdoor_raw is not None and comparably_badge is not None:
            vg_result = self.validation_gate.evaluate(glassdoor_raw, comparably_badge)
            validation_override = vg_result.override_triggered
            validation_penalty = vg_result.penalty_multiplier
        
        if validation_override:
            return {
                "regime": "VALIDATION_OVERRIDE_NEUTRAL",
                "contrarian_buy_authorized": False,
                "confidence": 0.0
            }
        
        if (glassdoor_raw is not None and comparably_badge is not None):
            n_gd = min(glassdoor_raw / 5.0, 1.0) if glassdoor_raw > 0 else 0.0
            n_comp = min(comparably_badge / 100.0, 1.0) if comparably_badge > 0 else 0.0
            
            if n_gd > 0.7 and n_comp < 0.4:
                divergence = abs(n_gd - n_comp) / max(n_gd, 0.1)
                if divergence > glassdoor_divergence_threshold:
                    return {
                        "regime": "ASYMMETRIC_GLASSDOOR_GITHUB_DIVERGENCE",
                        "contrarian_buy_authorized": False,
                        "confidence": 0.0
                    }
        
        if bull_bear_ratio <= panic_ratio and velocity_sigma >= panic_velocity_sigma:
            conf = min(1.0, (panic_velocity_sigma - velocity_sigma) / panic_velocity_sigma + 0.5)
            return {
                "regime": "PANIC_CAPITULATION",
                "contrarian_buy_authorized": True,
                "confidence": conf * validation_penalty
            }
            
        if bull_bear_ratio >= euphoria_ratio and velocity_sigma >= euphoria_velocity_sigma:
            conf = min(1.0, (velocity_sigma - euphoria_velocity_sigma) / euphoria_velocity_sigma + 0.5)
            return {
                "regime": "CROWD_EUPHORIA",
                "contrarian_buy_authorized": False,
                "confidence": conf * validation_penalty
            }
            
        if (employee_sentiment_proxy is not None and employee_sentiment_proxy <= asymmetric_employee_sigma and
            dev_velocity_sigma is not None and dev_velocity_sigma >= asymmetric_git_velocity_sigma):
            return {
                "regime": "ASYMMETRIC_DIVERGENCE",
                "contrarian_buy_authorized": False,
                "confidence": 0.7 * validation_penalty
            }
            
        if (apathy_ratio_min <= bull_bear_ratio <= apathy_ratio_max and 
            abs(velocity_sigma) <= apathy_velocity_sigma):
            return {
                "regime": "APATHY",
                "contrarian_buy_authorized": False,
                "confidence": 0.3 * validation_penalty
            }
            
        return {
            "regime": "NEUTRAL",
            "contrarian_buy_authorized": False,
            "confidence": 0.2 * validation_penalty
        }


def create_state_machine(config_dict: dict = None) -> PsychologicalStateMachine:
    return PsychologicalStateMachine(config_dict)


if __name__ == "__main__":
    sm = create_state_machine()
    
    test_cases = [
        (0.2, 2.5, None, None, None, None),
        (5.0, 3.0, None, None, None, None),
        (1.0, 0.2, -2.0, 1.5, None, None),
        (1.2, 0.3, None, None, None, None),
        (1.0, 0.0, None, None, None, None),
        (1.0, 1.0, None, None, 4.5, 30),
    ]
    
    for ratio, vel_sigma, emp, dev, gd, comp in test_cases:
        result = sm.evaluate(ratio, vel_sigma, emp, dev, gd, comp)
        print(f"Ratio: {ratio}, VelSigma: {vel_sigma}, Emp: {emp}, Dev: {dev}, GD: {gd}, Comp: {comp}")
        print(f"  -> {result}\n")