"""
sensitivity_vector.py — Relative Sensitivity Vector Engine

Implements Damodaran-style relative tracking by normalizing raw risk metrics
into a uniform [0, 1] sensitivity space via Min-Max feature scaling with
linear clip guards.

Raw metrics are mapped through:
    S = clip(0, 1, (X - Xmin) / (Xmax - Xmin))

Three risk dimensions are computed:
    S_hhi   — Fund concentration sensitivity (higher HHI = more fragile)
    S_icr   — Credit/solvency sensitivity (higher ICR = LESS fragile, inverted)
    S_macro — Macro regime sensitivity (higher inflation = more safe-haven demand)

A discrete interval classifier maps each S value to a qualitative risk label
for downstream allocation dispatch.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bounds Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SensitivityBounds:
    """Defines the normalization range for a single risk metric.

    Attributes:
        xmin: Raw lower bound (maps to S=0.0)
        xmax: Raw upper bound (maps to S=1.0)
        invert: If True, higher raw values map to LOWER S
                (used for ICR where high = safe).
    """
    xmin: float
    xmax: float
    invert: bool = False


# Default bounds tuned to observed fund universe data
DEFAULT_BOUNDS: Dict[str, SensitivityBounds] = {
    "hhi": SensitivityBounds(xmin=0.010, xmax=0.065, invert=False),
    "icr": SensitivityBounds(xmin=0.5, xmax=15.0, invert=True),
    "macro_inflation": SensitivityBounds(xmin=0.0, xmax=8.0, invert=False),
}


# ---------------------------------------------------------------------------
# Risk Label Classifier
# ---------------------------------------------------------------------------

class RiskLabel(Enum):
    """Discrete interval classifier for sensitivity scores.

    Maps [0, 1] S-values to Peter Lynch-style qualitative risk descriptions
    that drive downstream allocation dispatch.
    """
    INSULATED = "INSULATED"
    MODERATE_EXPOSURE = "MODERATE_EXPOSURE"
    ELEVATED_FRAGILITY = "ELEVATED_FRAGILITY"
    ACUTE_CLIFF_ALERT = "ACUTE_CLIFF_ALERT"


# Interval boundaries (lower-inclusive, upper-exclusive for first three)
_RISK_INTERVALS = [
    (0.00, 0.20, RiskLabel.INSULATED),
    (0.20, 0.45, RiskLabel.MODERATE_EXPOSURE),
    (0.45, 0.75, RiskLabel.ELEVATED_FRAGILITY),
    (0.75, 1.01, RiskLabel.ACUTE_CLIFF_ALERT),  # 1.01 to include 1.0
]


def classify_risk(s_value: float) -> RiskLabel:
    """Map a sensitivity score to its qualitative risk label."""
    for lo, hi, label in _RISK_INTERVALS:
        if lo <= s_value < hi:
            return label
    return RiskLabel.ACUTE_CLIFF_ALERT


# Allocation dispatch profiles per risk label
ALLOCATION_DISPATCH: Dict[RiskLabel, Dict[str, str]] = {
    RiskLabel.INSULATED: {
        "action": "HOLD_BASELINE",
        "description": "Safe baseline. No extra protective buffer overlay required.",
        "equity_tilt": "none",
    },
    RiskLabel.MODERATE_EXPOSURE: {
        "action": "HOLD_BASELINE",
        "description": "Balanced stance. Maintain normal tactical baseline allocations.",
        "equity_tilt": "none",
    },
    RiskLabel.ELEVATED_FRAGILITY: {
        "action": "TRIM_EQUITY",
        "description": "Risk concentration flagged. Begin trimming equity exposure.",
        "equity_tilt": "reduce",
    },
    RiskLabel.ACUTE_CLIFF_ALERT: {
        "action": "FLIGHT_TO_SAFETY",
        "description": "Structural danger detected. Trigger flight-to-safety protocol.",
        "equity_tilt": "strong_reduce",
    },
}


# ---------------------------------------------------------------------------
# Sensitivity Vector Dataclass
# ---------------------------------------------------------------------------

@dataclass
class SensitivityVector:
    """Computed sensitivity vector for a single ticker.

    Contains both raw and normalized values for full auditability.
    """
    ticker: str

    # Raw inputs
    raw_hhi: float
    raw_icr: float
    raw_macro: float

    # Normalized [0, 1] sensitivity scores
    s_hhi: float
    s_icr: float
    s_macro: float

    # Weighted composite
    composite: float

    # Qualitative classifiers per dimension
    label_hhi: RiskLabel
    label_icr: RiskLabel
    label_macro: RiskLabel
    label_composite: RiskLabel

    # Allocation dispatch profile for the composite
    dispatch: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        """Serialize for downstream consumers (OrderDraftGenerator, dashboard)."""
        return {
            "ticker": self.ticker,
            "raw": {"hhi": self.raw_hhi, "icr": self.raw_icr, "macro": self.raw_macro},
            "sensitivity": {"hhi": self.s_hhi, "icr": self.s_icr, "macro": self.s_macro},
            "composite": self.composite,
            "labels": {
                "hhi": self.label_hhi.value,
                "icr": self.label_icr.value,
                "macro": self.label_macro.value,
                "composite": self.label_composite.value,
            },
            "dispatch": self.dispatch,
        }


# ---------------------------------------------------------------------------
# Sensitivity Engine
# ---------------------------------------------------------------------------

class SensitivityEngine:
    """Normalizes raw risk metrics into a unified [0, 1] sensitivity space.

    Usage:
        engine = SensitivityEngine()
        vec = engine.compute(
            hhi=0.0225,
            icr=8.5,
            inflation=3.2,
            ticker="QQQ",
        )
        print(vec.composite)   # 0.31
        print(vec.label_composite)  # RiskLabel.MODERATE_EXPOSURE
    """

    def __init__(
        self,
        bounds: Optional[Dict[str, SensitivityBounds]] = None,
        weights: Optional[Dict[str, float]] = None,
    ):
        self.bounds = bounds or dict(DEFAULT_BOUNDS)
        self.weights = weights or {"hhi": 0.35, "icr": 0.35, "macro": 0.30}

        # Validate weights sum to 1.0
        w_sum = sum(self.weights.values())
        if abs(w_sum - 1.0) > 1e-6:
            raise ValueError(
                f"Weights must sum to 1.0, got {w_sum:.6f}. "
                "Normalize before passing."
            )

    def normalize(self, raw: float, metric: str) -> float:
        """Min-Max normalize a raw value with linear clip guard.

        Formula:
            S = clip(0, 1, (X - Xmin) / (Xmax - Xmin))

        If invert=True on the bounds, the result is flipped: S = 1 - S.
        """
        bounds = self.bounds.get(metric)
        if bounds is None:
            logger.warning(f"SensitivityEngine: No bounds for metric '{metric}', returning 0.5")
            return 0.5

        if bounds.xmax == bounds.xmin:
            return 0.5

        s = (raw - bounds.xmin) / (bounds.xmax - bounds.xmin)

        # Linear clip guard
        s = max(0.0, min(1.0, s))

        # Invert if needed (e.g., high ICR = low risk)
        if bounds.invert:
            s = 1.0 - s

        return s

    def compute(
        self,
        hhi: float,
        icr: float,
        inflation: float,
        ticker: str,
    ) -> SensitivityVector:
        """Compute the full sensitivity vector for a ticker.

        Args:
            hhi: Fund-level Herfindahl-Hirschman Index (concentration)
            icr: Aggregate Interest Coverage Ratio (solvency)
            inflation: Current macro inflation rate (e.g., 3.2 for 3.2%)
            ticker: Ticker symbol for labeling

        Returns:
            SensitivityVector with all normalized scores and labels
        """
        s_hhi = self.normalize(hhi, "hhi")
        s_icr = self.normalize(icr, "icr")
        s_macro = self.normalize(inflation, "macro_inflation")

        # Weighted composite
        composite = (
            self.weights["hhi"] * s_hhi
            + self.weights["icr"] * s_icr
            + self.weights["macro"] * s_macro
        )
        composite = max(0.0, min(1.0, composite))

        # Classify each dimension
        label_hhi = classify_risk(s_hhi)
        label_icr = classify_risk(s_icr)
        label_macro = classify_risk(s_macro)
        label_composite = classify_risk(composite)

        dispatch = dict(ALLOCATION_DISPATCH[label_composite])

        vec = SensitivityVector(
            ticker=ticker,
            raw_hhi=hhi,
            raw_icr=icr,
            raw_macro=inflation,
            s_hhi=s_hhi,
            s_icr=s_icr,
            s_macro=s_macro,
            composite=composite,
            label_hhi=label_hhi,
            label_icr=label_icr,
            label_macro=label_macro,
            label_composite=label_composite,
            dispatch=dispatch,
        )

        logger.info(
            f"SensitivityVector[{ticker}]: "
            f"S_hhi={s_hhi:.3f}({label_hhi.value}) "
            f"S_icr={s_icr:.3f}({label_icr.value}) "
            f"S_macro={s_macro:.3f}({label_macro.value}) "
            f"composite={composite:.3f}({label_composite.value})"
        )

        return vec

    def adjust_allocations(
        self,
        base_alloc: Dict[str, float],
        vectors: Dict[str, SensitivityVector],
        risk_tolerance: float = 0.3,
    ) -> Dict[str, float]:
        """Adjust target allocations based on sensitivity vectors.

        Positions with composite S above the portfolio-weighted average are
        scaled down; the freed capital is redistributed to below-average positions.

        Args:
            base_alloc: Original target allocation percentages (must sum to 1.0)
            vectors: SensitivityVector keyed by ticker
            risk_tolerance: 0.0 = no adjustment, 1.0 = maximum tilt

        Returns:
            Adjusted allocation percentages (sums to 1.0)
        """
        if risk_tolerance <= 0.0:
            return dict(base_alloc)

        # Compute portfolio-weighted average composite S
        total_weight = sum(base_alloc.get(t, 0.0) for t in vectors)
        if total_weight <= 0:
            return dict(base_alloc)

        portfolio_avg_s = sum(
            base_alloc.get(t, 0.0) * vectors[t].composite
            for t in vectors
        ) / total_weight

        # Compute adjustment magnitudes
        adjustments: Dict[str, float] = {}
        for ticker, alloc in base_alloc.items():
            if ticker not in vectors:
                adjustments[ticker] = 0.0
                continue
            vec = vectors[ticker]
            deviation = vec.composite - portfolio_avg_s
            adjustments[ticker] = -deviation * risk_tolerance

        # Apply adjustments, ensuring non-negative allocations
        adjusted: Dict[str, float] = {}
        for ticker, alloc in base_alloc.items():
            adj = adjustments.get(ticker, 0.0)
            adjusted[ticker] = max(0.0, alloc + adj)

        # Renormalize to sum to 1.0
        total = sum(adjusted.values())
        if total > 0:
            adjusted = {k: v / total for k, v in adjusted.items()}

        return adjusted
