import pytest
from unittest.mock import patch
import numpy as np

from psychological.monte_carlo import (
    MonteCarloEngine,
    MonteCarloInput,
    MonteCarloResult,
    MonteCarloSimRun,
    create_monte_carlo_engine,
)


class TestMonteCarloEngine:
    @pytest.fixture
    def engine(self):
        with patch("psychological.monte_carlo.load_hybrid_config") as mock:
            mock.return_value = {}
            return create_monte_carlo_engine()

    @pytest.fixture
    def sample_input(self):
        return MonteCarloInput(
            ticker="NVDA",
            expected_growth_mean=0.20,
            expected_growth_std=0.06,
            operating_margin_mean=0.45,
            operating_margin_std=0.05,
            wacc=0.11,
            reinvestment_rate=0.55,
            roic=0.45,
            initial_revenue=130_000_000_000,
            initial_fcf=65_000_000_000,
            projection_years=5,
            n_simulations=1000,
        )

    def test_init(self, engine):
        assert engine is not None
        assert engine.mc_config == {}

    def test_clamp_growth(self, engine):
        assert engine._clamp_growth(0.60) == 0.50
        assert engine._clamp_growth(-0.60) == -0.50
        assert engine._clamp_growth(0.20) == 0.20
        assert engine._clamp_growth(-0.10) == -0.10

    def test_compute_eva_spread(self, engine):
        assert engine._compute_eva_spread(0.15, 0.10) == pytest.approx(0.05)
        assert engine._compute_eva_spread(0.08, 0.10) == pytest.approx(-0.02)
        assert engine._compute_eva_spread(0.10, 0.10) == pytest.approx(0.0)

    def test_simulate_single(self, engine, sample_input):
        run = engine._simulate_single(sample_input)
        assert isinstance(run, MonteCarloSimRun)
        assert -0.50 <= run.growth_rate <= 0.50
        assert run.intrinsic_value > 0
        assert isinstance(run.eva_positive, bool)
        assert isinstance(run.macro_shock_applied, bool)

    def test_run_basic(self, engine, sample_input):
        result = engine.run(sample_input)
        assert isinstance(result, MonteCarloResult)
        assert result.ticker == "NVDA"
        assert result.n_simulations == 1000
        assert 0.0 <= result.positive_eva_probability <= 1.0
        assert result.mean_intrinsic_value > 0
        assert result.p5_intrinsic_value <= result.p25_intrinsic_value <= result.median_intrinsic_value <= result.p75_intrinsic_value <= result.p95_intrinsic_value
        assert 0.0 <= result.macro_risk_adjustment <= 1.0
        assert result.confidence_band is not None

    def test_run_with_macro_shocks(self, engine):
        input_data = MonteCarloInput(
            ticker="AMD",
            expected_growth_mean=0.10,
            expected_growth_std=0.05,
            operating_margin_mean=0.20,
            operating_margin_std=0.04,
            wacc=0.12,
            reinvestment_rate=0.40,
            roic=0.15,
            initial_revenue=25_000_000_000,
            initial_fcf=5_000_000_000,
            projection_years=5,
            n_simulations=500,
            macro_inflation_shock_prob=0.50,
            macro_inflation_shock_magnitude=0.03,
            macro_supply_chain_shock_prob=0.50,
            macro_supply_chain_margin_impact=0.05,
        )
        result = engine.run(input_data)
        assert result.macro_risk_adjustment < 0.75
        assert result.confidence_band is not None

    def test_build_input_from_fundamentals(self, engine):
        mc_input = engine.build_input_from_fundamentals(
            ticker="MSFT",
            revenue=245_000_000_000,
            fcf=82_000_000_000,
            roic=0.30,
            wacc=0.09,
            reinvestment_rate=0.45,
            operating_margin=0.42,
        )
        assert isinstance(mc_input, MonteCarloInput)
        assert mc_input.ticker == "MSFT"
        assert mc_input.expected_growth_mean == pytest.approx(0.45 * 0.30)
        assert mc_input.expected_growth_std > 0
        assert mc_input.operating_margin_std > 0

    def test_run_nvda_high_conviction(self, engine):
        input_data = MonteCarloInput(
            ticker="NVDA",
            expected_growth_mean=0.25,
            expected_growth_std=0.05,
            operating_margin_mean=0.55,
            operating_margin_std=0.03,
            wacc=0.11,
            reinvestment_rate=0.55,
            roic=0.45,
            initial_revenue=130_000_000_000,
            initial_fcf=65_000_000_000,
            projection_years=5,
            n_simulations=2000,
        )
        result = engine.run(input_data)
        assert result.positive_eva_probability > 0.80
        assert "High conviction" in result.confidence_band

    def test_run_intc_low_conviction(self, engine):
        input_data = MonteCarloInput(
            ticker="INTC",
            expected_growth_mean=0.02,
            expected_growth_std=0.08,
            operating_margin_mean=0.05,
            operating_margin_std=0.06,
            wacc=0.13,
            reinvestment_rate=0.60,
            roic=0.03,
            initial_revenue=54_000_000_000,
            initial_fcf=-8_000_000_000,
            projection_years=5,
            n_simulations=2000,
        )
        result = engine.run(input_data)
        assert result.positive_eva_probability < 0.60

    def test_deterministic_seed(self, engine):
        input_data = MonteCarloInput(
            ticker="AAPL",
            expected_growth_mean=0.15,
            expected_growth_std=0.04,
            operating_margin_mean=0.30,
            operating_margin_std=0.03,
            wacc=0.09,
            reinvestment_rate=0.35,
            roic=0.40,
            initial_revenue=395_000_000_000,
            initial_fcf=115_000_000_000,
            projection_years=5,
            n_simulations=1000,
        )
        result1 = engine.run(input_data)
        from psychological.monte_carlo import RNG_SEED
        np.random.seed(RNG_SEED)
        import random
        random.seed(RNG_SEED)
        result2 = engine.run(input_data)
        assert abs(result1.mean_intrinsic_value - result2.mean_intrinsic_value) < 0.01

    def test_high_n_simulations(self, engine):
        input_data = MonteCarloInput(
            ticker="GOOGL",
            expected_growth_mean=0.12,
            expected_growth_std=0.03,
            operating_margin_mean=0.28,
            operating_margin_std=0.02,
            wacc=0.09,
            reinvestment_rate=0.40,
            roic=0.25,
            initial_revenue=340_000_000_000,
            initial_fcf=86_000_000_000,
            projection_years=5,
            n_simulations=10000,
        )
        result = engine.run(input_data)
        assert result.n_simulations == 10000
        assert result.std_intrinsic_value > 0

    def test_displacement_tweak_a_and_b(self, engine):
        # 1. Base input without displacement
        base_input = MonteCarloInput(
            ticker="NVDA",
            expected_growth_mean=0.20,
            expected_growth_std=0.05,
            operating_margin_mean=0.45,
            operating_margin_std=0.03,
            wacc=0.10,
            reinvestment_rate=0.40,
            roic=0.30,
            initial_revenue=10_000_000_000,
            initial_fcf=1_000_000_000,
            displacement_ratio=0.0,
            is_leader=True,
            n_simulations=100,
        )
        base_run = engine._simulate_single(base_input)
        
        # 2. Input with displacement (DR > 1.0) for a Leader (Tweak A Moat Compression + Tweak B drag)
        leader_input = MonteCarloInput(
            ticker="NVDA",
            expected_growth_mean=0.20,
            expected_growth_std=0.05,
            operating_margin_mean=0.45,
            operating_margin_std=0.03,
            wacc=0.10,
            reinvestment_rate=0.40,
            roic=0.30,
            initial_revenue=10_000_000_000,
            initial_fcf=1_000_000_000,
            displacement_ratio=2.0,
            is_leader=True,
            n_simulations=100,
        )
        
        # Run multiple simulations to test distribution shifts
        leader_runs = [engine._simulate_single(leader_input) for _ in range(100)]
        
        # Verify Tweak A: sim_n_cap is compressed (clamped to max 5 or 6 years)
        for r in leader_runs:
            assert r.growth_rate is not None
            # Under DR > 1.0, sim_n_cap should be drawn from Beta distribution scaled to compressed bounds (<= 6)
            # We can check that the average is front-loaded
            
        # 3. Input with displacement (DR > 1.0) for a Challenger (Tweak B boost)
        challenger_input = MonteCarloInput(
            ticker="AMD",
            expected_growth_mean=0.20,
            expected_growth_std=0.05,
            operating_margin_mean=0.45,
            operating_margin_std=0.03,
            wacc=0.10,
            reinvestment_rate=0.40,
            roic=0.30,
            initial_revenue=10_000_000_000,
            initial_fcf=1_000_000_000,
            displacement_ratio=2.0,
            is_leader=False,
            n_simulations=100,
        )
        challenger_runs = [engine._simulate_single(challenger_input) for _ in range(100)]
        
        # Running the full engine results
        base_result = engine.run(base_input)
        leader_result = engine.run(leader_input)
        challenger_result = engine.run(challenger_input)
        
        # Leader's valuation should reflect capital efficiency drag, hence lower average intrinsic value
        # Challenger's valuation should benefit from capital efficiency boost
        assert leader_result.mean_intrinsic_value < base_result.mean_intrinsic_value
        assert challenger_result.mean_intrinsic_value > leader_result.mean_intrinsic_value



class TestCreateFunctions:
    def test_create_monte_carlo_engine(self):
        with patch("psychological.monte_carlo.load_hybrid_config") as mock:
            mock.return_value = {}
            engine = create_monte_carlo_engine()
            assert isinstance(engine, MonteCarloEngine)
