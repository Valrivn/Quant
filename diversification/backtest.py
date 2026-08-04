"""Walk-forward replay engine for the L2 diversification sleeve.

Replays the existing bond/gold/fund selector decision logic over the approved
sleeve asset set on trailing data only (no lookahead), then evaluates each
sleeve and the equal-weight 4-sleeve portfolio.

Replicated thresholds (read from the existing modules):
  - CreditSpreadMonitor._classify_regime: NORMAL < 200 bps, WIDENING 200-300,
    CRISIS > 300 bps (spread in basis points).
  - TreasuryAnchor REGIME_PROFILES: short-bill weights per regime
    (NORMAL BIL/SHY 40/60, WIDENING 60/40, CRISIS 80/20, UNKNOWN 50/50).
  - GoldMacroValuation: real-rate component maps real rate to [0,1] with
    high threshold +2.0% and low threshold 0.0%; M2 component maps M2 YoY to
    [0,1] with threshold +10%; composite = 0.5*real + 0.5*m2; signal
    UNDERVALUED >= 0.6, OVERVALUED <= 0.3, else FAIR_VALUE.
  - LiquidityGatekeeper: ADV > 1,000,000 shares/day, median bid-ask spread
    <= 0.02%, NAV deviation within +/-0.10%.
  - Equity leg: fixed documented split VTI/VB/BND = 55/25/20 (index_fund_handler
    is live-screening; historical replay uses this documented constant).
"""

import numpy as np
import pandas as pd

from Quantitative.bonds.credit_spread_monitor import CreditSpreadMonitor, SpreadRegime
from Quantitative.bonds.treasury_anchor import REGIME_PROFILES
from Quantitative.gold_etf.gold_macro_valuation import GoldMacroValuation, GoldMacroSignal
from valuation_alpha.alpha import ff5_residual_alpha, excess_vs_sp500
from valuation_alpha.stats import block_bootstrap_alpha, deflated_sharpe

from diversification.datastore import SLEEVES

_TRADING_DAYS = 252
SPREAD_LOOKBACK_DAYS = 90
M2_LAG_MONTHS = 12

CORPORATE_SPLIT = {"VCSH": 0.5, "VCIT": 0.5}
GOLD_SPLIT = {"GLD": 0.5, "IAU": 0.5}
GOLD_OVERVALUED_SPLIT = {"GLD": 0.25, "IAU": 0.25, "BIL": 0.5}
EQUITY_SPLIT = {"VTI": 0.55, "VB": 0.25, "BND": 0.20}
PORTFOLIO_WEIGHTS = {sleeve: 0.25 for sleeve in SLEEVES}


def _trailing_median(series: pd.Series, date, lookback: int) -> float:
    """Median of series values in the trailing lookback window ending at date."""
    if series is None or series.empty:
        return np.nan
    window = series[series.index <= date].tail(lookback)
    if window.empty:
        return np.nan
    return float(window.median())


def _trailing_last(series: pd.Series, date) -> float:
    """Last value of series at or before date."""
    if series is None or series.empty:
        return np.nan
    window = series[series.index <= date]
    if window.empty:
        return np.nan
    return float(window.iloc[-1])


def _m2_yoy(m2_series: pd.Series, date) -> float:
    """M2 year-over-year growth (%) computed from trailing M2SL values."""
    if m2_series is None or m2_series.empty:
        return np.nan
    window = m2_series[m2_series.index <= date]
    if len(window) < 2:
        return np.nan
    latest = float(window.iloc[-1])
    target = window.index[-1] - pd.DateOffset(months=M2_LAG_MONTHS)
    prior = window[window.index <= target]
    if prior.empty:
        return np.nan
    base = float(prior.iloc[-1])
    if base == 0:
        return np.nan
    return (latest - base) / base * 100.0


def _bond_decision(fred: dict, date, monitor: CreditSpreadMonitor) -> tuple:
    """Decide the bond sleeve holdings from trailing credit spread data."""
    spread_series = fred.get("BAA10Y")
    spread_pct = _trailing_median(spread_series, date, SPREAD_LOOKBACK_DAYS)
    spread_bps = spread_pct * 100.0 if not np.isnan(spread_pct) else np.nan
    if np.isnan(spread_bps):
        regime = SpreadRegime.UNKNOWN
    else:
        regime = monitor._classify_regime(spread_bps)
    if regime in (SpreadRegime.CRISIS, SpreadRegime.WIDENING):
        choice = "short_bills"
        weights = dict(REGIME_PROFILES[regime])
    else:
        choice = "corporate"
        weights = dict(CORPORATE_SPLIT)
    return choice, weights, regime


def _gold_decision(fred: dict, date, valuation: GoldMacroValuation) -> tuple:
    """Decide the gold sleeve holdings from trailing real-rate and M2 data."""
    real_rate = _trailing_last(fred.get("DFII10"), date)
    m2_yoy = _m2_yoy(fred.get("M2SL"), date)
    components = []
    if not np.isnan(real_rate):
        components.append(valuation._compute_real_rate_component(real_rate))
    if not np.isnan(m2_yoy):
        components.append(valuation._compute_m2_component(m2_yoy))
    if components:
        composite = float(np.mean(components))
        signal = valuation._classify_signal(composite)
    else:
        composite = np.nan
        signal = GoldMacroSignal.UNKNOWN
    if signal == GoldMacroSignal.OVERVALUED:
        choice = "half_gold"
        weights = dict(GOLD_OVERVALUED_SPLIT)
    else:
        choice = "full_gold"
        weights = dict(GOLD_SPLIT)
    return choice, weights, signal, real_rate, m2_yoy


def _equity_decision() -> tuple:
    """Decide the equity sleeve holdings (documented fixed split)."""
    return "fixed_split", dict(EQUITY_SPLIT)


def _rebalance_dates(prices: pd.DataFrame, rebalance_months: int, start, end) -> list:
    """Trading dates on which the sleeve is rebalanced."""
    idx = prices.index
    if start is not None:
        idx = idx[idx >= pd.Timestamp(start)]
    if end is not None:
        idx = idx[idx <= pd.Timestamp(end)]
    if len(idx) == 0:
        return []
    periods = pd.date_range(idx[0], idx[-1], freq=f"{rebalance_months}ME")
    dates = [idx[0]]
    for p in periods:
        cand = idx[idx <= p]
        if len(cand) and cand[-1] != dates[-1]:
            dates.append(cand[-1])
    return dates


def _sleeve_daily_returns(
    prices: pd.DataFrame,
    rebalance_dates: list,
    decisions_weights: list,
    slippage: float,
) -> pd.DataFrame:
    """Build daily sleeve returns from a per-rebalance weights schedule."""
    rets = prices.pct_change()
    sleeve_returns = {}
    for sleeve in SLEEVES:
        tickers = [t for t in SLEEVES[sleeve] if t in prices.columns]
        if not tickers:
            sleeve_returns[sleeve] = pd.Series(0.0, index=prices.index)
            continue
        weight_matrix = pd.DataFrame(0.0, index=prices.index, columns=tickers)
        for i, date in enumerate(rebalance_dates):
            end = rebalance_dates[i + 1] if i + 1 < len(rebalance_dates) else None
            period = prices.index[(prices.index >= date)]
            if end is not None:
                period = period[period < end]
            w = decisions_weights[i].get(sleeve, {})
            for t in tickers:
                weight_matrix.loc[period, t] = w.get(t, 0.0)
        sleeve_ret = (weight_matrix * rets[tickers]).sum(axis=1)
        sleeve_ret = sleeve_ret.mask(weight_matrix.sum(axis=1) == 0, 0.0)
        for date in rebalance_dates:
            if date in sleeve_ret.index:
                sleeve_ret.loc[date] = sleeve_ret.loc[date] - slippage
        sleeve_returns[sleeve] = sleeve_ret
    return pd.DataFrame(sleeve_returns)


def walk_forward_replay(
    historical: dict,
    rebalance_months: int = 3,
    start: str = None,
    end: str = None,
    slippage: float = 0.005,
    seed: int = 0,
) -> dict:
    """Replay the sleeve decision logic over a walk-forward window.

    At each rebalance date the sleeve holdings are decided from trailing data
    only (no lookahead). Returns sleeve_returns, portfolio_returns, decisions,
    and config.
    """
    prices = historical.get("prices")
    fred = historical.get("fred", {})
    if prices is None or prices.empty:
        return {
            "sleeve_returns": pd.DataFrame(),
            "portfolio_returns": pd.Series(dtype=float),
            "decisions": pd.DataFrame(),
            "config": {},
        }

    monitor = CreditSpreadMonitor()
    valuation = GoldMacroValuation()
    dates = _rebalance_dates(prices, rebalance_months, start, end)

    decisions = []
    weights_schedule = []
    for date in dates:
        bond_choice, bond_weights, regime = _bond_decision(fred, date, monitor)
        gold_choice, gold_weights, signal, real_rate, m2_yoy = _gold_decision(fred, date, valuation)
        equity_choice, equity_weights = _equity_decision()

        sleeve_weights = {
            "corporate_bonds": bond_weights if bond_choice == "corporate" else {t: 0.0 for t in SLEEVES["corporate_bonds"]},
            "short_bills": bond_weights if bond_choice == "short_bills" else {t: 0.0 for t in SLEEVES["short_bills"]},
            "gold": gold_weights,
            "equity_income": equity_weights,
        }
        weights_schedule.append(sleeve_weights)
        decisions.append(
            {
                "date": date,
                "bond_choice": bond_choice,
                "gold_choice": gold_choice,
                "spread_regime": regime.value,
                "real_rate": real_rate,
                "m2_yoy": m2_yoy,
                "weights": sleeve_weights,
            }
        )

    sleeve_returns = _sleeve_daily_returns(prices, dates, weights_schedule, slippage)
    portfolio_returns = sum(sleeve_returns[s] * PORTFOLIO_WEIGHTS[s] for s in SLEEVES)

    return {
        "sleeve_returns": sleeve_returns,
        "portfolio_returns": portfolio_returns,
        "decisions": pd.DataFrame(decisions),
        "config": {
            "rebalance_months": rebalance_months,
            "start": start,
            "end": end,
            "slippage": slippage,
            "seed": seed,
        },
    }


def run_sleeve_backtest(
    historical: dict,
    benchmark: pd.Series = None,
    horizon_days: int = 756,
) -> pd.DataFrame:
    """Evaluate each sleeve and the 4-sleeve portfolio.

    Uses FF5 residual alpha and block bootstrap when factors are present in
    historical["factors"]; otherwise falls back to simple annualized
    return/vol/SR. Missing benchmark/factors yield NaN columns.
    """
    replay = walk_forward_replay(historical)
    sleeve_returns = replay["sleeve_returns"]
    if sleeve_returns.empty:
        return pd.DataFrame()
    factors = historical.get("factors")
    if factors is not None and factors.empty:
        factors = None

    rows = []
    candidates = [(s, sleeve_returns[s]) for s in SLEEVES] + [("portfolio", replay["portfolio_returns"])]
    n_trials = len(candidates)
    for name, sr in candidates:
        sr = sr.dropna()
        n_obs = len(sr)
        ann_ret = float(sr.mean() * _TRADING_DAYS) if n_obs else np.nan
        ann_vol = float(sr.std(ddof=1) * np.sqrt(_TRADING_DAYS)) if n_obs > 1 else np.nan
        sharpe = ann_ret / ann_vol if ann_vol and ann_vol > 0 else np.nan
        dsr = deflated_sharpe(sharpe, n_obs, n_trials)["dsr"] if n_obs > 1 else np.nan

        alpha_ann = np.nan
        alpha_lo = np.nan
        alpha_hi = np.nan
        boot_lo = np.nan
        boot_hi = np.nan
        if factors is not None:
            alpha_res = ff5_residual_alpha(sr, factors, horizon_days=horizon_days)
            if alpha_res is not None:
                alpha_ann = alpha_res["alpha_annualized"]
                alpha_lo = alpha_res["ci_lower"]
                alpha_hi = alpha_res["ci_upper"]
            boot = block_bootstrap_alpha(sr, factors, horizon_days=horizon_days)
            boot_lo = boot["boot_ci_lower"]
            boot_hi = boot["boot_ci_upper"]

        excess = np.nan
        if benchmark is not None and len(benchmark):
            ex = excess_vs_sp500(sr, benchmark)
            if ex is not None:
                excess = ex["excess_annualized"]

        rows.append(
            {
                "sleeve": name,
                "annualized_return": ann_ret,
                "annualized_vol": ann_vol,
                "sharpe": sharpe,
                "deflated_sharpe": dsr,
                "alpha_annualized": alpha_ann,
                "alpha_ci_lower": alpha_lo,
                "alpha_ci_upper": alpha_hi,
                "excess_sp500": excess,
                "boot_ci_lower": boot_lo,
                "boot_ci_upper": boot_hi,
            }
        )
    return pd.DataFrame(rows)