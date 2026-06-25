import logging
from typing import Dict, Optional, List
from dataclasses import dataclass
from config import load_hybrid_config

logger = logging.getLogger(__name__)


@dataclass
class CrossValidationResult:
    layer_name: str
    convergence_score: float
    penalty_multiplier: float
    details: Dict


class CrossValidationEngine:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.thresholds = self.config.get("cross_validation", {})
        self.divergence_threshold = self.thresholds.get("divergence_threshold", 0.3)
        self.kappa = self.thresholds.get("kappa", 5.0)

    def _normalize_score(self, value: float, min_val: float, max_val: float) -> float:
        if value is None:
            return 0.5
        return max(0.0, min(1.0, (value - min_val) / (max_val - min_val)))

    def _compute_divergence(self, score1: float, score2: float) -> float:
        denom = max(score1, score2, 0.1)
        return abs(score1 - score2) / denom

    def _exponential_penalty(self, divergence: float) -> float:
        if divergence <= self.divergence_threshold:
            return 1.0
        import math
        penalty = math.exp(-self.kappa * (divergence - self.divergence_threshold))
        return max(penalty, 0.1)

    def validate_layer1_glassdoor_comparably(
        self, 
        glassdoor_raw: Optional[float], 
        comparably_badge: Optional[float]
    ) -> CrossValidationResult:
        n_gd = self._normalize_score(glassdoor_raw, 0, 5) if glassdoor_raw else 0.5
        n_comp = self._normalize_score(comparably_badge, 0, 100) if comparably_badge else 0.5
        
        divergence = self._compute_divergence(n_gd, n_comp)
        penalty = self._exponential_penalty(divergence)
        
        return CrossValidationResult(
            layer_name="Layer1_Glassdoor_Comparably",
            convergence_score=1.0 - divergence,
            penalty_multiplier=penalty,
            details={
                "glassdoor_normalized": n_gd,
                "comparably_normalized": n_comp,
                "divergence": divergence
            }
        )

    def validate_layer2_jobspy_github(
        self,
        jobspy_zscore: Optional[float],
        github_velocity: Optional[float]
    ) -> CrossValidationResult:
        n_jobs = self._normalize_score(jobspy_zscore, -3, 3) if jobspy_zscore is not None else 0.5
        n_github = self._normalize_score(github_velocity, -2, 2) if github_velocity is not None else 0.5
        
        divergence = self._compute_divergence(n_jobs, n_github)
        penalty = self._exponential_penalty(divergence)
        
        return CrossValidationResult(
            layer_name="Layer2_JobSpy_GitHub",
            convergence_score=1.0 - divergence,
            penalty_multiplier=penalty,
            details={
                "jobspy_zscore_normalized": n_jobs,
                "github_velocity_normalized": n_github,
                "divergence": divergence
            }
        )

    def validate_layer3_product_reddit(
        self,
        product_sentiment: Optional[float],
        reddit_ratio: Optional[float]
    ) -> CrossValidationResult:
        n_product = self._normalize_score(product_sentiment, -1, 1) if product_sentiment is not None else 0.5
        n_reddit = self._normalize_score(reddit_ratio, 0, 4) if reddit_ratio is not None else 0.5
        
        divergence = self._compute_divergence(n_product, n_reddit)
        penalty = self._exponential_penalty(divergence)
        
        return CrossValidationResult(
            layer_name="Layer3_Product_Reddit",
            convergence_score=1.0 - divergence,
            penalty_multiplier=penalty,
            details={
                "product_sentiment_normalized": n_product,
                "reddit_ratio_normalized": n_reddit,
                "divergence": divergence
            }
        )

    def validate_layer4_dcf_regime(
        self,
        dcf_signal: Optional[float],
        regime_confidence: Optional[float]
    ) -> CrossValidationResult:
        n_dcf = dcf_signal if dcf_signal is not None else 0.5
        n_regime = regime_confidence if regime_confidence is not None else 0.5
        
        divergence = self._compute_divergence(n_dcf, n_regime)
        penalty = self._exponential_penalty(divergence)
        
        return CrossValidationResult(
            layer_name="Layer4_DCF_Regime",
            convergence_score=1.0 - divergence,
            penalty_multiplier=penalty,
            details={
                "dcf_signal": n_dcf,
                "regime_confidence": n_regime,
                "divergence": divergence
            }
        )

    def validate_apewisdom_reddit_github(
        self,
        apewisdom_sentiment: Optional[float],
        reddit_bull_bear: Optional[float],
        github_velocity: Optional[float]
    ) -> CrossValidationResult:
        n_ape = self._normalize_score(apewisdom_sentiment, -1, 1) if apewisdom_sentiment is not None else 0.5
        n_reddit = self._normalize_score(reddit_bull_bear, 0, 4) if reddit_bull_bear is not None else 0.5
        n_github = self._normalize_score(github_velocity, -2, 2) if github_velocity is not None else 0.5
        
        convergence = 1.0 - (abs(n_ape - n_reddit) + abs(n_reddit - n_github) + abs(n_github - n_ape)) / 3.0
        divergence = 1.0 - convergence
        penalty = self._exponential_penalty(divergence)
        
        return CrossValidationResult(
            layer_name="ApeWisdom_Reddit_GitHub_Convergence",
            convergence_score=max(0.0, convergence),
            penalty_multiplier=penalty,
            details={
                "apewisdom_normalized": n_ape,
                "reddit_normalized": n_reddit,
                "github_normalized": n_github,
                "triangular_convergence": convergence
            }
        )

    def run_all_validations(
        self,
        glassdoor_raw: Optional[float] = None,
        comparably_badge: Optional[float] = None,
        jobspy_zscore: Optional[float] = None,
        github_velocity: Optional[float] = None,
        product_sentiment: Optional[float] = None,
        reddit_ratio: Optional[float] = None,
        dcf_signal: Optional[float] = None,
        regime_confidence: Optional[float] = None,
        apewisdom_sentiment: Optional[float] = None,
    ) -> Dict[str, CrossValidationResult]:
        results = {}
        
        results["layer1"] = self.validate_layer1_glassdoor_comparably(glassdoor_raw, comparably_badge)
        results["layer2"] = self.validate_layer2_jobspy_github(jobspy_zscore, github_velocity)
        results["layer3"] = self.validate_layer3_product_reddit(product_sentiment, reddit_ratio)
        results["layer4"] = self.validate_layer4_dcf_regime(dcf_signal, regime_confidence)
        results["apewisdom_reddit_github"] = self.validate_apewisdom_reddit_github(
            apewisdom_sentiment, reddit_ratio, github_velocity
        )
        
        return results

    def compute_aggregate_penalty(self, results: Dict[str, CrossValidationResult]) -> float:
        penalties = [r.penalty_multiplier for r in results.values()]
        if not penalties:
            return 1.0
        import math
        return math.prod(penalties) ** (1.0 / len(penalties))

    def evaluate_all_layers(
        self,
        ticker: str,
        regime_data: Dict,
        fintech_sentiment: float,
        reddit_bull_bear_ratio: float,
        dev_velocity: float,
        quant_value: float
    ) -> Dict:
        """Evaluate all 4 cross-validation layers plus ApeWisdom convergence"""
        glassdoor_raw = regime_data.get("employee_sentiment_proxy")
        comparably_badge = regime_data.get("comparably_badge_score")
        
        jobspy_zscore = regime_data.get("jobspy_zscore_1y")
        github_velocity = dev_velocity
        product_sentiment = regime_data.get("product_sentiment_proxy")
        reddit_ratio = reddit_bull_bear_ratio
        dcf_signal = quant_value
        regime_confidence = regime_data.get("confidence_score", 0.5)
        apewisdom_sentiment = fintech_sentiment
        
        results = self.run_all_validations(
            glassdoor_raw=glassdoor_raw,
            comparably_badge=comparably_badge,
            jobspy_zscore=jobspy_zscore,
            github_velocity=github_velocity,
            product_sentiment=product_sentiment,
            reddit_ratio=reddit_ratio,
            dcf_signal=dcf_signal,
            regime_confidence=regime_confidence,
            apewisdom_sentiment=apewisdom_sentiment
        )
        
        combined_penalty = self.compute_aggregate_penalty(results)
        
        final_override = False
        for result in results.values():
            if result.penalty_multiplier < 0.4:
                final_override = True
                break
        
        return {
            "layers": {name: {
                "convergence_score": r.convergence_score,
                "penalty_multiplier": r.penalty_multiplier,
                "details": r.details
            } for name, r in results.items()},
            "combined_penalty": combined_penalty,
            "final_override": final_override,
            "validation_passed": not final_override
        }


def create_cross_validation_engine(config_dict: dict = None) -> CrossValidationEngine:
    return CrossValidationEngine(config_dict)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    engine = create_cross_validation_engine()
    
    results = engine.run_all_validations(
        glassdoor_raw=4.0,
        comparably_badge=85,
        jobspy_zscore=1.5,
        github_velocity=0.8,
        product_sentiment=0.2,
        reddit_ratio=2.5,
        dcf_signal=0.7,
        regime_confidence=0.8,
        apewisdom_sentiment=0.6
    )
    
    for name, result in results.items():
        print(f"\n{result.layer_name}:")
        print(f"  Convergence: {result.convergence_score:.4f}")
        print(f"  Penalty: {result.penalty_multiplier:.4f}")
        print(f"  Details: {result.details}")
    
    agg_penalty = engine.compute_aggregate_penalty(results)
    print(f"\nAggregate Penalty: {agg_penalty:.4f}")