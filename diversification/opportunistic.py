"""Opportunistic equity engine (Phase 2, D-20260803-004).

Adds the pre-registered opportunistic overlay on top of the D-20260802-002
fee-aware liquidate-only discipline (sell only to redeploy or de-risk; every
executed change must clear the transaction fee — enforced by the share-
accounting gate in ``fee_sim3.Portfolio.run``, never bypassed here).

Rule (module constants, never fit):
- An ABSOLUTE BUYING OPPORTUNITY fires when an equity sleeve member's price
  z-score vs its own trailing ``LOOKBACK_YEARS`` window drops to
  ``<= OPPORTUNITY_Z``.
- The overlay acts ONLY inside the equity sleeve, ONLY in the bear macro state,
  and ONLY redistributes weight toward the oversold members (rich names are
  sold to redeploy into the cheap ones). It never adds equity-sleeve weight,
  never touches bonds/bills/gold, and the small-cap floor is untouched (the
  audited basket is large-cap by construction).
- If no opportunity fires, weights are returned unchanged (liquidate-only).
"""

import numpy as np
import pandas as pd

OPPORTUNITY_Z = -1.0
LOOKBACK_YEARS = 5
MIN_OBS = 250
TILT_MULT = 1.5
YEAR_DAYS = 365.25


def _trailing_z(price_series, date, lookback_years=LOOKBACK_YEARS, min_obs=MIN_OBS):
    """Trailing-only price z-score vs its own history (no lookahead)."""
    prior = price_series[price_series.index <= pd.Timestamp(date)]
    if prior.empty:
        return np.nan
    cutoff = prior.index[-1] - pd.Timedelta(days=lookback_years * YEAR_DAYS)
    w = prior[prior.index >= cutoff].dropna()
    if len(w) < min_obs:
        return np.nan
    mu = float(w.mean())
    sd = float(w.std(ddof=1))
    if sd <= 0:
        return 0.0
    return (float(prior.iloc[-1]) - mu) / sd


def absolute_buying_opportunity(price_series, date, z_threshold=OPPORTUNITY_Z):
    """True when the price sits at/below ``z_threshold`` vs its own history."""
    z = _trailing_z(price_series, date)
    return bool(z == z and z <= z_threshold)


def opportunistic_equity_weights(equity_tickers, prices, date, state,
                                 base_weights=None, z_threshold=OPPORTUNITY_Z):
    """Within-equity weights with the bear-state oversold tilt.

    ``base_weights``: dict ticker->weight summing to 1 (the pre-tilt sleeve
    allocation). Returns the tilt when ``state == "bear"`` and >=1 member fires
    the absolute-buying-opportunity; otherwise returns ``base_weights``.
    Oversold members get ``TILT_MULT`` x equal weight, everyone else equal
    weight, renormalized to sum 1 (total equity-sleeve weight is preserved).
    """
    if base_weights is None or state != "bear":
        return base_weights
    fired = [
        t for t in equity_tickers
        if absolute_buying_opportunity(prices[t], date, z_threshold=z_threshold)
    ]
    if not fired:
        return base_weights
    n = len(equity_tickers)
    ew = 1.0 / n
    w = {t: (TILT_MULT * ew if t in fired else ew) for t in equity_tickers}
    total = sum(w.values())
    if total <= 0:
        return base_weights
    return {t: v / total for t, v in w.items()}
