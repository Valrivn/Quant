"""Numeric core: gradient-descent weight optimizer with walk-forward validation."""

import numpy as np
import pandas as pd

from valuation_alpha.alpha import (
    apply_slippage,
    ff5_residual_alpha,
    excess_vs_sp500,
)
from valuation_alpha.stats import block_bootstrap_alpha, deflated_sharpe

_TRADING_DAYS = 252


def _softmax(logits):
    e = np.exp(logits - np.max(logits))
    return e / e.sum()


def _objective_grad(R, w, objective, reg, max_drawdown_cap):
    """Return (objective_value, gradient_w) for a weight vector on return matrix R."""
    n = R.shape[1]
    mu = R.mean(axis=0)
    ret = float(w @ mu)
    C = R - mu
    rc = C @ w
    var = float(rc @ rc) / (R.shape[0] - 1)
    s = float(np.sqrt(var)) if var > 0 else 0.0
    dm = mu
    dvar = (2.0 / (R.shape[0] - 1)) * (C.T @ rc)
    ds = dvar / (2.0 * s) if s > 1e-12 else np.zeros(n)

    if objective == "sharpe":
        if s > 1e-12:
            val = ret / s
            g = dm / s - (ret * ds) / (s * s)
        else:
            val = 0.0
            g = np.zeros(n)
    elif objective == "alpha":
        val = _TRADING_DAYS * ret - reg * (_TRADING_DAYS ** 2) * var
        g = _TRADING_DAYS * dm - reg * (_TRADING_DAYS ** 2) * dvar
    else:
        bench_w = np.full(n, 1.0 / n)
        bench_vol = float(np.sqrt(wb_var(R, bench_w)) * _TRADING_DAYS)
        alpha_proxy = _TRADING_DAYS * (ret - float(bench_w @ mu))
        sharpe = ret / s if s > 1e-12 else 0.0
        z_alpha = alpha_proxy / (bench_vol + 1e-12)
        z_sr = sharpe
        val = z_alpha + z_sr
        g = (_TRADING_DAYS / (bench_vol + 1e-12)) * dm
        if s > 1e-12:
            g = g + dm / s - (ret * ds) / (s * s)

    if max_drawdown_cap is not None and max_drawdown_cap > 0:
        cum = np.cumprod(1.0 + rc)
        peak = np.maximum.accumulate(cum)
        dd = float((cum / peak - 1.0).min())
        if dd < -max_drawdown_cap:
            pen = -max_drawdown_cap - dd
            val = val - pen
            g = g - pen * dvar
    return val, g


def wb_var(C, w):
    rc = C @ w
    return float(rc @ rc) / (C.shape[0] - 1)


def portfolio_weights(
    sleeve_returns,
    objective="dual",
    max_drawdown_cap=0.3,
    reg=0.01,
    max_iter=2000,
    lr=1e-3,
    seed=0,
):
    """Gradient descent over the sleeve weight simplex.

    Reparameterizes weights as softmax(logits) to keep the simplex constraint
    and differentiability. Objectives: "alpha" maximizes annualized mean return
    minus reg*annualized variance (a differentiable proxy for FF5 alpha, which
    is evaluated post-hoc in the backtest); "sharpe" maximizes mean/std;
    "dual" maximizes a z-scaled blend of the alpha proxy and the Sharpe ratio.
    Returns a dict with weights, objective_value, iters, and converged.
    """
    df = sleeve_returns.dropna()
    cols = list(df.columns)
    if len(cols) == 0:
        return {"weights": {}, "objective_value": 0.0, "iters": 0, "converged": False}
    R = df[cols].values
    n = R.shape[1]
    rng = np.random.default_rng(seed)
    logits = rng.normal(0.0, 0.05, n)
    w = _softmax(logits)
    best_w = w.copy()
    best_val = -np.inf
    prev_val = None
    converged = False
    for it in range(max_iter):
        val, g = _objective_grad(R, w, objective, reg, max_drawdown_cap)
        gl = w * (g - w @ g)
        norm = float(np.linalg.norm(gl))
        if norm > 1e-12:
            gl = gl / norm
        logits = logits + lr * gl
        w = _softmax(logits)
        if val > best_val:
            best_val = val
            best_w = w.copy()
        if prev_val is not None and abs(val - prev_val) < 1e-9:
            converged = True
            break
        prev_val = val
    return {
        "weights": {c: float(best_w[i]) for i, c in enumerate(cols)},
        "objective_value": float(best_val),
        "iters": it + 1,
        "converged": converged,
    }


def apply_vol_target(weights, sleeve_returns, target_vol=0.10):
    """Rescale weights to a target annualized vol, capped at 1.0 per sleeve."""
    cols = [c for c in weights if c in sleeve_returns.columns]
    if not cols:
        return {}
    w = np.array([weights[c] for c in cols], dtype=float)
    R = sleeve_returns[cols].dropna().values
    if R.shape[0] < 2:
        return {c: float(w[i]) for i, c in enumerate(cols)}
    port = R @ w
    vol = float(port.std(ddof=1) * np.sqrt(_TRADING_DAYS))
    scale = target_vol / vol if vol > 0 else 1.0
    scaled = np.minimum(w * scale, 1.0)
    return {c: float(scaled[i]) for i, c in enumerate(cols)}


def walk_forward_allocate(
    sleeve_returns,
    rebalance_days=63,
    train_days=756,
    objective="dual",
    target_vol=0.10,
    seed=0,
):
    """Expanding walk-forward allocation over the sleeve return history.

    On each rebalance date t (every rebalance_days, starting once train_days of
    history exist), fits portfolio_weights on the trailing window (data <= t
    only, no lookahead), applies apply_vol_target, and holds until the next
    rebalance. Returns a daily holding-weights DataFrame (step function) with
    the train window sizes recorded in .attrs["train_window_sizes"].
    """
    df = sleeve_returns.dropna()
    cols = list(df.columns)
    idx = df.index
    n = len(idx)
    rebalance = list(range(train_days, n, rebalance_days))
    if not rebalance:
        return pd.DataFrame(columns=cols)
    first = rebalance[0]
    weights = pd.DataFrame(0.0, index=idx[first:], columns=cols)
    train_sizes = []
    for pos in rebalance:
        window = df.iloc[: pos + 1]
        w = portfolio_weights(window, objective=objective, seed=seed)["weights"]
        w = apply_vol_target(w, window, target_vol)
        total = sum(w.values())
        if total > 0:
            w = {c: v / total for c, v in w.items()}
        end = min(pos + rebalance_days, n)
        lo = pos - first
        hi = end - first
        for c in cols:
            weights.iloc[lo:hi, weights.columns.get_loc(c)] = w.get(c, 0.0)
        train_sizes.append(len(window))
    weights.attrs["train_window_sizes"] = train_sizes
    return weights


def portfolio_backtest(
    weights_daily,
    sleeve_returns,
    benchmark=None,
    factors=None,
    horizon_days=756,
    slippage=0.005,
):
    """Evaluate a daily holding-weights schedule against sleeve returns.

    Weights are shifted forward one day to avoid same-day lookahead. Returns a
    dict of metrics plus the net portfolio return Series under "returns".
    """
    w = weights_daily.shift(1)
    cols = [c for c in w.columns if c in sleeve_returns.columns]
    port = (w[cols] * sleeve_returns[cols]).sum(axis=1)
    port = apply_slippage(port, slippage)
    r = port.dropna()
    n = len(r)
    ann_ret = float(r.mean() * _TRADING_DAYS) if n else np.nan
    ann_vol = float(r.std(ddof=1) * np.sqrt(_TRADING_DAYS)) if n > 1 else np.nan
    sharpe = ann_ret / ann_vol if ann_vol and ann_vol > 0 else np.nan
    cum = (1.0 + r).cumprod()
    max_dd = float((cum / cum.cummax() - 1.0).min()) if n else np.nan

    result = {
        "annualized_return": ann_ret,
        "annualized_vol": ann_vol,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "returns": port,
    }
    if n > 1 and sharpe == sharpe:
        result["deflated_sharpe"] = deflated_sharpe(sharpe, n, n_trials=1)
    if factors is not None and not factors.empty:
        result["alpha"] = ff5_residual_alpha(r, factors, horizon_days=horizon_days)
        result["block_bootstrap"] = block_bootstrap_alpha(r, factors, horizon_days=horizon_days)
    if benchmark is not None and len(benchmark):
        result["excess_vs_sp500"] = excess_vs_sp500(r, benchmark)
    return result