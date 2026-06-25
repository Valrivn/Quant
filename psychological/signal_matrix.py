import logging
from typing import Dict, Optional, List
from dataclasses import dataclass
from enum import Enum
from config import load_hybrid_config

logger = logging.getLogger(__name__)


class ExecutionDirective(Enum):
    STRONG_CONTRARIAN_BUY = "STRONG_CONTRARIAN_BUY"
    ASYMMETRIC_OVERRIDE_BUY = "ASYMMETRIC_OVERRIDE_BUY"
    WEAK_CONTRARIAN_BUY = "WEAK_CONTRARIAN_BUY"
    HOLD = "HOLD"
    REDUCE_RISK = "REDUCE_RISK"
    LOCKDOWN = "LOCKDOWN"
    NO_TRADE = "NO_TRADE"


class PsychologicalRegime(Enum):
    PANIC_CAPITULATION = "PANIC_CAPITULATION"
    CROWD_EUPHORIA = "CROWD_EUPHORIA"
    ASYMMETRIC_DIVERGENCE = "ASYMMETRIC_DIVERGENCE"
    ASYMMETRIC_GLASSDOOR_GITHUB_DIVERGENCE = "ASYMMETRIC_GLASSDOOR_GITHUB_DIVERGENCE"
    APATHY = "APATHY"
    NEUTRAL = "NEUTRAL"
    VALIDATION_OVERRIDE_NEUTRAL = "VALIDATION_OVERRIDE_NEUTRAL"


@dataclass
class SignalMatrixOutput:
    regime: str
    execution_directive: str
    contrarian_buy_authorized: bool
    fused_confidence: float
    dcf_floor_signal: Optional[float]
    validation_passed: bool
    validation_details: Dict
    rationale: str


class SignalMatrix:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.matrix_config = self.config.get("signal_matrix", {})
        self.thresholds = self.matrix_config.get("thresholds", {
            "strong_buy_confidence": 0.75,
            "weak_buy_confidence": 0.55,
            "lockdown_confidence": 0.70,
            "reduce_risk_confidence": 0.60,
        })
        self.dcf_weight = self.matrix_config.get("dcf_weight", 0.15)
        self.min_dcf_for_buy = self.matrix_config.get("min_dcf_for_buy", 0.3)

    def compute_dcf_signal(self, dcf_floor_data: Optional[Dict]) -> float:
        if not dcf_floor_data:
            return 0.5
        
        floor = dcf_floor_data.get("intrinsic_floor")
        ceiling = dcf_floor_data.get("intrinsic_ceiling")
        current = dcf_floor_data.get("current_price")
        margin = dcf_floor_data.get("margin_of_safety", 0.25)
        
        if floor is None or ceiling is None or current is None:
            return 0.5
        
        if ceiling <= floor:
            return 0.5
        
        upside = (ceiling - current) / (ceiling - floor)
        margin_adjusted = upside * (1 + margin)
        
        return min(1.0, max(0.0, margin_adjusted))

    def map_regime_to_directive(
        self, 
        regime: str, 
        fused_confidence: float, 
        dcf_signal: float,
        validation_passed: bool
    ) -> ExecutionDirective:
        if not validation_passed:
            return ExecutionDirective.NO_TRADE
        
        if regime == PsychologicalRegime.PANIC_CAPITULATION.value:
            if fused_confidence >= self.thresholds["strong_buy_confidence"] and dcf_signal >= self.min_dcf_for_buy:
                return ExecutionDirective.STRONG_CONTRARIAN_BUY
            elif fused_confidence >= self.thresholds["weak_buy_confidence"] and dcf_signal >= self.min_dcf_for_buy:
                return ExecutionDirective.WEAK_CONTRARIAN_BUY
            else:
                return ExecutionDirective.HOLD
                
        elif regime == PsychologicalRegime.CROWD_EUPHORIA.value:
            if fused_confidence >= self.thresholds["lockdown_confidence"]:
                return ExecutionDirective.LOCKDOWN
            else:
                return ExecutionDirective.REDUCE_RISK
                
        elif regime == PsychologicalRegime.ASYMMETRIC_DIVERGENCE.value:
            return ExecutionDirective.ASYMMETRIC_OVERRIDE_BUY
            
        elif regime == PsychologicalRegime.ASYMMETRIC_GLASSDOOR_GITHUB_DIVERGENCE.value:
            return ExecutionDirective.NO_TRADE
            
        elif regime == PsychologicalRegime.APATHY.value:
            return ExecutionDirective.HOLD
            
        elif regime == PsychologicalRegime.NEUTRAL.value:
            return ExecutionDirective.HOLD
            
        elif regime == PsychologicalRegime.VALIDATION_OVERRIDE_NEUTRAL.value:
            return ExecutionDirective.NO_TRADE
            
        return ExecutionDirective.NO_TRADE

    def evaluate(
        self,
        regime: str,
        fused_confidence: float,
        dcf_floor_data: Optional[Dict],
        validation_passed: bool,
        validation_details: Dict,
        contrarian_buy_authorized: bool
    ) -> SignalMatrixOutput:
        dcf_signal = self.compute_dcf_signal(dcf_floor_data)
        
        directive = self.map_regime_to_directive(
            regime, fused_confidence, dcf_signal, validation_passed
        )
        
        rationale_parts = [
            f"Regime: {regime}",
            f"Fused Confidence: {fused_confidence:.3f}",
            f"DCF Signal: {dcf_signal:.3f}",
            f"Validation: {'PASSED' if validation_passed else 'FAILED'}",
            f"Directive: {directive.value}"
        ]
        
        if regime == PsychologicalRegime.PANIC_CAPITULATION.value:
            rationale_parts.append(
                "Panic capitulation detected: extreme bearish sentiment with high velocity. "
                "Contrarian opportunity if DCF supports margin of safety."
            )
        elif regime == PsychologicalRegime.CROWD_EUPHORIA.value:
            rationale_parts.append(
                "Crowd euphoria detected: extreme bullish sentiment with high velocity. "
                "Risk reduction or lockdown recommended."
            )
        elif regime == PsychologicalRegime.ASYMMETRIC_DIVERGENCE.value:
            rationale_parts.append(
                "Asymmetric divergence: employee sentiment negative but dev velocity positive. "
                "Potential moat erosion warning - asymmetric override buy if confidence sufficient."
            )
        elif regime == PsychologicalRegime.VALIDATION_OVERRIDE_NEUTRAL.value:
            rationale_parts.append(
                "Cross-validation gate triggered: source divergence exceeds threshold. "
                "Forcing NEUTRAL - no trade authorized."
            )
        
        return SignalMatrixOutput(
            regime=regime,
            execution_directive=directive.value,
            contrarian_buy_authorized=contrarian_buy_authorized and directive in [
                ExecutionDirective.STRONG_CONTRARIAN_BUY,
                ExecutionDirective.ASYMMETRIC_OVERRIDE_BUY,
                ExecutionDirective.WEAK_CONTRARIAN_BUY
            ],
            fused_confidence=fused_confidence,
            dcf_floor_signal=dcf_signal,
            validation_passed=validation_passed,
            validation_details=validation_details,
            rationale=" | ".join(rationale_parts)
        )


def create_signal_matrix(config_dict: dict = None) -> SignalMatrix:
    return SignalMatrix(config_dict)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    matrix = create_signal_matrix()
    
    test_cases = [
        ("PANIC_CAPITULATION", 0.85, {"intrinsic_floor": 150, "intrinsic_ceiling": 200, "current_price": 160, "margin_of_safety": 0.25}, True, {}),
        ("CROWD_EUPHORIA", 0.80, None, True, {}),
        ("ASYMMETRIC_DIVERGENCE", 0.70, None, True, {}),
        ("APATHY", 0.30, None, True, {}),
        ("VALIDATION_OVERRIDE_NEUTRAL", 0.50, None, False, {}),
    ]
    
    for regime, conf, dcf, val_passed, val_details in test_cases:
        result = matrix.evaluate(regime, conf, dcf, val_passed, val_details, conf > 0.5)
        print(f"\n{result.regime}:")
        print(f"  Directive: {result.execution_directive}")
        print(f"  Buy Authorized: {result.contrarian_buy_authorized}")
        print(f"  Fused Conf: {result.fused_confidence:.3f}")
        print(f"  DCF Signal: {result.dcf_floor_signal:.3f}")
        print(f"  Rationale: {result.rationale}")