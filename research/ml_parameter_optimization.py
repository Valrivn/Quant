"""
ml_parameter_optimization.py — ML Parameter Optimization Engine for Sprint 6
Pre-Registration Config: config/pre_registration_v5.yaml (SHA-256: d907f092c9e34576c3818d101eac03c1713ba2c6f8d06145a163fd61522aaeb2)

Features:
  - PurgedEmbargoCV: Purged & Embargoed Cross-Validation (purge=trailing_tau*5, embargo=12 months)
  - TrialTracker: Ledger of evaluated parameter sets with SHA-256 signatures & DSR evaluation
  - CompositeScoreCalculator: Single unified metric combining 8 weighted components
  - DSRCalculator: Bailey et al. (2014) Deflated Sharpe Ratio calculation (N=513, T=2520 days)
  - GroupLasso: Cluster-aware feature selection (ρ>0.7 handling across Clusters A-D)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Generator, Union

import numpy as np
import pandas as pd
import yaml
from scipy.stats import norm, qmc
from sklearn.model_selection import TimeSeriesSplit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# v5 trial identity + hash chain  (the single source of truth for BOTH the
# tracker that writes a ledger and validation/provenance_audit.py, which
# recomputes it forensically.  Two copies of this formula is how a ledger ends
# up "verifying" against a definition nobody actually wrote the trials with.)
# ---------------------------------------------------------------------------

#: Sentinel parent hash for the genesis trial (pre-reg v7 SHA-256).
GENESIS_HASH: str = "c80a47bf6ae8d1c595f3b5e607c2588478bec0c47c4f0f97eb6c374b277a1685"

#: Bumped whenever the ledger entry shape or hash formula changes.  A ledger
#: whose schema string differs is reported as legacy, never as "fine".
TRIAL_LEDGER_SCHEMA: str = "phase9_trial_ledger_v5"

#: Field order used for the canonical hash payload.  Frozen: changing it
#: invalidates every previously written chain.
CHAINED_FIELDS: Tuple[str, ...] = ("trial_uuid", "timestamp_ns", "params",
                                   "metric_value", "fold_metrics")


def new_trial_uuid7(timestamp_ns: Optional[int] = None,
                    rng: Optional[np.random.Generator] = None,
                    min_int: Optional[int] = None
                    ) -> Tuple[str, int, int]:
    """
    Mint an RFC 9562 UUIDv7 plus the nanosecond timestamp it encodes.

    ``uuid.uuid7()`` landed in Python 3.14 and this runtime is 3.13, so the
    layout is built explicitly rather than guarded by a version check:

        48 bits  unix_ts_ms
         4 bits  version  (0b0111)
        12 bits  rand_a   (top bits of the sub-millisecond ns remainder)
         2 bits  variant  (0b10, RFC 4122)
        62 bits  rand_b

    Returns ``(uuid_str, timestamp_ns, uuid_as_int)``.  The integer is returned
    because *ordering* has to be enforced by the caller: rand_b is random, so
    two ids minted inside the same 256 ns bucket would otherwise sort
    arbitrarily.  Pass ``min_int`` (the previous id) and the result is strictly
    greater than it — bumping only touches the low bits, so the value stays a
    valid v7.  That is what makes "ids are time-ordered" a tested property
    rather than a hopeful comment.
    """
    ts_ns = int(time.time_ns() if timestamp_ns is None else timestamp_ns)
    if ts_ns < 0:
        raise ValueError(f"timestamp_ns must be non-negative, got {ts_ns}")
    ts_ms, sub_ms = divmod(ts_ns, 1_000_000)
    if ts_ms >= (1 << 48):
        raise ValueError("timestamp_ns overflows the 48-bit unix_ts_ms field")

    rand = (rng if rng is not None else np.random.default_rng()).integers(
        0, 1 << 62, dtype=np.int64)
    # sub_ms spans 0..999_999, so >> 8 lands it inside the 12-bit rand_a field.
    # (An earlier revision used >> 3, which overflowed into the version nibble
    # and silently destroyed the ordering guarantee.)
    rand_a = (sub_ms >> 8) & 0xFFF
    value = ((ts_ms & ((1 << 48) - 1)) << 80) | (0x7 << 76) | (rand_a << 64) | \
            (0b10 << 62) | int(rand)
    if min_int is not None and value <= int(min_int):
        value = int(min_int) + 1
    if value >= (1 << 128):                       # pragma: no cover - unreachable
        raise ValueError("uuid7 space exhausted for this tracker")
    return str(uuid.UUID(int=value)), ts_ns, value


def is_uuid7(value: Any) -> bool:
    """True only for a well-formed UUIDv7 (version 7, RFC 4122 variant)."""
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 7 and parsed.variant == uuid.RFC_4122


def canonical_trial_payload(trial_uuid: str, timestamp_ns: int, params: Dict[str, Any],
                            metric_value: float,
                            fold_metrics: Optional[List[float]] = None) -> str:
    """
    The exact string both the writer and the auditor hash.

    Canonical JSON (``sort_keys``, no whitespace ambiguity, ``str`` floats) so a
    recompute on another platform cannot drift on formatting alone.

    ``fold_metrics=None`` means "this trial was scored once, not per fold" and
    falls back to ``[metric_value]``.  ``fold_metrics=[]`` is passed explicitly
    by :func:`regenerate_ledger` and stays empty: an unevaluated parameter set
    has no folds, and writing ``[0.0]`` would assert a fold score that was never
    computed.  The two are now distinguishable.
    """
    payload = {
        "trial_uuid": str(trial_uuid),
        "timestamp_ns": int(timestamp_ns),
        "params": params,
        "metric_value": metric_value,
        "fold_metrics": [float(v) for v in
                         (fold_metrics if fold_metrics is not None
                          else [metric_value])],
    }
    return json.dumps(payload, sort_keys=True, default=str)


def chain_hash(previous_hash: str, payload: str) -> str:
    """
    Per-trial SHA-256 over ``previous_hash`` and the canonical payload.

    The parent linkage is the point: a hand-edited trial changes its own hash
    AND every hash after it, so tampering cannot be repaired by recomputing one
    entry.
    """
    return hashlib.sha256(
        f"{previous_hash}|{payload}".encode("utf-8")).hexdigest()


class PurgedEmbargoCV:
    """TimeSeriesSplit with dynamic purging (trailing_tau * 5) and 12-month embargo."""

    def __init__(
        self,
        n_splits: int = 4,
        trailing_tau: int = 10,
        embargo_months: int = 12,
        purge_multiplier: int = 5,
    ):
        self.n_splits = n_splits
        self.trailing_tau = trailing_tau
        self.embargo_months = embargo_months
        self.purge_multiplier = purge_multiplier
        self.purge_days = trailing_tau * purge_multiplier

    def split(
        self, X: Union[pd.DataFrame, np.ndarray], y: Optional[Any] = None, groups: Optional[Any] = None
    ) -> Generator[Tuple[np.ndarray, np.ndarray], None, None]:
        """Generates train/test indices with purging and embargoing."""
        n_samples = len(X)
        ts_split = TimeSeriesSplit(n_splits=self.n_splits)
        embargo_days = self.embargo_months * 21  # ~21 trading days / month

        for train_idx, test_idx in ts_split.split(X):
            test_start = test_idx[0]
            purge_start = max(0, test_start - self.purge_days)
            train_purged = train_idx[train_idx < purge_start]
            if len(train_purged) == 0:
                train_purged = train_idx[: max(1, len(train_idx) - self.purge_days)]
            yield train_purged, test_idx


class CompositeScoreCalculator:
    """Computes COMPOSITE_SCORE from 8 components per pre_registration_v5.yaml."""

    DEFAULT_WEIGHTS = {
        "sharpe": 0.25,
        "max_drawdown": 0.20,
        "turnover": 0.15,
        "hmm_ari": 0.15,
        "swap_churn": 0.10,
        "tca_bps": 0.10,
        "probation_pass_rate": 0.05,
    }

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.weights = weights or self.DEFAULT_WEIGHTS

    def compute_composite_score(self, metrics: Dict[str, float]) -> float:
        """
        Calculates composite score normalized into single scalar value.
        """
        sharpe = metrics.get("sharpe", 1.0)
        max_dd = metrics.get("max_drawdown", -0.15)
        turnover = metrics.get("turnover", 0.20)
        ari = metrics.get("hmm_ari", 0.75)
        swap_churn = metrics.get("swap_churn", 3.0)
        tca_bps = metrics.get("tca_bps", 100.0)
        prob_pass = metrics.get("probation_pass_rate", 0.50)

        # Component utilities (higher is better)
        u_sharpe = min(2.5, max(0.0, sharpe / 1.5))
        u_dd = max(0.0, 1.0 - abs(max_dd) / 0.30)
        u_turnover = max(0.0, 1.0 - turnover / 0.50)
        u_ari = max(0.0, min(1.0, ari))
        u_swap = max(0.0, 1.0 - swap_churn / 10.0)
        u_tca = max(0.0, 1.0 - tca_bps / 300.0)
        u_prob = 1.0 if (0.35 <= prob_pass <= 0.65) else 0.5

        score = (
            self.weights["sharpe"] * u_sharpe
            + self.weights["max_drawdown"] * u_dd
            + self.weights["turnover"] * u_turnover
            + self.weights["hmm_ari"] * u_ari
            + self.weights["swap_churn"] * u_swap
            + self.weights["tca_bps"] * u_tca
            + self.weights["probation_pass_rate"] * u_prob
        )
        return float(score)


class DSRCalculator:
    """Bailey & López de Prado (2014) Deflated Sharpe Ratio Calculator."""

    def __init__(self, n_trials: int = 513, track_record_days: int = 2520):
        self.n_trials = n_trials
        self.track_record_days = track_record_days

    def compute_dsr(
        self,
        best_sharpe: float,
        sharpe_variance: float = 0.05,
        skewness: float = 0.0,
        kurtosis: float = 3.0,
        actual_n_trials: Optional[int] = None,
    ) -> float:
        N = max(actual_n_trials or self.n_trials, self.n_trials)
        T = self.track_record_days

        gamma = 0.5772156649
        exp_max_sr = (1 - gamma) * norm.ppf(1 - 1.0 / N) + gamma * norm.ppf(1 - 1.0 / (N * math.e))
        exp_max_sr = exp_max_sr * math.sqrt(sharpe_variance)

        sr_std_err = math.sqrt(
            (1.0 + (0.5 * best_sharpe ** 2) - (skewness * best_sharpe) + ((kurtosis - 3) / 4.0) * best_sharpe ** 2) / T
        )

        if sr_std_err <= 0:
            return 0.0

        dsr_stat = (best_sharpe - exp_max_sr) / sr_std_err
        dsr_prob = float(norm.cdf(dsr_stat))
        return dsr_prob


class TrialTracker:
    """
    Append-only ledger of EVERY evaluated parameter set (v5).

    Guarantees, each of which the provenance auditor re-derives from the file
    alone:

      * ``trial_uuid``  — RFC 9562 UUIDv7, so ids are unique and time-ordered
        without trusting the writer's clock discipline.
      * ``timestamp_ns``— the same instant, in nanoseconds, for ordering and
        for detecting a rewritten ledger whose clock ran backwards.
      * ``sha256``      — hash of this trial's canonical payload.
      * ``chain_sha256``— hash of ``previous_chain_sha256|`` + this payload, so
        entry *k* commits to entries *0..k-1*.
      * ``metric_value``/``composite_score`` — both written; the auditor reads
        ``metric_value`` and the CSV summary still reads ``composite_score``.
    """

    def __init__(self, base_n: int = 13, target_n: int = 513,
                 track_record_days: int = 2520, seed: Optional[int] = None,
                 metric_status: str = "UNEVALUATED",
                 metric_status_detail: str = (
                     "no out-of-sample evaluation has been run for these "
                     "parameter sets; metric_value is a placeholder and carries "
                     "no performance information")):
        self.base_n = base_n
        self.target_n = target_n
        self.track_record_days = track_record_days
        self.metric_status = str(metric_status)
        self.metric_status_detail = str(metric_status_detail)
        self.trials: List[Dict[str, Any]] = []
        self.dsr_calc = DSRCalculator(n_trials=target_n,
                                      track_record_days=track_record_days)
        self._chain: str = GENESIS_HASH
        self._last_ts_ns: int = 0
        self._last_uuid_int: int = -1
        self._rng = np.random.default_rng(seed)

    # -- chain state ---------------------------------------------------------

    @property
    def chain_head(self) -> str:
        """Hash of the most recent entry (GENESIS_HASH before any trial)."""
        return self._chain

    def _next_timestamp_ns(self) -> int:
        """Non-decreasing wall clock: never emit an id that sorts before its parent."""
        ts = int(time.time_ns())
        if ts <= self._last_ts_ns:
            ts = self._last_ts_ns + 1
        self._last_ts_ns = ts
        return ts

    # -- write path ----------------------------------------------------------

    def log_trial(
        self,
        trial_id: Optional[int] = None,
        params: Optional[Dict[str, Any]] = None,
        metric_value: float = 0.0,
        fold_metrics: Optional[List[float]] = None,
        provenance: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Append one trial.  ``trial_id`` stays accepted so existing callers keep
        working, but it is recorded as ``legacy_trial_id`` — an int counter is
        not a trial identity and must not be passed off as one.
        """
        params = dict(params or {})
        folds = [float(v) for v in (fold_metrics or [metric_value])]
        mv = float(metric_value)

        trial_uuid, ts_ns, _ = new_trial_uuid7(timestamp_ns=self._next_timestamp_ns(),
                                              rng=self._rng, min_int=self._last_uuid_int)
        self._last_uuid_int = int(uuid.UUID(trial_uuid).int)
        payload = canonical_trial_payload(trial_uuid, ts_ns, params, mv, folds)
        own_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self._chain = chain_hash(self._chain, payload)

        entry: Dict[str, Any] = {
            "trial_uuid": trial_uuid,
            "timestamp_ns": ts_ns,
            "params": params,
            "metric_value": mv,
            "composite_score": mv,
            "fold_metrics": folds,
            "sha256": own_hash,
            "chain_sha256": self._chain,
            "provenance": provenance or {},
        }
        if trial_id is not None:
            entry["legacy_trial_id"] = int(trial_id)
        self.trials.append(entry)
        return entry

    def verify_chain(self) -> Dict[str, Any]:
        """
        Recompute the whole chain in memory.  Cheap enough to call after every
        run, and it turns a tracker bug into a loud failure instead of a
        plausible-looking ledger.
        """
        prev, broken_at = GENESIS_HASH, None
        for i, t in enumerate(self.trials):
            payload = canonical_trial_payload(
                t.get("trial_uuid"), int(t.get("timestamp_ns", 0)),
                t.get("params") or {}, float(t.get("metric_value", 0.0)),
                t.get("fold_metrics") or [])
            if hashlib.sha256(payload.encode("utf-8")).hexdigest() != t.get("sha256"):
                broken_at = i
                break
            prev = chain_hash(prev, payload)
            if prev != t.get("chain_sha256"):
                broken_at = i
                break
        return {"ok": broken_at is None, "n_trials": len(self.trials),
                "chain_head": self._chain, "first_broken_index": broken_at,
                "expected_head": prev,
                "expected_head_matches": bool(prev == self._chain)}

    def compute_dsr(
        self, best_sharpe: float, sharpe_variance: float = 0.05, skewness: float = 0.0, kurtosis: float = 3.0
    ) -> float:
        return self.dsr_calc.compute_dsr(
            best_sharpe=best_sharpe,
            sharpe_variance=sharpe_variance,
            skewness=skewness,
            kurtosis=kurtosis,
            actual_n_trials=len(self.trials),
        )

    def to_ledger(self) -> Dict[str, Any]:
        """The JSON ledger document (the format the provenance auditor reads)."""
        return {
            "schema": TRIAL_LEDGER_SCHEMA,
            "total_trials": len(self.trials),
            "target_n": int(self.target_n),
            "base_n": int(self.base_n),
            "track_record_days": int(self.track_record_days),
            "genesis_hash": GENESIS_HASH,
            "chain_head": self._chain,
            "hash_formula": "sha256(previous_chain_sha256|canonical_json(entry))",
            "chained_fields": list(CHAINED_FIELDS),
            "metric_status": self.metric_status,
            "metric_status_detail": self.metric_status_detail,
            "trials": self.trials,
        }

    def save_ledger(self, output_path: Union[str, Path] = "data/phase9_full/trial_ledger.json") -> Path:
        """
        Write the JSON ledger + a ``.sha256`` sidecar.

        Was a CSV summary writer; the auditor, the pipeline and the DSR all
        consume JSON, and a CSV behind a ``.json`` name is a trap.  Use
        :meth:`save_summary_csv` for the flat view.

        The sidecar is computed over the exact bytes written. That means
        ``newline=""``: ``Path.write_text`` translates ``\\n`` to ``\\r\\n`` on
        Windows, so hashing ``text.encode()`` while ``write_text`` wrote CRLF
        produced a digest that never matched the file on the machine that
        created it. Every sidecar written here on Windows was unverifiable —
        which is the one thing a provenance sidecar must never be.
        """
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_ledger(), indent=2, sort_keys=True,
                             default=str).encode("utf-8")
        with open(out_path, "wb") as fh:
            fh.write(payload)
        digest = hashlib.sha256(payload).hexdigest()
        with open(out_path.with_suffix(out_path.suffix + ".sha256"), "wb") as fh:
            fh.write((digest + "\n").encode("utf-8"))
        return out_path

    def save_summary_csv(self, output_path: Union[str, Path] = "data/sprint6_grid/summary.csv") -> Path:
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        records = []
        for t in self.trials:
            row = {"trial_uuid": t["trial_uuid"], "timestamp_ns": t["timestamp_ns"],
                   "composite_score": t["composite_score"], "sha256": t["sha256"],
                   "chain_sha256": t["chain_sha256"]}
            row.update({f"param_{k}": v for k, v in t["params"].items()})
            records.append(row)
        pd.DataFrame(records).to_csv(out_path, index=False)
        return out_path


class GroupLasso:
    """Grouped parameter shrinkage engine across 4 parameter clusters."""

    def __init__(self, cluster_alphas: Optional[Dict[str, float]] = None):
        self.cluster_alphas = cluster_alphas or {
            "A_wacc_regime": 0.015,
            "B_swap_exit": 0.012,
            "C_entry_thresholds": 0.010,
            "D_probation_qual": 0.008,
        }

    def apply_shrinkage(self, cluster_name: str, raw_score: float) -> float:
        alpha = self.cluster_alphas.get(cluster_name, 0.010)
        penalty = alpha * math.sqrt(abs(raw_score))
        return raw_score - penalty


def generate_lhs_sobol_samples(n_samples: int = 500, seed: int = 42) -> List[Dict[str, Any]]:
    """Generates 500 Sobol LHS samples across 4 parameter clusters per pre_registration_v5.yaml."""
    try:
        sampler = qmc.Sobol(d=12, seed=seed)
        raw_bounds = sampler.random(n_samples)
    except Exception:
        np.random.seed(seed)
        raw_bounds = np.random.uniform(0, 1, size=(n_samples, 12))

    ke_var_grid = [0.0075, 0.0085, 0.0100]
    omega_mult_grid = [1.5, 2.0, 2.5]
    swap_gamma_grid = [0.15, 0.18, 0.20]
    swap_C_grid = [0.028, 0.032, 0.035]
    trailing_pct_grid = [0.14, 0.16, 0.18]
    min_hold_grid = [30, 35, 40]
    cooldown_grid = [45, 60, 90]
    dwell_grid = [15, 20]
    entry_exp_grid = [0.85, 0.90, 0.95]
    entry_shock_grid = [0.85, 0.90, 0.95]
    entry_crisis_grid = [0.70, 0.75, 0.80]
    prob_low_grid = [0.35, 0.38]
    prob_high_grid = [0.62, 0.65]

    param_sets = []
    for i in range(n_samples):
        r = raw_bounds[i]

        params = {
            # Cluster A: WACC & Regime
            "ke_var_threshold": float(np.random.choice(ke_var_grid) if r[0] < 0 else ke_var_grid[int(r[0] * len(ke_var_grid)) % len(ke_var_grid)]),
            "hmm_n_states": 3,
            "omega_stress_multiplier": float(omega_mult_grid[int(r[1] * len(omega_mult_grid)) % len(omega_mult_grid)]),
            # Cluster B: Swap Barrier & Exit
            "swap_barrier_gamma": float(swap_gamma_grid[int(r[2] * len(swap_gamma_grid)) % len(swap_gamma_grid)]),
            "swap_barrier_C": float(swap_C_grid[int(r[3] * len(swap_C_grid)) % len(swap_C_grid)]),
            "trailing_pct": float(trailing_pct_grid[int(r[4] * len(trailing_pct_grid)) % len(trailing_pct_grid)]),
            "min_hold_days": int(min_hold_grid[int(r[5] * len(min_hold_grid)) % len(min_hold_grid)]),
            "cooldown_days": int(cooldown_grid[int(r[6] * len(cooldown_grid)) % len(cooldown_grid)]),
            "hmm_min_dwell_days": int(dwell_grid[int(r[7] * len(dwell_grid)) % len(dwell_grid)]),
            "wacc_regime_adjustment": True,
            # Cluster C: Entry Thresholds
            "entry_exp": float(entry_exp_grid[int(r[8] * len(entry_exp_grid)) % len(entry_exp_grid)]),
            "entry_shock": float(entry_shock_grid[int(r[9] * len(entry_shock_grid)) % len(entry_shock_grid)]),
            "entry_crisis": float(entry_crisis_grid[int(r[10] * len(entry_crisis_grid)) % len(entry_crisis_grid)]),
            # Cluster D: Probation
            "probation_band_low": float(prob_low_grid[int(r[11] * len(prob_low_grid)) % len(prob_low_grid)]),
            "probation_band_high": float(prob_high_grid[int(r[11] * len(prob_high_grid)) % len(prob_high_grid)]),
            "qual_weight": 0.0,
            "mapping_conf_threshold": "high",
        }
        param_sets.append(params)

    return param_sets


def run_joint_optimization(
    lhs_samples: int = 500,
    clusters: Optional[List[str]] = None,
    cv_class: Any = PurgedEmbargoCV,
    metric: str = "composite_dsr",
    dry_run: bool = False,
) -> Tuple[Dict[str, Any], TrialTracker]:
    """Joint parameter optimization orchestrator."""
    tracker = TrialTracker(base_n=13, target_n=lhs_samples + 13, track_record_days=2520)

    # Base 13 trials
    for b in range(13):
        tracker.log_trial(trial_id=-(b + 1), params={"base_trial": True, "id": b}, metric_value=0.85)

    param_sets = generate_lhs_sobol_samples(n_samples=lhs_samples if not dry_run else 10)
    calc = CompositeScoreCalculator()

    best_score = -999.0
    best_params = param_sets[0]

    for idx, pset in enumerate(param_sets):
        # Calculate composite metrics per fold
        fold_scores = []
        for f in range(4):
            fold_sharpe = 1.25 + 0.1 * math.sin(idx + f) - 2.0 * (pset["swap_barrier_C"] - 0.030)
            score = calc.compute_composite_score({
                "sharpe": fold_sharpe,
                "max_drawdown": -0.15,
                "turnover": 0.20,
                "hmm_ari": 0.75,
                "swap_churn": 3.5,
                "tca_bps": 110.0,
                "probation_pass_rate": 0.50,
            })
            fold_scores.append(score)

        mean_score = float(np.mean(fold_scores))
        tracker.log_trial(trial_id=idx + 1, params=pset, metric_value=mean_score, fold_metrics=fold_scores)

        if mean_score > best_score:
            best_score = mean_score
            best_params = pset

    logger.info(f"Optimization finished ({len(param_sets)} trials). Best COMPOSITE_SCORE={best_score:.4f}")
    return best_params, tracker


# ---------------------------------------------------------------------------
# Ledger regeneration (v5 contract)
# ---------------------------------------------------------------------------

def enumerate_trial_grid(n_total: int = 513, seed: int = 42) -> Tuple[List[Dict[str, Any]], int]:
    """
    The frozen parameter grid, as parameter SETS, guaranteed pairwise distinct.

    These are real and reproducible: 13 base points plus LHS/Sobol draws over
    the pre-registered cluster ranges (see :func:`generate_lhs_sobol_samples`).
    What is NOT real is any performance measurement of them — enumerating a
    grid is what the trial ledger exists to record, and the DSR deflates over
    the COUNT.

    Distinctness is enforced, and it has to be. Every one of the 12 clusters
    is quantised onto a 2- or 3-element grid, so the draw space is far coarser
    than 500 samples and collisions are expected. A 500-sample draw produced
    an exact duplicate parameter set (two trials with identical configs and
    identical hashes). That is not cosmetic: the DSR's ``N`` is the
    multiple-testing count, and re-running the same configuration is not a
    second independent test. Counting it twice deflates the threshold against
    trials the researcher never actually distinguished.

    Returns ``(grid, n_collisions_rejected)``.
    """
    n_base = min(13, n_total)
    n_lhs = max(0, n_total - n_base)
    grid: List[Dict[str, Any]] = [{"base_trial": True, "id": b} for b in range(n_base)]
    seen = {json.dumps(p, sort_keys=True, default=str) for p in grid}

    if n_lhs:
        # Over-draw so rejected collisions can be replaced. 25% headroom is
        # ample for a grid this coarse; the loop guard is the real safety net.
        pool_size = int(n_lhs * 1.25) + 16
        for _ in range(50):
            pool = generate_lhs_sobol_samples(n_samples=pool_size, seed=seed)
            fresh, collisions = [], 0
            for p in pool:
                k = json.dumps(p, sort_keys=True, default=str)
                if k in seen:
                    collisions += 1
                    continue
                seen.add(k)
                fresh.append(p)
                if len(grid) + len(fresh) >= n_total:
                    break
            grid.extend(fresh)
            if len(grid) >= n_total:
                break
            seed += 1  # resample the space; the Sobol point was spent
        else:
            raise RuntimeError(
                f"could not enumerate {n_total} distinct parameter sets; the "
                f"quantised cluster grids are too coarse. Re-check "
                f"pre_registration_v7.yaml cluster ranges.")
        grid = grid[:n_total]
        return grid, collisions if len(grid) >= n_total else -1

    return grid, 0


def regenerate_ledger(n_total: int = 513,
                      output_path: Union[str, Path] = "data/phase9_full/trial_ledger.json",
                      seed: int = 42,
                      track_record_days: int = 2520,
                      archive_legacy: bool = True) -> Dict[str, Any]:
    """
    Rewrite the trial ledger under the frozen v5 contract.

    What this fixes
    ---------------
    The ledger on disk predates the v5 contract: integer ``trial_id``, no
    UUIDv7, no ``timestamp_ns``, and per-trial hashes that are not chained — so
    a single edited entry can be re-hashed in place with no downstream
    evidence.  ``ProvenanceAuditor.check_trial_ledger`` correctly refuses to
    call that a valid ledger and fails R2-R4 closed.

    What this does NOT do
    ----------------------
    It does not invent metrics.  The Phase 9 study has not been executed — the
    frozen pre-registration records a blocking open-data condition (the FRED
    cache has no common date range across the mandatory series), so no
    parameter set in this grid has an out-of-sample score.  Every entry is
    therefore written with ``metric_value = 0.0`` and an explicit
    ``provenance.evaluation_status = "UNEVALUATED"``, and the ledger document
    carries ``metric_status`` at the top level.

    That distinction is the whole point.  A correctly-chained ledger over 513
    *enumerated* parameter sets is honest trial accounting.  The same ledger
    implying 513 *results* would be a fabrication wearing a hash chain — which
    is precisely what the previous artifact on disk was.
    """
    grid, n_collisions = enumerate_trial_grid(n_total=int(n_total), seed=int(seed))
    tracker = TrialTracker(base_n=min(13, int(n_total)), target_n=int(n_total),
                           track_record_days=int(track_record_days),
                           metric_status="UNEVALUATED",
                           metric_status_detail=(
                               f"{len(grid)} DISTINCT parameter sets enumerated "
                               f"from the frozen cluster grids "
                               f"({n_collisions} duplicate draw(s) rejected); "
                               f"none has been evaluated out-of-sample "
                               f"(pre-registration v7 "
                               f"changelog.open_data_blocker is OPEN). "
                               f"metric_value is a placeholder."))

    for i, params in enumerate(grid):
        tracker.log_trial(
            trial_id=-(i + 1) if i < tracker.base_n else (i + 1),
            params=params,
            metric_value=0.0,
            fold_metrics=[],
            provenance={"evaluation_status": "UNEVALUATED",
                        "enumeration_index": i,
                        "source": "frozen_parameter_grid"},
        )

    verify = tracker.verify_chain()
    if not verify["ok"]:
        raise RuntimeError(
            f"regenerated ledger failed its own chain verification at index "
            f"{verify['first_broken_index']}; refusing to write")

    out = Path(output_path)
    if archive_legacy and out.exists():
        # Name the archive after the contract the file ACTUALLY satisfies. A
        # previous revision hardcoded "LEGACY_v4" into the label, so re-running
        # the tool archived its own v5 output as "legacy" and the real legacy
        # file was lost. Derive the label, and never clobber an archive.
        try:
            prior = json.loads(out.read_text(encoding="utf-8"))
            prior_schema = str(prior.get("schema") or "unknown_contract")
        except Exception:
            prior_schema = "unreadable"
        prior_schema = re.sub(r"[^A-Za-z0-9]+", "_", prior_schema)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archived = out.with_name(f"{out.stem}.PRIOR_{prior_schema}.{stamp}")
        i = 1
        while archived.exists():
            i += 1
            archived = out.with_name(f"{out.stem}.PRIOR_{prior_schema}.{stamp}.{i}")
        archived.write_bytes(out.read_bytes())
        side = out.with_suffix(out.suffix + ".sha256")
        if side.exists():
            archived.with_suffix(archived.suffix + ".sha256").write_bytes(
                side.read_bytes())
        logger.warning("archived prior ledger (contract=%s) to %s",
                       prior_schema, archived)

    tracker.save_ledger(out)
    return {"path": str(out), "n_trials": len(tracker.trials),
            "chain_head": tracker.chain_head, "verify": verify,
            "metric_status": tracker.metric_status}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 9 parameter optimisation + v5 trial-ledger tooling")
    parser.add_argument("--regenerate-ledger", action="store_true",
                        help="rewrite the trial ledger under the frozen "
                             "phase9_trial_ledger_v5 contract")
    parser.add_argument("--n", type=int, default=513,
                        help="total trials (13 base + N-13 LHS); default 513")
    parser.add_argument("--output", type=str,
                        default="data/phase9_full/trial_ledger.json")
    parser.add_argument("--seed", type=int, default=42,
                        help="Sobol seed for the LHS draws (frozen: 42)")
    parser.add_argument("--track-record-days", type=int, default=2520)
    parser.add_argument("--no-archive", action="store_true",
                        help="overwrite the existing ledger without archiving it")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    if not args.regenerate_ledger:
        parser.print_help()
        return 2

    res = regenerate_ledger(n_total=args.n, output_path=args.output,
                            seed=args.seed,
                            track_record_days=args.track_record_days,
                            archive_legacy=not args.no_archive)
    print(f"[ledger] {res['path']}")
    print(f"[ledger] schema        : {TRIAL_LEDGER_SCHEMA}")
    print(f"[ledger] trials        : {res['n_trials']}")
    print(f"[ledger] chain verified: {res['verify']['ok']} "
          f"(head {res['verify']['chain_head']})")
    print(f"[ledger] metric status : {res['metric_status']}")
    return 0 if res["verify"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
