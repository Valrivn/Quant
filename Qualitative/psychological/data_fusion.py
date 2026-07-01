import numpy as np
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from dataclasses import dataclass
from psychological.scrapers.validation_gate import CrossValidationGate, ValidationGateResult
from psychological.nlp_engine import NLPEngine
from psychological.velocity_tracker import VelocityTracker
from config import load_hybrid_config


@dataclass
class FusedSignal:
    ticker: str
    timestamp: int
    source_provenance: Dict[str, Dict[str, Any]]
    compound_vader: float
    bull_bear_ratio: float
    bullish_count: int
    bearish_count: int
    mention_velocity: float
    comment_volume_sigma: float
    acceleration: float
    employee_sentiment_proxy: Optional[float]
    dev_fork_acceleration: Optional[float]
    validation_gate_penalty: float
    validation_override: bool
    fused_confidence: float
    metadata_json: str


class DataFusionEngine:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.nlp_engine = NLPEngine(config_dict)
        self.velocity_tracker = VelocityTracker(config_dict=config_dict)
        self.validation_gate = CrossValidationGate(config_dict)
        self.fusion_weights = self.config.get("psychological", {}).get("fusion_weights", {
            "psychological_regime": 0.60,
            "fintech_confirmation": 0.25,
            "quantitative_value": 0.15
        })
        
    def compute_vader_bull_bear_ratio(self, texts: List[str]) -> Dict[str, float]:
        if not texts:
            return {"ratio": 1.0, "bullish_total": 0, "bearish_total": 0}
        
        results = self.nlp_engine.analyze_batch(texts)
        bullish_total = sum(r.get("bullish_count", 0) for r in results)
        bearish_total = sum(r.get("bearish_count", 0) for r in results)
        compound_avg = np.mean([r.get("compound_vader", 0) for r in results]) if results else 0
        
        try:
            ratio = bullish_total / bearish_total if bearish_total > 0 else 99.0
        except ZeroDivisionError:
            ratio = 99.0
            
        return {
            "ratio": ratio,
            "bullish_total": bullish_total,
            "bearish_total": bearish_total,
            "compound_avg": float(compound_avg)
        }
    
    def apply_validation_gate_penalty(
        self, 
        base_confidence: float, 
        glassdoor_raw: Optional[float], 
        comparably_badge: Optional[float]
    ) -> tuple:
        result = self.validation_gate.evaluate(glassdoor_raw, comparably_badge)
        adjusted_confidence = self.validation_gate.apply_to_confidence(base_confidence, result.penalty_multiplier)
        return adjusted_confidence, result.penalty_multiplier, result.override_triggered
    
    def fuse_signals(
        self,
        ticker: str,
        texts: List[str],
        source_provenance: Dict[str, Dict[str, Any]],
        glassdoor_raw: Optional[float] = None,
        comparably_badge: Optional[float] = None,
        employee_sentiment_proxy: Optional[float] = None,
        dev_fork_acceleration: Optional[float] = None,
        fintech_confirmation: float = 0.5,
        quantitative_value: float = 0.5
    ) -> FusedSignal:
        vader_results = self.compute_vader_bull_bear_ratio(texts)
        
        velocity_metrics = self.velocity_tracker.calculate_velocity_metrics(ticker, 24)
        
        adjusted_conf, penalty, override = self.apply_validation_gate_penalty(
            base_confidence=0.8,
            glassdoor_raw=glassdoor_raw,
            comparably_badge=comparably_badge
        )
        
        fused_conf = (
            self.fusion_weights["psychological_regime"] * adjusted_conf +
            self.fusion_weights["fintech_confirmation"] * fintech_confirmation +
            self.fusion_weights["quantitative_value"] * quantitative_value
        )
        
        return FusedSignal(
            ticker=ticker,
            timestamp=int(datetime.now(timezone.utc).timestamp()),
            source_provenance=source_provenance,
            compound_vader=vader_results["compound_avg"],
            bull_bear_ratio=vader_results["ratio"],
            bullish_count=vader_results["bullish_total"],
            bearish_count=vader_results["bearish_total"],
            mention_velocity=velocity_metrics.get("mention_velocity", 0.0),
            comment_volume_sigma=velocity_metrics.get("comment_volume_sigma", 0.0),
            acceleration=velocity_metrics.get("acceleration", 0.0),
            employee_sentiment_proxy=employee_sentiment_proxy,
            dev_fork_acceleration=dev_fork_acceleration,
            validation_gate_penalty=penalty,
            validation_override=override,
            fused_confidence=min(1.0, max(0.0, fused_conf)),
            metadata_json=str(source_provenance)
        )


def create_data_fusion_engine(config_dict: dict = None) -> DataFusionEngine:
    return DataFusionEngine(config_dict)


if __name__ == "__main__":
    engine = create_data_fusion_engine()
    test_texts = [
        "AAPL calls to the moon! Diamond hands 💎🙌",
        "TSLA puts printing, paper hands everywhere",
        "NVDA undervalued, long calls"
    ]
    provenance = {
        "reddit:wallsstreetbets": {"weight": 0.6, "count": 2},
        "reddit:stocks": {"weight": 0.4, "count": 1}
    }
    signal = engine.fuse_signals(
        ticker="AAPL",
        texts=test_texts,
        source_provenance=provenance,
        glassdoor_raw=4.5,
        comparably_badge=90
    )
    print(f"Fused Signal for {signal.ticker}:")
    print(f"  Compound VADER: {signal.compound_vader:.4f}")
    print(f"  Bull/Bear Ratio: {signal.bull_bear_ratio:.4f}")
    print(f"  Mention Velocity: {signal.mention_velocity:.4f}")
    print(f"  Comment Volume Sigma: {signal.comment_volume_sigma:.4f}")
    print(f"  Validation Penalty: {signal.validation_gate_penalty:.4f}")
    print(f"  Validation Override: {signal.validation_override}")
    print(f"  Fused Confidence: {signal.fused_confidence:.4f}")