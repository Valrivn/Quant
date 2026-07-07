import pytest
from Quantitative.company_classifier import (
    CompanyClassifier,
    CompanyMetrics,
    LynchCategory,
    DamodaranStage,
    ValuationModel
)


def test_classify_fast_grower_young():
    metrics = CompanyMetrics(
        ticker="GROW",
        revenue_growth=0.35,
        operating_margin=-0.05,
        fcf=-1000000.0,
        market_cap=50000000.0,
        dividend_payout_ratio=0.0,
        sector="technology",
        margin_variance_10y=0.02,
        debt_to_capital=0.1,
        interest_coverage_ratio=5.0,
        synthetic_rating="A",
        cash_balance=10000000.0,
        ebitda=-500000.0,
        price_to_book=5.0,
        assets_liquidation_value=5000000.0,
        total_debt=1000000.0
    )
    result = CompanyClassifier.classify(metrics)
    assert result.primary_lynch == LynchCategory.FAST_GROWER
    assert result.primary_damodaran == DamodaranStage.YOUNG_GROWTH
    assert result.primary_model == ValuationModel.TOP_DOWN_DCF
    assert result.fuzzy_weights[LynchCategory.FAST_GROWER.value] > 0.5


def test_classify_fast_grower_scaling():
    metrics = CompanyMetrics(
        ticker="SCALE",
        revenue_growth=0.28,
        operating_margin=0.15,
        fcf=5000000.0,
        market_cap=500000000.0,
        dividend_payout_ratio=0.0,
        sector="technology",
        margin_variance_10y=0.03,
        debt_to_capital=0.15,
        interest_coverage_ratio=15.0,
        synthetic_rating="AA",
        cash_balance=50000000.0,
        ebitda=20000000.0,
        price_to_book=8.0,
        assets_liquidation_value=30000000.0,
        total_debt=10000000.0
    )
    result = CompanyClassifier.classify(metrics)
    assert result.primary_lynch == LynchCategory.FAST_GROWER
    assert result.primary_damodaran == DamodaranStage.HIGH_GROWTH
    assert result.primary_model == ValuationModel.MULTI_STAGE_DCF


def test_classify_stalwart():
    metrics = CompanyMetrics(
        ticker="STAL",
        revenue_growth=0.11,
        operating_margin=0.18,
        fcf=120000000.0,
        market_cap=12000000000.0,
        dividend_payout_ratio=0.35,
        sector="consumer_goods",
        margin_variance_10y=0.01,
        debt_to_capital=0.3,
        interest_coverage_ratio=8.0,
        synthetic_rating="AA",
        cash_balance=500000000.0,
        ebitda=1000000000.0,
        price_to_book=3.0,
        assets_liquidation_value=4000000000.0,
        total_debt=2000000000.0
    )
    result = CompanyClassifier.classify(metrics)
    assert result.primary_lynch == LynchCategory.STALWART
    assert result.primary_damodaran == DamodaranStage.MATURE_GROWTH
    assert result.primary_model == ValuationModel.MULTI_STAGE_DCF


def test_classify_slow_grower():
    metrics = CompanyMetrics(
        ticker="SLOW",
        revenue_growth=0.02,
        operating_margin=0.08,
        fcf=30000000.0,
        market_cap=500000000.0,
        dividend_payout_ratio=0.6,
        sector="utilities",
        margin_variance_10y=0.005,
        debt_to_capital=0.45,
        interest_coverage_ratio=4.0,
        synthetic_rating="A",
        cash_balance=20000000.0,
        ebitda=60000000.0,
        price_to_book=1.1,
        assets_liquidation_value=400000000.0,
        total_debt=300000000.0
    )
    result = CompanyClassifier.classify(metrics)
    assert result.primary_lynch == LynchCategory.SLOW_GROWER
    assert result.primary_damodaran == DamodaranStage.MATURE_STABLE
    assert result.primary_model == ValuationModel.GORDON_GROWTH


def test_classify_cyclical_by_sector():
    metrics = CompanyMetrics(
        ticker="CYCL",
        revenue_growth=0.08,
        operating_margin=0.05,
        fcf=20000000.0,
        market_cap=1500000000.0,
        dividend_payout_ratio=0.2,
        sector="steel",
        margin_variance_10y=0.03,
        debt_to_capital=0.25,
        interest_coverage_ratio=5.0,
        synthetic_rating="BBB",
        cash_balance=100000000.0,
        ebitda=120000000.0,
        price_to_book=1.5,
        assets_liquidation_value=800000000.0,
        total_debt=400000000.0
    )
    result = CompanyClassifier.classify(metrics)
    assert result.primary_lynch == LynchCategory.CYCLICAL
    assert result.primary_damodaran == DamodaranStage.MATURE_GROWTH
    assert result.primary_model == ValuationModel.NORMALIZED_EARNINGS_DCF


def test_classify_cyclical_by_variance():
    metrics = CompanyMetrics(
        ticker="VOL",
        revenue_growth=0.06,
        operating_margin=0.04,
        fcf=10000000.0,
        market_cap=800000000.0,
        dividend_payout_ratio=0.1,
        sector="unrelated_retail",
        margin_variance_10y=0.15,  # High variance
        debt_to_capital=0.20,
        interest_coverage_ratio=4.5,
        synthetic_rating="BBB",
        cash_balance=50000000.0,
        ebitda=70000000.0,
        price_to_book=1.2,
        assets_liquidation_value=300000000.0,
        total_debt=150000000.0
    )
    result = CompanyClassifier.classify(metrics)
    assert result.primary_lynch == LynchCategory.CYCLICAL
    assert result.primary_model == ValuationModel.NORMALIZED_EARNINGS_DCF


def test_classify_turnaround():
    metrics = CompanyMetrics(
        ticker="BURN",
        revenue_growth=-0.15,
        operating_margin=-0.25,
        fcf=-8000000.0,
        market_cap=20000000.0,
        dividend_payout_ratio=0.0,
        sector="retail",
        margin_variance_10y=0.04,
        debt_to_capital=0.85,  # Extreme debt
        interest_coverage_ratio=0.2,  # Low coverage
        synthetic_rating="CCC",
        cash_balance=1500000.0,
        ebitda=-3000000.0,  # Negative EBITDA
        price_to_book=0.8,
        assets_liquidation_value=12000000.0,
        total_debt=18000000.0
    )
    result = CompanyClassifier.classify(metrics)
    assert result.primary_lynch == LynchCategory.TURNAROUND
    assert result.primary_damodaran == DamodaranStage.DECLINE
    assert result.primary_model == ValuationModel.DISTRESS_ADJUSTED_DCF


def test_classify_asset_play():
    metrics = CompanyMetrics(
        ticker="LAND",
        revenue_growth=0.01,
        operating_margin=0.02,
        fcf=5000000.0,
        market_cap=50000000.0,
        dividend_payout_ratio=0.0,
        sector="holding",
        margin_variance_10y=0.01,
        debt_to_capital=0.10,
        interest_coverage_ratio=6.0,
        synthetic_rating="A",
        cash_balance=10000000.0,
        ebitda=2000000.0,
        price_to_book=0.45,  # Massive discount to book
        assets_liquidation_value=90000000.0,
        total_debt=10000000.0
    )
    result = CompanyClassifier.classify(metrics)
    assert result.primary_lynch == LynchCategory.ASSET_PLAY
    assert result.primary_damodaran == DamodaranStage.DECLINE
    assert result.primary_model == ValuationModel.ASSET_BASED_LIQUIDATION
