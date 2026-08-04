"""Thin service layer bridging Quantitative/stochastic models to the dashboard.

Exposes plain-dict/DataFrame builders for the four stochastic risk sections:

1. Monte Carlo / Poisson black-swan portfolio impact (PoissonBlackSwan.simulate)
2. Markov lifecycle projection (MarkovLifecycleChain(time_horizon=5).analyze)
3. Bernoulli shock default probability (BernoulliShockFilter + get_default_probability)
4. Dynamic sector shock probability (compute_dynamic_shock_probability)

LifecycleMetrics inputs are resolved in priority order:
1. an optional caller-provided ``metrics`` dict (recommended for real data),
2. per-ticker fundamentals from
   Quantitative/psychological/four_lane_pipeline.FUNDAMENTAL_ESTIMATES when that
   module imports cleanly (repo data source; may be unavailable at runtime),
3. documented sane defaults (DEFAULT_METRICS).

No new DB tables or network calls are introduced.
"""

import random
import time
from dataclasses import asdict

import pandas as pd

from Quantitative.stochastic.bernoulli_shock_filter import BernoulliShockFilter
from Quantitative.stochastic.default_probability_table import (
    get_default_probability,
    get_synthetic_rating,
)
from Quantitative.stochastic.markov_lifecycle import (
    MarkovLifecycleChain,
    LifecycleMetrics,
    STATES,
    N_STATES,
    STATE_INDEX,
)
from Quantitative.stochastic.poisson_blackswan import PoissonBlackSwan
from Quantitative.stochastic.sector_shock_data import (
    compute_dynamic_shock_probability,
    get_sector_shock_stats,
)

DEFAULT_SECTOR = "semiconductor"

DEFAULT_N_PORTFOLIOS = 500

KNOWN_SECTORS = [
    "semiconductor",
    "enterprise_software",
    "cloud_internet",
    "consumer_electronics",
    "hardware_oem",
    "networking",
]

DEFAULT_METRICS = {
    "reinvestment_rate": 0.40,
    "roic": 0.20,
    "revenue_growth": 0.08,
    "margin_variance_10y": 0.05,
    "operating_margin": 0.20,
    "debt_to_capital": 0.30,
    "interest_coverage_ratio": 8.0,
    "cash_burn_months": 0.0,
}


def _load_fundamental_estimates(ticker):
    try:
        from Qualitative.psychological.four_lane_pipeline import FUNDAMENTAL_ESTIMATES
    except Exception:
        try:
            from psychological.four_lane_pipeline import FUNDAMENTAL_ESTIMATES
        except Exception:
            return None
    return FUNDAMENTAL_ESTIMATES.get(ticker)


def build_lifecycle_metrics(ticker, metrics=None, use_fundamentals=True):
    merged = dict(DEFAULT_METRICS)
    if metrics is not None:
        merged.update(metrics)
    elif use_fundamentals:
        est = _load_fundamental_estimates(ticker)
        if est is not None:
            rr = est.get("rr", merged["reinvestment_rate"])
            roic = est.get("roic", merged["roic"])
            merged.update({
                "reinvestment_rate": rr,
                "roic": roic,
                "revenue_growth": est.get("revenue_growth", rr * roic),
                "margin_variance_10y": est.get("margin_variance_10y", merged["margin_variance_10y"]),
                "operating_margin": est.get("op_margin", merged["operating_margin"]),
                "debt_to_capital": est.get("debt_to_capital", merged["debt_to_capital"]),
                "interest_coverage_ratio": est.get("icr", merged["interest_coverage_ratio"]),
                "cash_burn_months": est.get("cash_burn_months", merged["cash_burn_months"]),
            })
    return LifecycleMetrics(**merged)


def resolve_sector(ticker, metrics=None, sector=None):
    if sector is not None:
        return sector
    if metrics is not None and metrics.get("sector"):
        return metrics["sector"]
    est = _load_fundamental_estimates(ticker)
    if est is not None and est.get("sector"):
        return est["sector"]
    return DEFAULT_SECTOR


def _run_markov(ticker, metrics=None, time_horizon=5):
    lifecycle_metrics = build_lifecycle_metrics(ticker, metrics)
    chain = MarkovLifecycleChain(time_horizon=time_horizon)
    result = chain.analyze(ticker, lifecycle_metrics)
    report = {
        "ticker": ticker,
        "current_state": result.current_state.value,
        "projected_state": result.projected_state.value,
        "n_steps": result.n_steps,
        "state_distribution": result.state_distribution,
        "projected_distribution": result.projected_distribution,
        "transition_volatility": result.transition_volatility,
        "convergence_step": result.convergence_step,
        "transition_matrix": result.transition_matrix,
        "metrics_used": asdict(lifecycle_metrics),
    }
    return chain, result, report


def run_markov_lifecycle(ticker, metrics=None, time_horizon=5):
    _, _, report = _run_markov(ticker, metrics, time_horizon)
    return report


def build_markov_projection_df(ticker, metrics=None, time_horizon=5):
    chain, result, report = _run_markov(ticker, metrics, time_horizon)
    initial_dist = chain.compute_initial_distribution(result.current_state)
    rows = []
    for step in range(time_horizon + 1):
        dist = chain.project_distribution(initial_dist, result.transition_matrix, step)
        row = {"step": step}
        for state in STATES:
            row[state.value] = round(dist[STATE_INDEX[state]], 4)
        rows.append(row)
    return pd.DataFrame(rows), report


def run_bernoulli_shock(
    icr=DEFAULT_METRICS["interest_coverage_ratio"],
    supplier_concentration=0.5,
    geopolitical_stress_factor=0.0,
    shock_severity=1.0,
    seed=42,
):
    shock_filter = BernoulliShockFilter(use_dynamic_icr=False)
    result = shock_filter.run_trial(
        icr=icr,
        supplier_concentration=supplier_concentration,
        geopolitical_stress_factor=geopolitical_stress_factor,
        shock_severity=shock_severity,
        rng=random.Random(seed),
    )
    return {
        "icr": icr,
        "synthetic_rating": get_synthetic_rating(icr),
        "p_default_1y": get_default_probability(icr, horizon=1),
        "p_default_5y": get_default_probability(icr, horizon=5),
        "shock_occurred": result.shock_occurred,
        "shock_probability": result.shock_probability,
        "penalty_multiplier": result.penalty_multiplier,
        "lgd": result.lgd,
        "recovery_rate": result.recovery_rate,
    }


def run_mc_portfolio_impact(
    current_spread_bps=None,
    regime="NORMAL",
    n_portfolios=DEFAULT_N_PORTFOLIOS,
    portfolio_weights=None,
):
    start = time.perf_counter()
    poisson = PoissonBlackSwan()
    result = poisson.simulate(
        current_spread_bps=current_spread_bps,
        regime=regime,
        portfolio_weights=portfolio_weights,
        n_portfolios=n_portfolios,
    )
    compute_ms = (time.perf_counter() - start) * 1000.0
    return {
        "n_shocks": result.n_shocks,
        "lambda_base": result.lambda_base,
        "lambda_stress": result.lambda_stress,
        "spread_ratio": result.spread_ratio,
        "current_spread_bps": result.current_spread_bps,
        "regime": result.regime,
        "portfolio_impact": result.portfolio_impact,
        "shock_magnitudes": result.shock_magnitudes,
        "compute_ms": round(compute_ms, 2),
    }


def run_sector_shock(
    sector=None,
    current_margin_vol=0.05,
    margin_vol_10y=None,
    supplier_concentration=0.5,
    geopolitical_stress_factor=0.0,
):
    sector = sector or DEFAULT_SECTOR
    stats = get_sector_shock_stats(sector)
    ref_vol = margin_vol_10y if margin_vol_10y is not None else stats.margin_vol_10y
    p = compute_dynamic_shock_probability(
        sector=sector,
        current_margin_vol=current_margin_vol,
        margin_vol_10y=margin_vol_10y,
        supplier_concentration=supplier_concentration,
        geopolitical_stress_factor=geopolitical_stress_factor,
    )
    return {
        "sector": sector,
        "p_base": stats.p_base,
        "margin_vol_10y": ref_vol,
        "current_margin_vol": current_margin_vol,
        "supplier_concentration": supplier_concentration,
        "geopolitical_stress_factor": geopolitical_stress_factor,
        "shock_probability": p,
    }


def build_sector_shock_curve(
    sector=None,
    margin_vol_10y=None,
    supplier_concentration=0.5,
    geopolitical_stress_factor=0.0,
):
    vols = [0.01, 0.02, 0.03, 0.05, 0.08, 0.10, 0.15, 0.20]
    rows = []
    for v in vols:
        res = run_sector_shock(
            sector=sector,
            current_margin_vol=v,
            margin_vol_10y=margin_vol_10y,
            supplier_concentration=supplier_concentration,
            geopolitical_stress_factor=geopolitical_stress_factor,
        )
        rows.append({"current_margin_vol": v, "shock_probability": res["shock_probability"]})
    return pd.DataFrame(rows)


def build_stochastic_report(
    ticker,
    metrics=None,
    sector=None,
    current_spread_bps=None,
    regime="NORMAL",
    n_portfolios=DEFAULT_N_PORTFOLIOS,
    portfolio_weights=None,
    icr=None,
):
    markov = run_markov_lifecycle(ticker, metrics)
    eff_icr = icr if icr is not None else markov["metrics_used"]["interest_coverage_ratio"]
    resolved_sector = resolve_sector(ticker, metrics, sector)
    return {
        "ticker": ticker,
        "metrics_used": markov["metrics_used"],
        "markov": markov,
        "bernoulli": run_bernoulli_shock(icr=eff_icr),
        "sector_shock": run_sector_shock(
            sector=resolved_sector,
            current_margin_vol=markov["metrics_used"]["margin_variance_10y"],
        ),
        "mc": run_mc_portfolio_impact(
            current_spread_bps=current_spread_bps,
            regime=regime,
            n_portfolios=n_portfolios,
            portfolio_weights=portfolio_weights,
        ),
    }
