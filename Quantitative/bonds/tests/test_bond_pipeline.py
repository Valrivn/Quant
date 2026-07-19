"""Tests for Quantitative Bond ETF sub-engine modules."""
import math
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from types import SimpleNamespace

from Quantitative.bonds.credit_spread_monitor import (
    CreditSpreadMonitor, SpreadRegime, SpreadDirection, SpreadRegimeResult
)
from Quantitative.bonds.corporate_look_through import (
    CorporateLookThrough, LookThroughResult, HoldingEntry, IssuerFinancials
)
from Quantitative.bonds.bond_etf_screener import (
    BondETFScreener, ScreenerVerdict, ZScoreResult, BondScreenerResult
)
from Quantitative.bonds.treasury_anchor import (
    TreasuryAnchor, AnchorMode, AnchorAllocation, TreasuryAnchorResult
)
from Quantitative.shared.etf_data_fetcher import ETFMetrics


# ── CreditSpreadMonitor Tests ──────────────────────────────────────────────

class TestCreditSpreadMonitorUnit:
    def test_classify_regime_normal(self):
        monitor = CreditSpreadMonitor(widening_threshold_bps=200.0, crisis_threshold_bps=300.0)
        assert monitor._classify_regime(150.0) == SpreadRegime.NORMAL
        assert monitor._classify_regime(199.9) == SpreadRegime.NORMAL

    def test_classify_regime_widening(self):
        monitor = CreditSpreadMonitor(widening_threshold_bps=200.0, crisis_threshold_bps=300.0)
        assert monitor._classify_regime(201.0) == SpreadRegime.WIDENING
        assert monitor._classify_regime(250.0) == SpreadRegime.WIDENING
        assert monitor._classify_regime(299.9) == SpreadRegime.WIDENING

    def test_classify_regime_crisis(self):
        monitor = CreditSpreadMonitor(widening_threshold_bps=200.0, crisis_threshold_bps=300.0)
        assert monitor._classify_regime(301.0) == SpreadRegime.CRISIS
        assert monitor._classify_regime(500.0) == SpreadRegime.CRISIS

    def test_detect_direction_stable(self):
        monitor = CreditSpreadMonitor()
        spreads = [150.0] * 30
        assert monitor._detect_direction(spreads) == SpreadDirection.STABLE

    def test_detect_direction_widening(self):
        monitor = CreditSpreadMonitor(direction_lookback=10)
        spreads = [150.0 + i * 5.0 for i in range(20)]
        assert monitor._detect_direction(spreads) == SpreadDirection.WIDENING

    def test_detect_direction_tightening(self):
        monitor = CreditSpreadMonitor(direction_lookback=10)
        spreads = [300.0 - i * 5.0 for i in range(20)]
        assert monitor._detect_direction(spreads) == SpreadDirection.TIGHTENING

    def test_detect_direction_insufficient_data(self):
        monitor = CreditSpreadMonitor()
        assert monitor._detect_direction([]) == SpreadDirection.STABLE
        assert monitor._detect_direction([150.0]) == SpreadDirection.STABLE

    def test_compute_spread_series(self):
        monitor = CreditSpreadMonitor()
        from Quantitative.shared.fred_scraper import FREDDataPoint
        baa = [FREDDataPoint(date="2024-01-01", value=5.0, series_id="BAA10Y"),
               FREDDataPoint(date="2024-01-02", value=5.5, series_id="BAA10Y")]
        treasury = [FREDDataPoint(date="2024-01-01", value=4.0, series_id="DGS10"),
                    FREDDataPoint(date="2024-01-02", value=4.2, series_id="DGS10")]
        spreads = monitor._compute_spread_series(baa, treasury)
        assert len(spreads) == 2
        assert spreads[0] == pytest.approx(100.0)  # (5.0 - 4.0) * 100
        assert spreads[1] == pytest.approx(130.0)  # (5.5 - 4.2) * 100

    def test_spread_result_to_dict(self):
        result = SpreadRegimeResult(
            current_spread_bps=185.3,
            regime=SpreadRegime.NORMAL,
            direction=SpreadDirection.STABLE,
            spread_history_bps=[180.0, 185.3],
        )
        d = result.to_dict()
        assert d["regime"] == "NORMAL"
        assert d["current_spread_bps"] == 185.3


# ── CorporateLookThrough Tests ─────────────────────────────────────────────

class TestCorporateLookThroughUnit:
    def test_issuer_financials_icr_computation(self):
        fin = IssuerFinancials(
            company_name="Test Corp",
            cik="0000123456",
            ebit=10_000_000,
            interest_expense=5_000_000,
        )
        assert fin.icr == 2.0

    def test_issuer_financials_zero_interest(self):
        fin = IssuerFinancials(
            company_name="Test Corp",
            cik="0000123456",
            ebit=10_000_000,
            interest_expense=0,
        )
        assert fin.icr == 999.0

    def test_issuer_financials_no_data(self):
        fin = IssuerFinancials(company_name="Test Corp", cik="0000123456")
        assert fin.icr is None

    def test_look_through_result_to_dict(self):
        result = LookThroughResult(
            ticker="VCSH",
            holdings=[],
            issuer_financials={},
            weighted_icr=5.2,
            data_completeness=0.75,
        )
        d = result.to_dict()
        assert d["ticker"] == "VCSH"
        assert d["weighted_icr"] == 5.2
        assert d["data_completeness"] == 0.75


# ── BondETFScreener Tests ─────────────────────────────────────────────────

class TestBondETFScreenerUnit:
    def test_z_score_computation(self):
        z = ZScoreResult(
            metric_name="ICR",
            value=8.0,
            peer_mean=5.0,
            peer_std=2.0,
            z_score=1.5,
            gate_threshold=1.0,
            passed=True,
        )
        assert z.passed is True
        assert z.z_score >= z.gate_threshold

    def test_z_score_reject(self):
        z = ZScoreResult(
            metric_name="ICR",
            value=3.0,
            peer_mean=5.0,
            peer_std=2.0,
            z_score=-1.0,
            gate_threshold=1.0,
            passed=False,
        )
        assert z.passed is False

    def test_screener_result_to_dict(self):
        result = BondScreenerResult(
            ticker="VCSH",
            category="corporate",
            verdict=ScreenerVerdict.SELECTED,
        )
        d = result.to_dict()
        assert d["ticker"] == "VCSH"
        assert d["verdict"] == "SELECTED"

    def test_treasury_auto_pass(self):
        screener = BondETFScreener()
        metrics_map = {
            "BIL": ETFMetrics(ticker="BIL", avg_daily_volume=5_000_000, closing_price=91.5),
            "SHY": ETFMetrics(ticker="SHY", avg_daily_volume=3_000_000, closing_price=82.3),
        }
        with patch.object(screener._look_through, 'look_through') as mock_lt, \
             patch.object(screener._credit_monitor, 'fetch_and_classify') as mock_cs:
            mock_cs.return_value = SpreadRegimeResult(
                current_spread_bps=150.0,
                regime=SpreadRegime.NORMAL,
                direction=SpreadDirection.STABLE,
                spread_history_bps=[150.0],
            )
            results = screener.screen_universe(["BIL", "SHY"], metrics_map=metrics_map)
            assert results["BIL"].verdict == ScreenerVerdict.TREASURY_PASS
            assert results["SHY"].verdict == ScreenerVerdict.TREASURY_PASS

    def test_empty_universe(self):
        screener = BondETFScreener()
        with patch.object(screener._credit_monitor, 'fetch_and_classify') as mock_cs:
            mock_cs.return_value = SpreadRegimeResult(
                current_spread_bps=150.0,
                regime=SpreadRegime.NORMAL,
                direction=SpreadDirection.STABLE,
                spread_history_bps=[150.0],
            )
            results = screener.screen_universe([], metrics_map={})
            assert len(results) == 0


# ── TreasuryAnchor Tests ──────────────────────────────────────────────────

class TestTreasuryAnchorUnit:
    def test_full_anchor(self):
        monitor = MagicMock()
        monitor.fetch_and_classify.return_value = SpreadRegimeResult(
            current_spread_bps=250.0,
            regime=SpreadRegime.WIDENING,
            direction=SpreadDirection.STABLE,
            spread_history_bps=[250.0],
        )
        anchor = TreasuryAnchor(credit_monitor=monitor)
        result = anchor.deploy(
            corporate_selected=[],
            corporate_rejected=["VCSH", "VCIT"],
            bond_budget=0.30,
        )
        assert result.mode == AnchorMode.FULL_ANCHOR
        assert len(result.allocations) == 2
        total_weight = sum(a.weight for a in result.allocations)
        assert total_weight == pytest.approx(0.30, abs=1e-6)

    def test_no_anchor(self):
        monitor = MagicMock()
        monitor.fetch_and_classify.return_value = SpreadRegimeResult(
            current_spread_bps=150.0,
            regime=SpreadRegime.NORMAL,
            direction=SpreadDirection.STABLE,
            spread_history_bps=[150.0],
        )
        anchor = TreasuryAnchor(credit_monitor=monitor)
        result = anchor.deploy(
            corporate_selected=["VCSH", "VCIT"],
            corporate_rejected=[],
            bond_budget=0.30,
        )
        assert result.mode == AnchorMode.NO_ANCHOR
        assert len(result.allocations) == 0

    def test_partial_anchor(self):
        monitor = MagicMock()
        monitor.fetch_and_classify.return_value = SpreadRegimeResult(
            current_spread_bps=220.0,
            regime=SpreadRegime.WIDENING,
            direction=SpreadDirection.STABLE,
            spread_history_bps=[220.0],
        )
        anchor = TreasuryAnchor(credit_monitor=monitor)
        result = anchor.deploy(
            corporate_selected=["VCSH"],
            corporate_rejected=["VCIT"],
            bond_budget=0.30,
        )
        assert result.mode == AnchorMode.PARTIAL_ANCHOR
        assert len(result.allocations) > 0

    def test_allocation_dict_property(self):
        result = TreasuryAnchorResult(
            mode=AnchorMode.FULL_ANCHOR,
            allocations=[
                AnchorAllocation(ticker="BIL", weight=0.12, rationale="test"),
                AnchorAllocation(ticker="SHY", weight=0.18, rationale="test"),
            ],
            spread_regime=SpreadRegime.WIDENING,
            corporate_selected=[],
            corporate_rejected=["VCSH"],
        )
        d = result.allocation_dict
        assert d["BIL"] == 0.12
        assert d["SHY"] == 0.18


# ── SensitivityVector Integration Tests ───────────────────────────────────

class TestSensitivityIntegration:
    def test_sensitivity_engine_compute(self):
        from Quantitative.sensitivity.sensitivity_vector import SensitivityEngine
        engine = SensitivityEngine()
        vec = engine.compute(hhi=0.0225, icr=8.5, inflation=3.2, ticker="TEST")
        assert 0.0 <= vec.composite <= 1.0
        assert vec.ticker == "TEST"

    def test_sensitivity_adjust_allocations(self):
        from Quantitative.sensitivity.sensitivity_vector import SensitivityEngine
        engine = SensitivityEngine()
        v1 = engine.compute(hhi=0.02, icr=10.0, inflation=2.0, ticker="SAFE")
        v2 = engine.compute(hhi=0.06, icr=2.0, inflation=6.0, ticker="RISKY")
        adjusted = engine.adjust_allocations(
            base_alloc={"SAFE": 0.5, "RISKY": 0.5},
            vectors={"SAFE": v1, "RISKY": v2},
            risk_tolerance=0.3,
        )
        assert adjusted["SAFE"] > 0.5
        assert adjusted["RISKY"] < 0.5
        assert abs(sum(adjusted.values()) - 1.0) < 1e-6
