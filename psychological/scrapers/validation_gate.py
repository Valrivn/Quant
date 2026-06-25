import math
import logging
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
from config import load_hybrid_config

logger = logging.getLogger(__name__)


@dataclass
class ValidationGateResult:
    normalized_glassdoor: Optional[float]
    normalized_comparably: Optional[float]
    divergence: Optional[float]
    penalty_multiplier: float
    override_triggered: bool
    confidence_floor: float
    kappa: float


class CrossValidationGate:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.gate_config = self.config.get("validation_gate", {})
        self.kappa = self.gate_config.get("kappa", 5.0)
        self.divergence_threshold = self.gate_config.get("divergence_threshold", 0.20)
        self.confidence_floor = self.gate_config.get("confidence_floor", 0.40)
        self.max_penalty = self.gate_config.get("max_penalty", 0.1)

    def normalize_glassdoor(self, raw_score: float) -> float:
        if raw_score is None or raw_score < 0:
            return 0.0
        return min(raw_score / 5.0, 1.0)

    def normalize_comparably(self, badge_score: float) -> float:
        if badge_score is None or badge_score < 0:
            return 0.0
        return min(badge_score / 100.0, 1.0)

    def compute_divergence(self, n_gd: float, n_comp: float) -> float:
        denominator = max(n_gd, 0.1)
        if denominator == 0:
            return 1.0
        return abs(n_gd - n_comp) / denominator

    def compute_penalty(self, divergence: float) -> float:
        if divergence <= self.divergence_threshold:
            return 1.0
        penalty = math.exp(-self.kappa * (divergence - self.divergence_threshold))
        return max(penalty, self.max_penalty)

    def check_hard_gate(self, penalty_multiplier: float) -> bool:
        return penalty_multiplier < self.confidence_floor

    def evaluate(self, glassdoor_raw: Optional[float], comparably_badge: Optional[float]) -> ValidationGateResult:
        n_gd = self.normalize_glassdoor(glassdoor_raw) if glassdoor_raw is not None else None
        n_comp = self.normalize_comparably(comparably_badge) if comparably_badge is not None else None

        if n_gd is None or n_comp is None:
            return ValidationGateResult(
                normalized_glassdoor=n_gd,
                normalized_comparably=n_comp,
                divergence=None,
                penalty_multiplier=1.0,
                override_triggered=False,
                confidence_floor=self.confidence_floor,
                kappa=self.kappa
            )

        divergence = self.compute_divergence(n_gd, n_comp)
        penalty_multiplier = self.compute_penalty(divergence)
        override_triggered = self.check_hard_gate(penalty_multiplier)

        if override_triggered:
            logger.warning(
                f"VALIDATION GATE OVERRIDE: divergence={divergence:.4f}, "
                f"penalty={penalty_multiplier:.4f} < floor={self.confidence_floor}. "
                f"Forcing NEUTRAL regime, contrarian_buy_authorized=False"
            )

        return ValidationGateResult(
            normalized_glassdoor=n_gd,
            normalized_comparably=n_comp,
            divergence=divergence,
            penalty_multiplier=penalty_multiplier,
            override_triggered=override_triggered,
            confidence_floor=self.confidence_floor,
            kappa=self.kappa
        )

    def apply_to_confidence(self, base_confidence: float, penalty_multiplier: float) -> float:
        adjusted = base_confidence * penalty_multiplier
        return max(0.0, min(1.0, adjusted))


def create_validation_gate(config_dict: dict = None) -> CrossValidationGate:
    return CrossValidationGate(config_dict)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    gate = create_validation_gate()

    test_cases = [
        (4.5, 90, "High alignment"),
        (4.0, 50, "Moderate divergence"),
        (3.0, 95, "High divergence - Glassdoor low, Comparably high"),
        (1.0, 10, "Both low"),
        (5.0, 100, "Perfect alignment"),
        (2.0, 80, "Significant divergence"),
    ]

    print("Validation Gate Test Results:")
    print("=" * 80)
    for gd_raw, comp_raw, desc in test_cases:
        result = gate.evaluate(gd_raw, comp_raw)
        print(f"\n{desc}")
        print(f"  Glassdoor: {gd_raw}/5.0 -> normalized: {result.normalized_glassdoor:.4f}")
        print(f"  Comparably: {comp_raw}/100 -> normalized: {result.normalized_comparably:.4f}")
        print(f"  Divergence: {result.divergence:.4f}")
        print(f"  Penalty: {result.penalty_multiplier:.4f}")
        print(f"  Override: {result.override_triggered}")
        if result.divergence:
            base_conf = 0.8
            adjusted = gate.apply_to_confidence(base_conf, result.penalty_multiplier)
            print(f"  Confidence: {base_conf} -> {adjusted:.4f}")