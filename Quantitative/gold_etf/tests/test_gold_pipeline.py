"""Tests for Quantitative Gold ETF sub-engine modules."""
import math
import pytest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from Quantitative.gold_etf.spot_gold_tracker import (
    SpotGoldTracker, SpotGoldResult, SpotGoldObservation
)
from Quantitative.gold_etf.gold_macro_valuation import (
    GoldMacroValuation, GoldMacroSignal, MacroMetrics, GoldMacroValuationResult
)
from Quantitative.gold_etf.gold_etf_screener import (
    GoldETFScreener, GoldVerdict, TrackingMetrics, GoldScreenerResult
)


# ── SpotGoldTracker Tests ─────────────────────────────────────────────────

class TestSpotGoldTrackerUnit:
    def test_compute_log_returns(self):
        tracker = SpotGoldTracker()
        prices = [100.0, 102.0, 101.0, 103.0]
        returns = tracker._compute_log_returns(prices)
        assert returns[0] is None
        assert returns[1] == pytest.approx(math.log(102.0 / 100.0), abs=1e-10)
        assert returns[2] == pytest.approx(math.log(101.0 / 102.0), abs=1e-10)
        assert returns[3] == pytest.approx(math.log(103.0 / 101.0), abs=1e-10)

    def test_compute_log_returns_edge_cases(self):
        tracker = SpotGoldTracker()
        assert tracker._compute_log_returns([]) == []
        assert tracker._compute_log_returns([100.0]) == [None]
        assert tracker._compute_log_returns([0.0, 100.0]) == [None, None]

    def test_spot_result_to_dict(self):
        result = SpotGoldResult(
            observations=[SpotGoldObservation(date="2024-01-01", price=2000.0)],
            latest_price=2000.0,
            latest_date="2024-01-01",
            annualized_return=0.08,
            annualized_volatility=0.15,
        )
        d = result.to_dict()
        assert d["latest_price"] == 2000.0
        assert d["annualized_return"] == 0.08


# ── GoldMacroValuation Tests ──────────────────────────────────────────────

class TestGoldMacroValuationUnit:
    def test_real_rate_component_high(self):
        valuation = GoldMacroValuation(real_rate_high_threshold=2.0, real_rate_low_threshold=0.0)
        assert valuation._compute_real_rate_component(3.0) == 1.0
        assert valuation._compute_real_rate_component(2.0) == 1.0

    def test_real_rate_component_low(self):
        valuation = GoldMacroValuation(real_rate_high_threshold=2.0, real_rate_low_threshold=0.0)
        assert valuation._compute_real_rate_component(-1.0) == 0.0
        assert valuation._compute_real_rate_component(0.0) == 0.0

    def test_real_rate_component_mid(self):
        valuation = GoldMacroValuation(real_rate_high_threshold=2.0, real_rate_low_threshold=0.0)
        assert valuation._compute_real_rate_component(1.0) == pytest.approx(0.5)

    def test_m2_component_high(self):
        valuation = GoldMacroValuation(m2_growth_threshold=10.0)
        assert valuation._compute_m2_component(15.0) == 1.0
        assert valuation._compute_m2_component(10.0) == 1.0

    def test_m2_component_zero(self):
        valuation = GoldMacroValuation(m2_growth_threshold=10.0)
        assert valuation._compute_m2_component(-1.0) == 0.0
        assert valuation._compute_m2_component(0.0) == 0.0

    def test_m2_component_mid(self):
        valuation = GoldMacroValuation(m2_growth_threshold=10.0)
        assert valuation._compute_m2_component(5.0) == pytest.approx(0.5)

    def test_classify_signal_undervalued(self):
        valuation = GoldMacroValuation()
        assert valuation._classify_signal(0.7) == GoldMacroSignal.UNDERVALUED
        assert valuation._classify_signal(0.6) == GoldMacroSignal.UNDERVALUED

    def test_classify_signal_fair_value(self):
        valuation = GoldMacroValuation()
        assert valuation._classify_signal(0.45) == GoldMacroSignal.FAIR_VALUE
        assert valuation._classify_signal(0.31) == GoldMacroSignal.FAIR_VALUE

    def test_classify_signal_overvalued(self):
        valuation = GoldMacroValuation()
        assert valuation._classify_signal(0.3) == GoldMacroSignal.OVERVALUED
        assert valuation._classify_signal(0.1) == GoldMacroSignal.OVERVALUED

    def test_macro_valuation_result_to_dict(self):
        result = GoldMacroValuationResult(
            signal=GoldMacroSignal.UNDERVALUED,
            metrics=MacroMetrics(real_rate_10y=2.5, m2_yoy_growth=12.0),
            composite_score=0.85,
        )
        d = result.to_dict()
        assert d["signal"] == "UNDERVALUED"
        assert d["composite_score"] == 0.85


# ── GoldETFScreener Tests ─────────────────────────────────────────────────

class TestGoldETFScreenerUnit:
    def test_tracking_metrics_computation(self):
        screener = GoldETFScreener()
        import numpy as np
        np.random.seed(42)
        etf_returns = np.random.normal(0.0005, 0.01, 100).tolist()
        spot_returns = [r + np.random.normal(0, 0.0001) for r in etf_returns]
        metrics = screener._compute_tracking_metrics(
            etf_returns, spot_returns,
            etf_annual_return=0.08, spot_annual_return=0.079,
            expense_ratio=0.0025,
        )
        assert metrics.tracking_error < 0.05
        assert metrics.correlation > 0.99
        assert metrics.aligned_observations == 100

    def test_tracking_metrics_insufficient_data(self):
        screener = GoldETFScreener()
        metrics = screener._compute_tracking_metrics(
            [0.01] * 10, [0.01] * 10,
            etf_annual_return=None, spot_annual_return=None,
            expense_ratio=None,
        )
        assert metrics.aligned_observations == 10
        assert metrics.tracking_error is None

    def test_gold_screener_result_to_dict(self):
        result = GoldScreenerResult(
            ticker="IAU",
            verdict=GoldVerdict.SELECTED,
            tracking_metrics=TrackingMetrics(
                tracking_error=0.005,
                correlation=0.998,
                aligned_observations=250,
            ),
            macro_signal=GoldMacroSignal.FAIR_VALUE,
        )
        d = result.to_dict()
        assert d["ticker"] == "IAU"
        assert d["verdict"] == "SELECTED"
        assert d["correlation"] == 0.998

    def test_reject_low_correlation(self):
        screener = GoldETFScreener(min_correlation=0.99)
        metrics = TrackingMetrics(
            tracking_error=0.005,
            correlation=0.95,
            aligned_observations=250,
        )
        assert metrics.correlation < screener._min_corr

    def test_reject_high_tracking_error(self):
        screener = GoldETFScreener(max_tracking_error=0.015)
        metrics = TrackingMetrics(
            tracking_error=0.05,
            correlation=0.999,
            aligned_observations=250,
        )
        assert metrics.tracking_error > screener._max_te


# ── Integration: Gold Pipeline Flow ───────────────────────────────────────

class TestGoldPipelineFlow:
    def test_gold_screener_no_data_returns_rejected(self):
        screener = GoldETFScreener()
        with patch.object(screener._fetcher, 'fetch_multiple') as mock_fetch, \
             patch.object(screener._macro_valuation, 'fetch_and_valuate') as mock_macro:
            mock_fetch.return_value = {}
            mock_macro.return_value = GoldMacroValuationResult(
                signal=GoldMacroSignal.FAIR_VALUE,
                metrics=MacroMetrics(real_rate_10y=1.5, m2_yoy_growth=5.0),
            )
            results = screener.screen_universe(["NONEXISTENT"])
            assert results["NONEXISTENT"].verdict == GoldVerdict.REJECTED_NO_DATA
