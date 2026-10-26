"""
BOCPD (Bayesian Online Change Point Detection) — Seismic Shock Alarm
Pre-Registration Config: config/pre_registration_v5.yaml

Detects instantaneous structural regime breaks (r_t = 0) using Bayesian online
change point detection with conjugate Gaussian-Gamma observations.

Architecture:
- Online Bayesian inference with conjugate prior (Gaussian-Gamma)
- Hazard function: constant hazard rate λ (default: 1/100 = 1% per day)
- Observation model: Gaussian with unknown mean/variance (Gaussian-Gamma conjugate)
- Output: run-length posterior p(r_t | x_{1:t}) for real-time CP detection

Trigger: P(r_t = 0 | x_{1:t}) > threshold → Seismic Alarm triggered
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
from scipy.special import gammaln, digamma

logger = logging.getLogger(__name__)


@dataclass
class BOCPDConfig:
    """Configuration for BOCPD Seismic Alarm."""
    hazard_rate: float = 0.01          # Constant hazard rate λ (per observation)
    threshold: float = 0.95            # P(r_t = 0 | data) threshold for alarm
    min_run_length: int = 5            # Minimum run length before alarm
    max_run_length: int = 500          # Maximum run length to track
    
    # Gaussian-Gamma prior parameters (conjugate prior for unknown mean/variance)
    mu_0: float = 0.0                  # Prior mean
    kappa_0: float = 1.0               # Prior precision scaling
    alpha_0: float = 2.0               # Prior shape (inverse-Gamma)
    beta_0: float = 1.0                # Prior scale (inverse-Gamma)
    
    # Numerical stability
    eps: float = 1e-12
    log_eps: float = -30.0


@dataclass
class BOCPDState:
    """Current BOCPD state for online updates."""
    run_length_probs: np.ndarray       # p(r_t | x_{1:t}) for r_t = 0..max_run_length
    run_length_probs_log: np.ndarray   # Log probabilities
    
    # Sufficient statistics for each run length
    N: np.ndarray                      # Count of observations
    sum_x: np.ndarray                  # Sum of observations
    sum_x2: np.ndarray                 # Sum of squared observations
    
    # Gaussian-Gamma posterior parameters for each run length
    mu_n: np.ndarray
    kappa_n: np.ndarray
    alpha_n: np.ndarray
    beta_n: np.ndarray
    
    # Current run length MAP estimate
    map_run_length: int
    p_r0: float                        # P(r_t = 0 | data)
    
    # Alarm state
    alarm_triggered: bool
    alarm_run_length: int


class BOCPD:
    """
    Bayesian Online Change Point Detection (BOCPD) for seismic shock detection.
    
    Uses conjugate Gaussian-Gamma model for online Bayesian inference.
    Based on Adams & MacKay (2007) "Bayesian Online Changepoint Detection".
    """
    
    def __init__(self, config: Optional[BOCPDConfig] = None):
        self.config = config or BOCPDConfig()
        self.state: Optional[BOCPDState] = None
        self.history: List[Dict] = []  # For diagnostics
        
    def initialize(self, initial_data: Optional[np.ndarray] = None) -> None:
        """Initialize BOCPD state with optional initial data."""
        cfg = self.config
        max_r = cfg.max_run_length
        
        # Initialize run length probabilities (uniform prior)
        rl_probs = np.ones(max_r + 1) / (max_r + 1)
        rl_probs_log = np.log(rl_probs + cfg.eps)
        
        # Sufficient statistics
        N = np.ones(max_r + 1)
        sum_x = np.zeros(max_r + 1)
        sum_x2 = np.zeros(max_r + 1)
        
        # Gaussian-Gamma posterior parameters
        mu_n = np.full(max_r + 1, cfg.mu_0)
        kappa_n = np.full(max_r + 1, cfg.kappa_0)
        alpha_n = np.full(max_r + 1, cfg.alpha_0)
        beta_n = np.full(max_r + 1, cfg.beta_0)
        
        # Initialize with optional data
        if initial_data is not None and len(initial_data) > 0:
            for x in initial_data:
                self._update(x)
            return
        
        self.state = BOCPDState(
            run_length_probs=rl_probs,
            run_length_probs_log=rl_probs_log,
            N=N,
            sum_x=sum_x,
            sum_x2=sum_x2,
            mu_n=mu_n,
            kappa_n=kappa_n,
            alpha_n=alpha_n,
            beta_n=beta_n,
            map_run_length=0,
            p_r0=1.0 / (max_r + 1),
            alarm_triggered=False,
            alarm_run_length=-1,
        )
        
        logger.info("BOCPD initialized")
    
    def _log_hazard(self, r: int) -> float:
        """Log hazard function: constant hazard rate."""
        return np.log(self.config.hazard_rate + self.config.eps)
    
    def _log_no_hazard(self, r: int) -> float:
        """Log no-hazard function: 1 - hazard."""
        return np.log(1 - self.config.hazard_rate + self.config.eps)
    
    def _predictive_log_prob(self, x: float, r: int) -> float:
        """
        Log predictive probability p(x | r_t = r, data_{t-r:t-1}).
        Uses Student-t distribution from Gaussian-Gamma posterior.
        """
        s = self.state
        cfg = self.config
        
        if r == 0:
            # New run: use prior predictive
            mu = self.config.mu_0
            kappa = self.config.kappa_0
            alpha = self.config.alpha_0
            beta = self.config.beta_0
        else:
            # Existing run: use posterior predictive
            mu = s.mu_n[r]
            kappa = s.kappa_n[r]
            alpha = s.alpha_n[r]
            beta = s.beta_n[r]
        
        # Student-t predictive: location=mu, scale=sqrt(beta*(kappa+1)/(kappa*alpha)), dof=2*alpha
        nu = 2 * alpha
        scale_sq = beta * (kappa + 1) / (kappa * alpha)
        
        # Log Student-t PDF
        log_prob = (gammaln((nu + 1) / 2) - gammaln(nu / 2) 
                    - 0.5 * np.log(nu * np.pi * scale_sq)
                    - (nu + 1) / 2 * np.log(1 + (x - mu)**2 / (nu * scale_sq)))
        
        return max(log_prob, cfg.log_eps)
    
    def _update_sufficient_stats(self, x: float, r: int) -> None:
        """Update sufficient statistics for run length r."""
        s = self.state
        
        if r == 0:
            # New run: start fresh
            s.N[0] = 1
            s.sum_x[0] = x
            s.sum_x2[0] = x * x
        else:
            # Extend existing run
            s.N[r] = s.N[r - 1] + 1
            s.sum_x[r] = s.sum_x[r - 1] + x
            s.sum_x2[r] = s.sum_x2[r - 1] + x * x
        
        # Update Gaussian-Gamma parameters
        if r == 0:
            s.kappa_n[0] = self.config.kappa_0 + 1
            s.mu_n[0] = (self.config.kappa_0 * self.config.mu_0 + x) / s.kappa_n[0]
            s.alpha_n[0] = self.config.alpha_0 + 0.5
            s.beta_n[0] = (self.config.beta_0 + 
                          0.5 * (x - self.config.mu_0)**2 * self.config.kappa_0 / s.kappa_n[0])
        else:
            s.kappa_n[r] = s.kappa_n[r - 1] + 1
            s.mu_n[r] = (s.kappa_n[r - 1] * s.mu_n[r - 1] + x) / s.kappa_n[r]
            s.alpha_n[r] = s.alpha_n[r - 1] + 0.5
            s.beta_n[r] = (s.beta_n[r - 1] + 
                          0.5 * (x - s.mu_n[r - 1])**2 * s.kappa_n[r - 1] / s.kappa_n[r])
    
    def update(self, x: float) -> Dict:
        """
        Update BOCPD with new observation x.
        Returns diagnostic dict with alarm state and probabilities.
        """
        if self.state is None:
            self.initialize()
        
        s = self.state
        cfg = self.config
        max_r = cfg.max_run_length
        
        # Compute predictive probabilities for all run lengths
        log_pred = np.array([self._predictive_log_prob(x, r) for r in range(max_r + 1)])
        
        # Compute growth probabilities (extend run): p(r_t = r | x, r_{t-1} = r-1)
        log_growth = s.run_length_probs_log[:-1] + log_pred[1:] + self._log_no_hazard(0)
        
        # Compute changepoint probabilities (new run): p(r_t = 0 | x, any r_{t-1})
        log_cp = np.logaddexp.reduce(s.run_length_probs_log + log_pred + self._log_hazard(0))
        
        # New run length probabilities
        new_log_probs = np.zeros(max_r + 1)
        new_log_probs[0] = log_cp
        new_log_probs[1:] = log_growth
        
        # Normalize
        max_log = np.max(new_log_probs)
        new_probs = np.exp(new_log_probs - max_log)
        new_probs = new_probs / (new_probs.sum() + cfg.eps)
        
        # Update sufficient statistics for all run lengths
        # For r=0 (new run)
        self._update_sufficient_stats(x, 0)
        
        # For r>0 (extend existing runs)
        # We need to shift statistics from r-1 to r
        # This is done by updating from max_r down to 1
        for r in range(max_r, 0, -1):
            if s.N[r - 1] > 0:
                self._update_sufficient_stats(x, r)
        
        # Update run length probabilities
        s.run_length_probs = new_probs
        s.run_length_probs_log = new_log_probs
        
        # MAP run length
        s.map_run_length = int(np.argmax(s.run_length_probs))
        s.p_r0 = s.run_length_probs[0]
        
        # Check alarm condition
        alarm = (s.p_r0 >= cfg.threshold and s.map_run_length <= cfg.min_run_length)
        s.alarm_triggered = bool(alarm)
        if alarm and not s.alarm_triggered:
            s.alarm_run_length = s.map_run_length
        
        # Diagnostic output
        diag = {
            "x": x,
            "p_r0": s.p_r0,
            "map_run_length": s.map_run_length,
            "alarm_triggered": s.alarm_triggered,
            "alarm_run_length": s.alarm_run_length,
            "run_length_probs": s.run_length_probs.copy(),
            "mean": self._posterior_mean(),
            "var": self._posterior_var(),
        }
        
        self.history.append(diag)
        return diag
    
    def _posterior_mean(self) -> float:
        """Posterior mean under most likely run length."""
        if self.state is None:
            return 0.0
        r = self.state.map_run_length
        if r == 0:
            return self.config.mu_0
        return self.state.mu_n[r]
    
    def _posterior_var(self) -> float:
        """Posterior variance under most likely run length."""
        if self.state is None:
            return 1.0
        r = self.state.map_run_length
        if r == 0:
            return self.config.beta_0 / (self.config.alpha_0 - 1)
        return self.state.beta_n[r] / (self.state.alpha_n[r] - 1)
    
    def get_diagnostics(self) -> Dict:
        """Get full diagnostic state."""
        if self.state is None:
            return {}
        s = self.state
        return {
            "p_r0": s.p_r0,
            "map_run_length": s.map_run_length,
            "alarm_triggered": s.alarm_triggered,
            "alarm_run_length": s.alarm_run_length,
            "run_length_dist": s.run_length_probs.copy(),
            "posterior_mean": self._posterior_mean(),
            "posterior_var": self._posterior_var(),
            "history_len": len(self.history),
        }
    
    def reset(self) -> None:
        """Reset BOCPD to initial state."""
        self.state = None
        self.history = []
        self.initialize()
        logger.info("BOCPD reset")


def build_bocpd(config: Optional[BOCPDConfig] = None) -> BOCPD:
    """Factory function to create and initialize BOCPD."""
    bocpd = BOCPD(config)
    bocpd.initialize()
    return bocpd


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Quick test with synthetic data
    np.random.seed(42)
    
    # Generate data with a changepoint
    n = 500
    cp = 250
    data = np.concatenate([
        np.random.normal(0, 1, cp),      # Pre-change: N(0,1)
        np.random.normal(3, 1.5, n - cp) # Post-change: N(3, 1.5)
    ])
    
    bocpd = build_bocpd(BOCPDConfig(hazard_rate=0.01, threshold=0.95))
    
    alarms = []
    for i, x in enumerate(data):
        diag = bocpd.update(x)
        if diag["alarm_triggered"]:
            alarms.append(i)
    
    print(f"True changepoint: {cp}")
    print(f"Detected alarms: {alarms}")
    if alarms:
        print(f"First alarm at: {alarms[0]} (delay: {alarms[0] - cp})")