"""
markov_lifecycle.py — Corporate Lifecycle & Macro State Transitions

Implements a discrete-time Markov Chain that models how a company
transitions across Peter Lynch's structural lifecycle stages over time.

Instead of a static classification (the current CompanyClassifier), this
module creates a stochastic state machine where transition probabilities
are dynamically computed from:

  1. Reinvestment Rate (j): (Net CapEx + ΔWC) / After-Tax EBIT
  2. ROIC Performance Moat: Return on Invested Capital
  3. Revenue Growth Rate
  4. Margin Variance (stability)

Transition Rules (examples):
  - If RR drops below 15% AND revenue growth < 3%:
    P(STALWART → SLOW_GROWER) shifts to 85%
  - If margin variance spikes past 8%:
    P(slipping into CYCLICAL) expands dramatically
  - If ROIC > 25% AND growth > 20%:
    P(FAST_GROWER → STALWART) is low (stays in growth)

The transition matrix is recomputed dynamically each pipeline run using
current financial metrics, providing a living lifecycle model rather than
a frozen snapshot.

References:
  - Peter Lynch, "One Up on Wall Street" — lifecycle categories
  - Aswath Damodaran, "Finding the Value in the Numbers" — stage mapping
  - Norris, "Applied Stochastic Processes" — Markov chain theory
"""

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class LifecycleState(Enum):
    """Peter Lynch's 6 structural lifecycle categories as Markov states."""
    FAST_GROWER = "FAST_GROWER"
    STALWART = "STALWART"
    SLOW_GROWER = "SLOW_GROWER"
    CYCLICAL = "CYCLICAL"
    TURNAROUND = "TURNAROUND"
    ASSET_PLAY = "ASSET_PLAY"


STATES = list(LifecycleState)
N_STATES = len(STATES)
STATE_INDEX = {s: i for i, s in enumerate(STATES)}


@dataclass
class LifecycleMetrics:
    """Financial metrics that drive transition probabilities."""
    reinvestment_rate: float
    roic: float
    revenue_growth: float
    margin_variance_10y: float
    operating_margin: float
    debt_to_capital: float
    interest_coverage_ratio: float
    cash_burn_months: float = 0.0


@dataclass
class MarkovTransitionResult:
    """Result of a lifecycle Markov Chain analysis."""
    ticker: str
    current_state: LifecycleState
    state_distribution: Dict[str, float]
    transition_matrix: List[List[float]]
    n_steps: int
    projected_state: LifecycleState
    projected_distribution: Dict[str, float]
    transition_volatility: float
    convergence_step: int


def _sigmoid(x: float, threshold: float, width: float) -> float:
    """Logistic sigmoid for smooth transitions."""
    try:
        return 1.0 / (1.0 + math.exp(-(x - threshold) / width))
    except OverflowError:
        return 1.0 if x > threshold else 0.0


def _bell_curve(x: float, center: float, width: float) -> float:
    """Gaussian membership function."""
    return math.exp(-((x - center) ** 2) / (2.0 * width ** 2))


class MarkovLifecycleChain:
    """
    Dynamic Markov Chain for corporate lifecycle state transitions.

    The transition matrix P is recomputed each pipeline run from current
    financial metrics. Each cell P[i][j] represents the probability of
    transitioning from state i to state j in one time step.
    """

    def __init__(self, time_horizon: int = 5):
        """
        Args:
            time_horizon: Number of time steps to project forward
        """
        self.time_horizon = time_horizon

    def _classify_initial_state(self, metrics: LifecycleMetrics) -> LifecycleState:
        """
        Classify the company's current lifecycle state using fuzzy scoring.
        Mirrors the logic in CompanyClassifier.classify() but returns
        a single dominant state for the Markov Chain starting point.
        """
        scores = {s: 0.0 for s in STATES}

        # Cyclicality
        margin_cyclic = _sigmoid(metrics.margin_variance_10y, 0.08, 0.02)
        scores[LifecycleState.CYCLICAL] = margin_cyclic

        # Turnaround
        debt_score = _sigmoid(metrics.debt_to_capital, 0.70, 0.05)
        coverage_score = 1.0 - _sigmoid(metrics.interest_coverage_ratio, 1.0, 0.2)
        scores[LifecycleState.TURNAROUND] = max(debt_score * coverage_score, 0.0)

        # Asset Play
        low_growth = 1.0 - _sigmoid(metrics.revenue_growth, 0.05, 0.02)
        scores[LifecycleState.ASSET_PLAY] = low_growth * 0.5

        # Growth categories
        scores[LifecycleState.FAST_GROWER] = _sigmoid(metrics.revenue_growth, 0.20, 0.03)
        scores[LifecycleState.STALWART] = _bell_curve(metrics.revenue_growth, 0.11, 0.04)
        scores[LifecycleState.SLOW_GROWER] = 1.0 - _sigmoid(metrics.revenue_growth, 0.03, 0.01)

        return max(scores, key=scores.get)

    def compute_transition_matrix(self, metrics: LifecycleMetrics) -> List[List[float]]:
        """
        Compute the dynamic transition matrix from current financial metrics.

        Each row sums to 1.0 (valid probability distribution).
        Transition probabilities are derived from:
          - Reinvestment Rate → determines staying power in growth states
          - ROIC → determines ability to maintain competitive advantage
          - Revenue growth → determines category membership
          - Margin variance → determines cyclical tendency

        Returns:
            N x N transition matrix where N = number of lifecycle states
        """
        rr = metrics.reinvestment_rate
        roic = metrics.roic
        growth = metrics.revenue_growth
        margin_var = metrics.margin_variance_10y
        icr = metrics.interest_coverage_ratio

        # Base transition probabilities (rows = from, cols = to)
        # Order: FAST_GROWER, STALWART, SLOW_GROWER, CYCLICAL, TURNAROUND, ASSET_PLAY
        matrix = [[0.0] * N_STATES for _ in range(N_STATES)]

        # --- FAST_GROWER transitions ---
        # High reinvestment + high ROIC → stays FAST_GROWER
        stay_fg = _sigmoid(rr, 0.30, 0.10) * _sigmoid(roic, 0.20, 0.05)
        # Growth decelerating → moves to STALWART
        fg_to_st = _sigmoid(1.0 - growth, 0.10, 0.05) * _sigmoid(rr, 0.20, 0.08)
        # Growth collapsing + low reinvestment → SLOW_GROWER
        fg_to_sg = _sigmoid(1.0 - growth, 0.15, 0.03) * (1.0 - _sigmoid(rr, 0.15, 0.05))
        # Margin variance high → CYCLICAL
        fg_to_cy = _sigmoid(margin_var, 0.08, 0.03)
        # Very low ICR + negative conditions → TURNAROUND
        fg_to_ta = _sigmoid(1.0 - icr, 2.0, 0.5) * 0.1

        fg_total = stay_fg + fg_to_st + fg_to_sg + fg_to_cy + fg_to_ta
        fg_to_ap = max(0.0, 1.0 - fg_total) if fg_total < 1.0 else 0.0

        matrix[0] = [stay_fg, fg_to_st, fg_to_sg, fg_to_cy, fg_to_ta, fg_to_ap]

        # --- STALWART transitions ---
        # Stable growth + healthy ROIC → stays STALWART
        stay_st = _sigmoid(roic, 0.12, 0.05) * _bell_curve(growth, 0.10, 0.06)
        # Growth accelerating + high reinvestment → FAST_GROWER
        st_to_fg = _sigmoid(growth, 0.18, 0.03) * _sigmoid(rr, 0.35, 0.10)
        # Growth declining below 3% → SLOW_GROWER
        st_to_sg = _sigmoid(1.0 - growth, 0.08, 0.02)
        # Margin variance spiking → CYCLICAL
        st_to_cy = _sigmoid(margin_var, 0.08, 0.03)
        # ICR collapsing → TURNAROUND
        st_to_ta = _sigmoid(1.0 - icr, 2.0, 0.5) * 0.05
        st_to_ap = max(0.0, 1.0 - stay_st - st_to_fg - st_to_sg - st_to_cy - st_to_ta)

        matrix[1] = [st_to_fg, stay_st, st_to_sg, st_to_cy, st_to_ta, st_to_ap]

        # --- SLOW_GROWER transitions ---
        # Low growth, stable → stays SLOW_GROWER
        stay_sg = _bell_curve(growth, 0.02, 0.02) * (1.0 - _sigmoid(margin_var, 0.08, 0.03))
        # Growth recovering + reinvestment rising → STALWART
        sg_to_st = _sigmoid(growth, 0.08, 0.03) * _sigmoid(rr, 0.25, 0.10)
        # Very low growth + low ROIC → ASSET_PLAY
        sg_to_ap = _sigmoid(1.0 - growth, 0.02, 0.01) * (1.0 - _sigmoid(roic, 0.08, 0.03))
        # Margin variance → CYCLICAL
        sg_to_cy = _sigmoid(margin_var, 0.08, 0.03)
        # Distress → TURNAROUND
        sg_to_ta = _sigmoid(1.0 - icr, 2.0, 0.5) * 0.05
        sg_to_fg = max(0.0, 1.0 - stay_sg - sg_to_st - sg_to_ap - sg_to_cy - sg_to_ta)

        matrix[2] = [sg_to_fg, sg_to_st, stay_sg, sg_to_cy, sg_to_ta, sg_to_ap]

        # --- CYCLICAL transitions ---
        # High margin variance → stays CYCLICAL
        stay_cy = _sigmoid(margin_var, 0.06, 0.03) * 0.7
        # Low variance + stable growth → STALWART
        cy_to_st = (1.0 - _sigmoid(margin_var, 0.08, 0.03)) * _sigmoid(growth, 0.08, 0.04)
        # Growth accelerating → FAST_GROWER
        cy_to_fg = _sigmoid(growth, 0.20, 0.04) * (1.0 - _sigmoid(margin_var, 0.08, 0.03))
        # Low growth → SLOW_GROWER
        cy_to_sg = (1.0 - _sigmoid(growth, 0.05, 0.02)) * (1.0 - _sigmoid(margin_var, 0.08, 0.03))
        # Distress → TURNAROUND
        cy_to_ta = _sigmoid(1.0 - icr, 2.0, 0.5) * 0.1
        cy_to_ap = max(0.0, 1.0 - stay_cy - cy_to_st - cy_to_fg - cy_to_sg - cy_to_ta)

        matrix[3] = [cy_to_fg, cy_to_st, cy_to_sg, stay_cy, cy_to_ta, cy_to_ap]

        # --- TURNAROUND transitions ---
        # Recovery (improving ROIC + growth) → STALWART or FAST_GROWER
        ta_recovery = _sigmoid(roic, 0.10, 0.05) * _sigmoid(growth, 0.10, 0.05)
        ta_to_st = ta_recovery * 0.6
        ta_to_fg = ta_recovery * 0.3
        # Stays TURNAROUND if still distressed
        stay_ta = 1.0 - ta_recovery - 0.05
        stay_ta = max(0.1, min(0.9, stay_ta))
        # Fails → ASSET_PLAY
        ta_to_ap = max(0.0, 1.0 - stay_ta - ta_to_st - ta_to_fg)
        ta_to_sg = 0.0
        ta_to_cy = 0.0

        matrix[4] = [ta_to_fg, ta_to_st, ta_to_sg, ta_to_cy, stay_ta, ta_to_ap]

        # --- ASSET_PLAY transitions ---
        # Low growth + low valuation → stays ASSET_PLAY
        stay_ap = _bell_curve(growth, 0.01, 0.02) * 0.6
        # Growth recovering → SLOW_GROWER
        ap_to_sg = _sigmoid(growth, 0.05, 0.02) * 0.3
        # Strong recovery → STALWART
        ap_to_st = _sigmoid(growth, 0.10, 0.03) * _sigmoid(roic, 0.12, 0.05) * 0.1
        ap_to_fg = 0.0
        ap_to_cy = 0.0
        ap_to_ta = max(0.0, 1.0 - stay_ap - ap_to_sg - ap_to_st)

        matrix[5] = [ap_to_fg, ap_to_st, ap_to_sg, ap_to_cy, ap_to_ta, stay_ap]

        # Normalize each row to sum to 1.0
        for i in range(N_STATES):
            row_sum = sum(matrix[i])
            if row_sum > 0:
                matrix[i] = [v / row_sum for v in matrix[i]]
            else:
                matrix[i][i] = 1.0  # Stay in current state if no info

        return matrix

    def _multiply_matrices(
        self, A: List[List[float]], B: List[List[float]]
    ) -> List[List[float]]:
        """Multiply two N x N matrices."""
        n = len(A)
        result = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                for k in range(n):
                    result[i][j] += A[i][k] * B[k][j]
        return result

    def _matrix_power(
        self, M: List[List[float]], power: int
    ) -> List[List[float]]:
        """Compute M^power using repeated squaring."""
        n = len(M)
        result = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
        base = [row[:] for row in M]

        while power > 0:
            if power % 2 == 1:
                result = self._multiply_matrices(result, base)
            base = self._multiply_matrices(base, base)
            power //= 2

        return result

    def compute_initial_distribution(
        self, current_state: LifecycleState
    ) -> List[float]:
        """Create a one-hot initial distribution from the current state."""
        dist = [0.0] * N_STATES
        dist[STATE_INDEX[current_state]] = 1.0
        return dist

    def project_distribution(
        self,
        initial_dist: List[float],
        transition_matrix: List[List[float]],
        n_steps: int,
    ) -> List[float]:
        """
        Project the state distribution forward n_steps using the
        Chapman-Kolmogorov equation: π(t+n) = π(t) × P^n
        """
        P_n = self._matrix_power(transition_matrix, n_steps)
        n = len(initial_dist)
        result = [0.0] * n
        for j in range(n):
            for i in range(n):
                result[j] += initial_dist[i] * P_n[i][j]
        return result

    def compute_transition_entropy(
        self, transition_matrix: List[List[float]]
    ) -> float:
        """
        Compute the entropy of the transition matrix.
        Higher entropy = more uncertainty about future state transitions.
        Used as a volatility signal for the Monte Carlo engine.
        """
        total_entropy = 0.0
        for i in range(N_STATES):
            row_entropy = 0.0
            for j in range(N_STATES):
                p = transition_matrix[i][j]
                if p > 0:
                    row_entropy -= p * math.log2(p)
            total_entropy += row_entropy / N_STATES
        return total_entropy

    def find_convergence_step(
        self,
        initial_dist: List[float],
        transition_matrix: List[List[float]],
        threshold: float = 0.05,
        max_steps: int = 20,
    ) -> int:
        """
        Find the step at which the state distribution stabilizes
        (changes less than threshold between consecutive steps).
        """
        prev_dist = list(initial_dist)
        for step in range(1, max_steps + 1):
            next_dist = self.project_distribution(prev_dist, transition_matrix, 1)
            max_change = max(abs(next_dist[j] - prev_dist[j]) for j in range(N_STATES))
            if max_change < threshold:
                return step
            prev_dist = next_dist
        return max_steps

    def analyze(
        self,
        ticker: str,
        metrics: LifecycleMetrics,
    ) -> MarkovTransitionResult:
        """
        Full Markov Chain lifecycle analysis.

        1. Classify current state
        2. Compute dynamic transition matrix
        3. Project forward by time_horizon steps
        4. Compute convergence and volatility metrics

        Args:
            ticker: Company ticker symbol
            metrics: Current financial metrics driving transitions

        Returns:
            MarkovTransitionResult with full analysis
        """
        current_state = self._classify_initial_state(metrics)
        transition_matrix = self.compute_transition_matrix(metrics)
        initial_dist = self.compute_initial_distribution(current_state)

        # Project forward
        projected_dist = self.project_distribution(
            initial_dist, transition_matrix, self.time_horizon
        )

        # Find projected state (highest probability)
        projected_idx = max(range(N_STATES), key=lambda i: projected_dist[i])
        projected_state = STATES[projected_idx]

        # Compute metrics
        entropy = self.compute_transition_entropy(transition_matrix)
        convergence_step = self.find_convergence_step(
            initial_dist, transition_matrix
        )

        # Build distribution dicts
        state_dist = {
            STATES[i].value: round(initial_dist[i], 4) for i in range(N_STATES)
        }
        proj_dist = {
            STATES[i].value: round(projected_dist[i], 4) for i in range(N_STATES)
        }

        logger.info(
            f"MarkovLifecycleChain({ticker}): "
            f"current={current_state.value}, projected={projected_state.value} "
            f"(step {self.time_horizon}), entropy={entropy:.3f}, "
            f"convergence@{convergence_step}"
        )

        return MarkovTransitionResult(
            ticker=ticker,
            current_state=current_state,
            state_distribution=state_dist,
            transition_matrix=transition_matrix,
            n_steps=self.time_horizon,
            projected_state=projected_state,
            projected_distribution=proj_dist,
            transition_volatility=entropy,
            convergence_step=convergence_step,
        )
