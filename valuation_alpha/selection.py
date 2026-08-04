"""Portfolio generation and ranking for the P2 selection layer."""

import numpy as np
import pandas as pd

from valuation_alpha.alpha import (
    ff5_residual_alpha,
    apply_slippage,
    excess_vs_sp500,
    portfolio_returns,
)
from valuation_alpha.stats import deflated_sharpe

_LIFECYCLE_QUALITY = {
    "FAST_GROWER": 1.0,
    "STALWART": 0.8,
    "SLOW_GROWER": 0.4,
    "CYCLICAL": 0.3,
    "TURNAROUND": 0.2,
    "ASSET_PLAY": 0.1,
}

_SECTOR_CAP = 0.30
_TRADING_DAYS = 252


def _zscore(s: pd.Series) -> pd.Series:
    s = s.astype(float)
    sd = s.std(ddof=1)
    if sd == 0 or np.isnan(sd):
        return pd.Series(0.0, index=s.index)
    return (s - s.mean()) / sd


def composite_score(df: pd.DataFrame, scoring: str = "blended") -> pd.Series:
    """Return a composite score Series indexed by ticker for the given scoring.

    "blended" weights 0.5 * zscore(alpha_3y_ann) + 0.3 * zscore(alpha_1y_ann)
    + 0.2 * percentile rank of lifecycle quality (FAST_GROWER/STALWART
    preferred). "alpha_only" ranks purely on alpha_3y_ann. "value" prefers LOW
    percentiles of debt_to_capital_pct and mahalanobis smallness as the current
    cheapness proxy until valuation multiples arrive.
    """
    if scoring == "alpha_only":
        out = df["alpha_3y_ann"].astype(float)
    elif scoring == "value":
        debt = _zscore(df["debt_to_capital_pct"].astype(float))
        maha = _zscore(df["mahalanobis"].astype(float))
        out = -(debt + maha)
    else:
        a3 = _zscore(df["alpha_3y_ann"].astype(float))
        a1 = _zscore(df["alpha_1y_ann"].astype(float))
        lq = df["lifecycle"].map(_LIFECYCLE_QUALITY).fillna(0.0)
        lq_pct = lq.rank(pct=True)
        out = 0.5 * a3 + 0.3 * a1 + 0.2 * lq_pct
    out.index = df["ticker"].values
    return out


def _sector_cap_weights(tickers: list, sectors: dict, cap: float = _SECTOR_CAP) -> dict:
    n = len(tickers)
    w = {t: 1.0 / n for t in tickers}
    sector_tickers = {}
    for t in tickers:
        sector_tickers.setdefault(sectors.get(t), []).append(t)
    for _ in range(50):
        sector_totals = {}
        for t in tickers:
            s = sectors.get(t)
            sector_totals[s] = sector_totals.get(s, 0.0) + w[t]
        capped = {s: min(tot, cap) for s, tot in sector_totals.items()}
        excess = sum(tot - cap for s, tot in sector_totals.items() if tot > cap)
        if excess <= 1e-12:
            break
        below = {s: capped[s] for s in sector_totals if sector_totals[s] < cap}
        total_below = sum(below.values())
        if total_below <= 0:
            break
        for s in below:
            capped[s] += excess * (below[s] / total_below)
        for s, ts in sector_tickers.items():
            sw = capped[s]
            for t in ts:
                w[t] = sw / len(ts)
    return w


def generate_candidates(
    names: pd.DataFrame,
    top_n: int = 5,
    k_values: list = [5, 10, 15],
    sectors: list = None,
    scoring: str = "blended",
) -> list:
    """Generate candidate portfolios, one per k in k_values.

    Each candidate is the top-k tickers by composite_score, drawn from the
    top_n pool, with equal weight within the basket and a sector-neutral cap
    (max 30% weight share per sector). Returns a list of dicts with name, k,
    scoring, tickers, and weights (summing to 1).
    """
    if sectors is None:
        sectors = dict(zip(names["ticker"], names["sector"]))
    score = composite_score(names, scoring)
    score = score.reindex(names["ticker"]).fillna(-np.inf)
    pool = score.nlargest(top_n).index.tolist()
    candidates = []
    for k in k_values:
        basket = pool[:k]
        weights = _sector_cap_weights(basket, sectors)
        candidates.append(
            {
                "name": f"k{k}_{scoring}",
                "k": k,
                "scoring": scoring,
                "tickers": basket,
                "weights": weights,
            }
        )
    return candidates


def rank_candidates(
    names: pd.DataFrame,
    factors: pd.DataFrame,
    sp500: pd.Series,
    prices: pd.DataFrame,
    candidates: list,
    horizon_days: int = 756,
) -> pd.DataFrame:
    """Backtest each candidate portfolio and return a ranking DataFrame.

    prices is the daily returns DataFrame (ticker columns). Each candidate is
    combined via portfolio_returns, charged slippage, and scored with
    ff5_residual_alpha at horizon_days. Rows are sorted by alpha descending.
    """
    rows = []
    n_trials = len(candidates)
    if prices is not None and not prices.empty and abs(float(prices.median().median())) > 1.5:
        prices = prices.pct_change()
    for cand in candidates:
        port = portfolio_returns(prices, cand["weights"])
        port_net = apply_slippage(port, slippage=0.005)
        alpha_res = ff5_residual_alpha(port_net, factors, horizon_days=horizon_days)
        if alpha_res is None:
            continue
        sd = port_net.std(ddof=1)
        sharpe = port_net.mean() / sd * np.sqrt(_TRADING_DAYS) if sd > 0 else 0.0
        dsr = deflated_sharpe(sharpe, alpha_res["n_obs"], n_trials)
        ex = excess_vs_sp500(port_net, sp500)
        rows.append(
            {
                "candidate_name": cand["name"],
                "tickers": ",".join(cand["tickers"]),
                "alpha_annualized": alpha_res["alpha_annualized"],
                "ci_lower": alpha_res["ci_lower"],
                "ci_upper": alpha_res["ci_upper"],
                "sharpe": sharpe,
                "deflated_sharpe": dsr["dsr"],
                "excess_sp500": ex["excess_annualized"] if ex else np.nan,
                "n_obs": alpha_res["n_obs"],
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("alpha_annualized", ascending=False).reset_index(drop=True)
    return df