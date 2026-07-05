import pytest
from Quantitative.shared.fred_scraper import FREDScraper
from Quantitative.shared.etf_data_fetcher import ETFDataFetcher
from Quantitative.bonds.liquidity_gatekeeper import LiquidityGatekeeper, GateResult, CriticalFlag

def test_fred_scraper_cache():
    # Verify that the scraper runs and handles data properly
    scraper = FREDScraper()
    # Pull BAA10Y which is a popular FRED series
    res = scraper.fetch_series("BAA10Y")
    assert res.series_id == "BAA10Y"
    assert res.title is not None
    # Cached or fresh should return data
    if res.observations:
        first = res.observations[0]
        assert first.date is not None
        assert isinstance(first.value, float)

def test_liquidity_gatekeeper_mock():
    # Test liquidity gatekeeper logic with mock metrics
    gatekeeper = LiquidityGatekeeper()
    
    from Quantitative.shared.etf_data_fetcher import ETFMetrics
    
    # 1. Test Passing Case
    pass_metrics = ETFMetrics(
        ticker="TEST_PASS",
        avg_daily_volume=1_500_000,
        median_bid_ask_spread=0.0001,  # 0.01%
        nav_premium_discount=0.0005,    # 0.05%
        expense_ratio=0.0010
    )
    res = gatekeeper.evaluate("TEST_PASS", metrics=pass_metrics)
    assert res.overall_pass is True
    assert res.critical_flag == CriticalFlag.NONE
    
    # 2. Test Volume Failure
    fail_vol_metrics = ETFMetrics(
        ticker="TEST_FAIL_VOL",
        avg_daily_volume=500_000,
        median_bid_ask_spread=0.0001,
        nav_premium_discount=0.0005,
        expense_ratio=0.0010
    )
    res = gatekeeper.evaluate("TEST_FAIL_VOL", metrics=fail_vol_metrics)
    assert res.overall_pass is False
    
    # 3. Test Critical Trigger (NAV discount below -0.50%)
    critical_metrics = ETFMetrics(
        ticker="TEST_CRITICAL",
        avg_daily_volume=2_000_000,
        median_bid_ask_spread=0.0001,
        nav_premium_discount=-0.0060,  # -0.60%
        expense_ratio=0.0010
    )
    res = gatekeeper.evaluate("TEST_CRITICAL", metrics=critical_metrics)
    assert res.overall_pass is False
    assert res.critical_flag == CriticalFlag.NAV_LIQUIDITY_FAILURE
