"""Friction-bounded, gradient-descent-like risk minimizer (Phase 1, D-20260803-003).

Solves ``min_w  w' Sigma w`` over the asset sleeves using projected gradient
descent (``w <- w - lr * 2 * Sigma @ w`` followed by a simplex projection that
respects per-sleeve bounds), where Sigma is estimated on a trailing
purge-and-embargo window ONLY (out-of-sample: never the full window), with
Ledoit-Wolf-style shrinkage toward the diagonal.

The candidate solution is ACCEPTED only if the variance reduction it delivers
clears the transaction cost of moving from the current weights — the
D-20260803-002 fee-aware liquidate-only rule. If it does not clear friction,
hold. Thresholds (window, embargo, shrinkage, learning rate, friction multiple)
are pre-registered module constants, never fit.
"""

import numpy as np
import pandas as pd

COV_WINDOW_DAYS = 252
EMBARGO_DAYS = 21
SHRINKAGE = 0.20
LEARNING_RATE = 0.01
MAX_ITERS = 2000
TOL = 1e-8
ANNUALIZE = 252.0


def _trailing_returns(rets, date, window=COV_WINDOW_DAYS, embargo=EMBARGO_DAYS):
    cutoff = date - pd.Timedelta(days=embargo)
    window_data = rets[rets.index <= cutoff].tail(window)
    if len(window_data) < 60:
        return pd.DataFrame()
    return window_data.dropna(axis=0, how="any")


def _estimate_cov(window_data, shrinkage=SHRINKAGE):
    if window_data is None or len(window_data) < 60:
        return None
    sample = window_data.cov().values
    diag = np.diag(np.diag(sample))
    return (1.0 - shrinkage) * sample + shrinkage * diag


def _simplex_project(w, bounds):
    """Project onto the capped simplex: lower_i <= w_i <= upper_i, sum(w) = 1.

    Deterministic proportional redistribution: clip into the box, then either
    add the shortfall into remaining headroom or shave the excess off existing
    weight, repeating until the sum is 1 within tolerance. Assumes the box is
    feasible (sum(lower) <= 1 <= sum(upper)).
    """
    lower = np.array([bounds[i][0] for i in range(len(bounds))], dtype=float)
    upper = np.array([bounds[i][1] for i in range(len(bounds))], dtype=float)
    w = np.clip(np.asarray(w, dtype=float), lower, upper)
    for _ in range(200):
        total = w.sum()
        if abs(total - 1.0) < 1e-9:
            break
        if total < 1e-12:
            w = np.clip(np.full_like(w, 1.0 / len(w)), lower, upper)
            continue
        if total < 1.0:
            room = np.maximum(upper - w, 0.0)
            if room.sum() <= 1e-12:
                break
            w = w + np.minimum((1.0 - total) / room.sum() * room, 1.0 - total)
        else:
            excess = total - 1.0
            base = w.sum()
            if base <= 0:
                break
            w = w * (1.0 - excess / base)
        w = np.clip(w, lower, upper)
    if w.sum() > 0:
        w = w / w.sum()
    return w


def gradient_descent(window_data, bounds, initial=None, lr=LEARNING_RATE,
                     max_iters=MAX_ITERS, tol=TOL):
    """Projected gradient descent for min w'Sigma w over the sleeve weights.

    Returns a numpy weight vector (length = len(bounds)) or None on insufficient
    data. Bounds are (lower, upper) per sleeve.
    """
    cov = _estimate_cov(window_data)
    if cov is None:
        return None
    n = cov.shape[0]
    if initial is None or len(initial) != n:
        initial = np.full(n, 1.0 / n)
    w = np.clip(np.array(initial, dtype=float), 0.0, 1.0)
    if w.sum() <= 0:
        w = np.full(n, 1.0 / n)
    w = w / w.sum()
    prev_obj = float("inf")
    for _ in range(max_iters):
        grad = 2.0 * cov @ w
        w_new = w - lr * grad
        w_new = _simplex_project(w_new, bounds)
        obj = float(w_new @ cov @ w_new)
        if abs(obj - prev_obj) < tol:
            w = w_new
            break
        w = w_new
        prev_obj = obj
    return np.clip(w / w.sum(), 0.0, 1.0)


def friction_bounded_rebalance(window_data, bounds, current, cov_scale=None,
                               fee_rate=0.005, lr=LEARNING_RATE,
                               max_iters=MAX_ITERS):
    """Return (new_weights, traded) applying the D-20260803-002 friction rule.

    Accepts the gradient-descent solution only when the annualized variance
    reduction (in portfolio-weight units, scaled by cov_scale) exceeds the
    turnover fee. ``current`` is the current sleeve weight vector; missing
    inputs yield (current, False).
    """
    cov = _estimate_cov(window_data)
    if cov is None or current is None or len(current) != len(bounds):
        return (np.array(current, dtype=float) if current is not None else None, False)
    if np.any(np.isnan(cov)):
        return (np.array(current, dtype=float), False)
    w_new = gradient_descent(window_data, bounds, initial=current, lr=lr, max_iters=max_iters)
    if w_new is None:
        return (np.array(current, dtype=float), False)
    w_cur = np.array(current, dtype=float) / np.array(current, dtype=float).sum()
    var_cur = float(w_cur @ cov @ w_cur)
    var_new = float(w_new @ cov @ w_new)
    turnover = float(np.sum(np.abs(w_new - w_cur)))
    improvement = (var_cur - var_new) * ANNUALIZE * (cov_scale if cov_scale else 1.0)
    fee = fee_rate * turnover * (cov_scale if cov_scale else 1.0)
    if improvement > fee:
        return (w_new, True)
    return (w_cur, False)
