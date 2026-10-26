"""
validation.composite_score — MONTHLY composite-score builder for the 3-arm backtest.
(bigpickle-worker / P0B fix, 2026-10-08)

WHY THIS MODULE EXISTS
----------------------
The P0B defect: ``data/backtests/arm_*/composite_score.parquet`` held **1,760
DAILY** rows of near-static composite values.  It was produced by
``gate_engine.compute_arm_a_composite_score()``, which walks the *daily* return
index and re-computes a rolling 252-day window at every daily observation.  Two
things are wrong with that artifact:

  1. **It is daily, not monthly.**  The frozen pre-registration declares an
     ``84``-month / ``2520``-day track record; the composite series that feeds
     the DSR must have one row per *rebalance* (month-end), not per trading day.
  2. **It is not derived from the rebalance-period portfolio metrics.**  It is
     re-derived from a rolling window of daily returns/weights, so consecutive
     rows are near-identical (the observed 0.71-0.81 band) and carry almost no
     information about the month they claim to score.

The correction: consume Workstream A's frozen interface artifact
``data/backtests/rolling_metrics_{a,b,c}_primary.parquet`` — one row per
rebalance, 84 rows per arm — and turn each period's *already rollup'd* metrics
into a single composite score in ``[0, 1]``.

INTERFACE CONTRACT (Workstream A -> Workstream B)
-------------------------------------------------
``rolling_metrics_{arm}_primary.parquet`` carries exactly these columns, 84
monthly rows per arm::

    rebalance_date (date)             arm (str)
    sharpe_rolling_12m (float)        max_dd_rolling_12m (float, negative)
    turnover_mom (float)              ari_concat (float)
    swaps_annualized (float)          tca_bps (float)
    probation_rate (float)            regime (str)
    ann_vol_12m (float)

AUTHORITATIVE WEIGHTS
---------------------
The weights are read from the FROZEN pre-registration
``statistical_design.composite_components`` (v7, SHA-256
``c80a47bf...``) and cross-checked against ``gate_engine.COMPONENT_SPECS``.
Both declare the SAME seven components summing to 1.0:

    sharpe 0.25 | max_drawdown 0.20 | turnover 0.15 | hmm_ari 0.15 |
    swap_churn 0.10 | tca_bps 0.10 | probation_pass_rate 0.05

``Data_Strategy.md`` §3 lists an 8-item "gate" table whose weights sum to 1.25
(Arm Superiority 0.25 + OOS Sharpe 0.25 + ...); that table is a DIAGNOSTIC
ledger, not the composite contract.  The code is the contract.

NORMALIZATION (documented; penalty direction per ``COMPONENT_SPECS``)
---------------------------------------------------------------------
For the four scalar components the module REUSES ``gate_engine``'s canonical
scorers verbatim; for the three metric components it applies the identical
ramp curve those scorers use, evaluated on A's already-annualized metric::

    sharpe          = clip((sharpe_rolling_12m - 0.5) / 0.5, 0, 1)     # 1.0 -> 1, <=0.5 -> 0
    max_drawdown    = clip((max_dd_rolling_12m + 0.40) / 0.20, 0, 1)   # -0.20 -> 1, <=-0.40 -> 0
    turnover        = clip((0.60 - turnover_mom) / 0.30, 0, 1)         # <=0.30 -> 1, >=0.60 -> 0
    hmm_ari         = gate_engine.score_hmm_ari(ari_concat)            # 0.70 -> 1, <=0.50 -> 0
    swap_churn      = gate_engine.score_swap_churn(swaps_annualized)   # <=5 -> 1, >=15 -> 0
    tca_bps         = gate_engine.score_tca_bps(tca_bps)               # <=150 -> 1, >=300 -> 0
    probation       = gate_engine.score_probation_pass_rate(probation_rate)

``composite_score_t = sum_i weight_i * component_i(t)``.

MAX-DRAWDOWN DIRECTION — KNOWN ENGINE DIVERGENCE
------------------------------------------------
``COMPONENT_SPECS['max_drawdown']`` declares ``"direction": "shallower"`` and the
pre-registration target is ``">= -0.20"`` (i.e. a shallower drawdown is better).
This module implements that declared direction: ``-0.20 -> 1.0``, ``<= -0.40 ->
0.0``, shallower always scores higher.

NOTE FOR REVIEW: ``gate_engine.score_max_drawdown`` (which operates on a returns
series) has an INVERTED curve — ``(-0.20 - mdd)/0.20`` maps ``-0.40 -> 1.0`` and
``-0.10 -> 0.0``, rewarding deeper drawdowns.  That contradicts both its own
docstring ("shallower than -20% scores 1.0") and the declared direction.  We do
NOT modify ``gate_engine`` (out of Workstream B's write scope) or propagate the
inversion; this is flagged as a finding.  Only the max-drawdown component is
affected — ``score_sharpe`` and ``score_turnover`` are correctly oriented.

DSR
---
Per the frozen pre-registration the Deflated Sharpe Ratio is computed on the
**monthly composite-score series** (not on returns), N=513, 2520-day track
record, Fold 4 (2024 TRUE OOS) reported separately and required to also reach
0.95.  ``gate_engine.deflated_sharpe_ratio`` is reused unchanged.

This module NEVER writes to ``data/backtests/arm_*/composite_score.parquet``.
New artifacts go to ``data/backtests/composite_scores_20261008/`` with a
provenance sidecar.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from ingestion.harness.gate_engine import (
    COMPONENT_NAMES,
    COMPONENT_SPECS,
    COMPONENT_WEIGHTS,
    DSR_N_TRIALS,
    DSR_THRESHOLD,
    DSR_TRACK_RECORD_DAYS,
    deflated_sharpe_ratio,
    score_hmm_ari,
    score_probation_pass_rate,
    score_swap_churn,
    score_tca_bps,
)
from ingestion.harness.prereg import MODULE_PREREG

__all__ = [
    "OUTPUT_COLUMNS",
    "REQUIRED_INPUT_COLUMNS",
    "METRIC_COLUMNS",
    "COMPONENT_TO_METRIC",
    "COMPONENT_NORMALIZERS",
    "MIN_MONTHLY_ROWS",
    "EXPECTED_MONTHLY_ROWS",
    "DEFAULT_OUTPUT_DIR",
    "CompositeScoreError",
    "RollingMetricsContractError",
    "NonMonthlyFrequencyError",
    "StaticCompositeError",
    "component_weights",
    "validate_monthly_rolling_metrics",
    "component_scores",
    "composite_series",
    "composite_score",
    "MonthlyCompositeResult",
    "evaluate_monthly_composite",
    "write_composite_artifacts",
]

# ---------------------------------------------------------------------------
# Frozen interface contract
# ---------------------------------------------------------------------------

OUTPUT_COLUMNS: Tuple[str, ...] = ("rebalance_date", "arm", "composite_score")

REQUIRED_INPUT_COLUMNS: Tuple[str, ...] = (
    "rebalance_date",
    "arm",
    "sharpe_rolling_12m",
    "max_dd_rolling_12m",
    "turnover_mom",
    "ari_concat",
    "swaps_annualized",
    "tca_bps",
    "probation_rate",
    "regime",
    "ann_vol_12m",
)

#: columns that are actually scored (must be finite in every row)
METRIC_COLUMNS: Tuple[str, ...] = (
    "sharpe_rolling_12m",
    "max_dd_rolling_12m",
    "turnover_mom",
    "ari_concat",
    "swaps_annualized",
    "tca_bps",
    "probation_rate",
)

#: frozen composite component name -> Workstream A rolling-metric column
COMPONENT_TO_METRIC: Dict[str, str] = {
    "sharpe": "sharpe_rolling_12m",
    "max_drawdown": "max_dd_rolling_12m",
    "turnover": "turnover_mom",
    "hmm_ari": "ari_concat",
    "swap_churn": "swaps_annualized",
    "tca_bps": "tca_bps",
    "probation_pass_rate": "probation_rate",
}

#: monthly cadence: 84 months over 2018-01-31 .. 2024-12-31
EXPECTED_MONTHLY_ROWS = 84
MIN_MONTHLY_ROWS = 24
MIN_MONTHLY_SPACING_DAYS = 20
MAX_MONTHLY_SPACING_DAYS = 45

DEFAULT_OUTPUT_DIR = Path("data/backtests/composite_scores_20261008")


class CompositeScoreError(ValueError):
    """Base class for composite-score construction failures."""


class RollingMetricsContractError(CompositeScoreError):
    """The rolling-metrics frame violates the frozen interface contract."""


class NonMonthlyFrequencyError(CompositeScoreError):
    """The rolling-metrics frame is not monthly (e.g. the 1,760-row daily defect)."""


class StaticCompositeError(CompositeScoreError):
    """The composite series is constant — the P0B near-static defect."""


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def _norm_sharpe(v: float) -> float:
    if not np.isfinite(v):
        return 0.0
    return float(np.clip((v - 0.5) / 0.5, 0.0, 1.0))


def _norm_max_drawdown(v: float) -> float:
    # Shallower drawdowns score higher (COMPONENT_SPECS direction="shallower",
    # target ">= -0.20"): -0.20 -> 1.0, <= -0.40 -> 0.0.  See the module
    # docstring note on gate_engine.score_max_drawdown's inverted arithmetic.
    if not np.isfinite(v):
        return 0.0
    return float(np.clip((v + 0.40) / 0.20, 0.0, 1.0))


def _norm_turnover(v: float) -> float:
    if not np.isfinite(v):
        return 0.0
    return float(np.clip((0.60 - v) / 0.30, 0.0, 1.0))


#: component name -> normalizer, producing a score in [0, 1]
COMPONENT_NORMALIZERS: Dict[str, Callable[[float], float]] = {
    "sharpe": _norm_sharpe,
    "max_drawdown": _norm_max_drawdown,
    "turnover": _norm_turnover,
    "hmm_ari": score_hmm_ari,
    "swap_churn": score_swap_churn,
    "tca_bps": score_tca_bps,
    "probation_pass_rate": score_probation_pass_rate,
}


def component_weights() -> Dict[str, float]:
    """The frozen 7-component weights, read from pre-reg v7 and cross-checked.

    Reads ``statistical_design.composite_components`` from the frozen YAML via
    the digest-verifying loader (the true source of truth), then asserts it is
    identical to ``gate_engine.COMPONENT_SPECS``.  A drift between the two is a
    pre-registration violation and raises rather than picking a winner.
    """
    declared = {str(c["name"]): float(c["weight"])
                for c in MODULE_PREREG.composite_components}
    coded = {n: float(w) for n, w in zip(COMPONENT_NAMES, COMPONENT_WEIGHTS)}

    if set(declared) != set(coded):
        raise CompositeScoreError(
            f"composite component names disagree between pre-registration "
            f"{sorted(declared)} and gate_engine {sorted(coded)}")
    for name, weight in declared.items():
        if abs(weight - coded[name]) > 1e-12:
            raise CompositeScoreError(
                f"composite weight for {name!r} disagrees: pre-registration "
                f"{weight} vs gate_engine {coded[name]}")

    total = float(sum(declared.values()))
    if abs(total - 1.0) > 1e-9:
        raise CompositeScoreError(
            f"frozen composite weights sum to {total}, not 1.0")

    expected_order = [c["name"] for c in COMPONENT_SPECS]
    if [str(c["name"]) for c in MODULE_PREREG.composite_components] != expected_order:
        raise CompositeScoreError(
            "composite component ORDER drifted from COMPONENT_SPECS")
    return declared


def _normalize_arm(arm: str) -> str:
    a = str(arm).strip().lower()
    if a not in ("a", "b", "c"):
        raise CompositeScoreError(f"arm must be one of A/B/C, got {arm!r}")
    return a


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_monthly_rolling_metrics(
    rolling_metrics: pd.DataFrame,
    arm: Optional[str] = None,
) -> pd.DataFrame:
    """Enforce the frozen monthly rolling-metrics contract; return a clean copy.

    Raises (never repairs) on a contract violation:
      * missing required columns,
      * unparseable / duplicate / unsorted rebalance dates,
      * fewer than ``MIN_MONTHLY_ROWS`` observations,
      * a non-monthly cadence (this is what rejects the 1,760-row daily defect),
      * an arm-column that disagrees with the requested arm,
      * NaN/inf in any scored metric column.
    """
    if not isinstance(rolling_metrics, pd.DataFrame):
        raise RollingMetricsContractError(
            f"rolling_metrics must be a DataFrame, got {type(rolling_metrics).__name__}")
    if rolling_metrics.empty:
        raise RollingMetricsContractError("rolling_metrics is empty")

    missing = [c for c in REQUIRED_INPUT_COLUMNS if c not in rolling_metrics.columns]
    if missing:
        raise RollingMetricsContractError(
            f"rolling_metrics missing required column(s) {missing}; "
            f"contract requires {list(REQUIRED_INPUT_COLUMNS)}")

    out = rolling_metrics.copy()

    out["rebalance_date"] = pd.to_datetime(out["rebalance_date"], errors="coerce")
    if out["rebalance_date"].isna().any():
        raise RollingMetricsContractError("rebalance_date contains unparseable values")
    if isinstance(out["rebalance_date"].dtype, pd.DatetimeTZDtype):
        out["rebalance_date"] = out["rebalance_date"].dt.tz_localize(None)

    out = out.sort_values("rebalance_date", kind="mergesort").reset_index(drop=True)
    if out["rebalance_date"].duplicated().any():
        dupes = sorted(out.loc[out["rebalance_date"].duplicated(), "rebalance_date"]
                       .astype(str).tolist())[:5]
        raise RollingMetricsContractError(
            f"rebalance_date has duplicate rows (e.g. {dupes}); the contract is "
            f"one row per rebalance")

    n = len(out)
    if n < MIN_MONTHLY_ROWS:
        raise RollingMetricsContractError(
            f"need >= {MIN_MONTHLY_ROWS} monthly rows, got {n}; a short or "
            f"daily-frequency input cannot form the monthly composite series")

    spacing = out["rebalance_date"].diff().dt.days.dropna()
    median_spacing = float(spacing.median()) if len(spacing) else float("nan")
    if not np.isfinite(median_spacing):
        raise RollingMetricsContractError("could not infer rebalance cadence")
    if median_spacing < MIN_MONTHLY_SPACING_DAYS:
        raise NonMonthlyFrequencyError(
            f"rolling_metrics is not monthly: median rebalance spacing "
            f"{median_spacing:.1f} days < {MIN_MONTHLY_SPACING_DAYS}. The P0B defect "
            f"was a 1,760-row DAILY series — monthly is required.")
    if median_spacing > MAX_MONTHLY_SPACING_DAYS:
        raise NonMonthlyFrequencyError(
            f"rolling_metrics cadence {median_spacing:.1f} days is coarser than "
            f"monthly (max {MAX_MONTHLY_SPACING_DAYS})")

    if arm is not None:
        want = _normalize_arm(arm)
        present = {str(v).strip().lower() for v in out["arm"].dropna().unique()}
        if present != {want}:
            raise RollingMetricsContractError(
                f"arm column {sorted(present)} disagrees with requested arm {want!r}")

    for col in METRIC_COLUMNS:
        vals = pd.to_numeric(out[col], errors="coerce").to_numpy(dtype=float)
        if not np.isfinite(vals).all():
            bad = int((~np.isfinite(vals)).sum())
            raise RollingMetricsContractError(
                f"metric column {col!r} carries {bad} missing/non-finite value(s); "
                f"absent data must be fixed upstream, never silently scored as 0")

    return out


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def component_scores(
    rolling_metrics: pd.DataFrame,
    arm: Optional[str] = None,
) -> pd.DataFrame:
    """Per-period normalized component scores (columns = the 7 components)."""
    df = validate_monthly_rolling_metrics(rolling_metrics, arm=arm)
    out = pd.DataFrame({"rebalance_date": df["rebalance_date"].to_numpy()})
    for name in COMPONENT_NAMES:
        metric = COMPONENT_TO_METRIC[name]
        fn = COMPONENT_NORMALIZERS[name]
        out[name] = [float(fn(float(v))) for v in df[metric].to_numpy(dtype=float)]
    return out


def composite_series(
    rolling_metrics: pd.DataFrame,
    arm: Optional[str] = None,
    *,
    allow_static: bool = False,
) -> pd.Series:
    """Monthly composite score, indexed by ``rebalance_date``."""
    df = validate_monthly_rolling_metrics(rolling_metrics, arm=arm)
    weights = component_weights()
    scores = component_scores(df, arm=arm)
    values = np.zeros(len(scores), dtype=float)
    for name in COMPONENT_NAMES:
        values += weights[name] * scores[name].to_numpy(dtype=float)

    if not allow_static:
        spread = float(np.nanmax(values) - np.nanmin(values)) if len(values) else 0.0
        if spread == 0.0:
            raise StaticCompositeError(
                "composite_score is constant across all months — the P0B "
                "near-static defect; the input metrics carry no variation")

    return pd.Series(values, index=pd.DatetimeIndex(df["rebalance_date"]),
                     name="composite_score")


def composite_score(
    rolling_metrics: pd.DataFrame,
    arm: str,
    *,
    allow_static: bool = False,
) -> pd.DataFrame:
    """Build the monthly composite-score table for one arm.

    Returns a DataFrame with EXACTLY the columns
    ``[rebalance_date, arm, composite_score]`` and one row per input rebalance
    date (84 for the canonical 2018-2024 window).  It never expands the input to
    a daily grid.
    """
    arm_label = _normalize_arm(arm)
    series = composite_series(rolling_metrics, arm=arm_label, allow_static=allow_static)
    out = pd.DataFrame({
        "rebalance_date": series.index.to_numpy(),
        "arm": arm_label,
        "composite_score": series.to_numpy(dtype=float),
    })
    return out[list(OUTPUT_COLUMNS)]


# ---------------------------------------------------------------------------
# DSR evaluation
# ---------------------------------------------------------------------------

@dataclass
class MonthlyCompositeResult:
    """Monthly composite score + DSR evidence for one arm."""

    arm: str
    composite: pd.DataFrame
    component_scores: pd.DataFrame
    composite_dsr: float
    passed: bool
    fold_4_dsr: Optional[float]
    fold_4_passed: Optional[bool]
    n_obs: int
    n_trials: int
    track_record_days: int
    threshold: float
    fold_4_window: Tuple[Optional[str], Optional[str]]
    component_means: Dict[str, float]
    prereg_sha256: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "arm": self.arm,
            "composite_dsr": round(float(self.composite_dsr), 6),
            "passed": bool(self.passed),
            "fold_4_dsr": (round(float(self.fold_4_dsr), 6)
                           if self.fold_4_dsr is not None else None),
            "fold_4_passed": (bool(self.fold_4_passed)
                              if self.fold_4_passed is not None else None),
            "fold_4_window": {"start": self.fold_4_window[0],
                              "end": self.fold_4_window[1]},
            "n_obs": int(self.n_obs),
            "n_trials": int(self.n_trials),
            "track_record_days": int(self.track_record_days),
            "threshold": float(self.threshold),
            "component_means": {k: round(float(v), 6)
                                for k, v in self.component_means.items()},
            "composite_score_mean": round(float(self.composite["composite_score"].mean()), 6),
            "composite_score_std": round(float(self.composite["composite_score"].std(ddof=1)), 6),
            "prereg_sha256": self.prereg_sha256,
            "detail": self.detail,
        }

    def to_json(self, path: Union[str, Path]) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def _fold_4_window() -> Tuple[str, str]:
    fold = MODULE_PREREG.get("windows.primary.folds.fold_4")
    return str(fold["test_start"]), str(fold["test_end"])


def evaluate_monthly_composite(
    rolling_metrics: pd.DataFrame,
    arm: str,
    *,
    n_trials: int = DSR_N_TRIALS,
    track_record_days: int = DSR_TRACK_RECORD_DAYS,
    threshold: float = DSR_THRESHOLD,
) -> MonthlyCompositeResult:
    """Compute the monthly composite + DSR (full record and Fold-4 OOS)."""
    arm_label = _normalize_arm(arm)
    df = validate_monthly_rolling_metrics(rolling_metrics, arm=arm_label)
    comp = composite_score(df, arm_label)
    scores = component_scores(df, arm=arm_label)

    series = pd.Series(comp["composite_score"].to_numpy(dtype=float),
                       index=pd.DatetimeIndex(comp["rebalance_date"]))

    composite_dsr = float(deflated_sharpe_ratio(
        series, n_trials=n_trials, track_record_days=track_record_days))

    start, end = _fold_4_window()
    mask = (series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))
    fold = series.loc[mask]
    if len(fold) >= 10:
        fold_4_dsr: Optional[float] = float(deflated_sharpe_ratio(
            fold, n_trials=n_trials, track_record_days=len(fold)))
        fold_4_passed: Optional[bool] = bool(fold_4_dsr >= threshold)
    else:
        fold_4_dsr, fold_4_passed = None, None

    passed = bool(composite_dsr >= threshold and
                  (fold_4_dsr is None or fold_4_passed))

    return MonthlyCompositeResult(
        arm=arm_label,
        composite=comp,
        component_scores=scores,
        composite_dsr=composite_dsr,
        passed=passed,
        fold_4_dsr=fold_4_dsr,
        fold_4_passed=fold_4_passed,
        n_obs=int(len(series)),
        n_trials=int(n_trials),
        track_record_days=int(track_record_days),
        threshold=float(threshold),
        fold_4_window=(start, end),
        component_means={c: float(scores[c].mean()) for c in COMPONENT_NAMES},
        prereg_sha256=str(MODULE_PREREG.declared_sha256),
        detail={
            "module": "validation/composite_score.py",
            "component_weights": component_weights(),
            "component_metrics": dict(COMPONENT_TO_METRIC),
            "frequency": "monthly",
            "no_daily_expansion": True,
            "dsr_on": "monthly_composite_score_series",
            "prereg_integrity_verified": bool(MODULE_PREREG.integrity_verified),
        },
    )


# ---------------------------------------------------------------------------
# Artifact writer (new directory only — never arm_*/composite_score.parquet)
# ---------------------------------------------------------------------------

def write_composite_artifacts(
    result: MonthlyCompositeResult,
    out_dir: Union[str, Path] = DEFAULT_OUTPUT_DIR,
    *,
    source_path: Optional[Union[str, Path]] = None,
) -> Dict[str, Path]:
    """Persist the composite table + DSR JSON + provenance sidecar.

    Writes into ``out_dir`` (default ``data/backtests/composite_scores_20261008``)
    and returns the paths.  The P0B defect path
    ``data/backtests/arm_*/composite_score.parquet`` is deliberately never
    touched.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    arm = result.arm

    c_path = out / f"composite_score_arm_{arm}.parquet"
    result.composite.to_parquet(c_path, index=False)
    sha = hashlib.sha256(c_path.read_bytes()).hexdigest()

    now = datetime.now(timezone.utc)
    dates = pd.to_datetime(result.composite["rebalance_date"])
    sidecar: Dict[str, Any] = {
        "source": "PRODUCER_MARKER_WORKSTREAM_B_MONTHLY_COMPOSITE",
        "source_version": "validation/composite_score.py @ 1.0",
        "pit_timestamp_column": "rebalance_date",
        "entity_key": "arm_composite_score",
        "retrieval_timestamp": now.isoformat(),
        "timestamp_ns": int(now.timestamp() * 1e9),
        "execution_uuid": str(uuid.uuid4()),
        "transformations": [
            "monthly_composite_score",
            "component_normalization_v7",
            "dsr_on_monthly_composite",
        ],
        "sha256": sha,
        "row_count": int(len(result.composite)),
        "date_range": {"min": dates.min().date().isoformat(),
                       "max": dates.max().date().isoformat()},
        "mapping_confidence": "high",
        "arm": arm,
        "component_weights": component_weights(),
        "composite_dsr": round(float(result.composite_dsr), 6),
        "fold_4_dsr": (round(float(result.fold_4_dsr), 6)
                       if result.fold_4_dsr is not None else None),
        "fold_4_window": {"start": result.fold_4_window[0],
                          "end": result.fold_4_window[1]},
        "preregistration_sha256": result.prereg_sha256,
        "inputs": ({"path": str(source_path)} if source_path is not None else None),
    }
    side_path = c_path.with_suffix(".parquet.provenance.json")
    side_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")

    dsr_path = out / f"composite_dsr_arm_{arm}.json"
    result.to_json(dsr_path)

    return {"composite": c_path, "provenance": side_path, "dsr": dsr_path}
