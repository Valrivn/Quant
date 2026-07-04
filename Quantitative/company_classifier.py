import logging
from enum import Enum
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class LynchCategory(Enum):
    FAST_GROWER = "FAST_GROWER"
    STALWART = "STALWART"
    SLOW_GROWER = "SLOW_GROWER"
    CYCLICAL = "CYCLICAL"
    TURNAROUND = "TURNAROUND"
    ASSET_PLAY = "ASSET_PLAY"


class DamodaranStage(Enum):
    STARTUP = "STARTUP"
    YOUNG_GROWTH = "YOUNG_GROWTH"
    HIGH_GROWTH = "HIGH_GROWTH"
    MATURE_GROWTH = "MATURE_GROWTH"
    MATURE_STABLE = "MATURE_STABLE"
    DECLINE = "DECLINE"


class ValuationModel(Enum):
    TOP_DOWN_DCF = "TOP_DOWN_DCF"
    MULTI_STAGE_DCF = "MULTI_STAGE_DCF"
    GORDON_GROWTH = "GORDON_GROWTH"
    DISTRESS_ADJUSTED_DCF = "DISTRESS_ADJUSTED_DCF"
    ASSET_BASED_LIQUIDATION = "ASSET_BASED_LIQUIDATION"
    NORMALIZED_EARNINGS_DCF = "NORMALIZED_EARNINGS_DCF"


@dataclass
class CompanyMetrics:
    ticker: str
    revenue_growth: float
    operating_margin: float
    fcf: float
    market_cap: float
    dividend_payout_ratio: float
    sector: str
    margin_variance_10y: float
    debt_to_capital: float
    interest_coverage_ratio: float
    synthetic_rating: str
    cash_balance: float
    ebitda: float
    price_to_book: float
    assets_liquidation_value: float
    total_debt: float


@dataclass
class ClassificationOutput:
    ticker: str
    primary_lynch: LynchCategory
    primary_damodaran: DamodaranStage
    primary_model: ValuationModel
    fuzzy_weights: Dict[str, float]  # Maps category name (e.g. "FAST_GROWER") to membership score (0.0 to 1.0)


class CompanyClassifier:
    CYCLICAL_SECTORS = {
        "autos", "airlines", "tires", "steel", "defense", 
        "chemicals", "natural_resources", "hardware_oem"
    }

    @staticmethod
    def _fuzzy_sigmoid(x: float, threshold: float, width: float) -> float:
        """Returns a fuzzy membership value [0.0, 1.0] using a sigmoid function."""
        import math
        try:
            return 1.0 / (1.0 + math.exp(-(x - threshold) / width))
        except OverflowError:
            return 1.0 if x > threshold else 0.0

    @classmethod
    def classify(cls, metrics: CompanyMetrics) -> ClassificationOutput:
        # Initialize fuzzy membership scores
        scores = {
            LynchCategory.FAST_GROWER.value: 0.0,
            LynchCategory.STALWART.value: 0.0,
            LynchCategory.SLOW_GROWER.value: 0.0,
            LynchCategory.CYCLICAL.value: 0.0,
            LynchCategory.TURNAROUND.value: 0.0,
            LynchCategory.ASSET_PLAY.value: 0.0
        }

        # 1. Cyclicality Score
        # Check sector list first
        is_cyclical_sector = metrics.sector.lower() in cls.CYCLICAL_SECTORS
        sector_score = 1.0 if is_cyclical_sector else 0.0
        # Check margin variance
        margin_score = cls._fuzzy_sigmoid(metrics.margin_variance_10y, 0.08, 0.02)
        scores[LynchCategory.CYCLICAL.value] = max(sector_score, margin_score)

        # 2. Turnaround / Distressed Score
        # Check leverage and coverage
        debt_score = cls._fuzzy_sigmoid(metrics.debt_to_capital, 0.70, 0.05)
        coverage_score = 1.0 - cls._fuzzy_sigmoid(metrics.interest_coverage_ratio, 1.0, 0.2)
        rating_score = 1.0 if metrics.synthetic_rating in ["CCC", "CC", "C", "D"] else 0.0
        # Cash burn runway calculation
        if metrics.ebitda < 0 and metrics.cash_balance > 0:
            cash_burn_months = (metrics.cash_balance / abs(metrics.ebitda)) * 12.0
            runway_score = 1.0 - cls._fuzzy_sigmoid(cash_burn_months, 6.0, 2.0)
        else:
            runway_score = 0.0
        scores[LynchCategory.TURNAROUND.value] = max(debt_score * coverage_score, rating_score, runway_score)

        # 3. Asset Play Score
        # Mismatch of dying core business (low growth) + high asset value vs market cap
        low_growth_factor = 1.0 - cls._fuzzy_sigmoid(metrics.revenue_growth, 0.05, 0.02)
        low_pb_factor = 1.0 - cls._fuzzy_sigmoid(metrics.price_to_book, 1.0, 0.1)
        scores[LynchCategory.ASSET_PLAY.value] = 1.2 * low_growth_factor * low_pb_factor * (1.0 - scores[LynchCategory.TURNAROUND.value])

        # 4. Growth Categories (Fast Grower, Stalwart, Slow Grower)
        # Fast Grower: Growth >= 20%
        fast_growth_score = cls._fuzzy_sigmoid(metrics.revenue_growth, 0.20, 0.03)
        scores[LynchCategory.FAST_GROWER.value] = fast_growth_score * (1.0 - scores[LynchCategory.TURNAROUND.value])

        # Stalwart: Growth 10% to 12% (range 8% to 15%)
        # Normal distribution bell-curve logic centered around 11%
        import math
        growth_diff = metrics.revenue_growth - 0.11
        stalwart_growth_score = math.exp(-(growth_diff ** 2) / 0.002)
        scores[LynchCategory.STALWART.value] = stalwart_growth_score * (1.0 - scores[LynchCategory.CYCLICAL.value])

        # Slow Grower: Growth <= 3%
        slow_growth_score = 1.0 - cls._fuzzy_sigmoid(metrics.revenue_growth, 0.03, 0.01)
        scores[LynchCategory.SLOW_GROWER.value] = slow_growth_score * (1.0 - scores[LynchCategory.TURNAROUND.value])

        # Normalize fuzzy weights so they sum to 1.0
        total_score = sum(scores.values())
        if total_score > 0:
            fuzzy_weights = {k: v / total_score for k, v in scores.items()}
        else:
            # Fallback to neutral stalwart
            fuzzy_weights = {k: 1.0 / len(scores) for k in scores.keys()}
            fuzzy_weights[LynchCategory.STALWART.value] = 0.5

        # Determine Primary Category
        primary_lynch = max(scores, key=scores.get)
        primary_lynch_enum = LynchCategory(primary_lynch)

        # Map to Damodaran Stages & Valuation Models
        if primary_lynch_enum == LynchCategory.FAST_GROWER:
            if metrics.operating_margin < 0 or metrics.fcf < 0:
                primary_damodaran = DamodaranStage.YOUNG_GROWTH
                primary_model = ValuationModel.TOP_DOWN_DCF
            else:
                primary_damodaran = DamodaranStage.HIGH_GROWTH
                primary_model = ValuationModel.MULTI_STAGE_DCF

        elif primary_lynch_enum == LynchCategory.STALWART:
            primary_damodaran = DamodaranStage.MATURE_GROWTH
            primary_model = ValuationModel.MULTI_STAGE_DCF

        elif primary_lynch_enum == LynchCategory.SLOW_GROWER:
            primary_damodaran = DamodaranStage.MATURE_STABLE
            primary_model = ValuationModel.GORDON_GROWTH

        elif primary_lynch_enum == LynchCategory.CYCLICAL:
            primary_damodaran = DamodaranStage.MATURE_GROWTH
            primary_model = ValuationModel.NORMALIZED_EARNINGS_DCF

        elif primary_lynch_enum == LynchCategory.TURNAROUND:
            primary_damodaran = DamodaranStage.DECLINE
            primary_model = ValuationModel.DISTRESS_ADJUSTED_DCF

        else:  # ASSET_PLAY
            primary_damodaran = DamodaranStage.DECLINE
            primary_model = ValuationModel.ASSET_BASED_LIQUIDATION

        return ClassificationOutput(
            ticker=metrics.ticker,
            primary_lynch=primary_lynch_enum,
            primary_damodaran=primary_damodaran,
            primary_model=primary_model,
            fuzzy_weights=fuzzy_weights
        )
