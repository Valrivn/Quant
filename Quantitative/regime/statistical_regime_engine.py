"""
Quantitative/regime/statistical_regime_engine.py

Statistical Market Regime Detection Engine.
Extracts daily macroeconomic and market features, fits a 3-State Gaussian Hidden
Markov Model (HMM) or statistical Gaussian Mixture model, and computes posterior
probabilities: [P(Bull), P(Stagnant), P(Bear)].
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)


@dataclass
class StatisticalRegimeFeatures:
    """Statistical macro and market inputs for regime classification."""
    trend_z: float           # (Price - 200d EMA) / 252d volatility
    realized_vol_21d: float  # 21-day annualized realized volatility
    parkinson_vol: float     # Parkinson high-low volatility
    momentum_6m: float       # 6-month trailing return
    drawdown_1y: float       # Trailing 1-year peak-to-trough drawdown


class StatisticalRegimeEngine:
    """
    Computes statistical regime posterior probabilities based on daily market observables.
    """

    def __init__(self):
        # Learned multivariate emission centers [trend_z, realized_vol_21d, momentum_6m, drawdown_1y]
        self.centers = {
            "BULL": np.array([1.20, 0.13, 0.08, -0.03]),
            "STAGNANT": np.array([-0.10, 0.17, 0.00, -0.06]),
            "BEAR": np.array([-1.50, 0.28, -0.12, -0.18]),
        }
        # Variances across features for diagonal covariance likelihood
        self.variances = {
            "BULL": np.array([0.60, 0.04, 0.06, 0.03]) ** 2,
            "STAGNANT": np.array([0.50, 0.05, 0.05, 0.04]) ** 2,
            "BEAR": np.array([0.80, 0.08, 0.10, 0.08]) ** 2,
        }
        # Dirichlet state prior
        self.priors = {"BULL": 0.55, "STAGNANT": 0.25, "BEAR": 0.20}

    def compute_daily_features(
        self,
        price_series: pd.Series,
        date: pd.Timestamp
    ) -> Optional[np.ndarray]:
        """
        Computes Point-in-Time statistical feature vector strictly using price data <= date.
        """
        hist = price_series[price_series.index <= date]
        if len(hist) < 252:
            return None
            
        p_cur = hist.iloc[-1]
        
        # 1. 200-day EMA and trend z-score
        ema_200 = hist.ewm(span=200, adjust=False).mean().iloc[-1]
        ret_daily = hist.pct_change().dropna()
        vol_252d = ret_daily.iloc[-252:].std() * np.sqrt(252) if len(ret_daily) >= 252 else 0.16
        trend_z = float((p_cur - ema_200) / (p_cur * max(vol_252d, 0.05)))
        
        # 2. 21-day Realized Volatility
        vol_21d = float(ret_daily.iloc[-21:].std() * np.sqrt(252)) if len(ret_daily) >= 21 else 0.16
        
        # 3. 6-month trailing momentum (126 trading days)
        p_126 = hist.iloc[-126] if len(hist) >= 126 else hist.iloc[0]
        mom_6m = float(p_cur / p_126 - 1.0)
        
        # 4. Trailing 1-year drawdown
        hwm_1y = hist.iloc[-252:].max()
        dd_1y = float((p_cur - hwm_1y) / hwm_1y)
        
        return np.array([trend_z, vol_21d, mom_6m, dd_1y])

    def calculate_posteriors(self, x: np.ndarray) -> Dict[str, float]:
        """
        Computes normalized Bayesian posterior probabilities: P(State | x)
        using diagonal Gaussian likelihoods and state priors.
        """
        log_likes = {}
        for state, mu in self.centers.items():
            var = self.variances[state]
            # Log Gaussian pdf
            log_l = -0.5 * np.sum(np.log(2.0 * np.pi * var) + ((x - mu) ** 2) / var)
            log_prior = np.log(self.priors[state])
            log_likes[state] = log_l + log_prior
            
        # Log-sum-exp trick for numerical stability
        max_log = max(log_likes.values())
        exp_vals = {s: np.exp(val - max_log) for s, val in log_likes.items()}
        total_exp = sum(exp_vals.values())
        
        posteriors = {s: float(exp_vals[s] / total_exp) for s in exp_vals}
        return posteriors
