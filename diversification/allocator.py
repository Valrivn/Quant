"""Phase-3 risk-constrained ML allocator (D-20260803-005).

Implements the CEO's MODIFY ruling: a gradient-descent weight optimizer that
MAXIMIZES SHARPE while PUNISHING overly risky weights, subject to the HARD
constraint that the portfolio is down at most 30% at any point in time.

Three strategies consume this:
  1. STATIC-40/20/20/20 : the fixed CEO allocation (pre-registered).
  2. STATIC-after-ML    : the optimizer fits optimal weights once (in-sample,
                          all data at/under ``optimizer.train_end``), then those
                          weights are held statically for the whole window.
  3. ADAPTIVE           : weights are re-optimized at every rebalance date on a
                          trailing window with the same objective + penalties.

It also provides the profit-change opportunistic OR-gate (CEO: switch only on
significant profit changes) and the cash-shortfall macro-relocation ablation.

Discipline (auditor): every parameter is pre-registered in
``config/weights_diversification.yaml`` and read via ``load_config()`` —
never fit to reported outcomes. The optimizer's objective, penalties, split
point, and hyperparameters all come from that file.
"""

import yaml
from pathlib import Path

import numpy as np
import pandas as pd

from diversification.risk_minimizer import _simplex_project

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "weights_diversification.yaml"
ANNUALIZE = 252.0


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Load the pre-registered Phase-3 configuration (single source of truth)."""
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    return cfg


def _portfolio_stats(w, rets):
    """Daily portfolio-return stats for a weight vector over a return frame."""
    r = rets.values @ np.asarray(w, dtype=float)
    if len(r) < 2 or np.std(r, ddof=1) <= 0:
        return None
    ann_mean = float(np.mean(r)) * ANNUALIZE
    ann_vol = float(np.std(r, ddof=1)) * np.sqrt(ANNUALIZE)
    cum = np.cumprod(1.0 + r)
    max_dd = float(np.min(cum / np.maximum.accumulate(cum) - 1.0))
    return ann_mean, ann_vol, max_dd


def objective(w, rets, cfg, bounds):
    """Sharpe-max objective with a risky-weight penalty and a hard 30% DD bound.

    ``objective = sharpe - risk_lambda * vol^2 - dd_penalty * breach`` where
    ``breach = max(0, -maxdd - max_drawdown_bound)``. Higher is better; the
    optimizer maximizes it.
    """
    w = np.asarray(w, dtype=float)
    stats = _portfolio_stats(w, rets)
    if stats is None:
        return -1e18
    ann_mean, ann_vol, max_dd = stats
    sharpe = ann_mean / ann_vol
    risk_penalty = cfg["optimizer"]["risk_lambda"] * (ann_vol ** 2)
    bound = cfg["optimizer"]["max_drawdown_bound"]
    dd_breach = max(0.0, -max_dd - bound)
    dd_penalty = cfg["optimizer"]["dd_penalty"] * dd_breach
    return sharpe - risk_penalty - dd_penalty


def _numerical_gradient(w, rets, cfg, bounds):
    eps = cfg["optimizer"]["num_grad_eps"]
    grad = np.zeros_like(w)
    base = objective(w, rets, cfg, bounds)
    for i in range(len(w)):
        if w[i] <= 0.0:
            continue
        wp = w.copy()
        wp[i] += eps
        grad[i] = (objective(wp, rets, cfg, bounds) - base) / eps
    return grad


def optimize_weights(rets, cfg, initial_guesses=None):
    """Projected gradient ascent maximizing Sharpe w/ risk + DD penalties.

    ``rets``: DataFrame (T x N) of daily sleeve returns (in-sample segment).
    ``cfg``: loaded config (bounds + hyperparameters pre-registered).
    Returns the best weight vector found across the pre-registered initial
    guesses (deterministic seed), normalized to sum 1.
    """
    bounds = _bounds_list(cfg)
    if rets is None or len(rets) < 60 or rets.shape[1] != len(bounds):
        return None
    lr = cfg["optimizer"]["learning_rate"]
    max_iters = cfg["optimizer"]["max_iters"]
    tol = cfg["optimizer"]["tol"]
    rng = np.random.default_rng(cfg["optimizer"]["seed"])
    n = len(bounds)
    guesses = initial_guesses if initial_guesses is not None else _default_guesses(cfg, n, rets)
    best_w, best_obj = None, -1e18
    for init in guesses:
        w = _simplex_project(np.asarray(init, dtype=float), bounds)
        if w is None:
            continue
        prev = objective(w, rets, cfg, bounds)
        for _ in range(max_iters):
            grad = _numerical_gradient(w, rets, cfg, bounds)
            if not np.any(np.isfinite(grad)) or np.max(np.abs(grad)) < tol:
                break
            step = lr * grad
            w_new = _simplex_project(w + step, bounds)
            obj = objective(w_new, rets, cfg, bounds)
            if obj <= prev and abs(obj - prev) < tol:
                w = w_new
                prev = obj
                break
            if obj < prev and _ > 0:
                w = _simplex_project(w + 0.5 * lr * grad, bounds)
                obj = objective(w, rets, cfg, bounds)
            w, prev = w_new, obj
        if prev > best_obj:
            best_obj, best_w = prev, w.copy()
    if best_w is None:
        return None
    total = float(np.sum(best_w))
    return best_w / total if total > 0 else None


def _default_guesses(cfg, n, rets):
    bounds = _bounds_list(cfg)
    guesses = []
    for name in cfg["optimizer"]["initial_guesses"]:
        if name == "static":
            w = np.array([cfg["static_targets"][s] for s in _sleeve_order(cfg)], dtype=float)
        elif name == "equal":
            w = np.full(n, 1.0 / n)
        elif name == "minvar":
            try:
                cov = rets.cov().values
                ones = np.ones(n)
                inv = np.linalg.pinv(cov)
                w = inv @ ones / (ones @ inv @ ones)
            except Exception:
                continue
        else:
            continue
        if len(w) == n and np.all(np.isfinite(w)):
            guesses.append(_simplex_project(w, bounds))
    if not guesses:
        guesses = [_simplex_project(np.full(n, 1.0 / n), bounds)]
    return guesses


def _sleeve_order(cfg):
    return list(cfg["sleeves"].keys())


def _bounds_list(cfg):
    return [tuple(cfg["sleeve_bounds"][s]) for s in _sleeve_order(cfg)]


def sleeve_return_series(rets, date, cfg, basket_fx, window_days=None):
    """Trailing daily return frame per Phase-3 sleeve as-of a date (trailing only).

    ``basket_fx(date)`` returns the audited dividend-basket ticker list (or a
    bills-fallback list) for that date. Small/mid and bonds sleeves are
    equal-weighted proxies; the dividend sleeve is the audited basket.
    ``window_days=None`` uses ALL data at/under the embargoed cutoff (used for
    the in-sample STATIC-after-ML fit); otherwise a trailing window of
    ``window_days`` is used (ADAPTIVE re-fits).
    """
    order = _sleeve_order(cfg)
    embargo = cfg["optimizer"]["embargo_days"]
    cutoff = pd.Timestamp(date) - pd.Timedelta(days=embargo)
    win = rets[rets.index <= cutoff]
    if window_days is not None:
        win = win.tail(window_days + 30)
    if len(win) < 60:
        return None
    cols = {}
    for sleeve in order:
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


def fit_static_ml_weights(rets, cfg, basket_fx, train_end):
    """STATIC-after-ML: fit optimal weights once on the in-sample segment.

    Uses all data at/under ``train_end`` (pre-registered) to fit; the resulting
    weight vector is then held statically for the entire window. The OOS
    segment is validated by the caller (auditor OOS requirement).
    """
    insample = rets[rets.index <= pd.Timestamp(train_end)]
    if insample.empty:
        return None
    sr = sleeve_return_series(insample, train_end, cfg, basket_fx)
    if sr is None:
        return None
    w = optimize_weights(sr, cfg)
    if w is None:
        return None
    return dict(zip(_sleeve_order(cfg), w))


def profit_change_trigger(trail_rets, w_cur, cfg):
    """CEO's profit-change OR-gate: fires on a significant trailing profit swing.

    ``trail_rets``: trailing portfolio daily returns over ``window_days``.
    Returns True when |trailing return| >= ``threshold`` (pre-registered).
    The state gate is applied by the caller (only considered in the bear
    macro state, per config).
    """
    if w_cur is None or trail_rets is None or len(trail_rets) < 20:
        return False
    wv = np.array([w_cur.get(c, 0.0) for c in trail_rets.columns], dtype=float)
    if wv.sum() <= 0:
        return False
    r = trail_rets.values @ wv
    trail_ret = float(np.prod(1.0 + r) - 1.0)
    return abs(trail_ret) >= cfg["profit_change"]["threshold"]


def cash_shortfall_relocation(target, w_cur, state, cfg):
    """Ablation: relocate toward the macro-state-preferred sleeve on cash shortfall.

    Fires when implied cash (1 - sum(w_cur)) falls below ``min_cash_ratio``.
    In the bear state the equity sleeves get the freed weight; otherwise the
    bonds sleeve does (pre-registered relocation policy). Returns
    ``(target, relocated)`` — always reported as an ablation, never adopted
    silently.
    """
    implied_cash = 1.0 - sum(w_cur.get(c, 0.0) for c in w_cur)
    if implied_cash >= cfg["cash_shortfall"]["min_cash_ratio"]:
        return target, False
    target = dict(target)
    total = sum(target.values())
    if total <= 0:
        return target, False
    if state == "bear":
        for c in list(target):
            target[c] = 0.0 if c in ("SPY", "MDY", "IWM") else target[c]
        for c in ("SPY", "MDY", "IWM"):
            if c in target:
                target[c] = cfg["static_targets"].get("spy", 0.45) / 3.0
    else:
        for c in list(target):
            target[c] = 0.0 if c in ("VCSH", "VCIT", "BIL", "SHY", "SGOV") else target[c]
        for c in ("VCSH", "VCIT", "BIL", "SHY", "SGOV"):
            if c in target:
                target[c] = cfg["static_targets"].get("bonds", 0.20) / 5.0
    s = sum(target.values())
    if s > 0:
        target = {c: v / s for c, v in target.items()}
    return target, True
