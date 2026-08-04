"""Macro-state allocator (Phase 1, D-20260803-003).

Classifies the market regime from PRE-REGISTERED thresholds (reusing the 200/300
bps credit-spread ladder of ``credit_spread_monitor``) plus an equity 200-day
trend confirmation:

  bull   -> trailing median BAA10Y spread < 200 bps AND equity above its 200d SMA
  bear   -> trailing median spread > 300 bps OR equity below its 200d SMA
  neutral-> otherwise

Target sleeve weights come from ``sleeves.MACRO_TARGETS``. All thresholds are
module constants, never fit; the trend uses a trailing window only (no
lookahead). Unknown/missing inputs degrade to "neutral" rather than forcing a
tilt.
"""

import numpy as np
import pandas as pd

SPREAD_LOOKBACK_DAYS = 90
SMA_DAYS = 200
MIN_TREND_DAYS = 20
THRESHOLD_WIDENING_BPS = 200.0
THRESHOLD_CRISIS_BPS = 300.0


def _trailing_median(series, date, lookback):
    if series is None or series.empty:
        return np.nan
    window = series[series.index <= date].tail(lookback)
    if window.empty:
        return np.nan
    return float(window.median())


def classify_state(spread_series, equity_series, date):
    """Return "bull" | "bear" | "neutral" for a date from trailing data only."""
    spread_pct = _trailing_median(spread_series, date, SPREAD_LOOKBACK_DAYS)
    spread_bps = spread_pct * 100.0 if spread_pct == spread_pct else np.nan

    px = pd.Series(dtype=float)
    if equity_series is not None and len(equity_series):
        px = equity_series[equity_series.index <= date]
    trend_known = len(px) >= MIN_TREND_DAYS
    sma = float(px.tail(SMA_DAYS).mean()) if trend_known else np.nan
    above_sma = (not np.isnan(sma)) and float(px.iloc[-1]) > sma

    spread_known = not np.isnan(spread_bps)

    if spread_known and spread_bps < THRESHOLD_WIDENING_BPS and above_sma:
        return "bull"
    if (spread_known and spread_bps > THRESHOLD_CRISIS_BPS) or (trend_known and not above_sma):
        return "bear"
    return "neutral"


def classify_state_price(equity_series, credit_series, date):
    """Price-based fallback classifier (used when FRED is unreachable).

    Uses two pre-registered 200-day SMA signals, trailing-only: the equity index
    and a credit-spread proxy (e.g. the HYG/LQD ETF ratio). Bear when either is
    below its SMA (risk-off), bull when both are above, neutral otherwise.
    """
    def _above_sma(series):
        px = pd.Series(dtype=float)
        if series is not None and len(series):
            px = series[series.index <= date]
        if len(px) < MIN_TREND_DAYS:
            return False
        sma = float(px.tail(SMA_DAYS).mean())
        if np.isnan(sma):
            return False
        return float(px.iloc[-1]) > sma

    equity_ok = _above_sma(equity_series)
    credit_ok = _above_sma(credit_series)
    if equity_ok and credit_ok:
        return "bull"
    if not equity_ok or not credit_ok:
        return "bear"
    return "neutral"


def macro_target_weights(state):
    """Sleeve weight target dict for a state, or the neutral target if unknown."""
    from diversification.sleeves import MACRO_TARGETS
    return dict(MACRO_TARGETS.get(state, MACRO_TARGETS["neutral"]))
