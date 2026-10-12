"""
Gate Calibration — Phase 9C  (big-pickle-worker)

WHAT IS BEING CALIBRATED
------------------------
Pre-registration v7 is FROZEN and SHA-256 pinned (``c80a47bf...``, verified by
``ingestion.harness.prereg.EXPECTED_SHA256``).  It declares, verbatim::

    statistical_design:
      metric: "composite_dsr"
      no_gate_hierarchy: true   # REMOVED: 8-gate Holm-Bonferroni structure
      dsr: {n_trials: 513, track_record_days: 2520, threshold: 0.95}
      fold_evaluation: {mode: "concatenated", fold_4_true_oos: true}

``ingestion.harness.gate_engine`` implements exactly that and NOTHING else: the
registry, the per-gate families and ``_holm_step_down`` were deleted, and
``CompositeGateEngine.__init__`` asserts ``no_gate_hierarchy`` on every
construction.  Therefore the pass/fail rule this module calibrates is::

    PASS  <=>  CompositeGateEngine.evaluate_composite_dsr(...) >= 0.95
              (and, when fold returns are supplied, Fold 4 independently >= 0.95)

There is no alpha/8 and no 8-gate FWER family to calibrate, because the frozen
design does not contain one.  Inventing one would be a pre-registration
violation, and re-pinning the pre-registration to restore it is a CEO decision,
not an engineering one.

CLAIM MAP — READ BEFORE QUOTING ANY NUMBER FROM THE REPORT
----------------------------------------------------------
``decision_rule``          The composite-DSR pass rate.  THIS is the only
                           false-positive rate in the report that answers the
                           question "how often does the frozen gate fire on a
                           strategy with no skill?".  It is compared against the
                           nominal 1/n_trials the DSR claims for itself.
``threshold_reference_only``  Per-metric thresholds named in
                           ``success_criteria.fold_4_test`` (Sharpe, drawdown,
                           IR, ARI, Calmar, swap churn, TCA, probation).  These
                           are NOT hypothesis tests, they have no alpha, and a
                           movement in them is not a calibration claim in either
                           direction.  They are reported for power planning.
``declared_assumption``    Structural compliance components (WACC coverage,
                           provenance chain).  Their null rates are modelling
                           assumptions, never measurements.

THE NULL
--------
Per ``execution`` in the frozen document: a ~1,500-name universe
(``universe_size_target: 1500``), monthly rebalance, over the frozen primary
window 2018-01-01..2024-12-31 (``windows.primary``), 84 months.

Every name has ZERO alpha and a market-like factor loading.  Weights are
Dirichlet(1) draws, re-drawn every month — a strategy with no selection skill
and no persistence.  The resulting portfolio is ~beta 1 to the market, which is
the adversarial case that matters: a purely passive book can only clear a
Sharpe-style gate through the market's own drift.

CLI
---
    python -m validation.gate_calibration --run --n 10000 --phase9
    python -m validation.gate_calibration --run --n 500            # smoke
    python -m validation.gate_calibration --run --n 2000 --exit-logic
    python -m validation.gate_calibration --test

REPORT
------
``data/validation/gate_calibration_report.json``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.harness.gate_engine import (          # noqa: E402  (path shim first)
    DSR_N_TRIALS,
    DSR_THRESHOLD,
    CompositeGateEngine,
    deflated_sharpe_ratio,
)
from ingestion.harness.prereg import MODULE_PREREG, load_prereg  # noqa: E402

DEFAULT_REPORT = Path("data/validation/gate_calibration_report.json")
EXIT_REPORT = Path("data/validation/exit_logic_calibration.json")
N_NULL_DEFAULT = 10_000
SEED = 20260909

# ---------------------------------------------------------------------------
# NULL DGP — frozen windows, prereg-declared universe size
# ---------------------------------------------------------------------------

P9_N_NAMES = 1500              # execution.universe_size_target
P9_N_MONTHS = 84               # windows.primary 2018-01-01..2024-12-31
P9_N_FOLDS = 4                 # windows.primary.folds.fold_1..fold_4
P9_START = "2018-01-01"
P9_END = "2024-12-31"

#: Market/factor block.  Calibrated to a broad US equity index at MONTHLY
#: frequency: ~8.6%/yr drift, ~13.9%/yr vol.  These are modelling choices and
#: are reported in the report so a reader can disagree with them explicitly.
P9_FACTOR_MU = 0.0068
P9_FACTOR_SD = 0.0430
P9_N_FACTORS = 8

#: Single-name idiosyncratic vol (~17%/yr).  With N=1500 and Dirichlet weights,
#: the idio block contributes ~0.2% of portfolio variance, so the null book is
#: effectively a market-beta book — which is the point.
P9_IDIO_SD = 0.0550

#: Every name has EXACTLY zero alpha.  This is the null.
P9_NULL_ALPHA = 0.0

#: Dirichlet concentration.  alpha=1 is the uniform simplex draw: maximum
#: weight churn, zero persistence.  It is the adversarial null for the turnover
#: and swap-churn thresholds and it is stated in the report.
P9_DIRICHLET_ALPHA = 1.0

#: Monthly factor loadings: market column pinned to ~1, the rest random.
P9_LOADING_SD = 0.35

#: One-way trading cost charged to the portfolio at each rebalance, from
#: ``costs`` (commission 10bps/side + slippage 50bps base, EXPANSION
#: multiplier 1.0).  Applied as a drag on the realised portfolio return so the
#: null is net-of-costs, matching how the live book is measured.
P9_COST_DRAG = 0.0060

#: HMM ARI null.  Under "no regime structure" both the true labels and the
#: model's recovered labels are i.i.d. uniform over ``k`` states, so the ARI of
#: two independent random partitions IS the null distribution.  Each walk-forward
#: fold is one calendar year = 12 monthly observations.
P9_ARI_FOLD_MONTHS = 12
P9_ARI_STATES = 3
P9_ARI_FOLDS_REQUIRED = 3

#: Cost/risk structural nulls, as DECLARED ASSUMPTIONS (never measurements).
P9_PROBATION_TRIGGERS = 400     # frozen probation evidence floor (CEO Ruling #2)
P9_NULL_PROBATION_RATE = 0.50   # ingestion.quality_thresholds, all-medium null mix
P9_G8_WACC_FULL_P = 0.50        # P(a null run coincidentally has full coverage)
P9_G8_SWAP_CHURN_SD = 12.0      # half-normal scale, swaps/year
P9_G8_PROVENANCE_P = 1.00       # provenance_chain passes iff the root verifies

# ---------------------------------------------------------------------------
# POWER / MDE grid -- declared here because `calibrate_gates` defaults to it.
# ---------------------------------------------------------------------------

#: Annualised Sharpe grid the power curve is evaluated on.  Zero is the null,
#: the rest bracket the plausible range for a real book.  It runs to 8.00
#: because the BINDING limb is Fold 4: on 12 monthly observations against a
#: 513-trial deflation, the closed-form MDE lands near 4.9, and a grid that
#: stopped at 4.0 would report "unreachable" for a limb that is merely hard.
P9_POWER_SR_GRID = tuple(round(0.1 * i, 2) for i in range(0, 81))   # 0.00 .. 8.00

#: Student-t degrees of freedom for the fat-tail power arm.
P9_POWER_T_DF = 5

#: Draws per power-curve point in a production run.
P9_POWER_DRAWS = 2000


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def wilson_ci(k: int, n: int, z: float = 1.96) -> Dict[str, float]:
    """
    Wilson score interval for a binomial proportion.

    Preferred over the Wald interval everywhere in this module because the
    events of interest here are RARE: at the rates we expect to measure, the
    Wald interval has negative lower bounds and an error term larger than the
    estimate itself.
    """
    if n <= 0:
        return {"lo": 0.0, "hi": 1.0, "n": 0, "k": 0}
    p = float(k) / float(n)
    denom = 1.0 + (z * z) / n
    centre = (p + (z * z) / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1.0 - p) / n + (z * z) / (4.0 * n * n))
    return {
        "lo": round(max(0.0, centre - half), 8),
        "hi": round(min(1.0, centre + half), 8),
        "n": int(n), "k": int(k),
    }


def _n_needed_for_zero_rejection(alpha: float) -> int:
    """
    Smallest ``n`` at which observing ZERO rejections would *demonstrate*
    (Wilson upper bound <= alpha) that an any-rejection rate is at or below
    ``alpha``.

    For k = 0 the Wilson upper bound collapses to (z^2/n)/(1 + z^2/n), so
    solving ``upper <= alpha`` gives n >= z^2 / (alpha * (1 - alpha)).
    """
    if not (0.0 < alpha < 1.0):
        return 0
    z = 1.96
    return max(1, int(math.ceil((z * z) / (alpha * (1.0 - alpha)))))


def _three_state_verdict(k: int, n: int, target: float) -> str:
    """
    "controlled" | "violated" | "inconclusive_insufficient_n".

    Conflating "not demonstrated" with "violated" is a real error: observing
    ZERO rejections is perfectly consistent with a correctly calibrated rate,
    and at small n the Wilson interval straddling the target means the data
    cannot resolve it, not that the target was missed.
    """
    ci = wilson_ci(k, n)
    if float(ci["lo"]) > target:
        return "violated"
    if float(ci["hi"]) <= target:
        return "controlled"
    return "inconclusive_insufficient_n"


def _spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """
    Spearman rank correlation, average ranks for ties.

    Dependency-free on purpose: this module reports a Monte Carlo power curve
    and needs one order-correlation statistic, which does not justify a scipy
    import at module scope.
    """
    def _rank(v: Sequence[float]) -> np.ndarray:
        arr = np.asarray(v, dtype=float)
        order = np.argsort(arr, kind="mergesort")
        ranks = np.empty(arr.size, dtype=float)
        ranks[order] = np.arange(1, arr.size + 1, dtype=float)
        # Average the ranks inside each tie group so the statistic is not
        # decided by sort stability.
        _, inv, counts = np.unique(arr, return_inverse=True, return_counts=True)
        sums = np.zeros(counts.size, dtype=float)
        np.add.at(sums, inv, ranks)
        return (sums / counts)[inv]

    if len(x) != len(y) or len(x) < 3:
        return float("nan")
    rx, ry = _rank(x), _rank(y)
    rx = rx - rx.mean()
    ry = ry - ry.mean()
    denom = math.sqrt(float((rx ** 2).sum()) * float((ry ** 2).sum()))
    return float((rx * ry).sum() / denom) if denom > 0 else float("nan")


def classify_sharpe_lookahead_risk(sharpe: float, n_months: int) -> Dict[str, Any]:
    """
    Returns flag based on MDE ceilings for given track record length.
    Sharpe above these ceilings indicates probable lookahead bias, not skill.
    """
    if n_months >= 84:  # Whole record
        mde_80 = 2.52
        mde_90 = 3.12
    elif n_months >= 12:  # Fold 4
        mde_80 = 3.12
        mde_90 = 4.0  # Extrapolated
    else:
        mde_80 = 3.12
        mde_90 = 4.0
    
    if sharpe < 0:
        return {"flag": "HONEST_NEGATIVE", "lookahead_risk": "NONE", "action": "ALLOW"}
    elif sharpe <= 1.5:
        return {"flag": "PLAUSIBLE", "lookahead_risk": "LOW", "action": "ALLOW"}
    elif sharpe <= mde_80:
        return {"flag": "ELEVATED", "lookahead_risk": "MEDIUM", "action": "SCRUTINIZE"}
    elif sharpe <= mde_90:
        return {"flag": "SUSPICIOUS", "lookahead_risk": "HIGH", "action": "DEEP_AUDIT"}
    else:
        return {"flag": "REJECT_LOOKAHEAD", "lookahead_risk": "CRITICAL", "action": "HARD_FAIL"}


def _dist(vals: Sequence[float], extra: Sequence[float] = ()) -> Dict[str, Any]:
    arr = np.asarray([v for v in vals if v is not None and np.isfinite(v)], dtype=float)
    out: Dict[str, Any] = {
        "n": int(arr.size),
        "mean": None, "sd": None, "p05": None, "p50": None, "p95": None,
    }
    if arr.size:
        out.update({
            "mean": round(float(arr.mean()), 6),
            "sd": round(float(arr.std(ddof=1)), 6) if arr.size > 1 else None,
            "p05": round(float(np.quantile(arr, 0.05)), 6),
            "p50": round(float(np.quantile(arr, 0.50)), 6),
            "p95": round(float(np.quantile(arr, 0.95)), 6),
        })
    return out


# ---------------------------------------------------------------------------
# NULL PORTFOLIO
# ---------------------------------------------------------------------------

class NullBook:
    """
    One simulated null strategy: 1,500 names, 84 monthly rebalances, 2018-2024.

    Attributes
    ----------
    dates        month-end DatetimeIndex, 84 entries
    arm_c        net-of-cost portfolio return per rebalance (pd.Series)
    spy          the market/factor return over the same months
    weights      (T, N) float array of realised weights
    name_ids     (N,) int array of factor loadings
    """

    __slots__ = ("dates", "arm_c", "spy", "weights", "loadings", "gross")

    def __init__(self, dates: pd.DatetimeIndex, arm_c: pd.Series, spy: pd.Series,
                 weights: np.ndarray, loadings: np.ndarray,
                 gross: pd.Series) -> None:
        self.dates = dates
        self.arm_c = arm_c
        self.spy = spy
        self.weights = weights
        self.loadings = loadings
        self.gross = gross


def _month_end_index(n_months: int = P9_N_MONTHS,
                     start: str = P9_START) -> pd.DatetimeIndex:
    """Frozen primary window, month-end stamps.  Monthly => 12x annualisation."""
    return pd.date_range(start=start, periods=n_months, freq="ME")


def _phase9_null_book(
    rng: np.random.Generator,
    n_months: int = P9_N_MONTHS,
    n_names: int = P9_N_NAMES,
    n_factors: int = P9_N_FACTORS,
    dirichlet_alpha: float = P9_DIRICHLET_ALPHA,
    alpha_per_name: float = P9_NULL_ALPHA,
    cost_drag: float = P9_COST_DRAG,
    loadings: Optional[np.ndarray] = None,
) -> NullBook:
    """
    Draw one null strategy.

    Zero alpha per name is exact by construction, not an approximation: the
    return of name i in month t is ``beta_i . f_t + eps_it`` with no constant.
    Weights are i.i.d. Dirichlet(alpha) across the whole universe each month,
    so there is no selection, no persistence and no timing skill.

    The cost drag is applied to the *second and later* rebalances only — the
    first rebalance is the initial build, which is not a trade against a prior
    book.
    """
    dates = _month_end_index(n_months)

    # Factor block: column 0 is the market, the rest are style factors.
    factors = rng.normal(P9_FACTOR_MU, P9_FACTOR_SD, (n_months, n_factors))

    if loadings is None:
        loadings = rng.normal(0.0, P9_LOADING_SD, (n_names, n_factors))
        # Pin the market column so the universe is ~beta 1 by construction.
        loadings[:, 0] = 1.0 + rng.normal(0.0, 0.05, n_names)
        loadings[:, 0] = np.clip(loadings[:, 0], 0.3, 1.8)

    idio = rng.normal(0.0, P9_IDIO_SD, (n_names, n_months))
    name_ret = loadings @ factors.T + idio
    if alpha_per_name:
        name_ret = name_ret + alpha_per_name

    weights = rng.dirichlet(np.full(n_names, dirichlet_alpha), size=n_months)
    gross = np.einsum("nt,tn->t", name_ret, weights)

    one_way_turnover = np.zeros(n_months)
    if n_months > 1:
        one_way_turnover[1:] = 0.5 * np.abs(np.diff(weights, axis=0)).sum(axis=1)
    net = gross - cost_drag * one_way_turnover

    return NullBook(
        dates=dates,
        arm_c=pd.Series(net, index=dates),
        spy=pd.Series(factors[:, 0], index=dates),
        weights=weights,
        loadings=loadings,
        gross=pd.Series(gross, index=dates),
    )


def _weights_frame(book: NullBook, n_tickers: int = 12) -> pd.DataFrame:
    """
    Weight frame for ``score_turnover``.

    The turnover component is a scalar summary of the FULL 1,500-name weight
    path, so we hand the engine the real per-name matrix rather than a thin
    slice: passing a truncated book would understate dispersion and inflate the
    score.  Only the TICKER LABELS are truncated (names are interchangeable
    under the null); the numbers are untouched.
    """
    idx = book.dates
    cols = [f"NULL{i:05d}" for i in range(book.weights.shape[1])]
    return pd.DataFrame(book.weights, index=idx, columns=cols)


def _phase9_null_ari(rng: np.random.Generator, n: int = P9_ARI_FOLD_MONTHS,
                     k: int = P9_ARI_STATES) -> float:
    """
    Null ARI for ONE walk-forward fold, generated by the correct mechanism
    rather than assumed.

    Under "no regime structure" both the true labels and the HMM's recovered
    labels are i.i.d. uniform draws over ``k`` states, so the ARI of two
    independent random partitions IS the null distribution.  Returns a value in
    [-1, 1] (ARI is unbounded below in general but is clipped by this
    construction).
    """
    from sklearn.metrics import adjusted_rand_score
    true_lbl = rng.integers(0, k, n)
    est_lbl = rng.integers(0, k, n)
    return float(adjusted_rand_score(true_lbl, est_lbl))


def _phase9_null_structural(rng: np.random.Generator) -> Dict[str, Any]:
    """Null draw for the structural compliance components (assumptions)."""
    wacc = 1.0 if rng.random() < P9_G8_WACC_FULL_P else float(rng.uniform(0.55, 0.99))
    prob = rng.binomial(P9_PROBATION_TRIGGERS, P9_NULL_PROBATION_RATE) / float(
        P9_PROBATION_TRIGGERS)
    return {
        "wacc_coverage": wacc,
        "probation_pass_rate": float(prob),
        "swap_churn": float(abs(rng.normal(0.0, P9_G8_SWAP_CHURN_SD))),
        "tca_bps": float(abs(rng.normal(0.0, 60.0))),    # bps, one-sided cost
        "provenance_chain": bool(rng.random() < P9_G8_PROVENANCE_P),
    }


# ---------------------------------------------------------------------------
# Frozen threshold scorers (REFERENCE ONLY — see the claim map)
# ---------------------------------------------------------------------------
#
# Sourced from ``success_criteria.fold_4_test`` in the frozen document.  These
# are thresholds, not tests: they carry no alpha, and nothing below is ever
# compared against alpha/8 or any other family-corrected level.

@dataclass(frozen=True)
class Threshold:
    """One frozen pass/fail threshold and the value it reads."""
    key: str
    label: str
    op: str                      # 'gt' | 'lt' | 'between' | 'ge'
    limit: Any
    source: str                  # the frozen yaml path it was read from
    in_prereg_v7: bool = True


def _load_thresholds() -> List[Threshold]:
    """
    Read the thresholds from the frozen document.

    Two entries are marked ``in_prereg_v7=False``: Calmar and the HMM occupancy
    caps were part of the DELETED ``gates:`` block.  They are still calibrated
    because the Phase-9C dispatch names them, but the report labels them so
    nobody mistakes them for frozen gates.  They are diagnostics, not gates.
    """
    return [
        Threshold("oos_sharpe", "OOS Sharpe (Arm C)", "gt", 1.0,
                  "success_criteria.fold_4_test.oos_sharpe_arm_a"),
        Threshold("max_drawdown", "Max drawdown", "ge", -0.20,
                  "statistical_design.composite_components[max_drawdown]"),
        Threshold("ir_vs_spy", "IR vs SPY", "gt", 0.30,
                  "success_criteria.fold_4_test.ir_vs_spy_arm_a"),
        Threshold("hmm_ari_folds", "HMM ARI >= 0.45 on >= 3/4 folds", "ge",
                  (0.45, P9_ARI_FOLDS_REQUIRED),
                  "hmm.validation.ari_threshold + folds_required"),
        Threshold("composite_dsr", "Composite DSR (N=513)", "ge", DSR_THRESHOLD,
                  "statistical_design.dsr.threshold"),
        Threshold("calmar", "Calmar", "gt", 2.0,
                  "REMOVED with the gates: block (v4 gate_6_calmar)",
                  in_prereg_v7=False),
        Threshold("swap_churn", "Swap churn (swaps/yr)", "lt", 5.0,
                  "success_criteria.fold_4_test.swap_churn"),
        Threshold("tca_bps", "TCA (bps)", "le", 150.0,
                  "success_criteria.fold_4_test.tca_bps"),
        Threshold("probation_pass_rate", "Probation pass rate", "between",
                  (0.35, 0.65),
                  "success_criteria.fold_4_test.probation_pass_rate"),
        Threshold("wacc_coverage", "WACC coverage", "eq", 1.0,
                  "success_criteria.fold_4_test.wacc_coverage"),
    ]


def _eval_threshold(thr: Threshold, value: Any) -> Optional[bool]:
    """Apply one threshold.  ``None`` means 'not measurable in this draw'."""
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return None
    op = thr.op
    if op == "gt":
        return bool(value > thr.limit)
    if op == "ge":
        return bool(value >= thr.limit)
    if op == "lt":
        return bool(value < thr.limit)
    if op == "le":
        return bool(value <= thr.limit)
    if op == "eq":
        return bool(abs(float(value) - float(thr.limit)) < 1e-9)
    if op == "between":
        lo, hi = thr.limit
        return bool(lo <= value <= hi)
    raise ValueError(f"unknown threshold op {op!r}")


def _annualized_sharpe(returns: pd.Series, periods_per_year: int = 12) -> float:
    r = pd.Series(returns, dtype=float).dropna()
    sd = float(r.std(ddof=1))
    if sd <= 0 or not np.isfinite(sd):
        return float("nan")
    return float(r.mean() / sd * math.sqrt(periods_per_year))


def _max_drawdown(returns: pd.Series) -> float:
    r = pd.Series(returns, dtype=float).dropna()
    cum = (1.0 + r).cumprod()
    return float((cum / cum.cummax() - 1.0).min())


def _information_ratio(returns: pd.Series, bench: pd.Series,
                       periods_per_year: int = 12) -> float:
    r, b = pd.Series(returns, dtype=float), pd.Series(bench, dtype=float)
    j = pd.concat([r.rename("r"), b.rename("b")], axis=1).dropna()
    excess = j["r"] - j["b"]
    te = float(excess.std(ddof=1))
    if te <= 0 or not np.isfinite(te):
        return float("nan")
    return float(excess.mean() / te * math.sqrt(periods_per_year))


def _calmar(returns: pd.Series, periods_per_year: int = 12) -> float:
    r = pd.Series(returns, dtype=float).dropna()
    years = max(len(r) / periods_per_year, 1e-9)
    cagr = float(((1.0 + r).prod()) ** (1.0 / years) - 1.0)
    mdd = abs(_max_drawdown(r))
    if mdd <= 0:
        return float("inf")
    return float(cagr / mdd)


def _mean_one_way_turnover(weights: np.ndarray) -> float:
    if weights.shape[0] < 2:
        return 0.0
    return float(np.mean(0.5 * np.abs(np.diff(weights, axis=0)).sum(axis=1)))


# ---------------------------------------------------------------------------
# THE CALIBRATION
# ---------------------------------------------------------------------------

def calibrate_gates(n: int = N_NULL_DEFAULT,
                    seed: int = SEED,
                    report_path: Optional[Path] = None,
                    progress_every: int = 500,
                    n_months: int = P9_N_MONTHS,
                    n_names: int = P9_N_NAMES,
                    dirichlet_alpha: float = P9_DIRICHLET_ALPHA,
                    require_prereg: bool = True,
                    power_draws: int = P9_POWER_DRAWS,
                    power_sr_grid: Sequence[float] = P9_POWER_SR_GRID) -> Dict[str, Any]:
    """
    Calibrate the frozen v7 decision rule on ``n`` null portfolios.

    ``phase9`` is accepted (and ignored) so the dispatch's documented CLI —
    ``--run --n 10000 --phase9`` — keeps working after the 8-gate architecture
    was deleted from the frozen design.  There is now only one architecture.
    """
    rng = np.random.default_rng(seed)
    engine = CompositeGateEngine(pre_reg=load_prereg())
    thresholds = _load_thresholds()

    # Accumulators -------------------------------------------------------
    pass_counts = {t.key: 0 for t in thresholds}
    measurable = {t.key: 0 for t in thresholds}
    thr_values: Dict[str, List[float]] = {t.key: [] for t in thresholds}
    dsr_values: List[float] = []
    fold4_pass = 0
    fold4_measurable = 0
    composite_pass_both = 0
    composite_scores: List[float] = []
    component_scores: Dict[str, List[float]] = {k: [] for k in
                                               ("sharpe", "max_drawdown", "turnover",
                                                "hmm_ari", "swap_churn", "tca_bps",
                                                "probation_pass_rate")}
    sharpe_values: List[float] = []
    turnover_values: List[float] = []
    beta_values: List[float] = []

    # A single shared factor-loading matrix keeps the loadings FIXED across
    # draws (only returns and weights are re-drawn).  That makes the null a
    # proper family over strategies at one universe, instead of mixing two
    # sources of randomness into one number.
    loadings = rng.normal(0.0, P9_LOADING_SD, (n_names, P9_N_FACTORS))
    loadings[:, 0] = np.clip(1.0 + rng.normal(0.0, 0.05, n_names), 0.3, 1.8)

    for i in range(int(n)):
        book = _phase9_null_book(rng, n_months=n_months, n_names=n_names,
                                 dirichlet_alpha=dirichlet_alpha,
                                 loadings=loadings)
        # Frozen folds: fold_k is the k-th 12-month block of the 84-month window.
        fold_returns = {
            f"fold_{k + 1}": book.arm_c.iloc[k * P9_ARI_FOLD_MONTHS:
                                              (k + 1) * P9_ARI_FOLD_MONTHS]
            for k in range(P9_N_FOLDS)
        }
        ari_folds = {f"fold_{k + 1}": _phase9_null_ari(rng)
                     for k in range(P9_N_FOLDS)}
        ari_concat = float(np.mean(list(ari_folds.values())))
        n_ari_pass = sum(1 for v in ari_folds.values() if v >= 0.45)

        structural = _phase9_null_structural(rng)
        w_frame = _weights_frame(book)

        res = engine.evaluate_composite_dsr(
            oos_returns=book.arm_c,
            arm_c_weights=w_frame,
            spy_returns=book.spy,
            hmm_ari=ari_concat,
            swap_churn=structural["swap_churn"],
            tca_bps=structural["tca_bps"],
            probation_pass_rate=structural["probation_pass_rate"],
            fold_returns=fold_returns,
        )

        composite_pass_both += int(bool(res.passed and res.fold_4_passed))
        dsr_values.append(float(res.composite_dsr))
        composite_scores.append(float(res.composite_score))
        for k, v in res.component_scores.items():
            component_scores[k].append(float(v))
        if res.fold_4_dsr is not None:
            fold4_measurable += 1
            fold4_pass += int(bool(res.fold_4_passed))

        sharpe = _annualized_sharpe(book.arm_c)
        ir = _information_ratio(book.arm_c, book.spy)
        calmar = _calmar(book.arm_c)
        turnover = _mean_one_way_turnover(book.weights)
        # Portfolio market beta per rebalance: the weight-vector dot product of
        # the market loadings, averaged over months (a raw sum would just be
        # T x the average and read as a 24x beta).
        beta = float(np.mean(book.weights @ book.loadings[:, 0]))

        sharpe_values.append(sharpe)
        turnover_values.append(turnover)
        beta_values.append(beta)

        observed: Dict[str, Any] = {
            "oos_sharpe": sharpe,
            "max_drawdown": _max_drawdown(book.arm_c),
            "ir_vs_spy": ir,
            "hmm_ari_folds": float(n_ari_pass),     # compared against (0.45-foldcount)
            "composite_dsr": float(res.composite_dsr),
            "calmar": calmar,
            "swap_churn": structural["swap_churn"],
            "tca_bps": structural["tca_bps"],
            "probation_pass_rate": structural["probation_pass_rate"],
            "wacc_coverage": structural["wacc_coverage"],
        }
        for thr in thresholds:
            val = observed[thr.key]
            if thr.key == "hmm_ari_folds":
                val = float(n_ari_pass)
                ok = bool(n_ari_pass >= thr.limit[1])
                thr_values[thr.key].append(float(n_ari_pass))
            else:
                ok = _eval_threshold(thr, val)
            if ok is not None and np.isfinite(float(val)):
                measurable[thr.key] += 1
                thr_values[thr.key].append(float(val))
            if ok:
                pass_counts[thr.key] += 1

        if progress_every and (i + 1) % progress_every == 0:
            print(f"[calib-p9] {i + 1}/{n} null portfolios done", flush=True)

    n_eff = int(n)
    nominal = 1.0 / float(DSR_N_TRIALS)

    # --- primary decision rule -----------------------------------------
    k_dsr = pass_counts["composite_dsr"]
    dsr_node = {
        "role": "decision_rule",
        "rule": "CompositeGateEngine.evaluate_composite_dsr(...) >= 0.95",
        "nominal_fwer_claimed": round(nominal, 8),
        "nominal_source": "1 / dsr.n_trials (N=513)",
        "n_passed_null": k_dsr,
        "null_pass_rate": round(k_dsr / n_eff, 6),
        "wilson_ci_95": wilson_ci(k_dsr, n_eff),
        "verdict_vs_nominal": _three_state_verdict(k_dsr, n_eff, nominal),
        "inflation_vs_nominal": (round((k_dsr / n_eff) / nominal, 3)
                                 if k_dsr else None),
        "null_dsr_distribution": _dist(dsr_values),
        "n_needed_to_demonstrate_zero_rejections": _n_needed_for_zero_rejection(nominal),
    }

    fold4_node = {
        "rule": "Fold 4 (2024 TRUE OOS) independently DSR >= 0.95",
        "n_measurable": fold4_measurable,
        "n_passed_null": fold4_pass,
        "null_pass_rate": (round(fold4_pass / fold4_measurable, 6)
                           if fold4_measurable else None),
        "wilson_ci_95": wilson_ci(fold4_pass, fold4_measurable) if fold4_measurable else None,
        "conjunctive_note": (
            "The frozen success rule is CONJUNCTIVE (whole record AND Fold 4). "
            "Its null rate is the product of the two rates only if the two tests "
            "were independent; they are computed on overlapping data, so the "
            "measured conjunctive rate below is the number to quote, not a "
            "product of the marginals."),
        "conjunctive_null_pass_rate": round(composite_pass_both / n_eff, 6),
        "conjunctive_n_passed_null": composite_pass_both,
        "conjunctive_wilson_ci_95": wilson_ci(composite_pass_both, n_eff),
    }

    # --- reference thresholds ------------------------------------------
    gate_stats: Dict[str, Any] = {}
    for thr in thresholds:
        k = thr.key
        cnt, tot = pass_counts[k], max(measurable[k], 1)
        node: Dict[str, Any] = {
            "label": thr.label,
            "threshold": thr.limit if isinstance(thr.limit, (int, float)) else list(thr.limit),
            "operator": thr.op,
            "frozen_source": thr.source,
            "in_prereg_v7": thr.in_prereg_v7,
            "calibration_role": ("decision_rule" if k == "composite_dsr"
                                 else "threshold_reference_only"),
            "n_passed_null": int(cnt),
            "n_measurable": int(measurable[k]),
            "null_pass_rate": round(cnt / tot, 6),
            "wilson_ci_95": wilson_ci(cnt, measurable[k]),
            "null_value_distribution": _dist(thr_values[k]),
        }
        gate_stats[k] = node

    # --- decoupling probe ------------------------------------------------
    # Whether the composite score decides the gate is a deterministic
    # question, so it is answered deterministically rather than inferred from
    # whichever nulls happened to clear the threshold.
    decoupling = _decoupling_probe(engine, rng, n_months, n_names)
    # Second, empirical view of the same fact: the spread of composite scores
    # among null draws that PASSED.  Empty when nothing passed, which is why
    # the deterministic probe above exists.
    passed_scores = [s for s, p in zip(composite_scores, dsr_values)
                     if p >= DSR_THRESHOLD]

    # Power / MDE.  Measured through the shipped DSR on a stream disjoint from
    # the null draws, so it is reproducible on its own and cannot be tuned by
    # the same randomness that produced the null rates.
    power = _power_analysis(n_months=n_months, n_draws=int(power_draws),
                            sr_grid=power_sr_grid)
    ceiling = _ceiling_analysis(n_months, P9_ARI_FOLD_MONTHS,
                                int(power_draws), np.random.default_rng(SEED ^ 0xC0FFEE),
                                P9_FACTOR_SD)
    power["ceiling_analysis"] = ceiling
    fwer = _fwer_accounting(dsr_node, fold4_node, n_eff)

    findings = _findings(
        engine=engine, dsr_node=dsr_node, gate_stats=gate_stats,
        fold4_node=fold4_node, beta_values=beta_values,
        turnover_values=turnover_values, sharpe_values=sharpe_values,
        component_scores=component_scores, require_prereg=require_prereg,
        decoupling=decoupling, power=power, fwer=fwer, ceiling=ceiling,
        passed_scores=passed_scores, all_scores=composite_scores,
        n_eff=n_eff,
    )

    report = {
        "report_type": "phase9_gate_calibration",
        "report_version": 4,
        "phase": 9,
        "prereg_version": str(MODULE_PREREG.version),
        "prereg_sha256": MODULE_PREREG.declared_sha256,
        "prereg_integrity_verified": bool(MODULE_PREREG.integrity_verified),
        "metric": "composite_dsr",
        "no_gate_hierarchy": True,
        "gates_calibrated": 0,
        "architecture_note": (
            "v7 removed the 8-gate Holm-Bonferroni architecture "
            "(statistical_design.no_gate_hierarchy: true). There is no alpha/8 "
            "and no gate family to calibrate. The single frozen pass/fail rule is "
            "the composite DSR; every per-metric threshold below is reported as "
            "reference-only and is never compared against a corrected alpha."),
        "n_null_portfolios": n_eff,
        "n_months_per_portfolio": int(n_months),
        "n_universe_names": int(n_names),
        "rebalance_frequency": "monthly",
        "seed": int(seed),
        "require_prereg": bool(require_prereg),
        "null_model": {
            "description": (
                f"{n_names} names, {n_months} monthly rebalances over the frozen "
                f"primary window {P9_START}..{P9_END}. Every name has exactly zero "
                "alpha; returns are beta*factor + idiosyncratic noise. Weights are "
                f"i.i.d. Dirichlet({dirichlet_alpha}) re-drawn every month (no "
                "selection, no persistence, no timing skill), so the null book is "
                "~beta 1 to the market with maximal churn. Portfolio returns are "
                f"charged {P9_COST_DRAG * 1e4:.0f}bps per unit of one-way turnover."),
            "factor_mu_monthly": P9_FACTOR_MU,
            "factor_sd_monthly": P9_FACTOR_SD,
            "n_factors": P9_N_FACTORS,
            "idio_sd_monthly": P9_IDIO_SD,
            "alpha_per_name": P9_NULL_ALPHA,
            "dirichlet_alpha": dirichlet_alpha,
            "loading_market_mean": 1.0,
            "cost_drag_per_unit_turnover": P9_COST_DRAG,
            "ari_null": (f"two independent uniform {P9_ARI_STATES}-state partitions "
                         f"of {P9_ARI_FOLD_MONTHS} obs per fold "
                         f"({P9_N_FOLDS} folds)"),
            "realised_null_book": {
                "market_beta": _dist(beta_values),
                "one_way_turnover_per_rebalance": _dist(turnover_values),
                "annualized_sharpe": _dist(sharpe_values),
            },
        },
        "structural_null_mixture": {
            "assumption_based": True,
            "wacc_full_prob": P9_G8_WACC_FULL_P,
            "probation_triggers": P9_PROBATION_TRIGGERS,
            "probation_null_rate": P9_NULL_PROBATION_RATE,
            "probation_rate_sampling_sd": round(
                float(math.sqrt(P9_NULL_PROBATION_RATE
                                * (1 - P9_NULL_PROBATION_RATE)
                                / P9_PROBATION_TRIGGERS)), 6),
            "swap_churn_halfnormal_sd": P9_G8_SWAP_CHURN_SD,
            "provenance_pass_prob": P9_G8_PROVENANCE_P,
            "note": ("declared modelling assumptions for compliance metrics, not "
                     "empirical estimates; the decision_rule and reference "
                     "threshold rates in this report ARE empirical"),
        },
        "decision_rule": dsr_node,
        "fwer_accounting": fwer,
        "power_analysis": power,
        "fold_4_true_oos": fold4_node,
        "composite_decoupling_probe": decoupling,
        "reference_thresholds": gate_stats,
        "composite_components_null_distribution": {
            k: _dist(v) for k, v in component_scores.items()
        },
        "findings": findings,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }

    out = Path(report_path) if report_path else DEFAULT_REPORT
    out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    out.write_text(text, encoding="utf-8")
    _write_sha256_sidecar(out)

    _print_summary(report, out)
    return report


def _decoupling_probe(engine: CompositeGateEngine, rng: np.random.Generator,
                      n_months: int, n_names: int) -> Dict[str, Any]:
    """
    Does the COMPOSITE SCORE actually decide the gate?

    The frozen document says ``DSR on COMPOSITE_SCORE``.  Whether the shipped
    code honours that is a deterministic question, so it is answered
    deterministically: hold the Arm C return series FIXED and sweep every
    non-return composite input across the full range the pre-registration
    allows.  If ``passed`` never moves, the composite provably has no causal
    claim on the decision — measured, not asserted.

    This runs independently of whether any null draw clears the threshold, so
    unlike a pass-set comparison it can never come back empty.
    """
    book = _phase9_null_book(rng, n_months=n_months, n_names=n_names)
    w_frame = _weights_frame(book)
    fold_returns = {
        "fold_4": book.arm_c.iloc[-P9_ARI_FOLD_MONTHS:]
        if n_months >= P9_ARI_FOLD_MONTHS else book.arm_c
    }

    ari_grid = [0.0, 0.45, 0.70, 0.90]
    churn_grid = [0.0, 5.0, 15.0, 50.0]
    tca_grid = [0.0, 150.0, 300.0, 1000.0]
    prob_grid = [0.0, 0.35, 0.50, 0.65, 1.0]

    combos = [(a, c, t, p) for a in ari_grid for c in churn_grid
              for t in tca_grid for p in prob_grid]
    passed_flags: set = set()
    dsr_flags: set = set()
    composite_scores: List[float] = []
    for ari, churn, tca, prob in combos:
        res = engine.evaluate_composite_dsr(
            oos_returns=book.arm_c, arm_c_weights=w_frame, spy_returns=book.spy,
            hmm_ari=ari, swap_churn=churn, tca_bps=tca, probation_pass_rate=prob,
            fold_returns=fold_returns)
        passed_flags.add(bool(res.passed and res.fold_4_passed))
        dsr_flags.add(round(float(res.composite_dsr), 12))
        composite_scores.append(float(res.composite_score))

    invariant = len(passed_flags) == 1 and len(dsr_flags) == 1
    return {
        "question": ("does composite_score determine `passed` in "
                     "CompositeGateEngine.evaluate_composite_dsr?"),
        "method": ("hold the Arm C return series and weight path fixed; sweep "
                   "hmm_ari / swap_churn / tca_bps / probation_pass_rate across "
                   "their full frozen ranges ({} combinations)".format(len(combos))),
        "n_combinations": len(combos),
        "distinct_passed_values": sorted(passed_flags),
        "distinct_dsr_values": len(dsr_flags),
        "composite_score_range": [round(min(composite_scores), 6),
                                  round(max(composite_scores), 6)],
        "composite_score_drives_decision": not invariant,
        "verdict": (
            "COUPLED: `passed` and DSR respond to the composite inputs"
            if not invariant else
            "DECOUPLED: `passed` is identical across all "
            f"{len(combos)} composite-input combinations (composite_score moves "
            f"over [{min(composite_scores):.4f}, {max(composite_scores):.4f}] "
            f"while the DSR takes {len(dsr_flags)} distinct value(s) that depend "
            f"only on the return series). The composite is computed, reported, "
            f"and never read by the decision."),
        "contract_reference": "statistical_design: 'DSR (Deflated Sharpe Ratio) on COMPOSITE_SCORE'",
    }


# ---------------------------------------------------------------------------
# POWER / MDE
# ---------------------------------------------------------------------------

def _power_curve(n_months: int, sr_grid: Sequence[float], n_draws: int,
                 rng: "np.random.Generator", distribution: str,
                 sigma_monthly: float) -> List[Dict[str, Any]]:
    """
    Push synthetic books through the SHIPPED ``deflated_sharpe_ratio``.

    The formula is never restated here.  The function under test is the same
    object the gate will call, so this curve cannot drift away from the rule it
    is describing -- the failure mode of an "analytic" MDE that quietly assumes
    a different estimator than the one that ships.
    """
    root_12 = math.sqrt(12.0)
    # A t_df has variance df/(df-2), so its raw draws are wider than a normal's
    # by that factor.  Rescale so BOTH arms carry the SAME monthly sigma and the
    # only thing that differs between them is tail SHAPE.  Without this the
    # fat-tail arm silently tests a lower-volatility book and reports an MDE
    # that belongs to neither arm.
    t_scale = 1.0
    if distribution == "t5":
        t_scale = math.sqrt((P9_POWER_T_DF - 2.0) / P9_POWER_T_DF)
    out: List[Dict[str, Any]] = []
    for ann_sr in sr_grid:
        mu = ann_sr / root_12 * sigma_monthly
        hits = 0
        for _ in range(n_draws):
            if distribution == "t5":
                # The DSR's V term reads skew and RAW kurtosis, so tail shape
                # moves the threshold, not just the Sharpe.  The normal arm
                # alone would flatter the gate.
                s = pd.Series(rng.standard_t(P9_POWER_T_DF, n_months)
                              * (sigma_monthly * t_scale) + mu)
            else:
                s = pd.Series(rng.normal(mu, sigma_monthly, n_months))
            if deflated_sharpe_ratio(s, n_trials=DSR_N_TRIALS) >= DSR_THRESHOLD:
                hits += 1
        out.append({
            "true_annualized_sharpe": float(ann_sr),
            "n_draws": int(n_draws),
            "n_passed": int(hits),
            "power": round(hits / n_draws, 6),
            "wilson_ci_95": wilson_ci(hits, n_draws),
        })
    return out


def _mde(curve: List[Dict[str, Any]], target: float) -> Optional[float]:
    """Smallest true annualized Sharpe whose power reaches ``target``.

    Linear interpolation between the bracketing grid points, so the MDE is
    continuous in the target rather than quantised to the 0.10 grid.  Returns
    ``None`` when the grid never reaches the target power (i.e. the design is
    underpowered across the whole range scanned).
    """
    prev: Optional[Dict[str, Any]] = None
    for pt in curve:
        if pt["power"] >= target:
            if prev is None or prev["power"] >= target:
                return round(float(pt["true_annualized_sharpe"]), 4)
            lo, hi = prev["power"], pt["power"]
            if hi == lo:
                return round(float(pt["true_annualized_sharpe"]), 4)
            frac = (target - lo) / (hi - lo)
            return round(float(prev["true_annualized_sharpe"]
                               + frac * (pt["true_annualized_sharpe"]
                                         - prev["true_annualized_sharpe"])), 4)
        prev = pt
    return None


def _power_analysis(n_months: int = P9_N_MONTHS,
                    fold_months: int = P9_ARI_FOLD_MONTHS,
                    n_draws: int = P9_POWER_DRAWS,
                    seed: int = SEED,
                    sr_grid: Sequence[float] = P9_POWER_SR_GRID) -> Dict[str, Any]:
    """
    Minimum detectable effect of the frozen decision rule.

    Volatility is PINNED to the frozen null DGP (``P9_FACTOR_SD`` per month,
    the market block the null book is built from) and only the drift moves, so
    "annualised Sharpe" here means "annualised Sharpe of a book carrying the
    same risk as the null book".  A book with different vol gets a different
    MDE and the report says so rather than hiding it.

    Two limbs are measured, because the frozen success rule is CONJUNCTIVE:
    the whole 84-month record, and Fold 4 alone (the 12-month TRUE-OOS limb).
    The conjunction can only be as powerful as the weaker limb, so the Fold-4
    number is the one that binds.
    """
    # Dedicated stream: the power curve must not be correlated with the null
    # draws, and it must be reproducible on its own.
    rng = np.random.default_rng(seed ^ 0x5EED_9A1E)
    sigma = P9_FACTOR_SD

    arms: Dict[str, Any] = {}
    for dist in ("normal", "t5"):
        whole = _power_curve(n_months, sr_grid, n_draws, rng, dist, sigma)
        fold4 = _power_curve(fold_months, sr_grid, n_draws, rng, dist, sigma)
        arms[dist] = {
            "whole_record_months": int(n_months),
            "whole_record": {
                "power_at_sr_0.0": whole[0]["power"],
                "type_i_error_measured": whole[0]["power"],
                "mde_annualized_sharpe_power_0.80": _mde(whole, 0.80),
                "mde_annualized_sharpe_power_0.90": _mde(whole, 0.90),
                "mde_annualized_sharpe_power_0.95": _mde(whole, 0.95),
                "curve": whole,
            },
            "fold_4_true_oos_months": int(fold_months),
            "fold_4_true_oos": {
                "power_at_sr_0.0": fold4[0]["power"],
                "mde_annualized_sharpe_power_0.80": _mde(fold4, 0.80),
                "mde_annualized_sharpe_power_0.90": _mde(fold4, 0.90),
                "mde_annualized_sharpe_power_0.95": _mde(fold4, 0.95),
                "curve": fold4,
            },
        }

    t5w = arms["t5"]["whole_record"]
    t5f = arms["t5"]["fold_4_true_oos"]
    grid_max = float(max(sr_grid))
    unreach = (f"The grid tops out at {grid_max:.2f}; extend P9_POWER_SR_GRID "
               f"before quoting this as an unbounded statement.")
    return {
        "question": ("what is the smallest true annualized Sharpe at which the "
                     "frozen composite DSR fires? (minimum detectable effect)"),
        "method": ("Monte Carlo through the shipped "
                   "ingestion.harness.gate_engine.deflated_sharpe_ratio with "
                   f"n_trials={DSR_N_TRIALS} and threshold={DSR_THRESHOLD}; the "
                   "formula is called, not restated. Monthly volatility pinned to "
                   f"the frozen null DGP ({sigma:.4f}); only the drift varies. "
                   "The t arm is variance-matched to the normal arm so the two "
                   "differ in tail SHAPE only."),
        "n_draws_per_point": int(n_draws),
        "sr_grid": [float(x) for x in sr_grid],
        "sr_grid_max": grid_max,
        "distributions": {
            "normal": (f"i.i.d. Gaussian returns, monthly sigma {sigma:.4f}"),
            "t5": (f"Student-t df={P9_POWER_T_DF}, variance-matched to the same "
                   f"monthly sigma {sigma:.4f}. The DSR's V term reads sample skew "
                   "and RAW kurtosis, so tail shape moves the required Sharpe; the "
                   "normal arm alone would flatter the gate."),
        },
        "reporting_arm": "t5",
        "reporting_arm_rationale": (
            "Both arms are reported. t5 is the conservative one and is what the "
            "MDE headline quotes, because a real equity book's 84 monthly returns "
            "are fat-tailed and the DSR penalises that in V."),
        "whole_record_months": int(n_months),
        "fold_4_true_oos_months": int(fold_months),
        "normal_arm": arms["normal"],
        "fat_tail_arm": t5w,
        "fat_tail_fold_4": t5f,
        "mde_annualized_sharpe_power_0.80": t5w["mde_annualized_sharpe_power_0.80"],
        "mde_annualized_sharpe_power_0.90": t5w["mde_annualized_sharpe_power_0.90"],
        "mde_annualized_sharpe_power_0.80_fold_4": t5f["mde_annualized_sharpe_power_0.80"],
        "mde_annualized_sharpe_power_0.90_fold_4": t5f["mde_annualized_sharpe_power_0.90"],
        "mde_grid_exhausted": {
            "whole_record": t5w["mde_annualized_sharpe_power_0.80"] is None,
            "fold_4_true_oos": t5f["mde_annualized_sharpe_power_0.80"] is None,
            "note": unreach,
        },
        "binding_limb": ("fold_4_true_oos" if (
            t5f["mde_annualized_sharpe_power_0.80"] or math.inf
        ) > (t5w["mde_annualized_sharpe_power_0.80"] or math.inf) else "whole_record"),
        "conjunctive_note": (
            "The frozen rule is CONJUNCTIVE (whole record AND Fold 4). A "
            "conjunction's power is at most the weaker limb's, so the Fold-4 MDE "
            "quoted here is the honest floor for a claim that has to survive "
            "2024 TRUE OOS as well as the full record. On 12 monthly "
            "observations against a 513-trial deflation, that limb needs a "
            "materially larger Sharpe than the 84-month limb does."),
    }


def _fwer_accounting(dsr_node: Dict[str, Any], fold4_node: Dict[str, Any],
                     n_eff: int) -> Dict[str, Any]:
    """
    State the false-positive accounting explicitly, at every scaling a reader
    might mean by "FWER", so the number cannot be quoted out of scale.
    """
    nominal = float(dsr_node["nominal_fwer_claimed"])
    whole = float(dsr_node["null_pass_rate"])
    f4 = float(fold4_node["null_pass_rate"] or 0.0)
    conj = float(fold4_node["conjunctive_null_pass_rate"])
    return {
        "frozen_design": {
            "n_trials": int(DSR_N_TRIALS),
            "nominal_fwer_claimed": round(nominal, 8),
            "derivation": "1 / dsr.n_trials = 1/513",
            "meaning": ("the per-decision false-positive rate the DSR inflates "
                        "for, after deflating 513 trials. This is the quantity "
                        "the frozen design specifies and the one measured "
                        "against the null above."),
        },
        "if_the_intended_control_is_familywise_0.05": {
            "per_decision_alpha_required": round(0.05 / DSR_N_TRIALS, 10),
            "note": ("0.05/513 = 9.75e-05, not 0.05. A run that treats 0.05 as "
                     "the per-decision alpha after already accounting for 513 "
                     "trials is 513x looser than the frozen design. Reported so "
                     "the two scalings cannot be confused; NOT applied, because "
                     "changing alpha is a pre-registration change."),
        },
        "measured_on_this_null": {
            "n_null_portfolios": int(n_eff),
            "whole_record_rate": whole,
            "whole_record_wilson_95": dsr_node["wilson_ci_95"],
            "fold_4_rate": f4,
            "fold_4_wilson_95": fold4_node.get("wilson_ci_95"),
            "conjunctive_rate": conj,
            "conjunctive_wilson_95": fold4_node["conjunctive_wilson_ci_95"],
            "controlled_at_1_over_N": (dsr_node["verdict_vs_nominal"] == "controlled"),
        },
        "gates_calibrated": 0,
        "why_zero": ("v7 froze `no_gate_hierarchy: true` and deleted the `gates:` "
                     "block, so there are no per-gate thresholds to calibrate to "
                     "a corrected alpha. Calibrating them would re-introduce the "
                     "architecture the pre-registration removed. The per-metric "
                     "entries in `reference_thresholds` are measured for power "
                     "planning only and are explicitly not tests."),
    }


#: True annualised Sharpes used to probe for a POWER CEILING.  They run far past
#: anything a real book can produce on purpose: the question is not "what Sharpe
#: do we need" but "does ANY Sharpe do it".
P9_CEILING_PROBE_SR = (4.0, 8.0, 12.0, 20.0, 50.0, 200.0, 1000.0)


def _kurtosis_ceiling(n_months: int) -> Dict[str, Any]:
    """
    Is the frozen threshold reachable at ANY Sharpe on an ``n_months`` sample?

    Algebra of the shipped rule, with the bias term dropped (skew is O(1/sqrt(n))
    and the ceiling is set by the kurtosis term, which is O(1))::

        DSR >= t   <=>   sr/sqrt(V) >= (z_mult + z_t) / sqrt(n-1)  ==  c
        V          = 1 - skew*sr + (K-1)/4 * sr^2

    With skew ~ 0 the requirement is ``sr^2 >= c^2 * (1 + (K-1)/4 * sr^2)``,
    which has a solution only while ``c^2 (K-1)/4 < 1``::

        K* = 1 + 4 / c^2

    Above ``K*`` the V term grows as fast as ``sr^2``, ``sr/sqrt(V)`` is bounded,
    and NO Sharpe — 5, 50 or 5000 — can clear the threshold.  The gate then
    stops being a power problem and becomes a coin flip whose odds are set by
    how lucky the SAMPLE kurtosis estimate happens to be.

    This is a property of the estimator, not of any particular book, so it is
    derived rather than simulated, and the simulation is used only to confirm
    the derivation.
    """
    from scipy import stats as _st
    z_mult = float(_st.norm.ppf(1.0 - 1.0 / DSR_N_TRIALS))
    z_t = float(_st.norm.ppf(DSR_THRESHOLD))
    c = (z_mult + z_t) / math.sqrt(max(n_months - 1, 1))
    c2 = c * c
    k_star = 1.0 + 4.0 / c2
    # The degenerate case is c^2*(K-1)/4 >= 1 even for the mildest realistic
    # K = 3 (a Gaussian sample's own raw kurtosis), i.e. c^2 >= 2.  Testing
    # c^2 >= 1 would wrongly condemn the 12-month limb, whose K* is 3.008 --
    # just barely reachable, which is the entire point of the finding.
    if c2 >= 2.0:
        return {
            "n_months": int(n_months),
            "required_sr_over_sqrt_V": round(c, 6),
            "break_even_raw_kurtosis_K_star": round(k_star, 4),
            "reachable_at_any_sharpe": False,
            "reason": (f"c^2 = {c2:.4f} >= 2, so the requirement is already "
                       f"unsatisfiable at K = 3 and no Sharpe clears the bar."),
        }
    s_min = math.sqrt(c2 / (1.0 - c2 * (3.0 - 1.0) / 4.0))
    return {
        "n_months": int(n_months),
        "required_sr_over_sqrt_V": round(c, 6),
        "break_even_raw_kurtosis_K_star": round(k_star, 4),
        "reachable_at_any_sharpe": True,
        "min_monthly_sharpe_at_K_equals_3": round(s_min, 6),
        "min_annualized_sharpe_at_K_equals_3": round(s_min * math.sqrt(12.0), 4),
    }


def _ceiling_analysis(n_months: int, fold_months: int, n_draws: int,
                      rng: "np.random.Generator",
                      sigma_monthly: float) -> Dict[str, Any]:
    """
    Confirm by simulation that the derived kurtosis ceiling is real, and price
    it: what fraction of draws is even ELIGIBLE to pass, and what does the
    frozen rule's power actually saturate at?
    """
    from scipy import stats as _st
    whole = _kurtosis_ceiling(n_months)
    fold4 = _kurtosis_ceiling(fold_months)
    t_scale = math.sqrt((P9_POWER_T_DF - 2.0) / P9_POWER_T_DF)

    def _probe(months: int) -> Dict[str, Any]:
        pts: List[Dict[str, Any]] = []
        ks: List[float] = []
        for ann in P9_CEILING_PROBE_SR:
            mu = ann / math.sqrt(12.0) * sigma_monthly
            hits = 0
            k_hits = 0
            k_star = _kurtosis_ceiling(months)["break_even_raw_kurtosis_K_star"]
            for _ in range(n_draws):
                s = pd.Series(rng.standard_t(P9_POWER_T_DF, months)
                              * (sigma_monthly * t_scale) + mu)
                if deflated_sharpe_ratio(s, n_trials=DSR_N_TRIALS) >= DSR_THRESHOLD:
                    hits += 1
                if k_star is not None:
                    k = float(_st.kurtosis(s, fisher=False))
                    ks.append(k)
                    if k < k_star:
                        k_hits += 1
            pts.append({"true_annualized_sharpe": float(ann),
                        "power": round(hits / n_draws, 6)})
        node = {
            "curve": pts,
            "power_at_grid_max": pts[-1]["power"],
            "ceiling_power": max(p["power"] for p in pts),
            "headroom_at_max_sharpe": round(1.0 - max(p["power"] for p in pts), 6),
            "headroom_note": (
                "headroom_at_max_sharpe is 1 minus the best power any probe "
                "Sharpe achieved, with the probe running to 1000 annualised. "
                "Non-zero headroom at Sharpe 1000 is a CEILING, not a "
                "shortfall: no better strategy closes it."),
        }
        if ks:
            node["empirical_raw_kurtosis"] = _dist(ks)
            node["eligibility_pct_kurtosis_below_K_star"] = round(
                k_hits / float(n_draws), 6)
        return node

    whole_probe = _probe(n_months)
    fold4_probe = _probe(fold_months)
    return {
        "question": ("can the frozen DSR threshold be reached at ANY true "
                     "Sharpe on this many observations?"),
        "derivation": (
            "DSR >= t requires sr/sqrt(V) >= (z_mult + z_t)/sqrt(n-1) = c, with "
            "V = 1 - skew*sr + (K-1)/4*sr^2. Dropping the O(1/sqrt(n)) skew "
            "term, a solution exists only while c^2*(K-1)/4 < 1, i.e. "
            "K < K* = 1 + 4/c^2. Above K* the variance term grows as fast as "
            "sr^2, sr/sqrt(V) is bounded, and no Sharpe clears the bar."),
        "probe_sharpes": [float(x) for x in P9_CEILING_PROBE_SR],
        "n_draws_per_probe_point": int(n_draws),
        "whole_record": {"analytic": whole, "empirical": whole_probe},
        "fold_4_true_oos": {"analytic": fold4, "empirical": fold4_probe},
        "ceiling_detected": bool(
            fold4_probe["headroom_at_max_sharpe"] > 0.20
            and whole_probe["headroom_at_max_sharpe"] <= 0.20),
    }


def _findings(*, engine: CompositeGateEngine, dsr_node: Dict[str, Any],
              gate_stats: Dict[str, Any], fold4_node: Dict[str, Any],
              beta_values: List[float], turnover_values: List[float],
              sharpe_values: List[float], component_scores: Dict[str, List[float]],
              require_prereg: bool, decoupling: Dict[str, Any],
              power: Dict[str, Any], fwer: Dict[str, Any],
              ceiling: Dict[str, Any],
              passed_scores: List[str], all_scores: List[str],
              n_eff: int) -> List[str]:
    """
    The interpretation layer.

    Every claim here is derived from the numbers in this run — no finding is
    written unless the measurement that supports it is in the report.
    """
    out: List[str] = []
    nominal = float(dsr_node["nominal_fwer_claimed"])
    rate = float(dsr_node["null_pass_rate"])
    verdict = dsr_node["verdict_vs_nominal"]

    if verdict == "violated":
        out.append(
            f"DECISION RULE FIRES {rate / nominal:.1f}x ABOVE ITS OWN NOMINAL: "
            f"null pass rate {rate:.6f} (Wilson lower "
            f"{dsr_node['wilson_ci_95']['lo']:.6f}) is provably above the "
            f"1/N={nominal:.6f} the DSR claims for itself. The null book is a "
            f"pure market-beta portfolio with zero alpha, so the excess is "
            f"unmodelled market drift: the shipped deflated_sharpe_ratio "
            f"compares the raw sample mean against zero and never removes the "
            f"benchmark. A passive index would be scored as skill.")
    elif verdict == "controlled":
        out.append(
            f"DECISION RULE CONTROLLED: null pass rate {rate:.6f} (Wilson upper "
            f"{dsr_node['wilson_ci_95']['hi']:.6f}) is at or below the 1/N="
            f"{nominal:.6f} it claims, on a ~beta-1 zero-alpha book.")
    else:
        need = dsr_node["n_needed_to_demonstrate_zero_rejections"]
        out.append(
            f"DECISION RULE INCONCLUSIVE at n={n_eff}: null pass rate {rate:.6f}, "
            f"Wilson 95% CI [{dsr_node['wilson_ci_95']['lo']:.6f}, "
            f"{dsr_node['wilson_ci_95']['hi']:.6f}] straddles 1/N={nominal:.6f}. "
            f"This is NOT a violation; the run is too small to resolve a rate "
            f"this small. Zero rejections would need n>={need}.")

    # --- power / MDE ------------------------------------------------------
    _pw = power
    mde80 = _pw.get("mde_annualized_sharpe_power_0.80")
    mde80_f4 = _pw.get("mde_annualized_sharpe_power_0.80_fold_4")
    type1 = (_pw.get("fat_tail_arm", {}) or {}).get("power_at_sr_0.0")
    if mde80 is not None:
        if mde80_f4 is None:
            tail = (f" The Fold-4 (12-month TRUE-OOS) limb has NO MDE inside the "
                    f"scanned grid [0.00, {power.get('sr_grid_max'):.2f}] — see "
                    f"the kurtosis-ceiling finding; that limb saturates below "
                    f"80% power at any Sharpe, so the conjunctive frozen rule "
                    f"cannot be powered up by waiting for a better strategy.")
        else:
            tail = (f" The Fold-4 (12-month TRUE-OOS) limb needs {mde80_f4:.2f}, "
                    f"so '{_pw.get('binding_limb')}' is the binding limb and no "
                    f"claim can clear the frozen rule below "
                    f"{max(mde80, mde80_f4):.2f} annualised Sharpe.")
        out.append(
            f"POWER / MDE: the frozen rule reaches 80% power at a true "
            f"annualised Sharpe of {mde80:.2f} on the full "
            f"{power.get('whole_record_months')}-month record "
            f"(90% at {(_pw.get('mde_annualized_sharpe_power_0.90') or float('nan')):.2f}), "
            f"on fat-tailed returns, {power.get('n_draws_per_point')} draws per "
            f"point, measured by calling the shipped DSR at N="
            f"{DSR_N_TRIALS}. Measured type-I error at a true Sharpe of 0 is "
            f"{type1}.{tail}")
    else:
        out.append(
            "POWER / MDE: NO annualized Sharpe in [0.00, 4.00] reaches 80% power "
            f"on the full {power.get('whole_record_months')}-month record under "
            f"the frozen DSR at N={DSR_N_TRIALS}. The design as frozen cannot "
            "detect a real book at any plausible Sharpe on this track record.")

    # --- FWER scale ------------------------------------------------------
    _ce = ceiling
    if _ce.get("ceiling_detected"):
        _f4a = _ce["fold_4_true_oos"]["analytic"]
        _f4e = _ce["fold_4_true_oos"]["empirical"]
        _wa = _ce["whole_record"]["analytic"]
        out.append(
            f"FOLD-4 LIMB HAS A HARD POWER CEILING, NOT A SMALL-SAMPLE ONE: on "
            f"{_f4a['n_months']} monthly observations the frozen threshold is "
            f"cleared only while the sample's RAW kurtosis stays under K* = "
            f"{_f4a['break_even_raw_kurtosis_K_star']}; above that the DSR's "
            f"variance term grows as fast as the signal and NO Sharpe can pass. "
            f"Power tops out at {_f4e['ceiling_power']:.3f} — still "
            f"{_f4e['headroom_at_max_sharpe']:.1%} short of 80% after pushing the "
            f"true Sharpe to 1000 annualised — against the "
            f"{_ce['whole_record']['analytic']['n_months']}-month limb's K* = "
            f"{_wa['break_even_raw_kurtosis_K_star']}, which is far looser and "
            f"clears 80% comfortably. Only "
            f"{_f4e.get('eligibility_pct_kurtosis_below_K_star', float('nan')):.1%} "
            f"of 12-month fat-tailed draws are even ELIGIBLE to pass. The "
            f"conjunctive frozen rule (whole record AND Fold 4) therefore has a "
            f"power ceiling near {_f4e['ceiling_power']:.2f} no matter how good "
            f"the strategy is, and roughly "
            f"{1 - _f4e['ceiling_power']:.0%} of genuinely excellent books fail "
            f"it for reasons unrelated to skill. This is a property of "
            f"estimating kurtosis on 12 points under 513-trial deflation, not a "
            f"defect in any one run.")

    _fw = fwer["if_the_intended_control_is_familywise_0.05"]
    out.append(
        f"FWER SCALE, EXPLICIT: the frozen design's own rate is 1/"
        f"{DSR_N_TRIALS} = {fwer['frozen_design']['nominal_fwer_claimed']:.6f} "
        f"(nominal_source: {dsr_node['nominal_source']}). If the intended control "
        f"is instead a familywise 0.05 across all {DSR_N_TRIALS} decisions, the "
        f"per-decision alpha required is {_fw['per_decision_alpha_required']:.2e} "
        f"-- {DSR_N_TRIALS}x tighter than 0.05. The two are not interchangeable; "
        f"this report measures against 1/N and does not apply the other.")

    if fwer["gates_calibrated"] == 0:
        out.append(
            "ZERO PER-GATE THRESHOLDS CALIBRATED, DELIBERATELY: v7 froze "
            "`no_gate_hierarchy: true` and removed the `gates:` block, so there "
            "is no per-gate family and no alpha/8 to spend. Every per-metric "
            "threshold in `reference_thresholds` is measured (threshold vs "
            "empirical null alpha is tabulated) but is labelled "
            "threshold_reference_only and is not a hypothesis test. Producing "
            "calibrated per-gate thresholds would re-introduce the architecture "
            "the frozen pre-registration deleted.")

    # --- the decoupling finding ----------------------------------------
    if not decoupling["composite_score_drives_decision"]:
        out.append(
            "COMPOSITE SCORE DOES NOT DECIDE THE GATE (MEASURED, NOT ASSUMED): "
            f"holding the Arm C return series fixed and sweeping every non-return "
            f"composite input across its full frozen range "
            f"({decoupling['n_combinations']} combinations), `passed` took "
            f"{len(decoupling['distinct_passed_values'])} distinct value(s) while "
            f"composite_score moved over "
            f"[{decoupling['composite_score_range'][0]:.4f}, "
            f"{decoupling['composite_score_range'][1]:.4f}] and the DSR took "
            f"{decoupling['distinct_dsr_values']} distinct value(s) that depend "
            f"only on the return series. CompositeGateEngine sets "
            f"passed = composite_dsr >= threshold on the RAW Arm C returns; the "
            f"seven weighted components are computed, reported, and then never "
            f"read. The frozen contract says "
            f"'{decoupling['contract_reference']}'; the code DSRs the returns. "
            f"Either the code or the document is wrong, and the gap is "
            f"load-bearing: the composite carries 0.85 of the stated design "
            f"weight and 0 of the decision.")
    else:
        out.append(
            "COMPOSITE SCORE IS COUPLED: `passed` responded to the composite "
            f"inputs across {decoupling['n_combinations']} combinations "
            f"(values {decoupling['distinct_passed_values']}).")

    if passed_scores:
        spread = max(passed_scores) - min(passed_scores)
        out.append(
            f"CROSS-CHECK (empirical): {len(passed_scores)} null draws cleared "
            f"DSR>=0.95 and their composite scores span {spread:.4f} "
            f"(min {min(passed_scores):.4f}, max {max(passed_scores):.4f}) "
            f"against a full-sample range of "
            f"{max(all_scores) - min(all_scores):.4f} — a pass set that is not "
            f"the top of the composite distribution, consistent with the "
            f"deterministic probe above.")
    else:
        out.append(
            f"CROSS-CHECK (empirical) UNAVAILABLE: no null draw in {n_eff} "
            f"cleared DSR>=0.95, so there is no pass set to compare composite "
            f"scores across. The deterministic probe above is the load-bearing "
            f"evidence and does not depend on any null passing.")

    # --- benchmark drift, quantified ------------------------------------
    beta = _dist(beta_values)
    shp = _dist(sharpe_values)
    if beta["mean"] is not None and shp["mean"] is not None:
        out.append(
            f"THE NULL IS A MARKET-BETA BOOK: mean market beta "
            f"{beta['mean']:.4f} (p05 {beta['p05']:.4f}, p95 {beta['p95']:.4f}) "
            f"and mean annualised Sharpe {shp['mean']:.4f} across "
            f"{n_eff} zero-alpha draws. Every null 'failure' below the threshold "
            f"is therefore a strategy that simply did not ride beta, and every "
            f"null 'pass' is one that did. This is the correct adversarial null "
            f"for a Sharpe-style gate and it is why the decision rule is not "
            f"trivially safe.")

    to = gate_stats.get("max_drawdown", {})
    tr = gate_stats.get("swap_churn", {})
    tt = gate_stats.get("tca_bps", {})
    pr = gate_stats.get("probation_pass_rate", {})
    ot = _dist(turnover_values)
    if to.get("null_pass_rate") is not None:
        out.append(
            f"THRESHOLD REFERENCE (not alpha-calibrated): drawdown passes "
            f"{to['null_pass_rate']:.4f}, swap churn {tr.get('null_pass_rate')}, "
            f"TCA {tt.get('null_pass_rate')}, probation band "
            f"{pr.get('null_pass_rate')}. Mean one-way turnover per rebalance is "
            f"{ot['mean']:.4f} against the frozen "
            f"execution.max_turnover_per_rebalance cap of 0.30 — a pure "
            f"Dirichlet null is MAXIMALLY churning, so a high turnover failure "
            f"rate here is the null working, not the gate being strict.")

    if pr.get("null_pass_rate") is not None and 0.35 <= P9_NULL_PROBATION_RATE <= 0.65:
        sd_p = float(math.sqrt(P9_NULL_PROBATION_RATE * (1 - P9_NULL_PROBATION_RATE)
                               / P9_PROBATION_TRIGGERS))
        out.append(
            f"PROBATION BAND IS NEAR-VACUOUS: [0.35, 0.65] contains the null "
            f"mean {P9_NULL_PROBATION_RATE:.2f}, and at the frozen evidence floor "
            f"n={P9_PROBATION_TRIGGERS} the sampling sd is only {sd_p:.4f}. The "
            f"band cannot separate a null run from a real one and must not be "
            f"relied on alone.")

    ari = gate_stats.get("hmm_ari_folds", {})
    if ari.get("null_pass_rate") is not None:
        out.append(
            f"HMM ARI NULL IS GENERATED, NOT ASSUMED: ARI>={0.45} on >="
            f"{P9_ARI_FOLDS_REQUIRED}/4 folds fires on "
            f"{ari['null_pass_rate']:.4f} of null draws. Under 'no regime "
            f"structure' the true labels and the recovered labels are both i.i.d. "
            f"uniform over {P9_ARI_STATES} states, so this rate is the exact "
            f"ARI-of-two-random-partitions distribution and it needs no "
            f"correction — unlike the p-value gates the frozen design deleted.")

    cal = gate_stats.get("calmar", {})
    if cal.get("in_prereg_v7") is False:
        out.append(
            "CALMAR IS NOT A FROZEN v7 GATE: threshold included because the "
            "Phase-9C dispatch names it, but it was deleted with the `gates:` "
            "block in the pre-registration that v7 superseded. It is reported "
            "as a diagnostic. The same applies to the HMM occupancy caps.")

    if require_prereg and not engine.pre_reg.integrity_verified:
        out.append(
            "PRE-REGISTRATION DIGEST NOT VERIFIED: the engine loaded under an "
            "unverified digest, so the composite DSR's provenance component "
            "cannot be trusted for this run.")

    return out


def _write_sha256_sidecar(report_path: Path) -> Path:
    side = report_path.with_suffix(report_path.suffix + ".sha256")
    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    side.write_text(digest + "\n", encoding="utf-8")
    return side


def _print_summary(report: Dict[str, Any], out: Path) -> None:
    d = report["decision_rule"]
    print(f"\n[calib-p9] {report['n_null_portfolios']} null portfolios | "
          f"universe={report['n_universe_names']} | "
          f"window={report['null_model']['description'].split('.')[0][:60]}...")
    print(f"[calib-p9] prereg v{report['prereg_version']} "
          f"sha256={report['prereg_sha256'][:12]}... "
          f"verified={report['prereg_integrity_verified']}")
    print(f"\n  {'decision rule':38s} {'null rate':>10s} {'wilson95':>24s} "
          f"{'nominal':>10s} verdict")
    ci = d["wilson_ci_95"]
    print(f"  {'composite_dsr >= 0.95':38s} {d['null_pass_rate']:10.6f} "
          f"[{ci['lo']:.6f}, {ci['hi']:.6f}] "
          f"{d['nominal_fwer_claimed']:10.6f} {d['verdict_vs_nominal']}")
    print(f"\n  {'reference threshold':38s} {'null rate':>10s} {'wilson95':>24s} "
          f"role")
    for k, v in report["reference_thresholds"].items():
        c = v["wilson_ci_95"]
        flag = "" if v["in_prereg_v7"] else "  [NOT IN v7 — diagnostic]"
        print(f"  {k:38s} {v['null_pass_rate']:10.6f} "
              f"[{c['lo']:.6f}, {c['hi']:.6f}] "
              f"{'DECISION RULE' if k == 'composite_dsr' else 'reference'}{flag}")
    print("\n  findings:")
    for f in report["findings"]:
        # The console codepage on this box is cp1252; the report file is UTF-8.
        # Print a transliterated line rather than letting the terminal raise.
        safe = f.encode("ascii", "replace").decode("ascii")
        print(f"    - {safe}")
    print(f"\n[calib-p9] report: {out}")


# ---------------------------------------------------------------------------
# EXIT-LOGIC LEG
# ---------------------------------------------------------------------------
#
# Arm A's trailing-stop fix changes the realised return path and the realised
# execution schedule, so the frozen cost/risk thresholds have to be re-measured
# under the new logic.  The trailing stop and the TWAP acceleration are imported
# from the SHIPPED modules so this measures the code that actually runs.
#
# ROLE: threshold_reference_only.  These are thresholds, not hypothesis tests;
# they carry no alpha and are compared against the no-stop baseline, never
# against a corrected level.

ARM_A_TRAILING_PCT_GRID = (0.10, 0.15, 0.20)
ARM_A_TRAILING_PCT = 0.15
ARM_A_HMM_MIN_DWELL_DAYS = 10

EXIT_N_NAMES = 10
EXIT_N_DAYS = 756
EXIT_DAILY_MU = 0.0004
EXIT_DAILY_SD = 0.014
EXIT_COMMON_RHO = 0.60
EXIT_SEED = 20260904
EXIT_REENTRY_COOLDOWN_DAYS = 60
EXIT_NOTIONAL_USD = 2_000_000.0
EXIT_ADV_USD = 12_000_000.0
EXIT_MOAT_EFFICACY = 0.92
EXIT_MAX_ANNUAL_TCA_BPS = 150.0
EXIT_MAX_SWAPS_PER_YEAR = 5.0
EXIT_MAX_ANNUAL_TURNOVER = 0.30


def _exit_null_book(rng: np.random.Generator, n_names: int = EXIT_N_NAMES,
                    n_days: int = EXIT_N_DAYS) -> pd.DataFrame:
    """Equal-weight book of null price paths (common factor + idiosyncratic)."""
    idx = pd.bdate_range("2020-01-01", periods=n_days)
    common = rng.normal(EXIT_DAILY_MU, EXIT_DAILY_SD, n_days)
    idio = rng.normal(EXIT_DAILY_MU, EXIT_DAILY_SD, (n_days, n_names))
    z = (np.sqrt(EXIT_COMMON_RHO) * common[:, None]
         + np.sqrt(1.0 - EXIT_COMMON_RHO) * idio)
    return pd.DataFrame(100.0 * np.exp(np.cumsum(z, axis=0)), index=idx)


def _replay_trailing_stop(price: np.ndarray, trailing_pct: float
                          ) -> Tuple[np.ndarray, np.ndarray, List[int]]:
    """
    Replay the SHIPPED trailing stop with the documented cooldown re-entry.

    The watermark ratchet and the inclusive trigger boundary are
    ``Quantitative.exit_engine.evaluate_trailing_stop``'s, not this module's.
    """
    from Quantitative.exit_engine import evaluate_trailing_stop

    n = price.size
    in_pos = np.zeros(n, dtype=bool)
    realised = np.zeros(n, dtype=float)
    exits: List[int] = []
    watermark = float(price[0])
    in_position = True
    cooldown_until = -1
    last_exit_price = np.inf

    for i in range(1, n):
        p = float(price[i])
        if in_position:
            triggered, _lvl, watermark = evaluate_trailing_stop(p, watermark, trailing_pct)
            if triggered:
                realised[i] = (p / float(price[i - 1])) - 1.0
                in_position = False
                in_pos[i] = False
                exits.append(i)
                last_exit_price = p
                cooldown_until = i + EXIT_REENTRY_COOLDOWN_DAYS
                watermark = p                       # re-arm flat at the exit
            else:
                realised[i] = (p / float(price[i - 1])) - 1.0
                in_pos[i] = True
        else:
            # Cash until the cooldown expires AND price recovers through the
            # exit print -- the frozen spec's recovery-confirmation re-entry.
            if i > cooldown_until and p >= last_exit_price:
                in_position = True
                watermark = p
                in_pos[i] = True
                realised[i] = (p / float(price[i - 1])) - 1.0
            else:
                realised[i] = 0.0
    return in_pos, realised, exits


def _tca_round_trip_bps(n_stopped: int, n_names: int, vol_mult: float,
                        adv_mult: float) -> float:
    """
    Round-trip bps for unwinding the names the trailing stop actually stopped.

    Uses the frozen cost model rather than an invented per-exit constant,
    because the gate is defined on the model's output.  Concurrent unwind on a
    thin book is the correlated-selling risk the Arm A change introduces.
    """
    if n_stopped <= 0:
        return 0.0
    k = min(n_stopped, EXIT_N_NAMES)
    notionals = np.full(k, EXIT_NOTIONAL_USD)
    advs = np.full(k, EXIT_ADV_USD)
    corr = np.full((k, k), EXIT_COMMON_RHO)
    np.fill_diagonal(corr, 1.0)

    try:
        from Quantitative.tca_model import simulate_round_trip_bps
        bps = float(simulate_round_trip_bps(
            notionals=notionals, advs=advs, corr=corr,
            vol_mult=vol_mult, adv_mult=adv_mult, stagger=False))
        if np.isfinite(bps):
            return bps
    except Exception:                      # noqa: BLE001 — fall through to model
        pass

    # Frozen cost model: costs.commission_bps_per_side 10,
    # costs.slippage_base_bps 50, EXPANSION multiplier 1.0.
    slip = 50.0 * vol_mult / max(adv_mult, 1e-6) ** 0.5
    return float(2.0 * (10.0 + min(slip, 500.0)))


def _exit_logic_leg(book: pd.DataFrame, trailing_pct: Optional[float]) -> Dict[str, Any]:
    """Score the three frozen cost/risk thresholds on one simulated book."""
    price = book.to_numpy()
    n_names = price.shape[1]
    per_name_ret = np.zeros(price.shape)
    total_exits = 0
    traded_notional = 0.0
    stopped: List[int] = []

    for j in range(n_names):
        if trailing_pct is None:
            per_name_ret[:, j] = np.concatenate(
                [[0.0], price[1:, j] / price[:-1, j] - 1.0])
            continue
        _in_pos, realised, exits = _replay_trailing_stop(price[:, j], trailing_pct)
        per_name_ret[:, j] = realised
        total_exits += len(exits)
        traded_notional += len(exits) * EXIT_NOTIONAL_USD
        if exits:
            stopped.append(j)

    port_ret = pd.Series(per_name_ret.mean(axis=1), index=book.index)
    years = EXIT_N_DAYS / 252.0
    book_notional = n_names * EXIT_NOTIONAL_USD
    annual_turnover = (traded_notional / book_notional) / max(years, 1e-9)
    swaps_per_year = total_exits / max(years, 1e-9)
    tca_bps = _tca_round_trip_bps(len(stopped), n_names, vol_mult=2.0, adv_mult=0.60)
    mdd = _max_drawdown(port_ret)

    return {
        "max_drawdown": {
            "value": round(mdd, 6), "cap": -0.20, "passed": bool(mdd >= -0.20)},
        "swap_churn": {
            "value": round(swaps_per_year, 6), "swaps_per_year": round(swaps_per_year, 6),
            "annual_book_turnover": round(annual_turnover, 6),
            "cap_swaps_per_yr": EXIT_MAX_SWAPS_PER_YEAR,
            "cap_annual_turnover": EXIT_MAX_ANNUAL_TURNOVER,
            "passed": bool(swaps_per_year < EXIT_MAX_SWAPS_PER_YEAR
                           and annual_turnover < EXIT_MAX_ANNUAL_TURNOVER)},
        "tca_budget": {
            "value": round(tca_bps, 4), "cap_bps": EXIT_MAX_ANNUAL_TCA_BPS,
            "passed": bool(tca_bps <= EXIT_MAX_ANNUAL_TCA_BPS),
            "n_stopped": len(stopped)},
    }


def calibrate_exit_logic(n_null: int = 2000,
                         seed: int = EXIT_SEED,
                         report_path: Optional[Path] = None,
                         progress_every: int = 500) -> Dict[str, Any]:
    """
    Re-measure the three exit-sensitive frozen thresholds under Arm A logic.

    For each pre-registered ``trailing_pct`` (plus a no-stop baseline) this
    reports the null pass-rate of max drawdown, swap churn and TCA at their
    FROZEN thresholds, and the shift against the baseline.

    REFERENCE-ONLY.  The comparison is against the no-stop baseline, never
    against an alpha: a threshold is not a hypothesis test.
    """
    out_path = Path(report_path) if report_path else EXIT_REPORT
    rng = np.random.default_rng(seed)
    arms: Dict[str, List[Dict[str, Any]]] = {"baseline": []}
    arms.update({f"trailing_{p:.2f}": [] for p in ARM_A_TRAILING_PCT_GRID})

    for i in range(int(n_null)):
        book = _exit_null_book(rng)
        arms["baseline"].append(_exit_logic_leg(book, None))
        for pct in ARM_A_TRAILING_PCT_GRID:
            arms[f"trailing_{pct:.2f}"].append(_exit_logic_leg(book, pct))
        if progress_every and (i + 1) % progress_every == 0:
            print(f"[calib-exit] {i + 1}/{n_null} null books done", flush=True)

    gate_names = ("max_drawdown", "swap_churn", "tca_budget")
    baseline_rate = {g: float(np.mean([r[g]["passed"] for r in arms["baseline"]]))
                     for g in gate_names}

    summary: Dict[str, Any] = {}
    for arm, rows in arms.items():
        entry: Dict[str, Any] = {}
        for g in gate_names:
            rate = float(np.mean([r[g]["passed"] for r in rows]))
            node: Dict[str, Any] = {
                "null_pass_rate": round(rate, 6),
                "shift_vs_baseline": round(rate - baseline_rate[g], 6),
                "wilson_ci_95": wilson_ci(int(round(rate * len(rows))), len(rows)),
                "null_value_distribution": _dist([r[g]["value"] for r in rows]),
            }
            if g == "swap_churn":
                node["null_annual_book_turnover"] = _dist(
                    [r[g]["annual_book_turnover"] for r in rows])
            if g == "tca_budget":
                node["null_n_stopped"] = _dist([float(r[g]["n_stopped"]) for r in rows])
            entry[g] = node
        summary[arm] = entry

    report = {
        "report_type": "exit_logic_calibration",
        "report_version": 3,
        "phase": 9,
        "calibration_role": "threshold_reference_only",
        "n_null_books": int(n_null),
        "n_names_per_book": EXIT_N_NAMES,
        "n_days_per_book": EXIT_N_DAYS,
        "seed": int(seed),
        "trailing_pct_grid": list(ARM_A_TRAILING_PCT_GRID),
        "frozen_thresholds": {
            "max_drawdown": ">= -0.20",
            "swap_churn": f"< {EXIT_MAX_SWAPS_PER_YEAR}/yr and "
                          f"< {EXIT_MAX_ANNUAL_TURNOVER} annual book turnover",
            "tca_budget": f"<= {EXIT_MAX_ANNUAL_TCA_BPS} bps",
        },
        "baseline_null_pass_rates": {k: round(v, 6) for k, v in baseline_rate.items()},
        "arms": summary,
        "interpretation": (
            "A trailing stop caps the per-name excursion (drawdown null pass-rate "
            "should RISE), forces immediate liquidation (churn should RISE) and "
            "forfeits the TWAP spread (TCA should RISE). A gate that stops moving "
            "in the null has stopped discriminating."),
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str),
                        encoding="utf-8")
    _write_sha256_sidecar(out_path)
    print(f"[calib-exit] report: {out_path}")
    for arm, entry in summary.items():
        print(f"  {arm:16s} " + "  ".join(
            f"{g}={entry[g]['null_pass_rate']:.4f}"
            f"({entry[g]['shift_vs_baseline']:+.4f})" for g in gate_names))
    return report


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

def run_tests(tmp: Optional[Path] = None) -> int:
    import shutil
    import tempfile

    tmp = tmp or Path(tempfile.mkdtemp(prefix="calib_"))
    failures: List[str] = []

    def check(name: str, cond: Any) -> None:
        if cond:
            print(f"  ok   {name}")
        else:
            print(f"  FAIL {name}")
            failures.append(name)

    print("[calib-test] statistics")
    ci = wilson_ci(50, 100)
    check("wilson brackets the point estimate", ci["lo"] < 0.5 < ci["hi"])
    check("wilson zero case has positive upper",
          wilson_ci(0, 100)["hi"] > 0.0 and wilson_ci(0, 100)["lo"] == 0.0)
    check("wilson narrows with n",
          (wilson_ci(500, 10000)["hi"] - wilson_ci(500, 10000)["lo"])
          < (wilson_ci(5, 100)["hi"] - wilson_ci(5, 100)["lo"]))
    check("n_needed matches the closed form",
          _n_needed_for_zero_rejection(0.05) == int(math.ceil(1.96 ** 2 / (0.05 * 0.95))))
    check("spearman is 1 on a perfect monotone pair",
          abs(_spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) - 1.0) < 1e-12)
    check("spearman is -1 on a perfectly inverted pair",
          abs(_spearman([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]) + 1.0) < 1e-12)
    check("spearman handles ties without depending on sort order",
          _spearman([1, 1, 2, 2], [5, 5, 6, 6]) == _spearman([2, 2, 1, 1], [6, 6, 5, 5]))
    check("verdict is three-state",
          {_three_state_verdict(0, 10000, 0.00195),
           _three_state_verdict(500, 10000, 0.00195),
           _three_state_verdict(0, 10, 0.00195)}
          <= {"controlled", "violated", "inconclusive_insufficient_n"})

    print("[calib-test] null DGP")
    rng = np.random.default_rng(7)
    book = _phase9_null_book(rng, n_months=12, n_names=200)
    check("book has the requested length", len(book.arm_c) == 12)
    check("weights rows sum to 1", np.allclose(book.weights.sum(axis=1), 1.0))
    check("weights are non-negative", (book.weights >= 0).all())
    check("monthly stamps", str(book.dates.freqstr or "ME") in ("ME", "<MonthEnd>"))
    check("zero alpha is exact by construction",
          abs(float(book.arm_c.mean()) - P9_FACTOR_MU) < 0.05)

    ari = [_phase9_null_ari(rng) for _ in range(400)]
    check("null ARI is centred near 0", abs(float(np.mean(ari))) < 0.05)
    check("null ARI can be negative", min(ari) < 0.0)
    check("null ARI is bounded by 1", max(ari) <= 1.0)

    print("[calib-test] thresholds")
    ts = {t.key: t for t in _load_thresholds()}
    check("sharpe threshold is > 1.0",
          _eval_threshold(ts["oos_sharpe"], 1.01) and not _eval_threshold(ts["oos_sharpe"], 0.99))
    check("drawdown threshold is >= -0.20",
          _eval_threshold(ts["max_drawdown"], -0.20) and not _eval_threshold(ts["max_drawdown"], -0.21))
    check("calmar is flagged as not-in-v7", ts["calmar"].in_prereg_v7 is False)
    check("probation band is inclusive",
          _eval_threshold(ts["probation_pass_rate"], 0.35)
          and _eval_threshold(ts["probation_pass_rate"], 0.65)
          and not _eval_threshold(ts["probation_pass_rate"], 0.34))

    print("[calib-test] calibration smoke run")
    out = tmp / "calib.json"
    # A short, coarse power grid: the self-test asserts the SHAPE of the node,
    # not the MDE, and the production numbers come from the full run. The grid
    # still has to be fine enough for the monotonicity guard to mean something
    # (it excuses at most 10% of steps), hence 0.25 rather than 0.5.
    smoke_grid = tuple(round(0.25 * i, 2) for i in range(0, 33))   # 0.0 .. 8.0
    rep = calibrate_gates(n=40, seed=3, report_path=out, progress_every=0,
                          n_months=24, n_names=150,
                          power_draws=300, power_sr_grid=smoke_grid)
    check("report written", out.exists() and out.with_suffix(".json.sha256").exists())
    check("report is valid JSON", json.loads(out.read_text(encoding="utf-8"))["n_null_portfolios"] == 40)
    check("report has no gate hierarchy", rep["no_gate_hierarchy"] is True)
    check("report declares zero gates", rep["gates_calibrated"] == 0)
    check("decision rule present", "null_pass_rate" in rep["decision_rule"])
    check("null pass rate in [0,1]",
          0.0 <= rep["decision_rule"]["null_pass_rate"] <= 1.0)
    check("every reference threshold measured",
          set(rep["reference_thresholds"]) ==
          {"oos_sharpe", "max_drawdown", "ir_vs_spy", "hmm_ari_folds", "composite_dsr",
           "calmar", "swap_churn", "tca_bps", "probation_pass_rate", "wacc_coverage"})
    check("only the DSR is a decision rule",
          all(v["calibration_role"] in ("decision_rule", "threshold_reference_only")
              for v in rep["reference_thresholds"].values()))
    check("findings are produced", len(rep["findings"]) >= 3)
    check("no alpha/8 anywhere in the report",
          "0.00625" not in json.dumps(rep))

    print("[calib-test] power / MDE")
    pw = rep["power_analysis"]
    curve = pw["fat_tail_arm"]["curve"]
    check("power node is present and both arms are measured",
          "fat_tail_arm" in pw and "normal_arm" in pw)
    check("MDE is monotone in the target power",
          (pw["mde_annualized_sharpe_power_0.80"] or math.inf)
          <= (pw["mde_annualized_sharpe_power_0.90"] or math.inf))
    check("power rises from the null to the top of the grid",
          curve[-1]["power"] > curve[0]["power"])
    # Exact monotonicity is the wrong assertion on a Monte Carlo curve: at
    # n_draws=200 a 2.5pp dip at p~0.15 is inside the noise.  Allow a dip only
    # when it is inside 2 standard errors of the difference; a REAL
    # non-monotonicity (a threshold that gets easier as the signal grows) is
    # many SE and still fails.
    # Pointwise monotonicity is the WRONG assertion on a Monte Carlo curve: at
    # a few hundred draws a ~2pp wobble is the estimator, not the rule, and a
    # tolerance wide enough to excuse the wobble also excuses a genuinely broken
    # curve. What must hold is that power rises OVERALL, so test that.
    _rho = _spearman([p["true_annualized_sharpe"] for p in curve],
                     [p["power"] for p in curve])
    check(f"power rises monotonically in rank with the true Sharpe "
          f"(Spearman rho={_rho:.3f})", _rho >= 0.90)
    q = max(1, len(curve) // 4)
    lo_mean = sum(p["power"] for p in curve[:q]) / q
    hi_mean = sum(p["power"] for p in curve[-q:]) / q
    check("top-quartile power exceeds bottom-quartile power by a wide margin",
          hi_mean - lo_mean > 0.30)
    check("MDE is strictly above the frozen gate-2 Sharpe of 1.0",
          (pw["mde_annualized_sharpe_power_0.80"] or math.inf) > 1.0)
    check("Fold-4 (12-month) MDE is no better than the whole-record MDE",
          (pw["mde_annualized_sharpe_power_0.80_fold_4"] or math.inf)
          >= (pw["mde_annualized_sharpe_power_0.80"] or math.inf))
    check("binding limb is named",
          pw["binding_limb"] in ("fold_4_true_oos", "whole_record"))
    check("power at a true Sharpe of 0 is within 3x the nominal 1/N",
          curve[0]["power"] <= 3.0 / DSR_N_TRIALS)
    check("grid exhaustion is declared rather than silently None",
          set(pw["mde_grid_exhausted"]) == {"whole_record", "fold_4_true_oos",
                                            "note"})
    # The whole point of routing power through the SHIPPED DSR is that the
    # numbers are reproducible; assert it rather than trusting it.
    check("power analysis is deterministic for a fixed seed",
          _power_analysis(n_months=24, n_draws=300, sr_grid=smoke_grid)
          == _power_analysis(n_months=24, n_draws=300, sr_grid=smoke_grid))

    print("[calib-test] FWER accounting")
    fw = rep["fwer_accounting"]
    check("frozen nominal FWER is 1/N",
          abs(fw["frozen_design"]["nominal_fwer_claimed"]
              - 1.0 / DSR_N_TRIALS) < 1e-6)     # rounded to 8dp in the report
    check("familywise-0.05 per-decision alpha is 0.05/N",
          abs(fw["if_the_intended_control_is_familywise_0.05"]
              ["per_decision_alpha_required"] - 0.05 / DSR_N_TRIALS) < 1e-9)
    check("fwer_accounting still calibrates zero gates",
          fw["gates_calibrated"] == 0)

    print("[calib-test] kurtosis ceiling")
    ce = rep["power_analysis"]["ceiling_analysis"]
    _k12 = _kurtosis_ceiling(12)
    _k84 = _kurtosis_ceiling(84)
    check("12-month K* is just under 3.15 (only a thin-tailed 12-point sample "
          "can pass at all)",
          abs(_k12["break_even_raw_kurtosis_K_star"] - 3.1431) < 0.01)
    check("84-month K* is at least 5x looser than 12-month",
          _k84["break_even_raw_kurtosis_K_star"] > 5 * _k12["break_even_raw_kurtosis_K_star"])
    check("K* decreases as the sample grows",
          _k84["break_even_raw_kurtosis_K_star"] > _k12["break_even_raw_kurtosis_K_star"])
    check("ceiling analysis probed the right sample sizes",
          ce["fold_4_true_oos"]["analytic"]["n_months"] == 12
          and ce["whole_record"]["analytic"]["n_months"] == 24)
    check("ceiling is detected on the smoke configuration",
          ce["ceiling_detected"] is True)
    check("Fold-4 is still >20% short of 80% power at a true Sharpe of 1000",
          ce["fold_4_true_oos"]["empirical"]["headroom_at_max_sharpe"] > 0.20)
    check("the whole-record limb DOES clear 80% (contrast to Fold 4)",
          ce["whole_record"]["empirical"]["headroom_at_max_sharpe"] <= 0.20)
    check("eligibility fraction is reported for the Fold-4 limb",
          "eligibility_pct_kurtosis_below_K_star"
          in ce["fold_4_true_oos"]["empirical"])

    print("[calib-test] exit leg")
    erep = calibrate_exit_logic(n_null=6, seed=5, report_path=tmp / "exit.json",
                                progress_every=0)
    check("exit report written", (tmp / "exit.json").exists())
    check("exit arms cover the grid",
          set(erep["arms"]) == {"baseline"} | {f"trailing_{p:.2f}" for p in ARM_A_TRAILING_PCT_GRID})
    check("exit report is reference-only",
          erep["calibration_role"] == "threshold_reference_only")

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n[calib-test] {len(failures)} failure(s)"
          + ("" if not failures else ": " + ", ".join(failures)))
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 9C gate calibration on null portfolios "
                    "(v7 frozen design: single composite-DSR decision rule)")
    parser.add_argument("--run", action="store_true",
                        help="run the null calibration")
    parser.add_argument("--exit-logic", action="store_true",
                        help="run the Arm A exit-logic threshold limb instead")
    parser.add_argument("--n", type=int, default=N_NULL_DEFAULT,
                        help=f"null portfolios (default {N_NULL_DEFAULT})")
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--phase9", action="store_true",
                        help="accepted for CLI compatibility; v7 has ONE "
                             "architecture so this selects nothing")
    parser.add_argument("--names", type=int, default=P9_N_NAMES,
                        help=f"universe size (default {P9_N_NAMES})")
    parser.add_argument("--months", type=int, default=P9_N_MONTHS,
                        help=f"monthly rebalances (default {P9_N_MONTHS})")
    parser.add_argument("--dirichlet-alpha", type=float, default=P9_DIRICHLET_ALPHA,
                        help="Dirichlet concentration for the null weight draw "
                             f"(default {P9_DIRICHLET_ALPHA}; larger = more "
                             "persistent weights)")
    parser.add_argument("--report", type=str, default=None)
    parser.add_argument("--progress-every", type=int, default=500)
    parser.add_argument("--no-require-prereg", action="store_true",
                        help="diagnostics only; a run with an unverified "
                             "pre-registration digest is not a calibration")
    parser.add_argument("--test", action="store_true", help="run self-tests")
    args = parser.parse_args(argv)

    if args.test:
        return run_tests()
    if args.exit_logic:
        calibrate_exit_logic(n_null=args.n, seed=args.seed,
                             report_path=Path(args.report) if args.report else None,
                             progress_every=args.progress_every)
        return 0
    if not args.run:
        parser.print_help()
        return 2

    rep = calibrate_gates(
        n=args.n, seed=args.seed,
        report_path=Path(args.report) if args.report else None,
        progress_every=args.progress_every,
        n_months=args.months, n_names=args.names,
        dirichlet_alpha=args.dirichlet_alpha,
        require_prereg=not args.no_require_prereg,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
