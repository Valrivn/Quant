import pytest
from psychological.qualitative_scoring import (
    FinancialReconstructionInterface,
    FinancialReconstructionResult,
    create_financial_reconstruction_interface,
)


class TestFinancialReconstructionInterface:
    @pytest.fixture
    def interface(self):
        return FinancialReconstructionInterface()

    def test_compute_rd_capitalisation_zero_expense(self, interface):
        cap_rate, life = interface.compute_rd_capitalisation(0.0, 5.0)
        assert cap_rate == 0.0
        assert life == 0.0

    def test_compute_rd_capitalisation_basic(self, interface):
        cap_rate, life = interface.compute_rd_capitalisation(100.0, 5.0)
        assert 0.0 <= cap_rate <= 1.0
        assert life == 5.0

    def test_compute_rd_capitalisation_with_history(self, interface):
        historical = [100.0, 90.0, 80.0, 70.0, 60.0]
        cap_rate, life = interface.compute_rd_capitalisation(100.0, 5.0, historical)
        assert 0.0 <= cap_rate <= 1.0
        assert life == 5.0

    def test_compute_rd_capitalisation_no_amortisation(self, interface):
        cap_rate, life = interface.compute_rd_capitalisation(100.0, 0.0)
        assert life == 0.0

    def test_sbc_drag_zero_revenue(self, interface):
        drag = interface.compute_sbc_drag(10.0, 0.0, 100_000_000, 150.0)
        assert drag == 0.0

    def test_sbc_drag_zero_shares(self, interface):
        drag = interface.compute_sbc_drag(10.0, 1000.0, 0, 150.0)
        assert drag == 0.0

    def test_sbc_drag_typical(self, interface):
        drag = interface.compute_sbc_drag(
            sbc_expense=500_000_000,
            revenue=10_000_000_000,
            shares_outstanding=1_000_000_000,
            share_price=150.0,
        )
        assert 0.0 <= drag <= 1.0

    def test_sbc_drag_high_intensity(self, interface):
        drag = interface.compute_sbc_drag(
            sbc_expense=2_000_000_000,
            revenue=1_000_000_000,
            shares_outstanding=100_000_000,
            share_price=50.0,
        )
        assert drag > 0.0

    def test_rd_efficiency_zero_rd(self, interface):
        eff = interface.compute_rd_efficiency(1000.0, 0.0, 600.0)
        assert eff == 0.5

    def test_rd_efficiency_high_margin(self, interface):
        eff = interface.compute_rd_efficiency(
            revenue=1000.0, rd_expense=50.0, gross_profit=800.0
        )
        assert 0.0 <= eff <= 1.0
        assert eff > 0.5

    def test_rd_efficiency_low_margin(self, interface):
        eff = interface.compute_rd_efficiency(
            revenue=1000.0, rd_expense=500.0, gross_profit=200.0
        )
        assert 0.0 <= eff <= 1.0

    def test_evaluate_full(self, interface):
        result = interface.evaluate(
            ticker="NVDA",
            rd_expense=8_000_000_000,
            revenue=60_000_000_000,
            gross_profit=40_000_000_000,
            sbc_expense=1_200_000_000,
            shares_outstanding=2_500_000_000,
            share_price=800.0,
            sector="semiconductor",
            operating_margin=0.35,
        )
        assert isinstance(result, FinancialReconstructionResult)
        assert result.ticker == "NVDA"
        assert 0.0 <= result.rd_capitalisation_rate <= 1.0
        assert 0.0 <= result.sbc_drag_intensity <= 1.0
        assert result.sbc_dilution_risk in ("low", "moderate", "elevated", "critical")
        assert result.reconstructed_fcf >= 0.0
        assert result.rd_asset_years > 0

    def test_evaluate_minimal(self, interface):
        result = interface.evaluate(
            ticker="AAPL",
            rd_expense=500_000_000,
            revenue=100_000_000_000,
            gross_profit=45_000_000_000,
            sbc_expense=50_000_000,
            shares_outstanding=15_000_000_000,
            share_price=200.0,
            sector="consumer",
        )
        assert result.sbc_dilution_risk == "low"
        assert isinstance(result.adjusted_operating_margin, float) or result.adjusted_operating_margin is None

    def test_evaluate_unknown_sector(self, interface):
        result = interface.evaluate(
            ticker="UNKN",
            rd_expense=10_000_000,
            revenue=100_000_000,
            gross_profit=50_000_000,
            sbc_expense=1_000_000,
            shares_outstanding=10_000_000,
            share_price=100.0,
            sector="unknown_sector",
        )
        assert result.rd_asset_years == 5.0

    def test_sbc_dilution_risk_thresholds(self, interface):
        result_low = interface.evaluate(
            ticker="T1", rd_expense=100, revenue=100_000, gross_profit=50_000,
            sbc_expense=10, shares_outstanding=1_000_000, share_price=100,
        )
        assert result_low.sbc_dilution_risk == "low"

        result_critical = interface.evaluate(
            ticker="T2", rd_expense=100, revenue=100, gross_profit=50,
            sbc_expense=500, shares_outstanding=1_000, share_price=10,
        )
        assert result_critical.sbc_dilution_risk == "critical"

    def test_create_factory(self):
        interface = create_financial_reconstruction_interface({"custom_key": "val"})
        assert isinstance(interface, FinancialReconstructionInterface)
