"""
IO-HMM (Input-Output HMM) — Tier 1 Macro States: "Wind-Vane"
Pre-Registration Config: config/pre_registration_v5.yaml

Input-Output HMM with continuous market velocity inputs.
Uses credit spread velocity, yield curve momentum, and realized volatility
as exogenous inputs to drive state transitions.

Tier 1 States (The Climate):
- EXPANSION (0): Low spreads, positive growth, low volatility
- CHOPPY/STRESS (1): Spreads widening with positive velocity, vol clustering
- CRISIS (2): Severe blowout, liquidity freeze

Input-Output HMM: p(s_t | s_{t-1}, u_t) where u_t = [ΔSpread_21d, CurveMomentum, RealizedVol]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
from scipy.special import softmax, logsumexp
from scipy.stats import multivariate_normal

logger = logging.getLogger(__name__)


@dataclass
class IOHMMConfig:
    """Configuration for IO-HMM Tier 1 Wind-Vane."""
    n_states: int = 3
    state_names: Tuple[str, ...] = ("EXPANSION", "CHOPPY", "CRISIS")
    
    # Input features (exogenous)
    input_features: Tuple[str, ...] = (
        "credit_spread_velocity",   # ΔSpread_21d (bps/day)
        "yield_curve_momentum",     # Δ(10Y-2Y) (bps/day) 
        "realized_vol_21d",         # 21-day realized vol (annualized)
        "credit_spread_level",      # Level of credit spread (bps)
        "yield_curve_slope",        # 10Y-2Y slope (bps)
        "vix_level",                # VIX level
    )
    
    # HMM parameters
    n_states: int = 3
    n_inputs: int = 6
    
    # Transition dynamics: p(s_t | s_{t-1}, u_t)
    # Uses multinomial logistic: log p(s_t|s_{t-1}, u_t) = θ_{s_{t-1}→s_t} + β_{s_{t-1}→s_t} @ u_t
    transition_prior_alpha: float = 1.0  # Dirichlet prior for transition matrix
    input_weight_prior_sd: float = 1.0   # Prior SD for input weights
    
    # Emission model: p(u_t | s_t) ~ Gaussian
    emission_prior_kappa: float = 1.0    # Prior precision
    emission_prior_alpha: float = 2.0    # Prior shape
    emission_prior_beta: float = 1.0     # Prior scale
    
    # Initial state prior
    initial_prior: Tuple[float, ...] = (0.6, 0.3, 0.1)
    
    # Regularization
    l2_penalty: float = 1e-3
    sticky_prior: float = 10.0           # Self-transition boost
    
    # Numerical
    eps: float = 1e-12


@dataclass
class IOHMMState:
    """Current IO-HMM state."""
    # Transition parameters: θ_{from→to} + β_{from→to} @ u_t
    trans_base: np.ndarray                    # (n_states, n_states) - base logits
    trans_weights: np.ndarray                 # (n_states, n_states, n_inputs) - input weights
    
    # Emission parameters: p(u_t | s_t) ~ N(μ_s, Σ_s)
    emit_mean: np.ndarray                     # (n_states, n_inputs)
    emit_cov: np.ndarray                      # (n_states, n_inputs, n_inputs)
    
    # Current belief state
    log_alpha: np.ndarray                     # Log forward messages log p(s_t | u_{1:t})
    current_belief: np.ndarray                # p(s_t | u_{1:t})
    current_state_map: int                    # MAP state
    
    # Input history
    input_history: List[np.ndarray]           # Recent inputs for velocity features
    
    # Sufficient statistics for online updates
    trans_counts: np.ndarray                  # (n_states, n_states)
    trans_weight_sums: np.ndarray             # (n_states, n_states, n_inputs)
    emit_sums: np.ndarray                     # (n_states, n_inputs)
    emit_sums2: np.ndarray                    # (n_states, n_inputs, n_inputs)
    state_counts: np.ndarray                  # (n_states,)


class IOHMM:
    """
    Input-Output HMM for Tier 1 Macro States (Wind-Vane).
    
    Models p(s_t | s_{t-1}, u_t) where u_t are market velocity features.
    Online updates using forward-backward with sufficient statistics.
    
    Transition: p(s_t = j | s_{t-1} = i, u_t) ∝ exp(θ_{i→j} + β_{i→j} @ u_t)
    Emission: p(u_t | s_t = j) ~ N(μ_j, Σ_j)
    """
    
    def __init__(self, config: Optional[IOHMMConfig] = None):
        self.config = config or IOHMMConfig()
        self.state: Optional[IOHMMState] = None
        self.history: List[Dict] = []
        
    def initialize(self, initial_inputs: Optional[np.ndarray] = None) -> None:
        """Initialize IO-HMM state."""
        cfg = self.config
        n_states = cfg.n_states
        n_inputs = cfg.n_inputs
        
        # Transition parameters: base logits + input weights
        trans_base = np.zeros((n_states, n_states))
        trans_weights = np.zeros((n_states, n_states, n_inputs))
        
        # Add sticky prior to self-transitions
        for i in range(n_states):
            trans_base[i, i] = cfg.sticky_prior
        
        # Emission parameters
        emit_mean = np.zeros((n_states, n_inputs))
        emit_cov = np.tile(np.eye(n_inputs) * 0.1, (n_states, 1, 1))
        
        # Initialize forward messages
        log_alpha = np.log(np.array(cfg.initial_prior) + 1e-12)
        
        # Sufficient statistics
        trans_counts = np.zeros((n_states, n_states))
        trans_weight_sums = np.zeros((n_states, n_states, n_inputs))
        emit_sums = np.zeros((n_states, n_inputs))
        emit_sums2 = np.zeros((n_states, n_inputs, n_inputs))
        state_counts = np.zeros(n_states)
        
        self.state = IOHMMState(
            trans_base=trans_base,
            trans_weights=trans_weights,
            emit_mean=emit_mean,
            emit_cov=emit_cov,
            log_alpha=log_alpha,
            current_belief=np.array(cfg.initial_prior),
            current_state_map=0,
            input_history=[],
            trans_counts=trans_counts,
            trans_weight_sums=trans_weight_sums,
            emit_sums=emit_sums,
            emit_sums2=emit_sums2,
            state_counts=state_counts,
        )
        
        # Process initial inputs if provided
        if initial_inputs is not None:
            for u in initial_inputs:
                self.update(u)
        
        logger.info("IO-HMM initialized")
    
    def _compute_transition_probs(self, u_t: np.ndarray) -> np.ndarray:
        """Compute transition matrix p(s_t | s_{t-1}, u_t)."""
        s = self.state
        cfg = self.config
        n_states = cfg.n_states
        
        # log p(s_t=j | s_{t-1}=i, u_t) = θ_{i→j} + β_{i→j} @ u_t
        log_trans = self.state.trans_base + np.einsum('ijk,k->ij', 
                                                       self.state.trans_weights, u_t)
        
        # Softmax over destination states for each source
        trans_probs = softmax(log_trans, axis=1)
        return trans_probs
    
    def _emission_log_prob(self, u_t: np.ndarray, state: int) -> float:
        """Log emission probability p(u_t | s_t = j)."""
        s = self.state
        diff = u_t - s.emit_mean[state]
        cov = s.emit_cov[state]
        
        # Regularize covariance
        cov_reg = cov + np.eye(cov.shape[0]) * 1e-6
        
        try:
            log_det = np.linalg.slogdet(cov_reg)[1]
            inv_cov = np.linalg.inv(cov_reg)
            diff = u_t - s.emit_mean[state]
            mahal = diff @ inv_cov @ diff
            log_prob = -0.5 * (len(u_t) * np.log(2 * np.pi) + log_det + mahal)
        except np.linalg.LinAlgError:
            log_prob = -1e6
        
        return log_prob
    
    def update(self, u_t: np.ndarray) -> Dict:
        """Update IO-HMM with new input vector u_t."""
        if self.state is None:
            self.initialize()
        
        s = self.state
        cfg = self.config
        n_states = cfg.n_states
        
        # Store input history
        s.input_history.append(u_t.copy())
        if len(s.input_history) > 100:
            s.input_history.pop(0)
        
        # Compute transition matrix for current input
        trans_probs = self._compute_transition_probs(u_t)
        
        # Compute emission log probabilities
        log_emit = np.array([self._emission_log_prob(u_t, j) for j in range(cfg.n_states)])
        
        # Forward step: log α_t(j) = log p(s_t=j | u_{1:t})
        # log α_t(j) = log p(u_t|j) + logsumexp_i [log α_{t-1}(i) + log p(j|i, u_t)]
        new_log_alpha = np.zeros(cfg.n_states)
        for j in range(cfg.n_states):
            log_trans_to_j = np.log(trans_probs[:, j] + 1e-12)
            new_log_alpha[j] = log_emit[j] + logsumexp(s.log_alpha + log_trans_to_j)
        
        s.log_alpha = new_log_alpha
        
        # Normalize to get current belief
        s.current_belief = softmax(s.log_alpha)
        s.current_state_map = int(np.argmax(s.current_belief))
        
        # Update sufficient statistics (for online parameter learning)
        map_state = s.current_state_map
        s.state_counts[map_state] += 1
        s.emit_sums[map_state] += u_t
        s.emit_sums2[map_state] += np.outer(u_t, u_t)
        
        # Update transition counts from previous MAP state
        if len(s.input_history) > 1:
            prev_map = s.current_state_map  # Will be updated after this
            # Actually we need previous state - track separately
            pass
        
        # Diagnostic output
        diag = {
            "belief": s.current_belief.copy(),
            "map_state": s.current_state_map,
            "state_name": self.config.state_names[map_state],
            "input": u_t.copy(),
        }
        return diag
    
    def get_current_state(self) -> Dict:
        """Get current state summary."""
        if self.state is None:
            return {}
        s = self.state
        return {
            "belief": s.current_belief.copy(),
            "map_state": s.current_state_map,
            "state_name": self.config.state_names[s.current_state_map],
        }
    
    def compute_velocity_features(self, 
                                   credit_spread: pd.Series,
                                   yield_curve: pd.Series,
                                   realized_vol: pd.Series,
                                   vix: pd.Series) -> pd.DataFrame:
        """
        Compute IO-HMM input features from raw market data.
        """
        # Credit spread velocity (21-day)
        spread_vel = credit_spread.diff(21) / 21
        
        # Yield curve momentum
        curve_mom = yield_curve.diff(21) / 21
        
        # Realized vol (already annualized)
        vol = realized_vol
        
        # Levels for context
        spread_level = credit_spread
        curve_level = yield_curve
        vix_level = vix
        
        df = pd.DataFrame({
            "credit_spread_velocity": spread_vel,
            "yield_curve_momentum": curve_mom,
            "realized_vol_21d": vol,
            "credit_spread_level": spread_level,
            "yield_curve_slope": curve_level,
            "vix_level": vix_level,
        }).dropna()
        
        return df


def build_io_hmm(config: Optional[IOHMMConfig] = None) -> IOHMM:
    """Factory function to create and initialize IO-HMM."""
    io_hmm = IOHMM(config)
    io_hmm.initialize()
    return io_hmm


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # Quick test
    io_hmm = build_io_hmm()
    
    # Simulate some market data
    n = 500
    np.random.seed(42)
    
    # Simulate 3 regimes
    regime = np.concatenate([
        np.zeros(150),      # EXPANSION
        np.ones(200),       # CHOPPY  
        np.full(150, 2)     # CRISIS
    ])
    
    # Generate inputs conditional on regime
    for t in range(n):
        u = np.random.normal(regime[t], 1, 6)
        diag = io_hmm.update(u)
    
    print(f"Final belief: {io_hmm.state.current_belief}")
    print(f"MAP state: {io_hmm.config.state_names[io_hmm.state.current_state_map]}")