"""Lightweight Markov-chain momentum (Discovery B-20260804-001).

A 2-state (up/down) Markov chain per equity member over a TRAILING-ONLY
feature-stability window (pre-registered in config/weights_diversification.yaml
``return_max.momentum``). The "momentum score" is P(up next | current state)
read off the member's 2x2 transition matrix. In the bull macro state the top-K
members by score are overweighted by the pre-registered tilt factor.

Discipline (auditor): the window, tilt, and K are pre-registered module-level
defaults and read from config; only trailing data at/under the decision date is
used (no lookahead). Degenerate series (no observed transitions) return no score
and are left at their base weight - the tilt degrades gracefully rather than
guessing.
"""

import numpy as np
import pandas as pd


def transition_counts(returns):
    """2x2 transition counts over a return series; rows=from, cols=to, 0=down 1=up."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return np.zeros((2, 2))
    states = (r >= 0.0).astype(int)
    counts = np.zeros((2, 2))
    for i in range(1, len(states)):
        counts[states[i - 1], states[i]] += 1.0
    return counts


def momentum_score(series, date, window_days=252, min_obs=120):
    """P(up next | current state) from trailing daily returns, or None.

    ``series`` is a price series; daily returns are derived inside
    (``pct_change``). Only the transition row out of the CURRENT state is
    required: a one-state series (all-up or all-down) has no transitions out of
    the unseen state, but its momentum is fully determined (score 1.0 / 0.0).
    """
    s = series[series.index <= pd.Timestamp(date)].tail(window_days + 1).dropna()
    if len(s) < min_obs + 1:
        return None
    r = s.pct_change(fill_method=None).dropna()
    if len(r) < min_obs:
        return None
    counts = transition_counts(r)
    cur = 1 if float(r.iloc[-1]) >= 0.0 else 0
    row = counts[cur, :]
    total = float(row.sum())
    if total <= 0:
        return None
    return float(row[1] / total)


def momentum_overweight(members, prices, date, cfg, base):
    """Overweight the top-K momentum members inside the equity complex.

    ``members``: equity tickers with a price series in ``prices``.
    ``base``: current {ticker: within-equity weight} (sums to ~1).
    ``cfg``: loaded config (``return_max.momentum`` block, pre-registered).
    Returns a renormalized weight dict; the tilt is a no-op outside the bull
    gate or when fewer than two scores are available.
    """
    m = cfg["return_max"]["momentum"]
    if not m.get("enabled") or not base or len(base) < 2:
        return base
    scores = {}
    for t in members:
        if t not in prices.columns:
            continue
        sc = momentum_score(prices[t], date, m["window_days"], m["min_obs"])
        if sc is not None:
            scores[t] = sc
    threshold = float(m.get("tilt_threshold", 0.5))
    leaders = {t for t, sc in scores.items() if sc > threshold}
    if not leaders:
        return base
    top = sorted(leaders, key=scores.get, reverse=True)[: int(m["max_tilted"])]
    w = dict(base)
    for t in top:
        w[t] = w.get(t, 0.0) * float(m["tilt"])
    s = sum(w.values())
    if s <= 0:
        return base
    return {t: v / s for t, v in w.items()}
