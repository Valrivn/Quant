"""Return-maximizing within-equity optimizer (Discovery B-20260804-001).

The CEO's return-max pivot: instead of maximizing Sharpe (Phase-3), maximize
expected annualized return of the equity complex. Volatility is NOT penalized;
the only risk term is a hard drawdown bound at the CEO's new 40% bar
(``return_max.max_drawdown_bound`` / ``dd_penalty`` in config). All parameters
are pre-registered in config/weights_diversification.yaml (invariant 4).

Two consumption modes:
  - RM-ML-STATIC : fit once on the in-sample segment (<= train_end), held
                   statically for the whole window (auditor OOS split applies).
  - RM-ML-ADAPTIVE: re-fit on a trailing window at each rebalance (trailing-only,
                   no lookahead).

The optimizer works on the EQUITY COMPLEX only (spy / small_mid / dividend
sleeves, normalized to sum 1). The state table (``state_equity``) stays the
CEO's hard anchor for the equity-vs-bonds split; ML only chooses which part of
the equity budget rides where.
"""

import numpy as np
import pandas as pd

from diversification.risk_minimizer import _simplex_project

ANNUALIZE = 252.0
ORDER = ["spy", "small_mid", "dividend"]


def _complex_stats(w, rets):
    r = rets.values @ np.asarray(w, dtype=float)
    if len(r) < 2 or np.std(r, ddof=1) <= 0:
        return None
    ann_mean = float(np.mean(r)) * ANNUALIZE
    cum = np.cumprod(1.0 + r)
    max_dd = float(np.min(cum / np.maximum.accumulate(cum) - 1.0))
    return ann_mean, max_dd


def objective_return_max(w, rets, cfg):
    """Return-max objective: ann_mean - dd_penalty*breach over the 40% bound."""
    w = np.asarray(w, dtype=float)
    stats = _complex_stats(w, rets)
    if stats is None:
        return -1e18
    ann_mean, max_dd = stats
    bound = cfg["return_max"]["max_drawdown_bound"]
    breach = max(0.0, -max_dd - bound)
    return ann_mean - cfg["return_max"]["dd_penalty"] * breach


def _numerical_gradient(w, rets, cfg):
    eps = cfg["return_max"]["num_grad_eps"]
    grad = np.zeros_like(w)
    base = objective_return_max(w, rets, cfg)
    for i in range(len(w)):
        if w[i] <= 0.0:
            continue
        wp = w.copy()
        wp[i] += eps
        grad[i] = (objective_return_max(wp, rets, cfg) - base) / eps
    return grad


def _bounds_list(cfg, low_div):
    b = cfg["return_max"]["within_equity_bounds_low" if low_div else "within_equity_bounds_high"]
    return [tuple(b[s]) for s in ORDER]


def optimize_return_weights(rets_complex, cfg, low_div=False, initial_guesses=None):
    """Projected gradient ascent over the 3-sleeve equity complex (sum = 1)."""
    if rets_complex is None or len(rets_complex) < 60 or rets_complex.shape[1] != len(ORDER):
        return None
    bounds = _bounds_list(cfg, low_div)
    lr = cfg["return_max"]["learning_rate"]
    max_iters = cfg["return_max"]["max_iters"]
    tol = cfg["return_max"]["tol"]
    rng = np.random.default_rng(cfg["return_max"]["seed"])
    guesses = initial_guesses if initial_guesses is not None else [
        np.array([cfg["return_max"]["static_within"][s] for s in ORDER]),
        np.full(len(ORDER), 1.0 / len(ORDER)),
    ]
    best_w, best_obj = None, -1e18
    for init in guesses:
        w = _simplex_project(np.asarray(init, dtype=float), bounds)
        if w is None:
            continue
        prev = objective_return_max(w, rets_complex, cfg)
        for _ in range(max_iters):
            grad = _numerical_gradient(w, rets_complex, cfg)
            if not np.any(np.isfinite(grad)) or np.max(np.abs(grad)) < tol:
                break
            w_new = _simplex_project(w + lr * grad, bounds)
            obj = objective_return_max(w_new, rets_complex, cfg)
            if obj <= prev and abs(obj - prev) < tol:
                w, prev = w_new, obj
                break
            if obj < prev and _ > 0:
                w = _simplex_project(w + 0.5 * lr * grad, bounds)
                obj = objective_return_max(w, rets_complex, cfg)
            w, prev = w_new, obj
        if prev > best_obj:
            best_obj, best_w = prev, w.copy()
    if best_w is None:
        return None
    total = float(np.sum(best_w))
    return best_w / total if total > 0 else None


def complex_return_series(rets, date, cfg, basket_fx, window_days=None):
    """Trailing daily return frame for the 3 equity-complex sleeves as-of date."""
    rm = cfg["return_max"]
    cutoff = pd.Timestamp(date) - pd.Timedelta(days=rm["embargo_days"])
    win = rets[rets.index <= cutoff]
    if window_days is not None:
        win = win.tail(window_days + 30)
    if len(win) < 60:
        return None
    cols = {}
    for sleeve in ORDER:
        if sleeve == "dividend":
            members = [t for t in basket_fx(pd.Timestamp(date)) if t in win.columns]
            if not members:
                members = [t for t in ["SHY"] if t in win.columns]
            if not members:
                return None
            cols[sleeve] = win[members].mean(axis=1)
        else:
            tickers = [t for t in cfg["sleeves"][sleeve] if t in win.columns]
            if not tickers:
                return None
            cols[sleeve] = win[tickers].mean(axis=1)
    sr = pd.DataFrame(cols).dropna(axis=0, how="any")
    return sr if len(sr) >= 60 else None


def fit_static_return_weights(rets, cfg, basket_fx, train_end):
    """RM-ML-STATIC: fit the equity complex once on the in-sample segment."""
    insample = rets[rets.index <= pd.Timestamp(train_end)]
    if insample.empty:
        return None
    sr = complex_return_series(insample, train_end, cfg, basket_fx)
    if sr is None:
        return None
    w = optimize_return_weights(sr, cfg, low_div=False)
    if w is None:
        return None
    return dict(zip(ORDER, w))
