"""
Tests for the three stochastic models:
  1. Bernoulli Shock Filter (Supply Chain & Operational Shock)
  2. Markov Lifecycle Chain (Corporate Lifecycle Transitions)
  3. Poisson Black Swan (Systemic Shock Counts)
"""

import pytest
import random
import numpy as np
from unittest.mock import patch

from Quantitative.stochastic.default_probability_table import (
    lookup_rating_tier,
    get_default_probability,
    get_synthetic_rating,
    get_credit_spread,
    get_recovery_rate,
    compute_shock_penalty_multiplier,
    build_default_probability_map,
    RATING_TABLE,
    DISTRESSED_TIER,
)
from Quantitative.stochastic.bernoulli_shock_filter import (
    BernoulliShockFilter,
    BernoulliShockResult,
)
from Quantitative.stochastic.markov_lifecycle import (
    MarkovLifecycleChain,
    LifecycleMetrics,
    LifecycleState,
    MarkovTransitionResult,
    STATE_INDEX,
    STATES,
    N_STATES,
)
from Quantitative.stochastic.poisson_blackswan import (
    PoissonBlackSwan,
    PoissonShockResult,
    DEFAULT_LAMBDA_BASE,
    HISTORICAL_MEAN_SPREAD_BPS,
)


# ===================================================================
# Test 1: Default Probability Table
# ===================================================================

class TestDefaultProbabilityTable:

    def test_lookup_rating_tier_high_icr(self):
        tier = lookup_rating_tier(15.0)
        assert tier.rating == "AAA"
        assert tier.p_default_1yr == 0.0001

    def test_lookup_rating_tier_nvda(self):
        tier = lookup_rating_tier(35.0)
        assert tier.rating == "AAA"

    def test_lookup_rating_tier_mid_range(self):
        tier = lookup_rating_tier(4.0)
        assert tier.rating == "BBB"

    def test_lookup_rating_tier_low_icr(self):
        tier = lookup_rating_tier(1.5)
        assert tier.rating == "B-"

    def test_lookup_rating_tier_distressed(self):
        tier = lookup_rating_tier(-1.0)
        assert tier.rating == "D"
        assert tier == DISTRESSED_TIER

    def test_lookup_rating_tier_zero(self):
        tier = lookup_rating_tier(0.0)
        assert tier.rating == "D"

    def test_get_default_probability_1yr(self):
        p = get_default_probability(35.0, horizon=1)
        assert p == 0.0001

    def test_get_default_probability_5yr(self):
        p = get_default_probability(35.0, horizon=5)
        assert p == 0.0004

    def test_get_default_probability_bbb(self):
        p = get_default_probability(4.0, horizon=1)
        assert p == 0.0018

    def test_get_synthetic_rating(self):
        assert get_synthetic_rating(35.0) == "AAA"
        assert get_synthetic_rating(4.0) == "BBB"
        assert get_synthetic_rating(2.0) == "B"

    def test_get_credit_spread(self):
        assert get_credit_spread(35.0) == 0.0063
        assert get_credit_spread(4.0) == 0.0150

    def test_get_recovery_rate(self):
        assert get_recovery_rate(35.0) == 0.60
        assert get_recovery_rate(2.0) == 0.35

    def test_penalty_multiplier_high_quality(self):
        # AAA: p_default=0.01%, recovery=60% → LGD=0.00004, penalty≈0.99996
        penalty = compute_shock_penalty_multiplier(35.0, shock_severity=1.0)
        assert penalty > 0.99

    def test_penalty_multiplier_low_quality(self):
        # CCC: p_default=5%, recovery=28% → LGD=0.036, penalty≈0.964
        penalty = compute_shock_penalty_multiplier(1.0, shock_severity=1.0)
        assert penalty < 0.98

    def test_penalty_multiplier_amplified(self):
        penalty_normal = compute_shock_penalty_multiplier(2.0, shock_severity=1.0)
        penalty_amplified = compute_shock_penalty_multiplier(2.0, shock_severity=2.0)
        assert penalty_amplified < penalty_normal

    def test_build_default_probability_map(self):
        d = build_default_probability_map()
        assert "AAA" in d
        assert "D" not in d
        assert len(d) == len(RATING_TABLE)

    def test_all_ratings_increasing_default_prob(self):
        """Default probability should increase as credit quality decreases (lower ICR)."""
        for i in range(len(RATING_TABLE) - 1):
            higher_tier = RATING_TABLE[i]
            lower_tier = RATING_TABLE[i + 1]
            assert lower_tier.p_default_1yr >= higher_tier.p_default_1yr


# ===================================================================
# Test 2: Bernoulli Shock Filter
# ===================================================================

class TestBernoulliShockFilter:

    @pytest.fixture
    def filter_engine(self):
        return BernoulliShockFilter()

    def test_high_icr_low_shock_prob(self, filter_engine):
        p = filter_engine.compute_shock_probability(icr=35.0)
        assert p < 0.01

    def test_low_icr_high_shock_prob(self, filter_engine):
        p = filter_engine.compute_shock_probability(icr=1.0)
        assert p > 0.03

    def test_concentration_amplifier(self, filter_engine):
        p_low = filter_engine.compute_shock_probability(icr=5.0, supplier_concentration=0.5)
        p_high = filter_engine.compute_shock_probability(icr=5.0, supplier_concentration=0.9)
        assert p_high > p_low

    def test_geopolitical_amplifier(self, filter_engine):
        p_base = filter_engine.compute_shock_probability(icr=5.0, geopolitical_stress_factor=0.0)
        p_stressed = filter_engine.compute_shock_probability(icr=5.0, geopolitical_stress_factor=0.5)
        assert p_stressed > p_base

    def test_run_trial_high_icr_no_shock(self, filter_engine):
        rng = random.Random(42)
        result = filter_engine.run_trial(icr=35.0, rng=rng)
        assert isinstance(result, BernoulliShockResult)
        assert result.synthetic_rating == "AAA"
        assert result.shock_probability < 0.01
        # With AAA, shock should almost never fire
        assert result.penalty_multiplier == 1.0 or result.shock_occurred

    def test_run_trial_low_icr_possible_shock(self, filter_engine):
        # Run many trials with low ICR to verify shock can fire
        rng = random.Random(42)
        shock_count = 0
        for _ in range(1000):
            result = filter_engine.run_trial(icr=1.0, rng=rng)
            if result.shock_occurred:
                shock_count += 1
        # CCC rating has p_default=5%, so in 1000 trials expect ~50 shocks
        assert shock_count > 10

    def test_fcf_vector_no_shock(self, filter_engine):
        rng = random.Random(42)
        penalties = filter_engine.compute_fcf_vector_penalty(
            icr=35.0, n_years=5, rng=rng
        )
        assert len(penalties) == 5
        assert all(p == 1.0 for p in penalties)

    def test_fcf_vector_with_shock(self, filter_engine):
        # Use a very low ICR and force shock by running many times
        found_shock = False
        for seed in range(100):
            rng = random.Random(seed)
            penalties = filter_engine.compute_fcf_vector_penalty(
                icr=1.0, n_years=5, shock_severity=2.0, rng=rng
            )
            if penalties[0] < 1.0:
                found_shock = True
                # Penalty decays over time: year 0 is strongest, year 2+ recovers
                assert penalties[0] > penalties[1] > penalties[2]
                assert len(penalties) == 5
                break
        assert found_shock


# ===================================================================
# Test 3: Markov Lifecycle Chain
# ===================================================================

class TestMarkovLifecycleChain:

    @pytest.fixture
    def chain(self):
        return MarkovLifecycleChain(time_horizon=5)

    def test_classify_nvda_fast_grower(self, chain):
        metrics = LifecycleMetrics(
            reinvestment_rate=0.55, roic=0.45,
            revenue_growth=0.25, margin_variance_10y=0.04,
            operating_margin=0.55, debt_to_capital=0.10,
            interest_coverage_ratio=35.0,
        )
        result = chain.analyze("NVDA", metrics)
        assert result.current_state == LifecycleState.FAST_GROWER

    def test_classify_intc_turnaround(self, chain):
        metrics = LifecycleMetrics(
            reinvestment_rate=0.60, roic=0.03,
            revenue_growth=0.02, margin_variance_10y=0.05,
            operating_margin=0.05, debt_to_capital=0.40,
            interest_coverage_ratio=3.5,
        )
        result = chain.analyze("INTC", metrics)
        # INTC should be classified as turnaround or slow grower
        assert result.current_state in (LifecycleState.TURNAROUND, LifecycleState.SLOW_GROWER, LifecycleState.CYCLICAL)

    def test_transition_matrix_rows_sum_to_one(self, chain):
        metrics = LifecycleMetrics(
            reinvestment_rate=0.40, roic=0.25,
            revenue_growth=0.15, margin_variance_10y=0.04,
            operating_margin=0.30, debt_to_capital=0.20,
            interest_coverage_ratio=10.0,
        )
        matrix = chain.compute_transition_matrix(metrics)
        for row in matrix:
            assert abs(sum(row) - 1.0) < 0.01

    def test_transition_matrix_shape(self, chain):
        metrics = LifecycleMetrics(
            reinvestment_rate=0.40, roic=0.25,
            revenue_growth=0.15, margin_variance_10y=0.04,
            operating_margin=0.30, debt_to_capital=0.20,
            interest_coverage_ratio=10.0,
        )
        matrix = chain.compute_transition_matrix(metrics)
        assert len(matrix) == N_STATES
        for row in matrix:
            assert len(row) == N_STATES

    def test_initial_distribution_one_hot(self, chain):
        dist = chain.compute_initial_distribution(LifecycleState.STALWART)
        assert dist[STATE_INDEX[LifecycleState.STALWART]] == 1.0
        assert sum(d for i, d in enumerate(dist) if i != STATE_INDEX[LifecycleState.STALWART]) == 0.0

    def test_project_distribution_preserves_probability(self, chain):
        metrics = LifecycleMetrics(
            reinvestment_rate=0.40, roic=0.25,
            revenue_growth=0.15, margin_variance_10y=0.04,
            operating_margin=0.30, debt_to_capital=0.20,
            interest_coverage_ratio=10.0,
        )
        matrix = chain.compute_transition_matrix(metrics)
        dist = chain.compute_initial_distribution(LifecycleState.STALWART)
        projected = chain.project_distribution(dist, matrix, 5)
        assert abs(sum(projected) - 1.0) < 0.01

    def test_fast_grower_stays_fast_with_high_rr_roic(self, chain):
        metrics = LifecycleMetrics(
            reinvestment_rate=0.50, roic=0.40,
            revenue_growth=0.25, margin_variance_10y=0.03,
            operating_margin=0.50, debt_to_capital=0.10,
            interest_coverage_ratio=20.0,
        )
        result = chain.analyze("NVDA", metrics)
        # With high RR + high ROIC + high growth, should start as FAST_GROWER
        assert result.current_state == LifecycleState.FAST_GROWER
        # The transition matrix should show FAST_GROWER has a strong
        # self-transition probability (>0.3) - it doesn't immediately leave
        fg_idx = STATE_INDEX[LifecycleState.FAST_GROWER]
        assert result.transition_matrix[fg_idx][fg_idx] > 0.3

    def test_high_margin_variance_pushes_to_cyclical(self, chain):
        metrics = LifecycleMetrics(
            reinvestment_rate=0.30, roic=0.15,
            revenue_growth=0.10, margin_variance_10y=0.12,
            operating_margin=0.20, debt_to_capital=0.30,
            interest_coverage_ratio=5.0,
        )
        matrix = chain.compute_transition_matrix(metrics)
        # CYCLICAL row should have high self-transition probability
        cyclical_idx = STATE_INDEX[LifecycleState.CYCLICAL]
        assert matrix[cyclical_idx][cyclical_idx] > 0.3

    def test_convergence_step(self, chain):
        metrics = LifecycleMetrics(
            reinvestment_rate=0.40, roic=0.25,
            revenue_growth=0.15, margin_variance_10y=0.04,
            operating_margin=0.30, debt_to_capital=0.20,
            interest_coverage_ratio=10.0,
        )
        matrix = chain.compute_transition_matrix(metrics)
        dist = chain.compute_initial_distribution(LifecycleState.STALWART)
        step = chain.find_convergence_step(dist, matrix)
        assert 1 <= step <= 20

    def test_transition_entropy_non_negative(self, chain):
        metrics = LifecycleMetrics(
            reinvestment_rate=0.40, roic=0.25,
            revenue_growth=0.15, margin_variance_10y=0.04,
            operating_margin=0.30, debt_to_capital=0.20,
            interest_coverage_ratio=10.0,
        )
        matrix = chain.compute_transition_matrix(metrics)
        entropy = chain.compute_transition_entropy(matrix)
        assert entropy >= 0.0

    def test_full_analysis_output(self, chain):
        metrics = LifecycleMetrics(
            reinvestment_rate=0.55, roic=0.45,
            revenue_growth=0.30, margin_variance_10y=0.04,
            operating_margin=0.55, debt_to_capital=0.08,
            interest_coverage_ratio=35.0,
        )
        result = chain.analyze("NVDA", metrics)
        assert isinstance(result, MarkovTransitionResult)
        assert result.ticker == "NVDA"
        assert len(result.state_distribution) == N_STATES
        assert len(result.projected_distribution) == N_STATES
        assert result.n_steps == 5


# ===================================================================
# Test 4: Poisson Black Swan
# ===================================================================

class TestPoissonBlackSwan:

    @pytest.fixture
    def poisson(self):
        return PoissonBlackSwan()

    def test_stress_lambda_normal(self, poisson):
        lam = poisson.compute_stress_lambda(regime="NORMAL")
        assert lam == pytest.approx(DEFAULT_LAMBDA_BASE)

    def test_stress_lambda_crisis(self, poisson):
        lam = poisson.compute_stress_lambda(regime="CRISIS")
        assert lam == pytest.approx(DEFAULT_LAMBDA_BASE * 2.5)

    def test_stress_lambda_widening(self, poisson):
        lam = poisson.compute_stress_lambda(regime="WIDENING")
        assert lam == pytest.approx(DEFAULT_LAMBDA_BASE * 1.5)

    def test_stress_lambda_from_spread(self, poisson):
        # If spread is 2x historical mean, lambda should be 2x base
        spread = HISTORICAL_MEAN_SPREAD_BPS * 2.0
        lam = poisson.compute_stress_lambda(current_spread_bps=spread)
        assert lam == pytest.approx(DEFAULT_LAMBDA_BASE * 2.0)

    def test_stress_lambda_from_wide_spread(self, poisson):
        spread = HISTORICAL_MEAN_SPREAD_BPS * 3.0
        lam = poisson.compute_stress_lambda(current_spread_bps=spread)
        assert lam == pytest.approx(DEFAULT_LAMBDA_BASE * 3.0)

    def test_sample_shock_count_returns_int(self, poisson):
        count = poisson.sample_shock_count(0.5)
        assert isinstance(count, int)
        assert count >= 0

    def test_sample_shock_count_zero_lambda(self, poisson):
        count = poisson.sample_shock_count(0.0)
        assert count == 0

    def test_sample_shock_count_high_lambda(self, poisson):
        np.random.seed(42)
        counts = [poisson.sample_shock_count(5.0) for _ in range(100)]
        mean_count = np.mean(counts)
        assert mean_count > 3.0  # Should be around 5

    def test_sample_shock_magnitudes_empty(self, poisson):
        mags = poisson.sample_shock_magnitudes(0)
        assert mags == []

    def test_sample_shock_magnitudes_negative(self, poisson):
        np.random.seed(42)
        mags = poisson.sample_shock_magnitudes(10)
        assert len(mags) == 10
        for m in mags:
            assert -0.50 <= m <= -0.01

    def test_compute_portfolio_impact_no_shocks(self, poisson):
        impact = poisson.compute_portfolio_impact([])
        assert impact == 0.0

    def test_compute_portfolio_impact_with_shocks(self, poisson):
        impact = poisson.compute_portfolio_impact([-0.10, -0.20])
        assert impact == pytest.approx(-0.30)

    def test_compute_portfolio_impact_weighted(self, poisson):
        impact = poisson.compute_portfolio_impact(
            [-0.10, -0.20],
            portfolio_weights=[0.6, 0.4]
        )
        assert impact == pytest.approx(-0.06 + -0.08)

    def test_simulate_returns_result(self, poisson):
        result = poisson.simulate()
        assert isinstance(result, PoissonShockResult)
        assert result.lambda_base == DEFAULT_LAMBDA_BASE

    def test_simulate_with_crisis_regime(self, poisson):
        result = poisson.simulate(regime="CRISIS")
        assert result.lambda_stress > DEFAULT_LAMBDA_BASE

    def test_simulate_with_spread_data(self, poisson):
        result = poisson.simulate(current_spread_bps=440.0)
        assert result.spread_ratio == pytest.approx(2.0)
        assert result.lambda_stress == pytest.approx(DEFAULT_LAMBDA_BASE * 2.0)

    def test_sample_per_simulation_shocks(self, poisson):
        np.random.seed(42)
        mags = poisson.sample_per_simulation_shocks(0.5)
        assert isinstance(mags, list)
        for m in mags:
            assert isinstance(m, float)

    def test_simulate_n_portfolios(self, poisson):
        result = poisson.simulate(n_portfolios=10)
        assert isinstance(result, PoissonShockResult)


# ===================================================================
# Test 5: Integration with MonteCarloEngine
# ===================================================================

class TestStochasticIntegration:

    @pytest.fixture
    def engine(self):
        with patch("psychological.monte_carlo.load_hybrid_config") as mock:
            mock.return_value = {}
            from psychological.monte_carlo import create_monte_carlo_engine
            return create_monte_carlo_engine()

    def test_mc_input_accepts_icr(self, engine):
        mc_input = engine.build_input_from_fundamentals(
            ticker="NVDA",
            revenue=130_000_000_000,
            fcf=65_000_000_000,
            roic=0.45,
            wacc=0.11,
            reinvestment_rate=0.55,
            operating_margin=0.55,
            interest_coverage_ratio=35.0,
            margin_variance_10y=0.04,
            revenue_growth=0.25,
        )
        assert mc_input.interest_coverage_ratio == 35.0
        assert mc_input.lifecycle_transition_volatility >= 0.0

    def test_mc_input_accepts_poisson_params(self, engine):
        mc_input = engine.build_input_from_fundamentals(
            ticker="NVDA",
            revenue=130_000_000_000,
            fcf=65_000_000_000,
            roic=0.45,
            wacc=0.11,
            reinvestment_rate=0.55,
            operating_margin=0.55,
            poisson_lambda_base=0.30,
            credit_spread_bps=440.0,
        )
        assert mc_input.poisson_lambda_base == 0.30
        assert mc_input.credit_spread_bps == 440.0

    def test_mc_result_has_new_fields(self, engine):
        mc_input = engine.build_input_from_fundamentals(
            ticker="NVDA",
            revenue=130_000_000_000,
            fcf=65_000_000_000,
            roic=0.45,
            wacc=0.11,
            reinvestment_rate=0.55,
            operating_margin=0.55,
            interest_coverage_ratio=35.0,
            n_simulations=100,
        )
        result = engine.run(mc_input)
        assert hasattr(result, "bernoulli_shock_count")
        assert hasattr(result, "poisson_total_shocks")
        assert hasattr(result, "mean_poisson_lambda_stress")
        assert hasattr(result, "lifecycle_state")
        assert hasattr(result, "lifecycle_projected_state")
        assert hasattr(result, "lifecycle_entropy")
        assert result.lifecycle_state in [s.value for s in LifecycleState]

    def test_mc_sim_run_has_new_fields(self, engine):
        from psychological.monte_carlo import MonteCarloInput
        mc_input = MonteCarloInput(
            ticker="TEST",
            expected_growth_mean=0.15,
            expected_growth_std=0.05,
            operating_margin_mean=0.30,
            operating_margin_std=0.03,
            wacc=0.10,
            reinvestment_rate=0.40,
            roic=0.25,
            initial_revenue=10_000_000_000,
            initial_fcf=2_000_000_000,
            n_simulations=10,
            interest_coverage_ratio=5.0,
        )
        run = engine._simulate_single(mc_input)
        assert hasattr(run, "bernoulli_shock_fired")
        assert hasattr(run, "poisson_shocks_count")

    def test_nvda_high_conviction_still_works(self, engine):
        mc_input = engine.build_input_from_fundamentals(
            ticker="NVDA",
            revenue=130_000_000_000,
            fcf=65_000_000_000,
            roic=0.45,
            wacc=0.11,
            reinvestment_rate=0.55,
            operating_margin=0.55,
            interest_coverage_ratio=35.0,
            revenue_growth=0.25,
            n_simulations=2000,
        )
        result = engine.run(mc_input)
        assert result.positive_eva_probability > 0.70
        assert result.lifecycle_state == "FAST_GROWER"

    def test_intc_low_conviction_still_works(self, engine):
        mc_input = engine.build_input_from_fundamentals(
            ticker="INTC",
            revenue=54_000_000_000,
            fcf=-8_000_000_000,
            roic=0.03,
            wacc=0.13,
            reinvestment_rate=0.60,
            operating_margin=0.05,
            interest_coverage_ratio=3.5,
            n_simulations=2000,
        )
        result = engine.run(mc_input)
        assert result.positive_eva_probability < 0.70
        assert result.lifecycle_state != "FAST_GROWER"

    def test_poisson_increases_shock_count_in_crisis(self, engine):
        # Normal regime
        mc_normal = engine.build_input_from_fundamentals(
            ticker="NVDA",
            revenue=130_000_000_000,
            fcf=65_000_000_000,
            roic=0.45,
            wacc=0.11,
            reinvestment_rate=0.55,
            operating_margin=0.55,
            interest_coverage_ratio=35.0,
            credit_spread_regime="NORMAL",
            n_simulations=500,
        )
        result_normal = engine.run(mc_normal)

        # Crisis regime
        mc_crisis = engine.build_input_from_fundamentals(
            ticker="NVDA",
            revenue=130_000_000_000,
            fcf=65_000_000_000,
            roic=0.45,
            wacc=0.11,
            reinvestment_rate=0.55,
            operating_margin=0.55,
            interest_coverage_ratio=35.0,
            credit_spread_bps=660.0,
            credit_spread_regime="CRISIS",
            n_simulations=500,
        )
        result_crisis = engine.run(mc_crisis)

        assert result_crisis.mean_poisson_lambda_stress > result_normal.mean_poisson_lambda_stress


# ===================================================================
# Test 6: Sector Shock Data Loader
# ===================================================================

class TestSectorShockData:

    def test_load_sector_stats_semiconductor(self):
        from Quantitative.stochastic.sector_shock_data import get_sector_shock_stats
        stats = get_sector_shock_stats("semiconductor")
        assert stats.sector == "semiconductor"
        assert 0.0 <= stats.p_base <= 0.15
        assert stats.margin_vol_10y > 0

    def test_load_sector_stats_platform_software(self):
        from Quantitative.stochastic.sector_shock_data import get_sector_shock_stats
        stats = get_sector_shock_stats("platform_software")
        assert stats.sector == "platform_software"
        assert 0.0 <= stats.p_base <= 0.15

    def test_load_sector_stats_hardware_oem(self):
        from Quantitative.stochastic.sector_shock_data import get_sector_shock_stats
        stats = get_sector_shock_stats("hardware_oem")
        assert stats.sector == "hardware_oem"
        assert 0.0 <= stats.p_base <= 0.15

    def test_unknown_sector_falls_back(self):
        from Quantitative.stochastic.sector_shock_data import get_sector_shock_stats
        stats = get_sector_shock_stats("unknown_sector")
        assert stats.p_base > 0

    def test_dynamic_shock_probability_semiconductor(self):
        from Quantitative.stochastic.sector_shock_data import compute_dynamic_shock_probability
        p = compute_dynamic_shock_probability(
            sector="semiconductor",
            current_margin_vol=0.06,
        )
        assert p > 0.01

    def test_dynamic_shock_increases_with_margin_vol(self):
        from Quantitative.stochastic.sector_shock_data import compute_dynamic_shock_probability
        p_low = compute_dynamic_shock_probability(
            sector="semiconductor", current_margin_vol=0.03,
        )
        p_high = compute_dynamic_shock_probability(
            sector="semiconductor", current_margin_vol=0.12,
        )
        assert p_high > p_low

    def test_dynamic_shock_concentration_amplifier(self):
        from Quantitative.stochastic.sector_shock_data import compute_dynamic_shock_probability
        p_low = compute_dynamic_shock_probability(
            sector="semiconductor", current_margin_vol=0.06,
            supplier_concentration=0.5,
        )
        p_high = compute_dynamic_shock_probability(
            sector="semiconductor", current_margin_vol=0.06,
            supplier_concentration=0.9,
        )
        assert p_high > p_low

    def test_dynamic_shock_bounded(self):
        from Quantitative.stochastic.sector_shock_data import compute_dynamic_shock_probability
        p = compute_dynamic_shock_probability(
            sector="semiconductor",
            current_margin_vol=0.50,
            supplier_concentration=0.95,
            geopolitical_stress_factor=1.0,
        )
        assert 0.0 <= p <= 1.0


# ===================================================================
# Test 7: Dynamic Bernoulli Shock Filter
# ===================================================================

class TestBernoulliShockFilterDynamic:

    @pytest.fixture
    def filter_engine(self):
        return BernoulliShockFilter()

    def test_run_trial_dynamic_nvda(self, filter_engine):
        rng = random.Random(42)
        result = filter_engine.run_trial_dynamic(
            icr=35.0,
            sector="semiconductor",
            current_margin_vol=0.06,
            margin_vol_10y=0.06,
            rng=rng,
        )
        assert isinstance(result, BernoulliShockResult)
        assert result.shock_probability > 0.005
        assert result.synthetic_rating == "AAA"

    def test_run_trial_dynamic_sector_dominates_nvda(self, filter_engine):
        rng = random.Random(42)
        result = filter_engine.run_trial_dynamic(
            icr=35.0,
            sector="semiconductor",
            current_margin_vol=0.06,
            rng=rng,
        )
        p_default = lookup_rating_tier(35.0).p_default_1yr
        assert result.shock_probability > p_default

    def test_run_trial_dynamic_credit_dominates_distressed(self, filter_engine):
        rng = random.Random(42)
        result = filter_engine.run_trial_dynamic(
            icr=0.5,
            sector="platform_software",
            current_margin_vol=0.03,
            margin_vol_10y=0.03,
            rng=rng,
        )
        p_default = lookup_rating_tier(0.5).p_default_1yr
        assert result.shock_probability >= p_default

    def test_run_trial_dynamic_can_fire_shocks(self, filter_engine):
        rng = random.Random(42)
        shock_count = 0
        for _ in range(1000):
            result = filter_engine.run_trial_dynamic(
                icr=35.0,
                sector="semiconductor",
                current_margin_vol=0.06,
                margin_vol_10y=0.06,
                rng=rng,
            )
            if result.shock_occurred:
                shock_count += 1
        assert shock_count > 0

    def test_run_trial_dynamic_fewer_shocks_for_stable_sector(self, filter_engine):
        rng_semiconductor = random.Random(42)
        rng_software = random.Random(42)
        sem_shocks = sum(
            1 for _ in range(2000)
            if filter_engine.run_trial_dynamic(
                icr=35.0, sector="semiconductor",
                current_margin_vol=0.06, margin_vol_10y=0.06,
                rng=rng_semiconductor,
            ).shock_occurred
        )
        sw_shocks = sum(
            1 for _ in range(2000)
            if filter_engine.run_trial_dynamic(
                icr=35.0, sector="platform_software",
                current_margin_vol=0.03, margin_vol_10y=0.03,
                rng=rng_software,
            ).shock_occurred
        )
        assert sem_shocks >= sw_shocks

    def test_monte_carlo_input_accepts_sector(self):
        from unittest.mock import patch as _patch
        with _patch("psychological.monte_carlo.load_hybrid_config") as mock:
            mock.return_value = {}
            from psychological.monte_carlo import create_monte_carlo_engine
            engine = create_monte_carlo_engine()
            mc_input = engine.build_input_from_fundamentals(
                ticker="NVDA",
                revenue=130_000_000_000,
                fcf=65_000_000_000,
                roic=0.45,
                wacc=0.11,
                reinvestment_rate=0.55,
                operating_margin=0.55,
                interest_coverage_ratio=35.0,
                sector="semiconductor",
            )
            assert mc_input.sector == "semiconductor"

    def test_monte_carlo_nvda_gets_operational_shocks(self):
        from unittest.mock import patch as _patch
        with _patch("psychological.monte_carlo.load_hybrid_config") as mock:
            mock.return_value = {}
            from psychological.monte_carlo import create_monte_carlo_engine
            engine = create_monte_carlo_engine()
            mc_input = engine.build_input_from_fundamentals(
                ticker="NVDA",
                revenue=130_000_000_000,
                fcf=65_000_000_000,
                roic=0.45,
                wacc=0.11,
                reinvestment_rate=0.55,
                operating_margin=0.55,
                interest_coverage_ratio=35.0,
                sector="semiconductor",
                n_simulations=10_000,
            )
            result = engine.run(mc_input)
            assert result.bernoulli_shock_count > 0

    def test_nvda_shock_count_reasonable(self):
        from unittest.mock import patch as _patch
        with _patch("psychological.monte_carlo.load_hybrid_config") as mock:
            mock.return_value = {}
            from psychological.monte_carlo import create_monte_carlo_engine
            engine = create_monte_carlo_engine()
            mc_input = engine.build_input_from_fundamentals(
                ticker="NVDA",
                revenue=130_000_000_000,
                fcf=65_000_000_000,
                roic=0.45,
                wacc=0.11,
                reinvestment_rate=0.55,
                operating_margin=0.55,
                interest_coverage_ratio=35.0,
                sector="semiconductor",
                n_simulations=10_000,
            )
            result = engine.run(mc_input)
            shock_rate = result.bernoulli_shock_count / 10_000
            assert 0.005 < shock_rate < 0.50

    def test_nvda_fewer_shocks_than_intc(self):
        """NVDA (ICR=35) should have fewer shocks than INTC (ICR=3.5) in same sector."""
        from unittest.mock import patch as _patch
        with _patch("psychological.monte_carlo.load_hybrid_config") as mock:
            mock.return_value = {}
            from psychological.monte_carlo import create_monte_carlo_engine
            engine = create_monte_carlo_engine()

            nvda_input = engine.build_input_from_fundamentals(
                ticker="NVDA", revenue=130e9, fcf=65e9, roic=0.45, wacc=0.11,
                reinvestment_rate=0.55, operating_margin=0.55,
                interest_coverage_ratio=35.0, sector="semiconductor",
                n_simulations=10_000,
            )
            intc_input = engine.build_input_from_fundamentals(
                ticker="INTC", revenue=54e9, fcf=-8e9, roic=0.03, wacc=0.13,
                reinvestment_rate=0.60, operating_margin=0.05,
                interest_coverage_ratio=3.5, sector="semiconductor",
                n_simulations=10_000,
            )
            nvda_r = engine.run(nvda_input)
            intc_r = engine.run(intc_input)
            assert nvda_r.bernoulli_shock_count < intc_r.bernoulli_shock_count


# ===================================================================
# Test 8: Balance Sheet Resilience Modifier (M_health)
# ===================================================================

class TestHealthModifier:

    def test_health_modifier_fortress(self):
        bf = BernoulliShockFilter()
        m = bf.compute_health_modifier(35.0)
        assert 0.60 < m < 0.75

    def test_health_modifier_average(self):
        bf = BernoulliShockFilter()
        m = bf.compute_health_modifier(4.0)
        assert 0.65 < m < 0.80

    def test_health_modifier_stressed(self):
        bf = BernoulliShockFilter()
        m = bf.compute_health_modifier(1.0)
        assert m > 1.0

    def test_health_modifier_distressed(self):
        bf = BernoulliShockFilter()
        m = bf.compute_health_modifier(0.5)
        assert m > 1.2

    def test_health_modifier_increases_as_icr_drops(self):
        bf = BernoulliShockFilter()
        m_high = bf.compute_health_modifier(35.0)
        m_low = bf.compute_health_modifier(1.0)
        assert m_high < m_low

    def test_health_modifier_bounded(self):
        bf = BernoulliShockFilter()
        for icr in [-5.0, 0.0, 1.0, 2.0, 5.0, 10.0, 35.0, 100.0]:
            m = bf.compute_health_modifier(icr)
            assert 0.5 <= m <= 2.0

    def test_health_modifier_nvda_discount(self):
        bf = BernoulliShockFilter()
        m_nvda = bf.compute_health_modifier(35.0)
        assert m_nvda < 1.0

    def test_health_modifier_intc_penalty(self):
        bf = BernoulliShockFilter()
        m_intc = bf.compute_health_modifier(3.5)
        assert m_intc > 0.7
