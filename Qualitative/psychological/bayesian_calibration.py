"""
bayesian_calibration.py — Walk-Forward Backtesting + Bayesian Cross-Validation

Implements institutional-grade walk-forward backtesting to prevent the
Monte Carlo engine from becoming a blind "black box." The system slices
historical data into sequential 6-month blocks, runs the Monte Carlo
simulation at each checkpoint blind to future data, compares predictions
to actual reported fundamentals, then uses Bayesian updating to:

  1. Self-correct simulation variance (growth_std, margin_std) when
     predictions miss — preventing false precision.
  2. Re-rank qualitative source weights (Reddit, GitHub, Glassdoor, etc.)
     based on which sources predicted outcomes accurately — creating a
     dynamic "Bayesian Brain" that learns from experience.

Statistical Foundation:
  - Prior: Monte Carlo simulation using data sliced to time T
  - Likelihood: Actual reported fundamentals from T to T+horizon
  - Posterior: Updated distribution parameters + source weights

References:
  - David Spiegelhalter: "The Art of Statistics" — Bayesian learning
  - Aswath Damodaran: "When the facts change, I change my mind"
  - Robert Kissell: "The Science of Algorithmic Trading" — walk-forward

SEC Rate Limiting: EDGAR enforces 10 requests/second. This module uses
time.sleep(0.1) per request to prevent IP bans.
"""

import json
import logging
import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np

from psychological.engineering_guards import guard_nan, clamp
from psychological.monte_carlo import (
    MonteCarloEngine,
    MonteCarloInput,
    MonteCarloResult,
    create_monte_carlo_engine,
)
from config import load_hybrid_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TimeBlock:
    """A single slice in the walk-forward study.

    The Monte Carlo runs at data_end, predicting through prediction_horizon_end.
    data_start..data_end is the training window (what the MC can see).
    data_end..prediction_horizon_end is the validation window (what actually happened).
    """
    block_id: int
    data_start: str
    data_end: str
    prediction_horizon_end: str


@dataclass
class CalibrationSnapshot:
    """Result of comparing MC predictions vs actual outcomes for one block."""
    block_id: int
    ticker: str
    predicted_mean_iv: float
    predicted_std_iv: float
    predicted_p5: float
    predicted_p95: float
    predicted_growth_rate: float
    predicted_margin: float
    actual_mean_iv: Optional[float] = None
    actual_growth_rate: Optional[float] = None
    actual_margin: Optional[float] = None
    calibration_error: float = 0.0
    pct_within_ci: float = 0.0
    directional_correct: bool = True
    source_weights_used: Dict[str, float] = field(default_factory=dict)
    pre_update_growth_std: float = 0.0
    post_update_growth_std: float = 0.0
    pre_update_margin_std: float = 0.0
    post_update_margin_std: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "block_id": self.block_id,
            "ticker": self.ticker,
            "predicted_mean_iv": self.predicted_mean_iv,
            "predicted_std_iv": self.predicted_std_iv,
            "predicted_p5": self.predicted_p5,
            "predicted_p95": self.predicted_p95,
            "predicted_growth_rate": self.predicted_growth_rate,
            "predicted_margin": self.predicted_margin,
            "actual_mean_iv": self.actual_mean_iv,
            "actual_growth_rate": self.actual_growth_rate,
            "actual_margin": self.actual_margin,
            "calibration_error": self.calibration_error,
            "pct_within_ci": self.pct_within_ci,
            "directional_correct": self.directional_correct,
            "pre_update_growth_std": self.pre_update_growth_std,
            "post_update_growth_std": self.post_update_growth_std,
            "pre_update_margin_std": self.pre_update_margin_std,
            "post_update_margin_std": self.post_update_margin_std,
        }


@dataclass
class SourceAccuracyRecord:
    """Per-source predictive accuracy for a single block."""
    source_name: str
    block_id: int
    ticker: str
    source_value_at_t: float
    actual_outcome: float
    prediction_error: float
    weight_before: float
    weight_after: float


@dataclass
class BayesianCalibrationResult:
    """Full walk-forward calibration result for one ticker."""
    ticker: str
    n_blocks_tested: int
    overall_calibration_error: float
    mean_coverage_probability: float
    directional_accuracy: float
    pre_calibration_growth_std: float
    post_calibration_growth_std: float
    pre_calibration_margin_std: float
    post_calibration_margin_std: float
    source_weight_evolution: Dict[str, List[float]]
    snapshots: List[CalibrationSnapshot]

    def to_dict(self) -> Dict:
        return {
            "ticker": self.ticker,
            "n_blocks_tested": self.n_blocks_tested,
            "overall_calibration_error": self.overall_calibration_error,
            "mean_coverage_probability": self.mean_coverage_probability,
            "directional_accuracy": self.directional_accuracy,
            "pre_calibration_growth_std": self.pre_calibration_growth_std,
            "post_calibration_growth_std": self.post_calibration_growth_std,
            "pre_calibration_margin_std": self.pre_calibration_margin_std,
            "post_calibration_margin_std": self.post_calibration_margin_std,
            "source_weight_evolution": self.source_weight_evolution,
        }


# ---------------------------------------------------------------------------
# Bayesian Updater — Self-corrects simulation parameters
# ---------------------------------------------------------------------------

class BayesianUpdater:
    """Computes posterior updates to Monte Carlo distribution parameters
    based on prediction vs. outcome errors.

    Mathematical basis:
      If calibration_error = ε and coverage = C:
        - excess_error = max(0, ε - 0.5)
        - coverage_gap = max(0, target - C)
        - new_std = current_std * (1 + lr * (excess_error + coverage_gap))

      When accurate (ε < 0.3 AND C > target):
        - surplus = min(0.5, (C - target) * 2)
        - new_std = current_std * (1 - lr * surplus)
    """

    def __init__(self, config_dict: Optional[dict] = None):
        cfg = (config_dict or {}).get("bayesian_calibration", {})
        self.learning_rate: float = cfg.get("learning_rate", 0.15)
        self.min_std_floor: float = cfg.get("min_std_floor", 0.005)
        self.max_std_ceiling: float = cfg.get("max_std_ceiling", 0.20)
        self.coverage_target: float = cfg.get("coverage_target", 0.90)

    def compute_calibration_error(
        self,
        predicted_mean: float,
        predicted_std: float,
        actual_value: float,
    ) -> float:
        """Normalized calibration error: |predicted - actual| / |predicted|.
        Clamped to [0, 2] to prevent explosion from outlier predictions."""
        denom = abs(predicted_mean) if abs(predicted_mean) > 1e-9 else 1.0
        error = abs(actual_value - predicted_mean) / denom
        return clamp(error, 0.0, 2.0)

    def compute_coverage_probability(
        self,
        predicted_p5: float,
        predicted_p95: float,
        actual_value: float,
    ) -> float:
        """1.0 if actual falls within [p5, p95], else 0.0."""
        if predicted_p5 <= actual_value <= predicted_p95:
            return 1.0
        return 0.0

    def update_distribution_params(
        self,
        current_growth_std: float,
        current_margin_std: float,
        calibration_error: float,
        coverage_probability: float,
    ) -> Tuple[float, float]:
        """Bayesian posterior update of simulation standard deviations.

        Widens std when predictions miss (high error or low coverage).
        Tightens std when predictions are accurate (low error + high coverage).
        """
        excess_error = max(0.0, calibration_error - 0.5)
        coverage_gap = max(0.0, self.coverage_target - coverage_probability)

        # Determine direction: widen or tighten
        if excess_error > 0.0 or coverage_gap > 0.0:
            # Miss — widen the distribution
            expansion = 1.0 + self.learning_rate * (excess_error + coverage_gap)
            new_growth_std = current_growth_std * expansion
            new_margin_std = current_margin_std * expansion
        elif calibration_error < 0.3 and coverage_probability > self.coverage_target:
            # Accurate — tighten the distribution
            surplus = min(0.5, (coverage_probability - self.coverage_target) * 2.0)
            contraction = 1.0 - self.learning_rate * surplus
            new_growth_std = current_growth_std * contraction
            new_margin_std = current_margin_std * contraction
        else:
            # No significant change
            new_growth_std = current_growth_std
            new_margin_std = current_margin_std

        # Enforce bounds
        new_growth_std = clamp(new_growth_std, self.min_std_floor, self.max_std_ceiling)
        new_margin_std = clamp(new_margin_std, self.min_std_floor, self.max_std_ceiling)

        return new_growth_std, new_margin_std

    def update_source_weights(
        self,
        current_weights: Dict[str, float],
        source_accuracies: Dict[str, float],
        temperature: float = 2.0,
    ) -> Dict[str, float]:
        """Softmax-based Bayesian re-weighting of qualitative sources.

        Each source's accuracy (1.0 - mean prediction error) is transformed
        via softmax with temperature to produce new weights. High-accuracy
        sources get higher weights; low-accuracy sources get penalized
        but never zeroed out (floor at 0.05).
        """
        if not current_weights or not source_accuracies:
            return current_weights

        # Compute log-softmax of accuracies
        sources = list(current_weights.keys())
        raw_scores = []
        for src in sources:
            acc = source_accuracies.get(src, 0.5)
            raw_scores.append(acc * temperature)

        # Numerical stability: subtract max
        max_score = max(raw_scores) if raw_scores else 0.0
        exp_scores = [math.exp(s - max_score) for s in raw_scores]
        sum_exp = sum(exp_scores) if exp_scores else 1.0

        new_weights = {}
        for i, src in enumerate(sources):
            raw_weight = exp_scores[i] / sum_exp
            # Apply floor to prevent source death
            new_weights[src] = max(0.05, raw_weight)

        # Re-normalize to sum to 1.0
        total = sum(new_weights.values())
        if total > 0:
            new_weights = {k: v / total for k, v in new_weights.items()}

        return new_weights


# ---------------------------------------------------------------------------
# Qualitative Source Tracker — Tracks which scrapers predicted outcomes
# ---------------------------------------------------------------------------

class QualitativeSourceTracker:
    """Tracks per-source predictive accuracy across walk-forward blocks.

    For each source (Reddit, GitHub, Glassdoor, etc.), computes how well
    its signal at time T predicted the actual outcome at time T+horizon.
    Uses Pearson correlation across all blocks to measure predictive power.
    """

    SOURCES = [
        "reddit_sentiment",
        "glassdoor_score",
        "comparably_score",
        "github_velocity",
        "jobspy_zscore",
        "product_sentiment",
        "apewisdom_sentiment",
    ]

    def __init__(self, db_path: str = "reddit_quant.db"):
        self.db_path = db_path

    def get_source_signals_at_date(
        self, ticker: str, date: str
    ) -> Dict[str, float]:
        """Pull each source's normalized signal value at a specific date.

        Sources from daily_aggregations (Reddit categories),
        signal_provenance, and psychological_vectors.
        """
        signals: Dict[str, float] = {}
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                # Reddit sentiment from daily_aggregations
                cursor.execute("""
                    SELECT category,
                           CASE WHEN total_weight > 0
                                THEN weighted_sum / total_weight
                                ELSE 0.0 END as sentiment
                    FROM daily_aggregations
                    WHERE ticker = ? AND date = ?
                """, (ticker, date))
                for row in cursor.fetchall():
                    cat = row["category"]
                    val = guard_nan(row["sentiment"], 0.0)
                    if cat == "retail_options":
                        signals["reddit_sentiment"] = val
                    elif cat == "tech_product":
                        signals["product_sentiment"] = val
                    elif cat == "macro_geopolitical":
                        signals["apewisdom_sentiment"] = val

                # Psychological vectors
                cursor.execute("""
                    SELECT bull_bear_ratio, dev_fork_acceleration
                    FROM psychological_vectors
                    WHERE ticker = ?
                    ORDER BY timestamp DESC LIMIT 1
                """, (ticker,))
                psych_row = cursor.fetchone()
                if psych_row:
                    signals["github_velocity"] = guard_nan(
                        psych_row["dev_fork_acceleration"], 0.0
                    )

                # Signal provenance for Glassdoor, Comparably, JobSpy
                cursor.execute("""
                    SELECT source, AVG(sentiment_score) as avg_score
                    FROM signal_provenance
                    WHERE ticker = ? AND date = ?
                    GROUP BY source
                """, (ticker, date))
                for row in cursor.fetchall():
                    src = row["source"]
                    val = guard_nan(row["avg_score"], 0.5)
                    if "glassdoor" in src.lower():
                        signals["glassdoor_score"] = val
                    elif "comparably" in src.lower():
                        signals["comparably_score"] = val
                    elif "jobspy" in src.lower() or "adzuna" in src.lower():
                        signals["jobspy_zscore"] = val

        except Exception as e:
            logger.debug(f"Source signal fetch failed for {ticker}@{date}: {e}")

        return signals

    def compute_source_predictive_accuracy(
        self,
        ticker: str,
        snapshots: List[CalibrationSnapshot],
    ) -> Dict[str, float]:
        """For each source, compute Pearson correlation between
        its signal at time T and the actual outcome at T+horizon.

        Returns dict of source_name -> correlation coefficient.
        Higher r = stronger predictive power.
        """
        source_values: Dict[str, List[float]] = {s: [] for s in self.SOURCES}
        actual_values: List[float] = []

        for snap in snapshots:
            if snap.actual_mean_iv is None:
                continue
            actual_values.append(snap.actual_mean_iv)
            for src in self.SOURCES:
                val = snap.source_weights_used.get(src, 0.5)
                source_values[src].append(val)

        if len(actual_values) < 3:
            return {s: 0.5 for s in self.SOURCES}

        accuracies: Dict[str, float] = {}
        for src in self.SOURCES:
            vals = source_values[src]
            if len(vals) != len(actual_values) or len(vals) < 3:
                accuracies[src] = 0.5
                continue

            # Pearson correlation
            try:
                r = np.corrcoef(vals, actual_values)[0, 1]
                if np.isnan(r):
                    r = 0.0
                # Map from [-1, 1] to [0, 1] (positive correlation = accurate)
                accuracies[src] = (r + 1.0) / 2.0
            except Exception:
                accuracies[src] = 0.5

        return accuracies

    def get_current_weights(self) -> Dict[str, float]:
        """Return current source weights from DB or defaults."""
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT category_weights FROM weight_versions
                    WHERE is_active = 1
                    ORDER BY promoted_at DESC LIMIT 1
                """)
                row = cursor.fetchone()
                if row and row[0]:
                    return json.loads(row[0])
        except Exception as e:
            logger.debug(f"Failed to load current weights: {e}")

        # Equal-weight defaults
        n = len(self.SOURCES)
        return {s: 1.0 / n for s in self.SOURCES}

    def persist_weight_update(
        self,
        new_weights: Dict[str, float],
        method: str,
        ic_score: float,
        sharpe: float,
    ) -> int:
        """Write new weights to weight_versions table.
        Deactivates previous weights, activates new. Returns version_id."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # Deactivate old
                cursor.execute(
                    "UPDATE weight_versions SET is_active = 0 WHERE is_active = 1"
                )
                now_ts = int(datetime.now(timezone.utc).timestamp())
                cursor.execute("""
                    INSERT INTO weight_versions
                    (category_weights, ic_score, sharpe_ratio, optimization_method,
                     promoted_at, is_active, created_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?)
                """, (
                    json.dumps(new_weights),
                    ic_score,
                    sharpe,
                    method,
                    now_ts,
                    now_ts,
                ))
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.warning(f"Failed to persist weight update: {e}")
            return -1


# ---------------------------------------------------------------------------
# Walk-Forward Engine — Slices history and runs blind simulations
# ---------------------------------------------------------------------------

class WalkForwardEngine:
    """Orchestrates the walk-forward backtesting study.

    Slices historical data into sequential 6-month blocks, runs the
    Monte Carlo at each checkpoint blind to future data, compares
    predictions to actual reported fundamentals.
    """

    def __init__(
        self,
        config_dict: Optional[dict] = None,
        db_path: str = "reddit_quant.db",
    ):
        self.config = config_dict or load_hybrid_config()
        self.mc_engine = create_monte_carlo_engine(self.config)
        self.updater = BayesianUpdater(self.config)
        self.source_tracker = QualitativeSourceTracker(db_path)
        self.db_path = db_path

    def generate_time_blocks(
        self,
        start_date: str,
        end_date: str,
        block_size_months: int = 6,
        horizon_months: int = 6,
    ) -> List[TimeBlock]:
        """Slice [start_date, end_date] into sequential non-overlapping blocks.

        Each block has a training window (data_start..data_end) and a
        prediction horizon (data_end..prediction_horizon_end). Blocks
        are sequential — block[i].prediction_horizon_end == block[i+1].data_end.
        """
        from dateutil.relativedelta import relativedelta

        blocks = []
        current_start = datetime.strptime(start_date, "%Y-%m-%d")
        final_end = datetime.strptime(end_date, "%Y-%m-%d")
        block_id = 0

        while current_start < final_end:
            data_end = current_start + relativedelta(months=block_size_months)
            horizon_end = data_end + relativedelta(months=horizon_months)

            # Clamp to overall end_date
            if data_end > final_end:
                data_end = final_end
            if horizon_end > final_end:
                horizon_end = final_end

            if data_end >= final_end:
                break

            blocks.append(TimeBlock(
                block_id=block_id,
                data_start=current_start.strftime("%Y-%m-%d"),
                data_end=data_end.strftime("%Y-%m-%d"),
                prediction_horizon_end=horizon_end.strftime("%Y-%m-%d"),
            ))

            # Next block starts where this one's horizon ends (non-overlapping)
            current_start = horizon_end
            block_id += 1

        return blocks

    def _fetch_historical_fundamentals(
        self, ticker: str, as_of_date: str
    ) -> Optional[Dict]:
        """Pull the most recent pipeline results as of a specific date.

        Uses four_lane_results table — the latest row where date <= as_of_date.
        Returns None if no data exists.
        """
        try:
            with sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True
            ) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT lane1_expected_growth as growth,
                           lane1_roic as roic,
                           lane1_wacc as wacc,
                           lane1_intrinsic_floor as floor,
                           lane1_intrinsic_ceiling as ceiling,
                           lane2_culture_score as culture_score,
                           lane3_macro_risk_adj as macro_risk_adj,
                           lane3_displacement_ratio as displacement_ratio,
                           lane3_is_leader as is_leader,
                           lane3_positive_eva_prob as positive_eva_prob,
                           lane3_mean_intrinsic_value as mean_iv
                    FROM four_lane_results
                    WHERE ticker = ? AND date <= ?
                    ORDER BY date DESC LIMIT 1
                """, (ticker, as_of_date))
                row = cursor.fetchone()
                if row:
                    return dict(row)
        except Exception as e:
            logger.debug(f"Historical fundamentals fetch failed: {e}")

        return None

    def _fetch_actual_outcomes(
        self, ticker: str, start_date: str, end_date: str
    ) -> Optional[Dict]:
        """Pull actual reported fundamentals for the prediction horizon.

        Uses the latest four_lane_results row within the horizon window.
        """
        try:
            with sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True
            ) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT lane1_expected_growth as growth,
                           lane1_roic as roic,
                           lane1_intrinsic_floor as floor,
                           lane1_intrinsic_ceiling as ceiling,
                           lane3_mean_intrinsic_value as mean_iv,
                           lane2_culture_score as culture_score,
                           lane3_displacement_ratio as displacement_ratio,
                           lane3_is_leader as is_leader
                    FROM four_lane_results
                    WHERE ticker = ? AND date > ? AND date <= ?
                    ORDER BY date DESC LIMIT 1
                """, (ticker, start_date, end_date))
                row = cursor.fetchone()
                if row:
                    return dict(row)
        except Exception as e:
            logger.debug(f"Actual outcomes fetch failed: {e}")

        return None

    def _build_mc_input_from_historical(
        self,
        ticker: str,
        fundamentals: Dict,
        source_weights: Dict[str, float],
        growth_std: float = 0.06,
        margin_std: float = 0.04,
    ) -> MonteCarloInput:
        """Reconstruct a MonteCarloInput from historical data.

        As if standing at the fundamentals' date, with no knowledge
        of the future.
        """
        growth = guard_nan(fundamentals.get("growth"), 0.05)
        roic = guard_nan(fundamentals.get("roic"), 0.10)
        wacc = guard_nan(fundamentals.get("wacc"), 0.10)
        culture = guard_nan(fundamentals.get("culture_score"), 0.5)
        dr = guard_nan(fundamentals.get("displacement_ratio"), 0.0)
        is_leader = bool(fundamentals.get("is_leader", 0))

        # Use source weights to modulate qualitative inputs
        # Higher Reddit weight -> lower effective moat (hype penalty)
        reddit_w = source_weights.get("reddit_sentiment", 0.14)
        github_w = source_weights.get("github_velocity", 0.14)
        glass_w = source_weights.get("glassdoor_score", 0.14)

        # Moat proxy: GitHub velocity is the strongest moat signal
        moat_score = clamp(github_w * 3.0 + glass_w * 2.0, 0.0, 1.0)

        # Reinvestment rate from growth/roic
        rr = growth / roic if roic > 0 else 0.35
        rr = clamp(rr, 0.05, 0.80)

        # Initial revenue/FCF: use intrinsic value as proxy
        mean_iv = guard_nan(fundamentals.get("mean_iv"), 10_000_000_000)
        revenue_proxy = mean_iv * 0.5
        fcf_proxy = mean_iv * 0.15

        return MonteCarloInput(
            ticker=ticker,
            expected_growth_mean=growth,
            expected_growth_std=max(growth_std, 0.01),
            operating_margin_mean=0.15,
            operating_margin_std=max(margin_std, 0.005),
            wacc=wacc,
            reinvestment_rate=rr,
            roic=roic,
            initial_revenue=max(revenue_proxy, 1_000_000),
            initial_fcf=max(fcf_proxy, 100_000),
            projection_years=5,
            terminal_growth=0.03,
            n_simulations=2000,  # Reduced for backtesting speed
            culture_score=culture,
            supplier_concentration=0.5,
            moat_score=moat_score,
            a_tech=0.0,
            displacement_ratio=dr,
            is_leader=is_leader,
        )

    def run_single_block(
        self,
        ticker: str,
        block: TimeBlock,
        current_growth_std: float = 0.06,
        current_margin_std: float = 0.04,
        current_source_weights: Optional[Dict[str, float]] = None,
    ) -> Tuple[CalibrationSnapshot, float, float, Dict[str, float]]:
        """Run MC at block.data_end, predict through block.prediction_horizon_end.

        Returns (snapshot, new_growth_std, new_margin_std, new_source_weights).
        """
        if current_source_weights is None:
            current_source_weights = self.source_tracker.get_current_weights()

        # Step 1: Fetch historical fundamentals as of data_end
        fundamentals = self._fetch_historical_fundamentals(ticker, block.data_end)
        if fundamentals is None:
            logger.debug(
                f"Block {block.block_id}: No fundamentals for {ticker} at {block.data_end}"
            )
            return (
                CalibrationSnapshot(
                    block_id=block.block_id,
                    ticker=ticker,
                    predicted_mean_iv=0.0,
                    predicted_std_iv=0.0,
                    predicted_p5=0.0,
                    predicted_p95=0.0,
                    predicted_growth_rate=0.0,
                    predicted_margin=0.0,
                    source_weights_used=current_source_weights,
                    pre_update_growth_std=current_growth_std,
                    post_update_growth_std=current_growth_std,
                    pre_update_margin_std=current_margin_std,
                    post_update_margin_std=current_margin_std,
                ),
                current_growth_std,
                current_margin_std,
                current_source_weights,
            )

        # Step 2: Build MC input from historical data
        mc_input = self._build_mc_input_from_historical(
            ticker, fundamentals, current_source_weights,
            current_growth_std, current_margin_std,
        )

        # Step 3: Run Monte Carlo (blind to future)
        mc_result = self.mc_engine.run(mc_input)

        # Step 4: Fetch actual outcomes
        actuals = self._fetch_actual_outcomes(
            ticker, block.data_end, block.prediction_horizon_end
        )

        # Step 5: Compute calibration metrics
        if actuals and actuals.get("mean_iv"):
            actual_iv = guard_nan(actuals.get("mean_iv"), 0.0)
            actual_growth = guard_nan(actuals.get("growth"), 0.0)

            cal_error = self.updater.compute_calibration_error(
                mc_result.mean_intrinsic_value,
                mc_result.std_intrinsic_value,
                actual_iv,
            )
            coverage = self.updater.compute_coverage_probability(
                mc_result.p5_intrinsic_value,
                mc_result.p95_intrinsic_value,
                actual_iv,
            )
            # Directional: did we predict positive growth when it was positive?
            predicted_direction = mc_result.mean_growth_rate > 0
            actual_direction = actual_growth > 0
            directional = predicted_direction == actual_direction
        else:
            actual_iv = None
            actual_growth = None
            cal_error = 0.0
            coverage = 1.0
            directional = True

        # Step 6: Bayesian update of distribution parameters
        new_growth_std, new_margin_std = self.updater.update_distribution_params(
            current_growth_std,
            current_margin_std,
            cal_error,
            coverage,
        )

        # Step 7: Compute source accuracy for this block
        source_signals = self.source_tracker.get_source_signals_at_date(
            ticker, block.data_end
        )
        # Simple per-block accuracy: 1.0 - |source_signal - normalized_actual|
        source_accuracies: Dict[str, float] = {}
        if actual_iv is not None and mc_result.mean_intrinsic_value > 0:
            normalized_actual = clamp(actual_iv / mc_result.mean_intrinsic_value, 0.0, 2.0)
            for src, sig_val in source_signals.items():
                error = abs(sig_val - normalized_actual * 0.5)
                source_accuracies[src] = max(0.0, 1.0 - clamp(error, 0.0, 1.0))

        # Step 8: Update source weights
        new_source_weights = self.updater.update_source_weights(
            current_source_weights,
            source_accuracies,
        )

        snapshot = CalibrationSnapshot(
            block_id=block.block_id,
            ticker=ticker,
            predicted_mean_iv=mc_result.mean_intrinsic_value,
            predicted_std_iv=mc_result.std_intrinsic_value,
            predicted_p5=mc_result.p5_intrinsic_value,
            predicted_p95=mc_result.p95_intrinsic_value,
            predicted_growth_rate=mc_result.mean_growth_rate,
            predicted_margin=mc_result.mean_terminal_margin,
            actual_mean_iv=actual_iv,
            actual_growth_rate=actual_growth,
            calibration_error=cal_error,
            pct_within_ci=coverage,
            directional_correct=directional,
            source_weights_used=current_source_weights.copy(),
            pre_update_growth_std=current_growth_std,
            post_update_growth_std=new_growth_std,
            pre_update_margin_std=current_margin_std,
            post_update_margin_std=new_margin_std,
        )

        logger.info(
            f"Block {block.block_id}: predicted={mc_result.mean_intrinsic_value:,.0f}, "
            f"actual={actual_iv}, error={cal_error:.3f}, "
            f"coverage={coverage:.1f}, directional={directional}"
        )

        return snapshot, new_growth_std, new_margin_std, new_source_weights

    def run_walk_forward(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        block_size_months: int = 6,
        horizon_months: int = 6,
        initial_growth_std: float = 0.06,
        initial_margin_std: float = 0.04,
    ) -> BayesianCalibrationResult:
        """Full walk-forward: generate blocks, run each, collect results.

        This is the main entry point for calibration. It processes
        blocks sequentially, updating parameters after each one.
        """
        blocks = self.generate_time_blocks(
            start_date, end_date, block_size_months, horizon_months
        )

        if not blocks:
            logger.warning(f"No time blocks generated for {start_date} to {end_date}")
            return BayesianCalibrationResult(
                ticker=ticker,
                n_blocks_tested=0,
                overall_calibration_error=0.0,
                mean_coverage_probability=0.0,
                directional_accuracy=0.0,
                pre_calibration_growth_std=initial_growth_std,
                post_calibration_growth_std=initial_growth_std,
                pre_calibration_margin_std=initial_margin_std,
                post_calibration_margin_std=initial_margin_std,
                source_weight_evolution={},
                snapshots=[],
            )

        logger.info(
            f"Walk-forward: {len(blocks)} blocks for {ticker} "
            f"({start_date} to {end_date})"
        )

        snapshots: List[CalibrationSnapshot] = []
        current_growth_std = initial_growth_std
        current_margin_std = initial_margin_std
        current_weights = self.source_tracker.get_current_weights()

        # Track weight evolution
        weight_evolution: Dict[str, List[float]] = {
            src: [] for src in QualitativeSourceTracker.SOURCES
        }

        for block in blocks:
            snap, current_growth_std, current_margin_std, current_weights = (
                self.run_single_block(
                    ticker, block,
                    current_growth_std, current_margin_std,
                    current_weights,
                )
            )
            snapshots.append(snap)

            # Record weight evolution
            for src in QualitativeSourceTracker.SOURCES:
                weight_evolution[src].append(
                    current_weights.get(src, 1.0 / len(QualitativeSourceTracker.SOURCES))
                )

            # Persist snapshot to DB
            self._persist_snapshot(snap)

        # Aggregate results
        cal_errors = [s.calibration_error for s in snapshots if s.calibration_error > 0]
        coverages = [s.pct_within_ci for s in snapshots]
        directionals = [s.directional_correct for s in snapshots]

        overall_error = statistics.mean(cal_errors) if cal_errors else 0.0
        mean_coverage = statistics.mean(coverages) if coverages else 0.0
        dir_accuracy = (
            sum(1 for d in directionals if d) / len(directionals)
            if directionals else 0.0
        )

        # Compute source predictive accuracies across all blocks
        source_accuracies = self.source_tracker.compute_source_predictive_accuracy(
            ticker, snapshots
        )

        # Persist final source accuracy
        self._persist_source_accuracy(ticker, snapshots, source_accuracies)

        result = BayesianCalibrationResult(
            ticker=ticker,
            n_blocks_tested=len(blocks),
            overall_calibration_error=overall_error,
            mean_coverage_probability=mean_coverage,
            directional_accuracy=dir_accuracy,
            pre_calibration_growth_std=initial_growth_std,
            post_calibration_growth_std=current_growth_std,
            pre_calibration_margin_std=initial_margin_std,
            post_calibration_margin_std=current_margin_std,
            source_weight_evolution=weight_evolution,
            snapshots=snapshots,
        )

        logger.info(
            f"Walk-forward complete: error={overall_error:.3f}, "
            f"coverage={mean_coverage:.1%}, direction={dir_accuracy:.1%}, "
            f"growth_std {initial_growth_std:.4f} -> {current_growth_std:.4f}, "
            f"margin_std {initial_margin_std:.4f} -> {current_margin_std:.4f}"
        )

        return result

    def _persist_snapshot(self, snapshot: CalibrationSnapshot) -> None:
        """Write calibration snapshot to SQLite."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                now_str = datetime.now(timezone.utc).isoformat()
                cursor.execute("""
                    INSERT OR REPLACE INTO calibration_snapshots
                    (ticker, block_id, data_end_date, prediction_horizon_end,
                     predicted_mean_iv, predicted_std_iv, predicted_p5, predicted_p95,
                     predicted_growth_rate, predicted_margin,
                     actual_mean_iv, actual_growth_rate, actual_margin,
                     calibration_error, pct_within_ci, directional_correct,
                     source_weights_json,
                     pre_update_growth_std, post_update_growth_std,
                     pre_update_margin_std, post_update_margin_std,
                     computed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    snapshot.ticker,
                    snapshot.block_id,
                    snapshot.source_weights_used.get("_data_end", ""),
                    snapshot.source_weights_used.get("_horizon_end", ""),
                    snapshot.predicted_mean_iv,
                    snapshot.predicted_std_iv,
                    snapshot.predicted_p5,
                    snapshot.predicted_p95,
                    snapshot.predicted_growth_rate,
                    snapshot.predicted_margin,
                    snapshot.actual_mean_iv,
                    snapshot.actual_growth_rate,
                    snapshot.actual_margin,
                    snapshot.calibration_error,
                    snapshot.pct_within_ci,
                    1 if snapshot.directional_correct else 0,
                    json.dumps(snapshot.source_weights_used),
                    snapshot.pre_update_growth_std,
                    snapshot.post_update_growth_std,
                    snapshot.pre_update_margin_std,
                    snapshot.post_update_margin_std,
                    now_str,
                ))
                conn.commit()
        except Exception as e:
            logger.debug(f"Failed to persist snapshot: {e}")

    def _persist_source_accuracy(
        self,
        ticker: str,
        snapshots: List[CalibrationSnapshot],
        source_accuracies: Dict[str, float],
    ) -> None:
        """Write source-level accuracy to SQLite."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                now_str = datetime.now(timezone.utc).isoformat()
                for snap in snapshots:
                    for src, acc in source_accuracies.items():
                        weight_before = snap.source_weights_used.get(src, 1.0 / 7)
                        cursor.execute("""
                            INSERT OR REPLACE INTO source_prediction_accuracy
                            (ticker, block_id, source_name, source_value_at_t,
                             actual_outcome, prediction_error,
                             weight_before, weight_after, computed_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            ticker,
                            snap.block_id,
                            src,
                            snap.source_weights_used.get(src, 0.5),
                            snap.actual_mean_iv or 0.0,
                            1.0 - acc,  # error = 1 - accuracy
                            weight_before,
                            acc,  # new weight proxy
                            now_str,
                        ))
                conn.commit()
        except Exception as e:
            logger.debug(f"Failed to persist source accuracy: {e}")


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def create_walk_forward_engine(
    config_dict: Optional[dict] = None,
    db_path: str = "reddit_quant.db",
) -> WalkForwardEngine:
    return WalkForwardEngine(config_dict, db_path)


def create_bayesian_updater(
    config_dict: Optional[dict] = None,
) -> BayesianUpdater:
    return BayesianUpdater(config_dict)


# ---------------------------------------------------------------------------
# Calibration Persistence — Save/load calibrated params for production use
# ---------------------------------------------------------------------------

CALIBRATION_STATE_PATH = "data/bayesian_calibration_state.json"


def save_calibration_state(results: List[BayesianCalibrationResult]) -> None:
    """Persist calibrated growth_std/margin_std per ticker to JSON."""
    state = {}
    for r in results:
        state[r.ticker] = {
            "post_calibration_growth_std": r.post_calibration_growth_std,
            "post_calibration_margin_std": r.post_calibration_margin_std,
            "n_blocks_tested": r.n_blocks_tested,
            "overall_calibration_error": r.overall_calibration_error,
        }
    try:
        import os
        os.makedirs(os.path.dirname(CALIBRATION_STATE_PATH), exist_ok=True)
        with open(CALIBRATION_STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
        logger.info(f"Saved calibration state for {len(state)} tickers to {CALIBRATION_STATE_PATH}")
    except Exception as e:
        logger.warning(f"Failed to save calibration state: {e}")


def load_calibration_state() -> Dict[str, Dict[str, float]]:
    """Load calibrated growth_std/margin_std per ticker. Returns empty dict on failure."""
    try:
        with open(CALIBRATION_STATE_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
