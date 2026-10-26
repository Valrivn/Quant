"""
Tier 2 Micro-State Classifiers — Economic Contextual Engines
Pre-Registration Config: config/pre_registration_v5.yaml

Four micro-state classifiers that operate within Tier 1 macro states:
- Micro A: GOLDILOCKS (Low Inflation + Healthy Credit)
- Micro B: LATE-CYCLE DRAG (Flattening/Inverted Curve + Tightening Solvency)
- Micro C: LIQUIDITY CRUNCH (High Real Rates + Credit Freeze)
- Micro D: STAGFLATIONARY SHOCK (Hot Inflation + Supply Disruption)

Each micro-state has specific pipeline actions for portfolio adjustments.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
from scipy.special import softmax

from Quantitative.hmm_regime import load_fred_cache

logger = logging.getLogger(__name__)


class MicroState(Enum):
    """Tier 2 Micro-States (The Specific Threat)."""
    GOLDILOCKS = "GOLDILOCKS"           # Micro A: Low inflation, healthy credit
    LATE_CYCLE_DRAG = "LATE_CYCLE_DRAG" # Late-cycle drag
    LIQUIDITY_CRUNCH = "LIQUIDITY_CRUNCH" # Liquidity crunch
    STAGFLATION = "STAGFLATION"         # Stagflationary shock


@dataclass
class MicroStateConfig:
    """Configuration for micro-state classifiers."""
    # GOLDILOCKS thresholds
    goldilocks_inflation_max: float = 2.5    # CPI YoY <= 2.5%
    goldilocks_real_rate_max: float = 1.5    # Real rate <= 1.5%
    goldilocks_spread_max: float = 150       # HY OAS <= 150 bps
    goldilocks_vix_max: float = 20           # VIX <= 20
    goldilocks_curve_min: float = 50         # 10Y-2Y >= 50 bps
    
    # LATE_CYCLE_DRAG thresholds
    late_cycle_curve_max: float = 0          # 10Y-2Y <= 0 (inverted)
    late_cycle_spread_min: float = 200       # HY OAS >= 200 bps
    late_cycle_margin_rising: bool = True    # Operating margin variance rising
    
    # LIQUIDITY_CRUNCH thresholds
    crunch_real_rate_min: float = 2.0        # Real rate >= 2%
    crunch_spread_min: float = 400           # HY OAS >= 400 bps
    crunch_stl_fsi_min: float = 1.5          # STLFSI > 1.5
    
    # STAGFLATION thresholds
    stagflation_cpi_min: float = 3.5         # CPI YoY >= 3.5%
    stagflation_real_rate_min: float = 1.0   # Real rate >= 1%
    stagflation_supply_shock: bool = True    # Supply disruption indicator
    
    # PIT lags (months)
    cpi_lag: int = 1
    unrate_lag: int = 1
    market_lag: int = 1  # days


class MicroStateClassifier:
    """
    Tier 2 Micro-State Classifiers (Economic Contextual Engines).
    
    Each micro-state identifies the specific economic threat within the
    Tier 1 macro regime, enabling precise portfolio actions.
    
    Micro A: GOLDILOCKS - Low inflation, healthy credit → Lower hurdle rates, extend runways
    Micro B: LATE_CYCLE_DRAG - Inverted curve, tightening solvency → Tighten entry, trim cyclicals
    Micro C: LIQUIDITY_CRUNCH - High real rates, credit freeze → Downside shock filter, shift to BIL
    Micro D: STAGFLATION - Hot inflation, supply disruption → Route from cash to Gold/tangibles
    """
    
    def __init__(self, config: Optional[MicroStateConfig] = None):
        self.config = config or MicroStateConfig()
        self.history: List[Dict] = []
        self._cached_data: Optional[Dict[str, pd.Series]] = None
        
    def _load_economic_data(self) -> Dict[str, pd.Series]:
        """Load all required economic data from FRED cache."""
        if self._cached_data is None:
            self._cached_data = {
                "cpi": load_fred_cache("CPIAUCSL"),
                "core_cpi": load_fred_cache("CPILFESL"),
                "unrate": load_fred_cache("UNRATE"),
                "dgs10": load_fred_cache("DGS10"),
                "dgs2": load_fred_cache("DGS2"),
                "dfii10": load_fred_cache("DFII10"),
                "vix": load_fred_cache("VIXCLS"),
                "stl_fsi": load_fred_cache("STLFSI2"),
                "hy_oas": load_fred_cache("BAMLH0A0HYM2"),
                "baa": load_fred_cache("BAA"),
                "aaa": load_fred_cache("AAA"),
            }
        return self._cached_data
    
    def _compute_features(self, data: Dict[str, pd.Series], as_of: pd.Timestamp) -> Dict[str, float]:
        """Compute all features needed for micro-state classification as of date."""
        cfg = self.config
        
        # CPI YoY (with PIT lag)
        cpi = data["cpi"]
        if len(cpi) > 12:
            cpi_yoy = cpi.pct_change(12, fill_method=None).iloc[-1] * 100
        else:
            cpi_yoy = 2.0
        
        # Core CPI
        core_cpi = data.get("core_cpi")
        if core_cpi is not None and len(core_cpi) > 12:
            core_cpi_yoy = core_cpi.pct_change(12, fill_method=None).iloc[-1] * 100
        else:
            core_cpi_yoy = cpi_yoy
        
        # Unemployment for Sahm Rule
        unrate = data["unrate"]
        if len(unrate) > 12:
            unrate_3m = unrate.rolling(3).mean().iloc[-1]
            unrate_12m_low = unrate.rolling(12).min().iloc[-1]
            sahm_signal = unrate_3m - unrate_12m_low
        else:
            sahm_signal = 0.0
        
        # Real rate (10Y TIPS)
        real_rate = data["dfii10"].iloc[-1] if len(data["dfii10"]) > 0 else 1.5
        
        # Yield curve
        dgs10 = data["dgs10"]
        dgs2 = data["dgs2"]
        curve = (dgs10.iloc[-1] - dgs2.iloc[-1]) if len(dgs10) > 0 and len(dgs2) > 0 else 100
        
        # Credit spreads
        hy_oas = data["hy_oas"].iloc[-1] if len(data["hy_oas"]) > 0 else 300
        baa = data["baa"].iloc[-1] if len(data["baa"]) > 0 else 5.0
        aaa = data["aaa"].iloc[-1] if len(data["aaa"]) > 0 else 4.0
        baa_aaa = baa - aaa
        
        # VIX and stress
        vix = data["vix"].iloc[-1] if len(data["vix"]) > 0 else 20
        stl_fsi = data["stl_fsi"].iloc[-1] if len(data["stl_fsi"]) > 0 else 0
        
        return {
            "cpi_yoy": cpi_yoy,
            "core_cpi_yoy": core_cpi_yoy,
            "sahm_signal": sahm_signal,
            "real_rate": real_rate,
            "curve_10y2y": curve,
            "hy_oas": hy_oas,
            "baa_aaa_spread": baa_aaa,
            "vix": vix,
            "stl_fsi": stl_fsi,
            "as_of": pd.Timestamp.now(),
        }
    
    def classify(self, features: Dict[str, float]) -> Tuple[MicroState, Dict[str, float]]:
        """
        Classify current micro-state based on economic features.
        Returns (micro_state, scores_dict).
        """
        cfg = self.config
        f = features
        
        scores = {
            MicroState.GOLDILOCKS: 0.0,
            MicroState.LATE_CYCLE_DRAG: 0.0,
            MicroState.LIQUIDITY_CRUNCH: 0.0,
            MicroState.STAGFLATION: 0.0,
        }
        
        # GOLDILOCKS: Low inflation, healthy credit
        if (f["cpi_yoy"] <= cfg.goldilocks_inflation_max and
            f["real_rate"] <= cfg.goldilocks_real_rate_max and
            f["hy_oas"] <= cfg.goldilocks_spread_max and
            f["vix"] <= cfg.goldilocks_vix_max and
            f["curve_10y2y"] >= cfg.goldilocks_curve_min):
            scores[MicroState.GOLDILOCKS] = 1.0
        
        # LATE_CYCLE_DRAG: Inverted curve + tightening
        if (f["curve_10y2y"] <= cfg.late_cycle_curve_max and
            f["hy_oas"] >= cfg.late_cycle_spread_min):
            scores[MicroState.LATE_CYCLE_DRAG] = 1.0
        
        # LIQUIDITY_CRUNCH: High real rates + credit freeze
        if (f["real_rate"] >= cfg.crunch_real_rate_min and
            f["hy_oas"] >= cfg.crunch_spread_min and
            f["stl_fsi"] >= cfg.crunch_stl_fsi_min):
            scores[MicroState.LIQUIDITY_CRUNCH] = 1.0
        
        # STAGFLATION: Hot inflation + supply shock
        if (f["cpi_yoy"] >= cfg.stagflation_cpi_min and
            f["real_rate"] >= cfg.stagflation_real_rate_min):
            scores[MicroState.STAGFLATION] = 1.0
        
        # Normalize scores (softmax)
        score_values = np.array(list(scores.values()))
        if score_values.sum() > 0:
            score_values = softmax(score_values * 5)  # Temperature=5 for sharper
        else:
            score_values = np.array([0.25, 0.25, 0.25, 0.25])
        
        scores = dict(zip(scores.keys(), score_values))
        best_state = max(scores, key=scores.get)
        
        # Audit trail
        self.history.append({
            "features": features.copy(),
            "scores": scores.copy(),
            "state": best_state,
            "as_of": pd.Timestamp.now(),
        })
        
        return best_state, scores
    
    def get_pipeline_actions(self, state: MicroState) -> Dict:
        """Get pipeline actions for a given micro-state."""
        actions = {
            MicroState.GOLDILOCKS: {
                "action": "LOWER_HURDLE_RATES",
                "description": "Lower hurdle rates (WACC), extend competitive runways, maximize high-ROIC growth exposure",
                "wacc_adjustment": -0.5,  # bps
                "capex_extension": True,
                "growth_allocation": "MAXIMIZE",
            },
            MicroState.LATE_CYCLE_DRAG: {
                "action": "TIGHTEN_ENTRY_TRIM_CYCLICALS",
                "description": "Tighten entry thresholds, trim high-debt cyclicals, raise quality bar",
                "wacc_adjustment": 0.25,
                "debt_cap": True,
                "cyclical_trim": True,
            },
            MicroState.LIQUIDITY_CRUNCH: {
                "action": "DOWN_FILTER_SHIFT_TO_BIL",
                "description": "Downside shock filter triggers, shift to BIL, enforce cash flow penalties",
                "lambda_stress_mult": 2.0,
                "cash_flow_penalty": True,
                "bil_allocation": "MAXIMIZE",
            },
            MicroState.STAGFLATION: {
                "action": "ROUTE_CASH_TO_GOLD",
                "description": "Route defensive capital from cash to Gold/tangible assets",
                "gold_allocation": "INCREASE",
                "cash_penalty": True,
                "tangible_assets": "INCREASE",
            },
        }
        return actions.get(self._last_state, {})
    
    def _set_last_state(self, state: MicroState):
        self._last_state = state
    
    def classify_current(self) -> Tuple[MicroState, Dict]:
        """Classify current micro-state using latest FRED data."""
        data = self._load_economic_data()
        features = self._compute_features(data, pd.Timestamp.now())
        state, scores = self.classify(features)
        self._set_last_state(state)
        return state, scores


def build_micro_classifier(config: Optional[MicroStateConfig] = None) -> MicroStateClassifier:
    """Factory function to create micro-state classifier."""
    return MicroStateClassifier(config)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    classifier = build_micro_classifier()
    state, scores = classifier.classify_current()
    
    print(f"Current micro-state: {state.value}")
    print(f"Scores: {scores}")
    print(f"Pipeline actions: {classifier.get_pipeline_actions(state)}")