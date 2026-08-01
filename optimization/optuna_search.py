"""Weight-configuration search for the sentiment pipeline.

Uses Optuna when available and falls back to a seeded randomized search so the
module works offline. Evaluates candidate category/subreddit weights with the
walk-forward backtest objective.
"""
import logging
from typing import Any, Dict, Optional

import numpy as np

from config import CATEGORY_WEIGHTS, SUBREDDIT_TAXONOMY
from backtesting.backtest import run_walk_forward_backtest

logger = logging.getLogger(__name__)

try:
    import optuna  # noqa: F401

    _HAS_OPTUNA = True
except ImportError:
    _HAS_OPTUNA = False


def _sample_weights(rng: np.random.Generator) -> Dict[str, Any]:
    cat = {c: max(0.05, w * float(rng.uniform(0.7, 1.3))) for c, w in CATEGORY_WEIGHTS.items()}
    total = sum(cat.values()) or 1.0
    cat = {c: w / total for c, w in cat.items()}
    sub = {
        c: {s: max(0.05, sw * float(rng.uniform(0.7, 1.3))) for s, sw in sdict.items()}
        for c, sdict in SUBREDDIT_TAXONOMY.items()
    }
    return {"category_weights": cat, "subreddit_weights": sub}


def run_bayesian_optimization(
    trials: int = 10,
    objective_metric: str = "sharpe",
) -> Dict[str, Any]:
    """Search weight configs and return the best candidate.

    Returns a dict with keys: category_weights, subreddit_weights, metrics.
    """
    n_trials = max(1, int(trials))
    base_cat = dict(CATEGORY_WEIGHTS)
    base_sub = {c: dict(s) for c, s in SUBREDDIT_TAXONOMY.items()}

    best: Optional[tuple] = None
    rng = np.random.default_rng(0)

    for _ in range(n_trials):
        candidate = _sample_weights(rng)
        res = run_walk_forward_backtest(
            category_weights=candidate["category_weights"],
            subreddit_weights=candidate["subreddit_weights"],
            lookback_days=30,
        )
        metric = float(res.get(objective_metric, 0.0))
        if best is None or metric > best[0]:
            best = (metric, candidate)

    if best is None:
        return {
            "category_weights": base_cat,
            "subreddit_weights": base_sub,
            "metrics": {},
        }

    metric, candidate = best
    metrics = {k: res.get(k, 0.0) for k in ("ic", "sharpe", "hit_rate")}
    metrics[f"best_{objective_metric}"] = metric
    return {
        "category_weights": candidate["category_weights"],
        "subreddit_weights": candidate["subreddit_weights"],
        "metrics": metrics,
    }
