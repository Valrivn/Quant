"""
Monte Carlo DCF valuation.
"""

from typing import Dict, Any, Optional
import numpy as np
import pandas as pd


def monte_carlo_valuation(
    base_wacc: float,
    base_growth: float,
    base_term_growth: float,
    n: int = 10000,
    seed: Optional[Any] = None,
    macro: Optional[pd.DataFrame] = None,
    revenue: Optional[float] = None,
    margin_mean: Optional[float] = None,
    margin_std: Optional[float] = None,
    growth_std: Optional[float] = None,
    tax_rate: Optional[float] = None,
    reinvestment_rate: Optional[float] = None,
    net_debt: Optional[float] = None,
    shares_out: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Monte Carlo DCF valuation simulating path outputs and perturbing inputs.
    Supports simple proxy valuation or detailed 10-year Damodaran DCF simulation.
    Emits p10, p25, p50, p75, p90, mean, std, z-score, and seed.
    """
    seed_int = None
    if seed is not None:
        if isinstance(seed, str):
            seed_int = int(seed, 16) if all(c in '0123456789abcdefABCDEF' for c in seed) else hash(seed) % (2**31 - 1)
        else:
            seed_int = int(seed)

    rng = np.random.default_rng(seed_int)

    if revenue is not None and revenue > 0 and margin_mean is not None and shares_out is not None and shares_out > 0:
        # Full 10-year Damodaran DCF simulation
        m_std = margin_std if margin_std is not None and margin_std > 0 else 0.05
        g_std = growth_std if growth_std is not None and growth_std > 0 else max(0.5 * m_std, 0.05)
        tau = tax_rate if tax_rate is not None else 0.21
        reinv = reinvestment_rate if reinvestment_rate is not None else 0.30
        nd = net_debt if net_debt is not None else 0.0

        margin_samples = rng.normal(margin_mean, m_std, size=n)
        margin_samples = np.clip(margin_samples, -0.20, 0.90)

        growth_samples = rng.normal(base_growth, g_std, size=n)
        growth_samples = np.clip(growth_samples, -0.30, 0.60)

        wacc_samples = rng.normal(base_wacc, 0.005, size=n)
        wacc_samples = np.maximum(wacc_samples, base_term_growth + 0.005)

        rev_paths = np.zeros((n, 10))
        cur_rev = np.full(n, float(revenue))

        # Years 1-5 (high growth)
        for t in range(5):
            cur_rev = cur_rev * (1.0 + growth_samples)
            rev_paths[:, t] = cur_rev

        # Years 6-10 (linear fade to terminal growth)
        for k in range(1, 6):
            fade_g = growth_samples + (k / 5.0) * (base_term_growth - growth_samples)
            cur_rev = cur_rev * (1.0 + fade_g)
            rev_paths[:, 5 + k - 1] = cur_rev

        nopat_paths = rev_paths * margin_samples[:, None] * (1.0 - tau)
        fcf_paths = nopat_paths * (1.0 - reinv)

        years = np.arange(1, 11)
        discount_factors = (1.0 + wacc_samples[:, None]) ** years
        pv_fcf = np.sum(fcf_paths / discount_factors, axis=1)

        reinv_term = min(reinv, 0.30)
        nopat_10 = nopat_paths[:, 9]
        denom = np.maximum(wacc_samples - base_term_growth, 0.005)
        tv10 = (nopat_10 * (1.0 + base_term_growth) * (1.0 - reinv_term)) / denom
        pv_tv = tv10 / ((1.0 + wacc_samples) ** 10)

        ev_samples = pv_fcf + pv_tv
        equity_val_samples = np.maximum(ev_samples - nd, 0.0)
        valuations = equity_val_samples / float(shares_out)
    else:
        # Simple proxy path
        wacc_samples = base_wacc + rng.normal(0, 0.01, size=n)
        growth_samples = base_growth + rng.normal(0, 0.02, size=n)
        term_growth_samples = base_term_growth + rng.normal(0, 0.005, size=n)
        wacc_samples = np.maximum(wacc_samples, term_growth_samples + 0.005)
        valuations = (1.0 + growth_samples) / (wacc_samples - term_growth_samples)

    p10 = float(np.percentile(valuations, 10))
    p25 = float(np.percentile(valuations, 25))
    p50 = float(np.percentile(valuations, 50))
    p75 = float(np.percentile(valuations, 75))
    p90 = float(np.percentile(valuations, 90))
    mean_val = float(np.mean(valuations))
    std_val = float(np.std(valuations)) if np.std(valuations) > 0 else 1.0

    trailing_5yr_mean = mean_val
    if macro is not None and "valuation_hist" in macro.columns:
        trailing_5yr_mean = float(macro["valuation_hist"].tail(5 * 252).mean())

    z_score = float((mean_val - trailing_5yr_mean) / std_val)

    return {
        "valuations": valuations,
        "p10": p10,
        "p25": p25,
        "p50": p50,
        "p75": p75,
        "p90": p90,
        "mean": mean_val,
        "std": std_val,
        "z_score": z_score,
        "seed": seed,
    }

