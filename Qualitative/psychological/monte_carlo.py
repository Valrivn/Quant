import logging
import math
import random
import statistics
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from psychological.engineering_guards import guard_nan, guard_bounds
from config import load_hybrid_config
from Quantitative.stochastic.bernoulli_shock_filter import BernoulliShockFilter
from Quantitative.stochastic.poisson_blackswan import PoissonBlackSwan
from Quantitative.stochastic.markov_lifecycle import MarkovLifecycleChain, LifecycleMetrics

logger = logging.getLogger(__name__)

RNG_SEED = 42


@dataclass
class MonteCarloInput:
    ticker: str
    expected_growth_mean: float
    expected_growth_std: float
    operating_margin_mean: float
    operating_margin_std: float
    wacc: float
    reinvestment_rate: float
    roic: float
    initial_revenue: float
    initial_fcf: float
    projection_years: int = 5
    terminal_growth: float = 0.03
    n_simulations: int = 10_000
    macro_inflation_shock_prob: float = 0.10
    macro_inflation_shock_magnitude: float = 0.02
    macro_supply_chain_shock_prob: float = 0.05
    macro_supply_chain_margin_impact: float = 0.03
    culture_score: float = 0.5
    supplier_concentration: float = 0.5
    moat_score: float = 0.5
    a_tech: float = 0.0
    geopolitical_stress_factor: float = 0.0
    geopolitical_risk_premium_rate: float = 0.0
    displacement_ratio: float = 0.0
    is_leader: bool = False
    # Stochastic model inputs
    interest_coverage_ratio: float = 5.0
    margin_variance_10y: float = 0.05
    revenue_growth: float = 0.10
    debt_to_capital: float = 0.30
    poisson_lambda_base: float = 0.30
    credit_spread_bps: Optional[float] = None
    credit_spread_regime: str = "NORMAL"
    lifecycle_transition_volatility: float = 0.0
    sector: str = "technology"


@dataclass
class MonteCarloSimRun:
    growth_rate: float
    operating_margin: float
    terminal_fcf: float
    intrinsic_value: float
    eva_positive: bool
    macro_shock_applied: bool
    regime: str = "normal"
    catastrophe_event: bool = False
    bernoulli_shock_fired: bool = False
    poisson_shocks_count: int = 0
    lifecycle_state_transition: str = ""


@dataclass
class MonteCarloResult:
    ticker: str
    n_simulations: int
    positive_eva_probability: float
    mean_intrinsic_value: float
    median_intrinsic_value: float
    std_intrinsic_value: float
    p5_intrinsic_value: float
    p25_intrinsic_value: float
    p75_intrinsic_value: float
    p95_intrinsic_value: float
    mean_growth_rate: float
    std_growth_rate: float
    mean_terminal_margin: float
    mean_terminal_fcf: float
    macro_risk_adjustment: float
    confidence_band: str
    projection_years: int
    survival_probability: float = 1.0
    catastrophe_event_count: int = 0
    mean_geopolitical_wacc_premium: float = 0.0
    bernoulli_shock_count: int = 0
    poisson_total_shocks: int = 0
    mean_poisson_lambda_stress: float = 0.0
    lifecycle_state: str = ""
    lifecycle_projected_state: str = ""
    lifecycle_entropy: float = 0.0
    source_weights_snapshot: Optional[Dict[str, float]] = None
    computed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class MonteCarloEngine:
    def __init__(self, config_dict: Optional[dict] = None):
        self.config = config_dict or load_hybrid_config()
        self.mc_config = self.config.get("monte_carlo", {})
        random.seed(RNG_SEED)
        np.random.seed(RNG_SEED)

    @staticmethod
    def _clamp_growth(g: float) -> float:
        return max(-0.50, min(0.50, g))

    @staticmethod
    def _compute_eva_spread(roic: float, wacc: float) -> float:
        return roic - wacc

    def _apply_poisson_shocks(
        self,
        growth: float,
        margin: float,
        input_data: MonteCarloInput,
        poisson_engine: PoissonBlackSwan,
    ) -> Tuple[float, float, bool, int]:
        """Apply Poisson-distributed systemic shocks to growth and margin.

        Replaces the old Bernoulli macro shocks with a regime-aware Poisson
        process. The number of simultaneous shocks is drawn from
        Poisson(lambda_stress), where lambda scales with credit spreads.
        """
        shock_applied = False
        total_shocks = 0

        lambda_stress = poisson_engine.compute_stress_lambda(
            input_data.credit_spread_bps, input_data.credit_spread_regime
        )
        shock_magnitudes = poisson_engine.sample_per_simulation_shocks(lambda_stress)

        for magnitude in shock_magnitudes:
            total_shocks += 1
            shock_applied = True
            # Alternate between growth and margin impacts
            if total_shocks % 2 == 1:
                growth += magnitude
            else:
                margin += magnitude

        growth = self._clamp_growth(growth)
        margin = max(0.01, min(0.60, margin))

        return growth, margin, shock_applied, total_shocks

    @staticmethod
    def _compute_effective_wacc(
        input_data: MonteCarloInput,
    ) -> Tuple[float, float]:
        premium = 0.0
        effective = input_data.wacc
        if input_data.supplier_concentration > 0.70 and input_data.geopolitical_risk_premium_rate > 0:
            premium = input_data.supplier_concentration * input_data.geopolitical_risk_premium_rate
            effective = min(0.25, max(0.03, input_data.wacc + premium))
        return effective, premium

    def _simulate_disrupted(
        self, input_data: MonteCarloInput, effective_wacc: float
    ) -> MonteCarloSimRun:
        recovery_growth = [-0.85, -0.50, -0.20, 0.0, 0.05]
        recovery_margin = [-0.15, -0.08, 0.0, 0.05, 0.10]

        projected_fcfs: List[float] = []
        revenue = input_data.initial_revenue
        invested_capital = input_data.initial_revenue * 0.8
        fcf = input_data.initial_fcf

        for year in range(1, input_data.projection_years + 1):
            idx = min(year - 1, len(recovery_growth) - 1)
            g = recovery_growth[idx]
            m = recovery_margin[idx]

            prev_rev = revenue
            revenue *= (1.0 + g)
            revenue = max(revenue, 0.0)

            delta_rev = revenue - prev_rev
            sim_sc = 0.5
            reinvestment = delta_rev / sim_sc

            prev_ic = invested_capital
            invested_capital += reinvestment
            if invested_capital < 0:
                invested_capital = 0.0

            tax_rate = 0.21
            sim_ebit_adj = revenue * m
            if prev_ic > 0:
                sim_roic = sim_ebit_adj * (1.0 - tax_rate) / prev_ic
            else:
                sim_roic = -0.50

            if year > 5:
                decay_factor = (year - 5) / (10.0 - 5.0 + 0.001)
                decay_factor = max(0.0, min(1.0, decay_factor))
                sim_roic = sim_roic * (1.0 - decay_factor) + effective_wacc * decay_factor

            fcf = sim_ebit_adj * (1.0 - tax_rate) - reinvestment
            pv_fcf = fcf / ((1.0 + effective_wacc) ** year)
            projected_fcfs.append(pv_fcf)

        total_pv_fcf = sum(projected_fcfs)
        terminal_fcf = fcf * (1.0 + input_data.terminal_growth)
        terminal_value = terminal_fcf / (effective_wacc - input_data.terminal_growth)
        pv_terminal = terminal_value / ((1.0 + effective_wacc) ** input_data.projection_years)

        intrinsic_value = total_pv_fcf + pv_terminal

        return MonteCarloSimRun(
            growth_rate=recovery_growth[0],
            operating_margin=recovery_margin[0],
            terminal_fcf=terminal_fcf,
            intrinsic_value=intrinsic_value,
            eva_positive=False,
            macro_shock_applied=True,
            regime="disrupted",
            catastrophe_event=True,
        )

    def _simulate_single(
        self, input_data: MonteCarloInput
    ) -> MonteCarloSimRun:
        # --- Bernoulli Shock Filter (replaces hardcoded catastrophe prob) ---
        bernoulli = BernoulliShockFilter()

        # Compute current margin volatility for dynamic sector scaling
        R_risk = input_data.supplier_concentration * (1.0 - input_data.culture_score)
        lambda_vol = 1.0 + 1.5 / (1.0 + np.exp(-10.0 * (R_risk - 0.5)))
        current_margin_vol = 0.04 * lambda_vol

        shock_result = bernoulli.run_trial_dynamic(
            icr=input_data.interest_coverage_ratio,
            sector=input_data.sector,
            current_margin_vol=current_margin_vol,
            margin_vol_10y=input_data.margin_variance_10y,
            supplier_concentration=input_data.supplier_concentration,
            geopolitical_stress_factor=input_data.geopolitical_stress_factor,
        )

        # Compute FCF penalty vector (graded, not binary)
        if shock_result.shock_occurred:
            fcf_penalty = bernoulli.compute_fcf_vector_penalty(
                icr=input_data.interest_coverage_ratio,
                n_years=input_data.projection_years,
                supplier_concentration=input_data.supplier_concentration,
                geopolitical_stress_factor=input_data.geopolitical_stress_factor,
                shock_severity=1.0,
            )
        else:
            fcf_penalty = [1.0] * input_data.projection_years

        effective_wacc, grp_premium = self._compute_effective_wacc(input_data)

        # --- Poisson Black Swan Engine (replaces old Bernoulli macro shocks) ---
        poisson_engine = PoissonBlackSwan(
            lambda_base=input_data.poisson_lambda_base,
        )

        # --- Markov Lifecycle: modulate margin_std from transition volatility ---
        lifecycle_vol = input_data.lifecycle_transition_volatility
        lifecycle_std_boost = 1.0 + lifecycle_vol * 0.3

        # Connection 2: Operational Stability Link (Internal Culture)
        R_risk = input_data.supplier_concentration * (1.0 - input_data.culture_score)
        lambda_vol = 1.0 + 1.5 / (1.0 + np.exp(-10.0 * (R_risk - 0.5)))
        margin_std = 0.04 * lambda_vol * lifecycle_std_boost

        # Sample operating margin from N(mu_M, sigma_M)
        margin = np.random.normal(input_data.operating_margin_mean, margin_std)
        margin = max(0.01, min(0.60, margin))

        # Connection 1: The Longevity Moat Link
        E = input_data.moat_score * (1.0 - input_data.a_tech)
        H = 1.0 / (1.0 + np.exp(-8.0 * (E - 0.5)))
        A = int(max(3, np.round(3 + H * 5)))
        B = int(max(A + 2, np.round(5 + H * 10)))
        
        if input_data.displacement_ratio > 1.0:
            compressed_A = min(A, 5)
            compressed_B = min(B, 6)
            if compressed_B < compressed_A:
                compressed_B = compressed_A + 1
            beta_val = np.random.beta(2.0, 5.0)
            sim_n_cap = int(np.round(compressed_A + (compressed_B - compressed_A) * beta_val))
        else:
            sim_n_cap = random.randint(A, B)

        # Connection 3: Supply Chain Risk Link
        sc_penalty = 1.0 - 0.4 / (1.0 + np.exp(-10.0 * (input_data.supplier_concentration - 0.7)))
        base_sc_ratio = input_data.initial_revenue / (input_data.initial_revenue * 0.8) if input_data.initial_revenue > 0 else 1.25
        if base_sc_ratio < 1.0:
            base_sc_ratio = 1.5
        mu_SC = base_sc_ratio * (0.5 + H) * sc_penalty
        
        if input_data.displacement_ratio > 1.0:
            if input_data.is_leader:
                sc_drag = min(0.30, 0.15 * (input_data.displacement_ratio - 1.0))
                mu_SC = mu_SC * (1.0 - sc_drag)
            else:
                sc_bonus = min(0.30, 0.15 * (input_data.displacement_ratio - 1.0))
                mu_SC = mu_SC * (1.0 + sc_bonus)
                
        sigma_SC = 0.15
        sim_sc = np.random.lognormal(np.log(mu_SC), sigma_SC)

        expected_growth = input_data.reinvestment_rate * input_data.roic
        growth = self._clamp_growth(
            np.random.normal(expected_growth, input_data.expected_growth_std)
        )

        # Apply Poisson-based systemic shocks (replaces old Bernoulli macro shocks)
        growth, margin, shock_applied, poisson_shock_count = self._apply_poisson_shocks(
            growth, margin, input_data, poisson_engine
        )

        projected_fcfs: List[float] = []
        revenue = input_data.initial_revenue
        invested_capital = input_data.initial_revenue * 0.8
        fcf = input_data.initial_fcf

        for year in range(1, input_data.projection_years + 1):
            prev_rev = revenue
            revenue *= (1.0 + growth)
            delta_rev = revenue - prev_rev
            reinvestment = delta_rev / sim_sc
            if reinvestment < 0:
                reinvestment = 0.0

            prev_ic = invested_capital
            invested_capital += reinvestment

            tax_rate = 0.21
            sim_ebit_adj = revenue * margin
            if prev_ic > 0:
                sim_roic = sim_ebit_adj * (1.0 - tax_rate) / prev_ic
            else:
                sim_roic = 0.0

            if year > sim_n_cap:
                decay_factor = (year - sim_n_cap) / (10.0 - sim_n_cap + 0.001)
                decay_factor = max(0.0, min(1.0, decay_factor))
                sim_roic = sim_roic * (1.0 - decay_factor) + effective_wacc * decay_factor

            fcf = sim_ebit_adj * (1.0 - tax_rate) - reinvestment
            fcf *= fcf_penalty[min(year - 1, len(fcf_penalty) - 1)]
            pv_fcf = fcf / ((1.0 + effective_wacc) ** year)
            projected_fcfs.append(pv_fcf)

        total_pv_fcf = sum(projected_fcfs)
        terminal_fcf = fcf * (1.0 + input_data.terminal_growth)
        terminal_value = terminal_fcf / (effective_wacc - input_data.terminal_growth)
        pv_terminal = terminal_value / ((1.0 + effective_wacc) ** input_data.projection_years)

        intrinsic_value = total_pv_fcf + pv_terminal
        eva_spread = input_data.roic - effective_wacc
        eva_positive = eva_spread > 0

        return MonteCarloSimRun(
            growth_rate=growth,
            operating_margin=margin,
            terminal_fcf=terminal_fcf,
            intrinsic_value=intrinsic_value,
            eva_positive=eva_positive,
            macro_shock_applied=shock_applied,
            regime="normal",
            catastrophe_event=False,
            bernoulli_shock_fired=shock_result.shock_occurred,
            poisson_shocks_count=poisson_shock_count,
        )

    def run(
        self,
        input_data: MonteCarloInput,
        source_weights: Optional[Dict[str, float]] = None,
    ) -> MonteCarloResult:
        n = input_data.n_simulations
        runs: List[MonteCarloSimRun] = []
        shocks = 0
        catastrophes = 0
        bernoulli_shocks = 0
        poisson_total = 0
        for _ in range(n):
            run = self._simulate_single(input_data)
            runs.append(run)
            if run.macro_shock_applied:
                shocks += 1
            if run.catastrophe_event:
                catastrophes += 1
            if run.bernoulli_shock_fired:
                bernoulli_shocks += 1
            poisson_total += run.poisson_shocks_count

        values = [r.intrinsic_value for r in runs]
        eva_positives = [r.eva_positive for r in runs]
        growth_rates = [r.growth_rate for r in runs]
        margins = [r.operating_margin for r in runs]
        terminal_fcfs = [r.terminal_fcf for r in runs]

        sorted_vals = sorted(values)
        n_vals = len(sorted_vals)
        p5 = sorted_vals[int(n_vals * 0.05)]
        p25 = sorted_vals[int(n_vals * 0.25)]
        p75 = sorted_vals[int(n_vals * 0.75)]
        p95 = sorted_vals[int(n_vals * 0.95)]

        mean_val = statistics.mean(values)
        median_val = statistics.median(values)
        std_val = statistics.stdev(values) if n_vals > 1 else 0.0

        positive_eva_prob = sum(eva_positives) / n_vals if n_vals > 0 else 0.0
        macro_risk_adj = 1.0 - (shocks / n_vals) if n_vals > 0 else 1.0
        survival_prob = 1.0 - (catastrophes / n_vals) if n_vals > 0 else 1.0
        _, grp_premium = self._compute_effective_wacc(input_data)

        # Compute Poisson stress lambda for reporting
        poisson_report = PoissonBlackSwan(lambda_base=input_data.poisson_lambda_base)
        mean_lambda_stress = poisson_report.compute_stress_lambda(
            input_data.credit_spread_bps, input_data.credit_spread_regime
        )

        # Run Markov lifecycle analysis for reporting
        markov = MarkovLifecycleChain(time_horizon=input_data.projection_years)
        lifecycle_metrics = LifecycleMetrics(
            reinvestment_rate=input_data.reinvestment_rate,
            roic=input_data.roic,
            revenue_growth=input_data.revenue_growth,
            margin_variance_10y=input_data.margin_variance_10y,
            operating_margin=input_data.operating_margin_mean,
            debt_to_capital=input_data.debt_to_capital,
            interest_coverage_ratio=input_data.interest_coverage_ratio,
        )
        markov_result = markov.analyze(input_data.ticker, lifecycle_metrics)

        confidence_band = self._format_confidence_band(
            positive_eva_prob, mean_val, median_val, std_val
        )

        return MonteCarloResult(
            ticker=input_data.ticker,
            n_simulations=n,
            positive_eva_probability=positive_eva_prob,
            mean_intrinsic_value=mean_val,
            median_intrinsic_value=median_val,
            std_intrinsic_value=std_val,
            p5_intrinsic_value=p5,
            p25_intrinsic_value=p25,
            p75_intrinsic_value=p75,
            p95_intrinsic_value=p95,
            mean_growth_rate=statistics.mean(growth_rates),
            std_growth_rate=statistics.stdev(growth_rates) if len(growth_rates) > 1 else 0.0,
            mean_terminal_margin=statistics.mean(margins),
            mean_terminal_fcf=statistics.mean(terminal_fcfs),
            macro_risk_adjustment=macro_risk_adj,
            confidence_band=confidence_band,
            projection_years=input_data.projection_years,
            survival_probability=survival_prob,
            catastrophe_event_count=catastrophes,
            mean_geopolitical_wacc_premium=grp_premium,
            bernoulli_shock_count=bernoulli_shocks,
            poisson_total_shocks=poisson_total,
            mean_poisson_lambda_stress=mean_lambda_stress,
            lifecycle_state=markov_result.current_state.value,
            lifecycle_projected_state=markov_result.projected_state.value,
            lifecycle_entropy=markov_result.transition_volatility,
            source_weights_snapshot=source_weights,
        )

    @staticmethod
    def _format_confidence_band(
        eva_prob: float, mean_iv: float, median_iv: float, std_iv: float
    ) -> str:
        if eva_prob >= 0.90:
            return f"{eva_prob:.0%} probability of generating positive EVA — High conviction"
        elif eva_prob >= 0.70:
            return f"{eva_prob:.0%} probability of generating positive EVA — Moderate conviction"
        elif eva_prob >= 0.50:
            return f"{eva_prob:.0%} probability of generating positive EVA — Low conviction"
        else:
            return f"{eva_prob:.0%} probability of generating positive EVA — Negative expected EVA"

    def build_input_from_fundamentals(
        self,
        ticker: str,
        revenue: float,
        fcf: float,
        roic: float,
        wacc: float,
        reinvestment_rate: float,
        operating_margin: float,
        growth_std: Optional[float] = None,
        margin_std: Optional[float] = None,
        culture_score: float = 0.5,
        supplier_concentration: float = 0.5,
        moat_score: float = 0.5,
        a_tech: float = 0.0,
        geopolitical_stress_factor: float = 0.0,
        geopolitical_risk_premium_rate: float = 0.0,
        displacement_ratio: float = 0.0,
        is_leader: bool = False,
        interest_coverage_ratio: float = 5.0,
        margin_variance_10y: float = 0.05,
        revenue_growth: float = 0.10,
        debt_to_capital: float = 0.30,
        poisson_lambda_base: float = 0.30,
        credit_spread_bps: Optional[float] = None,
        credit_spread_regime: str = "NORMAL",
        n_simulations: int = 10_000,
        sector: str = "technology",
    ) -> MonteCarloInput:
        expected_growth = reinvestment_rate * roic
        growth_std = growth_std or abs(expected_growth * 0.3)
        margin_std = margin_std or abs(operating_margin * 0.2)

        # Compute lifecycle transition volatility via Markov Chain
        markov = MarkovLifecycleChain(time_horizon=5)
        lifecycle_metrics = LifecycleMetrics(
            reinvestment_rate=reinvestment_rate,
            roic=roic,
            revenue_growth=revenue_growth,
            margin_variance_10y=margin_variance_10y,
            operating_margin=operating_margin,
            debt_to_capital=debt_to_capital,
            interest_coverage_ratio=interest_coverage_ratio,
        )
        markov_result = markov.analyze(ticker, lifecycle_metrics)

        return MonteCarloInput(
            ticker=ticker,
            expected_growth_mean=expected_growth,
            expected_growth_std=max(growth_std, 0.01),
            operating_margin_mean=operating_margin,
            operating_margin_std=max(margin_std, 0.005),
            wacc=wacc,
            reinvestment_rate=reinvestment_rate,
            roic=roic,
            initial_revenue=revenue,
            initial_fcf=fcf,
            culture_score=culture_score,
            supplier_concentration=supplier_concentration,
            moat_score=moat_score,
            a_tech=a_tech,
            geopolitical_stress_factor=geopolitical_stress_factor,
            geopolitical_risk_premium_rate=geopolitical_risk_premium_rate,
            displacement_ratio=displacement_ratio,
            is_leader=is_leader,
            interest_coverage_ratio=interest_coverage_ratio,
            margin_variance_10y=margin_variance_10y,
            revenue_growth=revenue_growth,
            debt_to_capital=debt_to_capital,
            poisson_lambda_base=poisson_lambda_base,
            credit_spread_bps=credit_spread_bps,
            credit_spread_regime=credit_spread_regime,
            lifecycle_transition_volatility=markov_result.transition_volatility,
            n_simulations=n_simulations,
            sector=sector,
        )


def create_monte_carlo_engine(
    config_dict: Optional[dict] = None,
) -> MonteCarloEngine:
    return MonteCarloEngine(config_dict)
