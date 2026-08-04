"""L1 core engine for the equity sleeve."""

import numpy as np
import pandas as pd

from Quantitative.stochastic.markov_lifecycle import MarkovLifecycleChain
from valuation_alpha.universe.roster import get_universe
from valuation_alpha.ratios import (
    compute_lifecycle_metrics,
    peer_percentiles,
    mahalanobis_state,
)
from valuation_alpha.alpha import ff5_residual_alpha, apply_slippage, excess_vs_sp500

_METRIC_KEYS = [
    "reinvestment_rate",
    "roic",
    "revenue_growth",
    "margin_variance_10y",
    "operating_margin",
    "debt_to_capital",
    "interest_coverage_ratio",
    "cash_burn_months",
]


def run_l1(
    universe_tickers: list,
    prices: pd.DataFrame,
    factors: pd.DataFrame,
    sp500: pd.Series,
    sector_by_ticker: dict,
    quarterly_by_ticker: dict,
    horizons_days: dict = {252: "1y", 756: "3y"},
    include_bias: bool = True,
    slippage: float = 0.005,
) -> dict:
    """Run the L1 equity-sleeve core engine over the universe.

    Computes lifecycle metrics, peer percentiles, Mahalanobis distance, Markov
    lifecycle projection, and per-horizon residual alpha / excess vs S&P 500
    for each name. Tickers lacking data are recorded with NaN rather than
    crashing the run. Returns {"names": df, "markov": dict, "config": dict}.
    """
    bias_by = {r["ticker"]: r["bias"] for r in get_universe()}
    tickers = [
        t for t in universe_tickers if include_bias or not bias_by.get(t, False)
    ]

    metrics_by_ticker = {}
    for t in tickers:
        q = quarterly_by_ticker.get(t)
        if q is not None and not q.empty:
            metrics_by_ticker[t] = compute_lifecycle_metrics(q)

    pct = peer_percentiles(metrics_by_ticker, sector_by_ticker, _METRIC_KEYS)
    maha = mahalanobis_state(metrics_by_ticker, _METRIC_KEYS)

    chain = MarkovLifecycleChain(time_horizon=5)
    markov = {}
    for t in tickers:
        if t in metrics_by_ticker:
            try:
                markov[t] = chain.analyze(t, metrics_by_ticker[t]).projected_state.value
            except Exception:
                markov[t] = None
        else:
            markov[t] = None

    rows = []
    for t in tickers:
        row = {
            "ticker": t,
            "sector": sector_by_ticker.get(t),
            "bias": bias_by.get(t, False),
            "lifecycle": markov.get(t),
            "mahalanobis": np.nan,
        }
        if t in maha.index:
            row["mahalanobis"] = maha.loc[t, "mahalanobis"]
        for key in _METRIC_KEYS:
            col = f"{key}_pct"
            row[col] = pct.loc[t, col] if t in pct.index else np.nan

        ret = pd.Series(dtype=float)
        if t in prices.columns:
            ret = prices[t].pct_change().dropna()

        for h, label in horizons_days.items():
            ret_net = apply_slippage(ret, slippage=slippage)
            a = ff5_residual_alpha(ret_net, factors, horizon_days=h)
            row[f"alpha_{label}_ann"] = a["alpha_annualized"] if a else np.nan
            row[f"alpha_{label}_ci_lower"] = a["ci_lower"] if a else np.nan
            row[f"alpha_{label}_ci_upper"] = a["ci_upper"] if a else np.nan
            ex = excess_vs_sp500(ret, sp500) if len(ret) else None
            row[f"excess_{label}"] = ex["excess_annualized"] if ex else np.nan
        rows.append(row)

    names = pd.DataFrame(rows)
    config = {
        "horizons_days": horizons_days,
        "include_bias": include_bias,
        "slippage": slippage,
        "time_horizon": 5,
    }
    return {"names": names, "markov": markov, "config": config}