"""Rule-based LLM proposal layer: emits candidate configs and rationales.

Deterministic and offline. Simulates what an LLM would propose given the
historical sleeve statistics; the actual LLM invocation happens at the
orchestration site in P5, not here.
"""

import numpy as np
import pandas as pd

_TRADING_DAYS = 252


def _sleeve_stats(sleeve_returns):
    stats = {}
    for c in sleeve_returns.columns:
        r = sleeve_returns[c].dropna()
        ann_ret = float(r.mean() * _TRADING_DAYS) if len(r) else np.nan
        ann_vol = float(r.std(ddof=1) * np.sqrt(_TRADING_DAYS)) if len(r) > 1 else np.nan
        sharpe = ann_ret / ann_vol if ann_vol and ann_vol > 0 else np.nan
        cum = (1.0 + r).cumprod()
        max_dd = float((cum / cum.cummax() - 1.0).min()) if len(r) else np.nan
        stats[c] = {
            "annualized_return": ann_ret,
            "annualized_vol": ann_vol,
            "sharpe": sharpe,
            "max_drawdown": max_dd,
        }
    return stats


def _trailing_vol_proxy(sleeve_returns):
    r = sleeve_returns.mean(axis=1).dropna()
    if len(r) < 21:
        return np.nan
    return float(r.rolling(21).std().iloc[-1] * np.sqrt(_TRADING_DAYS))


def _corr_spike(sleeve_returns):
    corr = sleeve_returns.corr()
    vals = corr.values[np.triu_indices_from(corr.values, k=1)]
    if len(vals) == 0:
        return np.nan
    return float(np.mean(vals))


def propose_configs(sleeve_returns, budget_token_hint=600):
    """Propose ~6 candidate configurations for the numeric solver.

    Each config is a dict with name, objective, target_vol, max_drawdown_cap,
    rationale, and regime. Rationales are human-readable strings in the spirit
    of an LLM proposing a configuration.
    """
    stats = _sleeve_stats(sleeve_returns)
    tv = _trailing_vol_proxy(sleeve_returns)
    corr = _corr_spike(sleeve_returns)
    best_two = sorted(stats, key=lambda c: stats[c]["sharpe"] if stats[c]["sharpe"] == stats[c]["sharpe"] else -np.inf, reverse=True)[:2]

    configs = [
        {
            "name": "max_sharpe",
            "objective": "dual",
            "target_vol": 0.12,
            "max_drawdown_cap": 0.30,
            "regime": "normal",
            "rationale": (
                "The dual objective balances residual alpha against the Sharpe "
                "ratio, so I propose a higher-vol target to let the optimizer "
                "reach for return while still penalizing variance."
            ),
        },
        {
            "name": "defensive",
            "objective": "alpha",
            "target_vol": 0.06,
            "max_drawdown_cap": 0.15,
            "regime": "defensive",
            "rationale": (
                "With a low vol target and tight drawdown cap, the solver should "
                "favor short bills and gold, keeping the portfolio resilient in "
                "a drawdown-prone regime."
            ),
        },
        {
            "name": "momentum",
            "objective": "sharpe",
            "target_vol": 0.14,
            "max_drawdown_cap": 0.35,
            "regime": "trend",
            "rationale": (
                "A pure Sharpe objective with a generous vol budget lets the "
                "optimizer concentrate in the equity sleeve when it has been "
                "the strongest trend."
            ),
        },
        {
            "name": "equal_weight",
            "objective": "dual",
            "target_vol": 0.10,
            "max_drawdown_cap": 0.30,
            "regime": "neutral",
            "rationale": (
                "Equal-weight is the baseline: it diversifies across all four "
                "sleeves and gives the walk-forward a neutral reference point "
                "to beat."
            ),
        },
        {
            "name": "regime_aware",
            "objective": "alpha",
            "target_vol": 0.08,
            "max_drawdown_cap": 0.20,
            "regime": "regime_aware",
            "rationale": (
                "When sleeve correlations spike or trailing vol is elevated, "
                "shifting toward bills and gold reduces the portfolio's "
                "sensitivity to a single risk factor."
            ),
        },
        {
            "name": "expert",
            "objective": "dual",
            "target_vol": 0.11,
            "max_drawdown_cap": 0.28,
            "regime": "expert",
            "rationale": (
                "Blending the two best Sharpe sleeves concentrates the "
                "portfolio in the strongest historical drivers while keeping "
                "a moderate vol target."
            ),
        },
    ]
    for cfg in configs:
        cfg["_best_two"] = best_two
    return configs


def _config_score(cfg):
    objective_bonus = {"dual": 0.2, "sharpe": 0.1, "alpha": 0.0}.get(cfg["objective"], 0.0)
    vol_score = min(cfg["target_vol"] / 0.10, 1.0) * 0.3
    dd_score = (1.0 - cfg["max_drawdown_cap"]) * 0.5
    return objective_bonus + vol_score + dd_score


def select_configs(proposed, k=3):
    """Rank proposed configs by a deterministic quality heuristic and return top k."""
    ranked = sorted(proposed, key=_config_score, reverse=True)
    return ranked[:k]