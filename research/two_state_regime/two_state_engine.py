"""
research/two_state_regime/two_state_engine.py

Isolated 2-State (Bull/Bear) Statistical Regime Engine.
This module is strictly experimental and decoupled from the main strategy engine.

Computes:
1. Daily Point-in-Time Statistical Feature Vector:
   - Normalized trend z-score: (Price - 200d EMA) / (Price * 252d vol)
   - Realized Volatility: 21-day annualized volatility
   - Trailing 6-Month Momentum: Rate of change over 126 trading days
   - 1-Year Drawdown: Peak-to-trough distance over 252 sessions
2. 2-State Bayesian Gaussian Posterior Classifier:
   Emits P(BULL) and P(BEAR).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class TwoStateRegimeEngine:
    """
    Independent 2-State statistical classifier emitting P(BULL) and P(BEAR).
    """

    def __init__(self):
        # Learned multivariate emission centers [trend_z, realized_vol_21d, momentum_6m, drawdown_1y]
        self.centers = {
            "BULL": np.array([1.10, 0.14, 0.07, -0.04]),
            "BEAR": np.array([-1.20, 0.25, -0.09, -0.16]),
        }
        # Variances for diagonal covariance Gaussian likelihoods
        self.variances = {
            "BULL": np.array([0.70, 0.05, 0.07, 0.04]) ** 2,
            "BEAR": np.array([0.80, 0.07, 0.09, 0.07]) ** 2,
        }
        # Unconditional state priors (historically ~70% expansion, ~30% contraction/grind)
        self.priors = {"BULL": 0.70, "BEAR": 0.30}

    def compute_daily_features(
        self, price_series: pd.Series, date: pd.Timestamp
    ) -> Optional[np.ndarray]:
        """
        Computes Point-in-Time feature vector strictly using data <= date.
        """
        hist = price_series[price_series.index <= date]
        if len(hist) < 252:
            return None

        p_cur = hist.iloc[-1]
        ema_200 = hist.ewm(span=200, adjust=False).mean().iloc[-1]
        ret_daily = hist.pct_change().dropna()
        vol_252d = ret_daily.iloc[-252:].std() * np.sqrt(252) if len(ret_daily) >= 252 else 0.16
        trend_z = float((p_cur - ema_200) / (p_cur * max(vol_252d, 0.05)))

        vol_21d = float(ret_daily.iloc[-21:].std() * np.sqrt(252)) if len(ret_daily) >= 21 else 0.16

        p_126 = hist.iloc[-126] if len(hist) >= 126 else hist.iloc[0]
        mom_6m = float(p_cur / p_126 - 1.0)

        hwm_1y = hist.iloc[-252:].max()
        dd_1y = float((p_cur - hwm_1y) / hwm_1y)

        return np.array([trend_z, vol_21d, mom_6m, dd_1y])

    def calculate_posteriors(self, x: np.ndarray) -> Dict[str, float]:
        """
        Computes normalized Bayesian posterior probabilities: P(BULL | x) and P(BEAR | x).
        """
        log_likes = {}
        for state, mu in self.centers.items():
            var = self.variances[state]
            log_l = -0.5 * np.sum(np.log(2.0 * np.pi * var) + ((x - mu) ** 2) / var)
            log_prior = np.log(self.priors[state])
            log_likes[state] = log_l + log_prior

        max_log = max(log_likes.values())
        exp_vals = {s: np.exp(val - max_log) for s, val in log_likes.items()}
        total_exp = sum(exp_vals.values())

        return {s: float(exp_vals[s] / total_exp) for s in exp_vals}
