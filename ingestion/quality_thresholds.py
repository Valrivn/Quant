"""
Probation v2 — Weighted-OR decision rule + null calibration.
(big-pickle-worker / Sprint 1, Phase 9A Validation Infrastructure)

Frozen source: ``config/pre_registration_v4.yaml`` ->
``parameter_clusters.D_probation_qual`` (low-correlation cluster, rho < 0.3):

    probation_band_low      in [0.30, 0.35, 0.40]
    probation_band_high     in [0.60, 0.65, 0.70]
    qual_weight             in [0.30, 0.40, 0.50]
    mapping_conf_threshold  in ["medium", "high"]

Decision rule (weighted OR, not an AND of gates)
------------------------------------------------
    pass_score = qual_weight * mapping_conf
               + (1 - qual_weight) * entry_score

    PASS  iff  pass_score in [probation_band_low, probation_band_high]
              AND mapping_conf >= mapping_conf_threshold   (hard admissibility)

``mapping_conf`` is the ordinal mapping confidence of the qualitative entity
mapping, scored ``high = 1.0, medium = 0.5, low = 0.0``.  ``low`` is REJECTED by
construction: the pre-registered threshold grid admits only ``medium`` and
``high``, so a low-confidence mapping can never reach a PASS regardless of how
strong the quantitative entry score is.  That is the whole point of keeping the
threshold separate from the weighted blend.

Band semantics
--------------
The WRITTEN band is the pre-registered ``[0.38, 0.62]`` and it is what the gate
uses (inclusive on both edges).  The dispatch annotates it "effective ~
[0.40, 0.60]"; that tighter band is available as a *diagnostic only* via
``band_mode="effective"`` and is never substituted for the frozen value.

Null calibration — an honest note on the 50% target
--------------------------------------------------
``calibrate_null`` measures the pass rate of this rule on i.i.d. null
portfolios.  The measured rate is NOT a free parameter: it is fully determined
by ``(qual_weight, mapping_conf_threshold, null mapping-confidence mix)``:

    P(pass | level) = clip((high - w*m)/(1-w), 0, 1) - clip((low - w*m)/(1-w), 0, 1)

Because the band ``[0.38, 0.62]`` is not centred on the null distribution of the
blend, a null pass rate of ~0.50 is reachable only at particular grid points:

    w=0.4, threshold="medium":  high 0.45833 | medium 0.50000 | low rejected (band [0.38, 0.62])
    w=0.3, threshold="medium":  high 0.47619 | medium 0.42857 | low rejected (band [0.38, 0.62])
    w=0.5, threshold="medium":  high 0.30000 | medium 0.60000 | low rejected (band [0.38, 0.62])

So an all-medium null at ``w=0.4`` with band [0.38, 0.62] hits exactly 0.50, while a 50/30/20
high/medium/low null at the same grid point gives 0.3583.  The dispatch's
"pass rate ~50% on null" is therefore satisfiable but NOT structural: it holds
only for a null whose mapping-confidence composition is (near) all-medium, or
for ``w=0.5`` with an all-medium null.  This module reports the measured rate
and its analytic counterpart and flags the target as structurally unreachable
for a given mix rather than tuning the band or the weight to hit it.

CLI
---
    python -m ingestion.quality_thresholds --calibrate --n 10000
    python -m ingestion.quality_thresholds --feasibility
    python -m ingestion.quality_thresholds --test
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Frozen constants — pre_registration_v5.yaml, parameter_clusters.D_probation_qual
# ---------------------------------------------------------------------------
# Mirrored from the FROZEN artifact (run_tests() asserts the mirror against it
# on every invocation, so drift between this module and the YAML is an error,
# not an assumption).  What v5 changed against v4, and why it matters:
#
#   qual_weight:  [0.4]  ->  [0.0]   "FROZEN at 0.0 - Arm B/C retired per CEO
#                                      ruling 2026-09-27"
#   mapping_conf_threshold: ["medium","high"] -> ["high"]  "Only high-confidence
#                                      qual mappings (unused, Arm B retired)"
#
# CONSEQUENCE, stated plainly because it changes what the rule IS: with
# ``qual_weight = 0`` the "weighted OR" collapses to
#
#       pass_score = entry_score
#
# and with the threshold grid admitting only ``high``, ``low`` AND ``medium``
# rows are inadmissible.  The rule that remains is a CONJUNCTION — a high-
# confidence mapping whose entry score lands inside a two-sided band — which is
# exactly what the CEO ruling asked for.  Any claim that this module still
# implements a weighted OR is stale, and so is any test that asserts one.
#
# CONSEQUENCE 2, reported rather than papered over: a null calibration that
# expects a ~50% pass rate is UNREACHABLE on this grid.  With ``medium`` rows
# inadmissible, an all-medium null portfolio has an analytic pass rate of
# exactly 0.  ``feasibility_report()`` therefore sweeps the real grid and
# reports zero reachable cells; widening the grid to manufacture a reachable
# cell would be re-introducing the very parameter the CEO retired.

PROBATION_BAND_LOW: float = 0.35
PROBATION_BAND_HIGH: float = 0.65

#: The narrowed diagnostic band — the real strictness dial, reported alongside
#: the pre-registered written band.
EFFECTIVE_BAND_LOW: float = 0.40
EFFECTIVE_BAND_HIGH: float = 0.60

#: Cluster D pre-registered grids (frozen values; see the note above).
QUAL_WEIGHT_GRID: Tuple[float, ...] = (0.0,)
BAND_LOW_GRID: Tuple[float, ...] = (0.35, 0.38)
BAND_HIGH_GRID: Tuple[float, ...] = (0.62, 0.65)
MAPPING_CONF_THRESHOLD_GRID: Tuple[str, ...] = ("high",)

#: Ordinal mapping-confidence scale.
MAPPING_CONF_LEVELS: Tuple[str, ...] = ("low", "medium", "high")
MAPPING_CONF_NUMERIC: Dict[str, float] = {"low": 0.0, "medium": 0.5, "high": 1.0}
MAPPING_CONF_RANK: Dict[str, int] = {lvl: i for i, lvl in enumerate(MAPPING_CONF_LEVELS)}
#: Levels that can never be used: the frozen threshold grid admits ``high`` only,
#: so both ``low`` and ``medium`` are inadmissible under every legal config.
REJECTED_MAPPING_CONF: Tuple[str, ...] = ("low", "medium")

#: Null-portfolio mapping-confidence compositions.  The pre-registration does
#: NOT define a null mix — it is an assumption of the calibration, so it is an
#: explicit, named, reportable input rather than a hidden constant.
NULL_CONF_MIXES: Dict[str, Dict[str, float]] = {
    # Degenerate: every null mapping is medium confidence.
    "all_medium": {"low": 0.0, "medium": 1.0, "high": 0.0},
    # Defensible prior for a heuristic entity-mapper on a null universe.
    "declared_heuristic": {"low": 0.20, "medium": 0.30, "high": 0.50},
    # Level-symmetric.
    "uniform_levels": {"low": 1.0 / 3.0, "medium": 1.0 / 3.0, "high": 1.0 / 3.0},
}
DEFAULT_NULL_MIX: str = "declared_heuristic"

TARGET_NULL_PASS_RATE: float = 0.50
TARGET_NULL_TOLERANCE: float = 0.05
DEFAULT_SEED: int = 20260926
N_NULL_DEFAULT: int = 10_000

_BAND_EPS = 1e-12


class ProbationConfigError(ValueError):
    """Raised when a ProbationConfig field is outside the frozen Cluster-D grid."""


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbationDecision:
    """One probation decision under the weighted-OR rule."""

    entry_score: float
    mapping_confidence: str
    pass_score: float
    passed: bool
    qual_weight: float
    band_low: float
    band_high: float
    mapping_confidence_numeric: float
    mapping_confidence_admissible: bool
    mapping_confidence_threshold: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_score": round(float(self.entry_score), 6),
            "mapping_confidence": self.mapping_confidence,
            "mapping_confidence_numeric": self.mapping_confidence_numeric,
            "mapping_confidence_threshold": self.mapping_confidence_threshold,
            "mapping_confidence_admissible": self.mapping_confidence_admissible,
            "pass_score": round(float(self.pass_score), 6),
            "passed": bool(self.passed),
            "qual_weight": self.qual_weight,
            "band": [self.band_low, self.band_high],
            "reason": self.reason,
        }


def _normalise_confidence(value: Any) -> str:
    """Accept a level name or a numeric 0..1 confidence; return the level."""
    if isinstance(value, str):
        lvl = value.strip().lower()
        if lvl in MAPPING_CONF_NUMERIC:
            return lvl
        raise ProbationConfigError(
            f"unknown mapping confidence {value!r}; expected one of "
            f"{list(MAPPING_CONF_LEVELS)} (or a number in [0, 1])")
    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool):
        v = float(value)
        if not (0.0 <= v <= 1.0):
            raise ProbationConfigError(f"numeric mapping confidence {v} outside [0, 1]")
        # Nearest declared level; ties resolve DOWN (conservative).
        best = min(MAPPING_CONF_LEVELS, key=lambda l: (abs(MAPPING_CONF_NUMERIC[l] - v),
                                                      MAPPING_CONF_RANK[l]))
        return best
    raise ProbationConfigError(
        f"mapping confidence must be a level name or a number, got "
        f"{type(value).__name__}")


# ---------------------------------------------------------------------------
# ProbationConfig
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProbationConfig:
    """
    Frozen probation configuration + the weighted-OR decision rule.

    Every field is validated against the Cluster-D pre-registered grid in
    ``__post_init__``; an off-grid value raises instead of silently working, so
    an out-of-spec run cannot be mistaken for a compliant one.
    """

    qual_weight: float = 0.0
    probation_band_low: float = PROBATION_BAND_LOW
    probation_band_high: float = PROBATION_BAND_HIGH
    mapping_conf_threshold: str = "high"
    band_mode: str = "written"
    enforce_grid: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "qual_weight", float(self.qual_weight))
        object.__setattr__(self, "probation_band_low", float(self.probation_band_low))
        object.__setattr__(self, "probation_band_high", float(self.probation_band_high))
        thr = str(self.mapping_conf_threshold).strip().lower()
        object.__setattr__(self, "mapping_conf_threshold", thr)

        if not (0.0 <= self.qual_weight <= 1.0):
            raise ProbationConfigError(
                f"qual_weight {self.qual_weight} outside [0, 1]")
        if not (0.0 <= self.probation_band_low < self.probation_band_high <= 1.0):
            raise ProbationConfigError(
                f"invalid probation band [{self.probation_band_low}, "
                f"{self.probation_band_high}]")
        if self.band_mode not in ("written", "effective"):
            raise ProbationConfigError(
                f"band_mode must be 'written' or 'effective', got {self.band_mode!r}")
        if thr not in MAPPING_CONF_THRESHOLD_GRID:
            raise ProbationConfigError(
                f"mapping_conf_threshold {thr!r} is not in the pre-registered "
                f"grid {list(MAPPING_CONF_THRESHOLD_GRID)}")
        if self.enforce_grid:
            self._assert_on_grid()

    def _assert_on_grid(self) -> None:
        if not any(abs(self.qual_weight - g) <= 1e-9 for g in QUAL_WEIGHT_GRID):
            raise ProbationConfigError(
                f"qual_weight {self.qual_weight} is off the pre-registered "
                f"Cluster-D grid {list(QUAL_WEIGHT_GRID)}")
        if not any(abs(self.probation_band_low - g) <= 1e-9 for g in BAND_LOW_GRID):
            raise ProbationConfigError(
                f"probation_band_low {self.probation_band_low} is off the "
                f"pre-registered grid {list(BAND_LOW_GRID)}")
        if not any(abs(self.probation_band_high - g) <= 1e-9 for g in BAND_HIGH_GRID):
            raise ProbationConfigError(
                f"probation_band_high {self.probation_band_high} is off the "
                f"pre-registered grid {list(BAND_HIGH_GRID)}")

    # -- band ---------------------------------------------------------------

    @property
    def band(self) -> Tuple[float, float]:
        return (self.probation_band_low, self.probation_band_high)

    @property
    def effective_band(self) -> Tuple[float, float]:
        return (EFFECTIVE_BAND_LOW, EFFECTIVE_BAND_HIGH)

    def active_band(self, band_mode: Optional[str] = None) -> Tuple[float, float]:
        mode = band_mode or self.band_mode
        return self.band if mode == "written" else self.effective_band

    # -- admissibility ------------------------------------------------------

    def is_admissible(self, mapping_confidence: Any) -> bool:
        """True when the mapping confidence clears the pre-registered floor."""
        lvl = _normalise_confidence(mapping_confidence)
        return (MAPPING_CONF_RANK[lvl] >= MAPPING_CONF_RANK[self.mapping_conf_threshold]
                and lvl not in REJECTED_MAPPING_CONF)

    @staticmethod
    def confidence_numeric(mapping_confidence: Any) -> float:
        return MAPPING_CONF_NUMERIC[_normalise_confidence(mapping_confidence)]

    # -- the weighted OR ----------------------------------------------------

    def pass_score(self, entry_score: float, mapping_confidence: Any) -> float:
        """
        ``qual_weight * mapping_conf + (1 - qual_weight) * entry_score``.

        Under the FROZEN v5 grid ``qual_weight = 0.0``, so this is exactly the
        entry score and the blend term is inert.  The formula is kept (rather
        than deleted) because ``qual_weight`` is a declared grid parameter and
        the arithmetic is what makes that collapse visible and testable.

        Computed even for inadmissible rows (so the score is inspectable) but
        never sufficient on its own — see :meth:`decide`.
        """
        e = float(entry_score)
        if not math.isfinite(e):
            raise ProbationConfigError(f"entry_score must be finite, got {entry_score!r}")
        if not (0.0 <= e <= 1.0):
            raise ProbationConfigError(f"entry_score {e} outside [0, 1]")
        m = self.confidence_numeric(mapping_confidence)
        return self.qual_weight * m + (1.0 - self.qual_weight) * e

    def decide(self, entry_score: float, mapping_confidence: Any,
               band_mode: Optional[str] = None) -> ProbationDecision:
        """Evaluate one row.  PASS requires admissibility AND band containment."""
        lvl = _normalise_confidence(mapping_confidence)
        m = MAPPING_CONF_NUMERIC[lvl]
        admissible = self.is_admissible(lvl)
        score = self.pass_score(entry_score, lvl)
        lo, hi = self.active_band(band_mode)

        if not admissible:
            reason = (f"mapping_confidence_{lvl}_below_threshold_"
                      f"{self.mapping_conf_threshold}")
        elif score < lo - _BAND_EPS:
            reason = "pass_score_below_band"
        elif score > hi + _BAND_EPS:
            reason = "pass_score_above_band"
        else:
            reason = "in_band"
        passed = bool(admissible and (lo - _BAND_EPS) <= score <= (hi + _BAND_EPS))

        return ProbationDecision(
            entry_score=float(entry_score),
            mapping_confidence=lvl,
            pass_score=float(score),
            passed=passed,
            qual_weight=self.qual_weight,
            band_low=float(lo),
            band_high=float(hi),
            mapping_confidence_numeric=float(m),
            mapping_confidence_admissible=bool(admissible),
            mapping_confidence_threshold=self.mapping_conf_threshold,
            reason=reason,
        )

    # -- vectorised ---------------------------------------------------------

    def pass_scores(self, entry_scores: np.ndarray,
                    mapping_confidences: Sequence[Any]) -> np.ndarray:
        """Vectorised :meth:`pass_score` for arrays of entry scores."""
        e = np.asarray(entry_scores, dtype=float)
        m = np.asarray([self.confidence_numeric(c) for c in mapping_confidences],
                       dtype=float)
        if e.shape != m.shape:
            raise ProbationConfigError(
                f"entry_scores shape {e.shape} != mapping_confidences shape {m.shape}")
        return self.qual_weight * m + (1.0 - self.qual_weight) * e

    def evaluate(self, entry_scores: Sequence[float],
                 mapping_confidences: Sequence[Any],
                 band_mode: Optional[str] = None
                 ) -> Dict[str, Any]:
        """
        Vectorised evaluation of many rows.

        Returns the pass rate plus the per-level and reason breakdowns that the
        composite ``probation_pass_rate`` component and the null calibration
        consume.
        """
        e = np.asarray(entry_scores, dtype=float).ravel()
        levels = [_normalise_confidence(c) for c in mapping_confidences]
        if len(levels) != e.size:
            raise ProbationConfigError(
                f"entry_scores ({e.size}) and mapping_confidences ({len(levels)}) "
                f"length mismatch")
        if e.size == 0:
            return {"n": 0, "n_passed": 0, "pass_rate": None,
                    "per_level": {}, "per_reason": {}, "band": list(self.active_band(band_mode))}
        if not np.all(np.isfinite(e)):
            raise ProbationConfigError("entry_scores contain non-finite values")
        if e.min() < 0.0 or e.max() > 1.0:
            raise ProbationConfigError(
                f"entry_scores outside [0, 1] (min={e.min()}, max={e.max()})")

        m = np.asarray([MAPPING_CONF_NUMERIC[l] for l in levels], dtype=float)
        scores = self.qual_weight * m + (1.0 - self.qual_weight) * e
        lo, hi = self.active_band(band_mode)
        adm = np.asarray([self.is_admissible(l) for l in levels], dtype=bool)
        passed = adm & (scores >= lo - _BAND_EPS) & (scores <= hi + _BAND_EPS)

        per_level: Dict[str, Dict[str, Any]] = {}
        for lvl in MAPPING_CONF_LEVELS:
            sel = np.asarray([l == lvl for l in levels], dtype=bool)
            cnt = int(sel.sum())
            per_level[lvl] = {
                "n": cnt,
                "n_passed": int(passed[sel].sum()) if cnt else 0,
                "pass_rate": (float(passed[sel].mean()) if cnt else None),
                "admissible": self.is_admissible(lvl),
                "mean_pass_score": (float(scores[sel].mean()) if cnt else None),
            }
        reasons: Dict[str, int] = {}
        for i, lvl in enumerate(levels):
            r = self.decide(float(e[i]), lvl, band_mode=band_mode).reason
            reasons[r] = reasons.get(r, 0) + 1

        return {
            "n": int(e.size),
            "n_passed": int(passed.sum()),
            "pass_rate": float(passed.mean()),
            "band": [lo, hi],
            "band_mode": band_mode or self.band_mode,
            "qual_weight": self.qual_weight,
            "mapping_conf_threshold": self.mapping_conf_threshold,
            "per_level": per_level,
            "per_reason": reasons,
            "mean_pass_score": float(scores.mean()),
        }

    # -- construction from the frozen config --------------------------------

    @classmethod
    def from_prereg(cls, prereg: Any = None, qual_weight: float = 0.0,
                    probation_band_low: float = PROBATION_BAND_LOW,
                    probation_band_high: float = PROBATION_BAND_HIGH,
                    mapping_conf_threshold: str = "high",
                    band_mode: str = "written") -> "ProbationConfig":
        """
        Build from the frozen pre-registration's Cluster-D grids.

        When ``prereg`` is supplied the requested values are checked against the
        declared grids read from the YAML, so a drift between this module and
        the frozen artifact is detected instead of assumed away.
        """
        if prereg is not None:
            lows = [float(v) for v in prereg.cluster_d_values("probation_band_low")]
            highs = [float(v) for v in prereg.cluster_d_values("probation_band_high")]
            weights = [float(v) for v in prereg.cluster_d_values("qual_weight")]
            thresholds = [str(v).strip().lower()
                          for v in prereg.cluster_d_values("mapping_conf_threshold")]
            for label, val, grid in (("probation_band_low", probation_band_low, lows),
                                     ("probation_band_high", probation_band_high, highs),
                                     ("qual_weight", qual_weight, weights)):
                if not any(abs(float(val) - g) <= 1e-9 for g in grid):
                    raise ProbationConfigError(
                        f"{label}={val} not in pre-registered Cluster-D grid {grid}")
            if str(mapping_conf_threshold).strip().lower() not in thresholds:
                raise ProbationConfigError(
                    f"mapping_conf_threshold={mapping_conf_threshold!r} not in "
                    f"pre-registered Cluster-D grid {thresholds}")
        return cls(qual_weight=qual_weight,
                   probation_band_low=probation_band_low,
                   probation_band_high=probation_band_high,
                   mapping_conf_threshold=mapping_conf_threshold,
                   band_mode=band_mode)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "qual_weight": self.qual_weight,
            "probation_band": [self.probation_band_low, self.probation_band_high],
            "effective_band_diagnostic": list(self.effective_band),
            "band_mode": self.band_mode,
            "mapping_conf_threshold": self.mapping_conf_threshold,
            "mapping_conf_numeric": dict(MAPPING_CONF_NUMERIC),
            "rejected_mapping_confidence": list(REJECTED_MAPPING_CONF),
            "decision_rule": ("pass_score = qual_weight*mapping_conf + "
                              "(1-qual_weight)*entry_score; PASS iff pass_score in "
                              "band AND mapping_conf >= mapping_conf_threshold"),
            "pre_registered_grids": {
                "qual_weight": list(QUAL_WEIGHT_GRID),
                "probation_band_low": list(BAND_LOW_GRID),
                "probation_band_high": list(BAND_HIGH_GRID),
                "mapping_conf_threshold": list(MAPPING_CONF_THRESHOLD_GRID),
            },
        }


# ---------------------------------------------------------------------------
# Null calibration
# ---------------------------------------------------------------------------

def analytic_pass_rate(qual_weight: float, mapping_conf_level: str,
                       band: Tuple[float, float] = (PROBATION_BAND_LOW,
                                                    PROBATION_BAND_HIGH)) -> float:
    """
    Exact P(pass | level) for an i.i.d. UNIFORM[0, 1] null entry score.

        pass_score = w*m + (1-w)*e  in [lo, hi]
        =>  e in [(lo - w*m)/(1-w), (hi - w*m)/(1-w)]  (clipped to [0, 1])
    """
    m = MAPPING_CONF_NUMERIC[mapping_conf_level]
    lo, hi = float(band[0]), float(band[1])
    if qual_weight >= 1.0:
        return 1.0 if (lo - _BAND_EPS) <= m <= (hi + _BAND_EPS) else 0.0
    scale = 1.0 - qual_weight
    a = (lo - qual_weight * m) / scale
    b = (hi - qual_weight * m) / scale
    return float(max(0.0, min(1.0, b) - max(0.0, min(1.0, a))))


def null_confidence_levels(rng: np.random.Generator, n: int,
                           mix: str = DEFAULT_NULL_MIX) -> np.ndarray:
    """Draw ``n`` mapping-confidence levels from a named null mix."""
    if mix not in NULL_CONF_MIXES:
        raise ProbationConfigError(
            f"unknown null mix {mix!r}; expected one of {list(NULL_CONF_MIXES)}")
    probs = NULL_CONF_MIXES[mix]
    levels = list(MAPPING_CONF_LEVELS)
    p = np.asarray([float(probs[l]) for l in levels], dtype=float)
    total = p.sum()
    if total <= 0:
        raise ProbationConfigError(f"null mix {mix!r} has zero total probability")
    p = p / total
    idx = rng.choice(len(levels), size=n, p=p)
    return np.asarray(levels, dtype=object)[idx]


def calibrate_null(n: int = N_NULL_DEFAULT, seed: int = DEFAULT_SEED,
                   qual_weight: float = 0.0,
                   mapping_conf_threshold: str = "high",
                   band: Tuple[float, float] = (PROBATION_BAND_LOW,
                                                PROBATION_BAND_HIGH),
                   band_mode: str = "written",
                   null_mix: str = DEFAULT_NULL_MIX,
                   target: float = TARGET_NULL_PASS_RATE,
                   tolerance: float = TARGET_NULL_TOLERANCE,
                   n_folds: int = 0) -> Dict[str, Any]:
    """
    Measure the weighted-OR pass rate on ``n`` i.i.d. null portfolios.

    Null model (explicit, because the pre-registration does not define one):
        entry_score          ~ U[0, 1]  (no skill)
        mapping_confidence   ~ ``null_mix`` over {low, medium, high}

    The report carries the empirical rate, the exact analytic rate, the
    per-level breakdown and a verdict that distinguishes "off target" from
    "target not attainable for this configuration".
    """
    cfg = ProbationConfig(qual_weight=qual_weight,
                          probation_band_low=band[0], probation_band_high=band[1],
                          mapping_conf_threshold=mapping_conf_threshold,
                          band_mode=band_mode)
    n = int(n)
    if n <= 0:
        raise ValueError(f"n must be positive, got {n}")
    rng = np.random.default_rng(seed)
    entries = rng.random(n)
    levels = null_confidence_levels(rng, n, mix=null_mix)
    out = cfg.evaluate(entries, levels)

    per_level_analytic = {
        lvl: analytic_pass_rate(cfg.qual_weight, lvl, cfg.active_band(band_mode))
        for lvl in MAPPING_CONF_LEVELS
    }
    admissible_levels = [l for l in MAPPING_CONF_LEVELS if cfg.is_admissible(l)]
    expected = float(sum(NULL_CONF_MIXES[null_mix][l] * per_level_analytic[l]
                         for l in admissible_levels))
    # Every admissible level can contribute at most its own analytic rate.
    attainable_max = float(max((per_level_analytic[l] for l in admissible_levels),
                               default=0.0))
    attainable_min = 0.0   # an all-low null is never admissible => 0

    rate = float(out["pass_rate"])
    within = abs(rate - target) <= tolerance
    attainable = attainable_min - _BAND_EPS <= target <= attainable_max + _BAND_EPS
    if within:
        verdict = "within_target"
    elif attainable:
        verdict = "off_target_reachable"
    else:
        verdict = "target_not_attainable_for_config"

    report: Dict[str, Any] = {
        "report_type": "probation_v2_null_calibration",
        "n_null": n,
        "seed": int(seed),
        "null_model": {
            "entry_score": "Uniform[0,1] i.i.d. (no skill)",
            "mapping_confidence": dict(NULL_CONF_MIXES[null_mix]),
            "null_mix_name": null_mix,
        },
        "config": cfg.to_dict(),
        "empirical_pass_rate": round(rate, 6),
        "analytic_pass_rate": round(expected, 6),
        "analytic_abs_error": round(abs(rate - expected), 6),
        "per_level_empirical": {k: (None if v["pass_rate"] is None else
                                    round(v["pass_rate"], 6))
                                for k, v in out["per_level"].items()},
        "per_level_analytic": {k: round(v, 6) for k, v in per_level_analytic.items()},
        "admissible_levels": admissible_levels,
        "target_pass_rate": float(target),
        "target_tolerance": float(tolerance),
        "target_attainable_range": [attainable_min, round(attainable_max, 6)],
        "verdict": verdict,
        "within_target": bool(within),
        "note": ("the null pass rate is determined by (qual_weight, "
                 "mapping_conf_threshold, null mapping-confidence mix); it is not "
                 "tunable by the band without violating the frozen pre-registration"),
    }
    if n_folds and n_folds > 0:
        report["gate_8_probation_component"] = {
            "threshold_low": band[0],
            "threshold_high": band[1],
            "null_pass_rate": round(rate, 6),
            "inside_written_band": bool(band[0] - _BAND_EPS <= rate <= band[1] + _BAND_EPS),
        }
    return report


def feasibility_report(band: Tuple[float, float] = (PROBATION_BAND_LOW,
                                                    PROBATION_BAND_HIGH)
                       ) -> Dict[str, Any]:
    """
    Sweep the whole pre-registered (qual_weight x mapping_conf_threshold x
    null-mix) grid and report which cells can reach a 0.50 null pass rate.

    This is the falsification-first artefact for the dispatch's "pass rate ~50%
    on null" criterion: it shows the criterion is grid-dependent rather than a
    property of the rule.
    """
    cells: List[Dict[str, Any]] = []
    for mix_name in NULL_CONF_MIXES:
        mix = NULL_CONF_MIXES[mix_name]
        for w in QUAL_WEIGHT_GRID:
            for thr in MAPPING_CONF_THRESHOLD_GRID:
                adm = [l for l in MAPPING_CONF_LEVELS
                       if MAPPING_CONF_RANK[l] >= MAPPING_CONF_RANK[thr]
                       and l not in REJECTED_MAPPING_CONF]
                per_level = {l: analytic_pass_rate(w, l, band) for l in adm}
                rate = float(sum(mix[l] * per_level[l] for l in adm))
                cells.append({
                    "null_mix": mix_name,
                    "qual_weight": w,
                    "mapping_conf_threshold": thr,
                    "admissible_levels": adm,
                    "per_level_analytic": {k: round(v, 6) for k, v in per_level.items()},
                    "analytic_pass_rate": round(rate, 6),
                    "reaches_0.50": bool(abs(rate - TARGET_NULL_PASS_RATE)
                                         <= TARGET_NULL_TOLERANCE),
                })
    hits = [c for c in cells if c["reaches_0.50"]]
    return {
        "report_type": "probation_v2_feasibility",
        "band": list(band),
        "target_pass_rate": TARGET_NULL_PASS_RATE,
        "target_tolerance": TARGET_NULL_TOLERANCE,
        "n_cells": len(cells),
        "n_reaching_target": len(hits),
        "cells": cells,
        "verdict": ("target reachable only on a subset of the frozen grid"
                    if hits else "target unreachable across the entire frozen grid"),
        "note": ("a null pass rate of 0.50 requires an all-medium (or otherwise "
                 "medium-dominated) null mapping-confidence mix; a level-symmetric "
                 "or high-dominated null lands near 0.30-0.43"),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_tests() -> int:
    """
    Deterministic self-test for the FROZEN v5 Cluster-D grid.

    Two things are checked beyond the arithmetic, because both were the source
    of a stale claim in the previous revision:

      1. the mirrored grids still equal the ones in
         ``config/pre_registration_v5.yaml`` (drift is an error, not a
         convenience);
      2. the ~50% null pass-rate criterion is reported as UNREACHABLE on this
         grid, rather than quietly satisfied by widening it.
    """
    print("Probation v2 (frozen v5 Cluster-D) self-test")
    print("=" * 66)

    # 0) the mirror must match the frozen artifact
    try:
        from ingestion.harness.prereg import MODULE_PREREG as _PRE
    except Exception:                                          # noqa: BLE001
        _PRE = None
    if _PRE is not None and _PRE.integrity_verified:
        assert tuple(float(v) for v in _PRE.cluster_d_values("qual_weight")) == \
            QUAL_WEIGHT_GRID, (QUAL_WEIGHT_GRID, _PRE.cluster_d_values("qual_weight"))
        assert tuple(float(v) for v in _PRE.cluster_d_values("probation_band_low")) == \
            BAND_LOW_GRID
        assert tuple(float(v) for v in _PRE.cluster_d_values("probation_band_high")) == \
            BAND_HIGH_GRID
        assert tuple(str(v).strip().lower() for v in
                     _PRE.cluster_d_values("mapping_conf_threshold")) == \
            MAPPING_CONF_THRESHOLD_GRID
        print(f"[OK] mirrored grids match the frozen pre-registration "
              f"({_PRE.integrity.actual_sha256[:12]}...)")
    else:
        print("[WARN] frozen pre-registration not verified — mirror not cross-checked")

    cfg = ProbationConfig()
    assert cfg.qual_weight == 0.0, cfg.qual_weight
    assert cfg.band == (0.35, 0.65), cfg.band
    assert cfg.mapping_conf_threshold == "high"

    # 1) the blend term is INERT at the frozen qual_weight: pass_score == entry
    for lvl in MAPPING_CONF_LEVELS:
        assert cfg.pass_score(0.73, lvl) == 0.73, lvl
    print("[OK] qual_weight=0.0 collapses pass_score to the entry score "
          "(Arm B/C retired)")

    # 2) admissibility: only `high` clears the frozen floor
    assert cfg.is_admissible("high") is True
    assert cfg.is_admissible("medium") is False
    assert cfg.is_admissible("low") is False
    d_low = cfg.decide(0.50, "low")
    assert not d_low.passed and d_low.mapping_confidence_admissible is False
    assert d_low.reason == "mapping_confidence_low_below_threshold_high", d_low.reason
    d_med = cfg.decide(0.50, "medium")
    assert not d_med.passed, d_med
    assert d_med.reason == "mapping_confidence_medium_below_threshold_high"
    print("[OK] admissibility is a hard conjunction (low AND medium rejected)")

    # 3) in-band high-confidence row passes
    d_ok = cfg.decide(0.50, "high")
    assert d_ok.passed is True and d_ok.reason == "in_band", d_ok
    print(f"[OK] high/0.50 -> score={d_ok.pass_score:.4f} PASS (band {cfg.band})")

    # 4) the band is TWO-SIDED: a too-strong signal is not selective enough
    d_hot = cfg.decide(0.90, "high")
    assert not d_hot.passed and d_hot.reason == "pass_score_above_band", d_hot
    d_cold = cfg.decide(0.0, "high")
    assert not d_cold.passed and d_cold.reason == "pass_score_below_band", d_cold
    print("[OK] band is two-sided (both extremes fail)")

    # 5) band edges are inclusive
    lo_edge, hi_edge = cfg.band
    assert cfg.decide(lo_edge, "high").passed, "lower band edge must pass"
    assert cfg.decide(hi_edge, "high").passed, "upper band edge must pass"
    assert not cfg.decide(lo_edge - 1e-6, "high").passed
    assert not cfg.decide(hi_edge + 1e-6, "high").passed
    assert cfg.active_band("effective") == (EFFECTIVE_BAND_LOW, EFFECTIVE_BAND_HIGH)
    print("[OK] band edges inclusive; effective band is the tighter diagnostic")

    # 6) out-of-range entry scores are rejected, not clamped
    for bad in (-0.1, 1.5, float("nan"), float("inf")):
        try:
            cfg.decide(bad, "high")
        except ProbationConfigError:
            pass
        else:
            raise AssertionError(f"out-of-range entry_score {bad} accepted")
    print("[OK] out-of-range entry_score raises")

    # 7) off-grid configuration is rejected
    for bad in (0.4, 0.3, 0.5, 1.0):
        try:
            ProbationConfig(qual_weight=bad)
        except ProbationConfigError:
            pass
        else:
            raise AssertionError(f"off-grid qual_weight {bad} accepted")
    for bad in ("low", "medium"):
        try:
            ProbationConfig(mapping_conf_threshold=bad)
        except ProbationConfigError:
            pass
        else:
            raise AssertionError(f"mapping_conf_threshold={bad!r} accepted")
    print("[OK] Cluster-D grid enforced (qual_weight (0.0,), threshold ('high',))")

    # 8) analytic pass rate: exact for a high-confidence uniform null
    assert abs(analytic_pass_rate(0.0, "high", (0.35, 0.65)) - 0.30) < 1e-9
    assert abs(analytic_pass_rate(0.0, "high", (0.40, 0.60)) - 0.20) < 1e-9
    print("[OK] analytic pass rate exact: 0.30 written band, 0.20 effective band")

    # 9) null calibration on the frozen grid
    rep = calibrate_null(n=20_000, seed=11, null_mix="all_medium")
    assert rep["admissible_levels"] == ["high"], rep["admissible_levels"]
    assert rep["analytic_pass_rate"] == 0.0, rep["analytic_pass_rate"]
    assert rep["empirical_pass_rate"] == 0.0, rep["empirical_pass_rate"]
    assert rep["verdict"] == "target_not_attainable_for_config", rep["verdict"]
    assert rep["within_target"] is False
    print(f"[OK] all-medium null: empirical={rep['empirical_pass_rate']:.4f} "
          f"analytic={rep['analytic_pass_rate']:.4f} — 0.50 is UNREACHABLE on "
          f"the frozen grid (medium rows are inadmissible)")

    rep2 = calibrate_null(n=20_000, seed=11, null_mix=DEFAULT_NULL_MIX)
    assert rep2["analytic_abs_error"] < 0.02, rep2["analytic_abs_error"]
    print(f"[OK] {DEFAULT_NULL_MIX} null: empirical="
          f"{rep2['empirical_pass_rate']:.4f} analytic="
          f"{rep2['analytic_pass_rate']:.4f} (0.5 mass on high x 0.30)")

    feas = feasibility_report()
    assert feas["n_cells"] == len(NULL_CONF_MIXES) * len(QUAL_WEIGHT_GRID) * \
        len(MAPPING_CONF_THRESHOLD_GRID), feas["n_cells"]
    assert feas["n_reaching_target"] == 0, feas["n_reaching_target"]
    print(f"[OK] feasibility sweep: {feas['n_reaching_target']}/{feas['n_cells']} "
          f"cells reach 0.50 - {feas['verdict']}")
    print("     (a 50% null pass rate needs a medium-dominated null; medium is "
          "inadmissible on the frozen grid)")
    print("\nAll Probation v2 tests passed.")
    return 0
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probation v2 weighted-OR: decision rule + null calibration")
    parser.add_argument("--calibrate", action="store_true",
                        help="run the null calibration and print the report")
    parser.add_argument("--n", type=int, default=N_NULL_DEFAULT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--qual-weight", type=float, default=0.0,
                        help=f"Cluster-D grid {list(QUAL_WEIGHT_GRID)}")
    parser.add_argument("--mapping-conf-threshold", type=str, default="high",
                        help=f"Cluster-D grid {list(MAPPING_CONF_THRESHOLD_GRID)}")
    parser.add_argument("--band-low", type=float, default=PROBATION_BAND_LOW)
    parser.add_argument("--band-high", type=float, default=PROBATION_BAND_HIGH)
    parser.add_argument("--band-mode", type=str, default="written",
                        choices=["written", "effective"])
    parser.add_argument("--null-mix", type=str, default=DEFAULT_NULL_MIX,
                        help=f"named null mix {list(NULL_CONF_MIXES)}")
    parser.add_argument("--feasibility", action="store_true",
                        help="sweep the frozen grid for target attainability")
    parser.add_argument("--report", type=str, default=None,
                        help="write the JSON report to this path")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args(argv)

    if args.test:
        return run_tests()

    if args.feasibility:
        rep = feasibility_report(band=(args.band_low, args.band_high))
        print(json.dumps({k: v for k, v in rep.items() if k != "cells"},
                         indent=2, default=str))
        for c in rep["cells"]:
            flag = "  <== 0.50" if c["reaches_0.50"] else ""
            print(f"  mix={c['null_mix']:18s} w={c['qual_weight']} "
                  f"thr={c['mapping_conf_threshold']:6s} "
                  f"rate={c['analytic_pass_rate']:.4f}{flag}")
        if args.report:
            _write(rep, args.report)
        return 0

    if args.calibrate:
        rep = calibrate_null(n=args.n, seed=args.seed,
                             qual_weight=args.qual_weight,
                             mapping_conf_threshold=args.mapping_conf_threshold,
                             band=(args.band_low, args.band_high),
                             band_mode=args.band_mode,
                             null_mix=args.null_mix)
        print(json.dumps(rep, indent=2, default=str))
        if args.report:
            _write(rep, args.report)
        return 0

    parser.print_help()
    return 1


def _write(rep: Dict[str, Any], path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rep, indent=2, sort_keys=True, default=str),
                 encoding="utf-8")
    print(f"[probation] report: {p}")


if __name__ == "__main__":
    raise SystemExit(main())
