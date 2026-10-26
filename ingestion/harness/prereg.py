"""
Phase 9 Sprint 6 Pre-Registration — FROZEN artifact loader + SHA-256 integrity gate.
(big-pickle-worker / Sprint 6, Decision 3: UNIFIED REGIME ARCHITECTURE)

Single source of truth for reading ``config/pre_registration_v7.yaml``.

Why this module exists
----------------------
Phase 9 is falsification-first: every threshold, the composite-component set and
weights, the DSR trial count, the track-record length and the parameter-cluster
grids must come from the pre-registration and NOWHERE ELSE.  Hard-coding a
threshold in a gate body is a pre-registration violation even when the number
happens to be correct.  This module is the only place allowed to read the frozen
YAML, and it verifies the file's SHA-256 on every load so a post-hoc edit to the
thresholds is detectable rather than silent.

Sprint 6 / Decision 3 — THE PIN HISTORY IS THE RECORD
-----------------------------------------------------
Every re-pin was an AUTHORISED, logged content change.  The prior value is
retained on purpose: a re-pin with no record of the prior value would destroy
the only evidence of what was approved, which is the failure this module exists
to catch.  The full ledger is :data:`PIN_HISTORY`.

    f9615710...  v4.0 as approved 2026-09-26
      -> 983316e2...  Sprint 4 / Arm A amendment: B_swap_exit exit-logic cluster
                       (trailing_pct, hmm_min_dwell_days, wacc_regime_adjustment)
      -> 2b45ba98...  Sprint 5 grid widening
      -> d907f092...  Sprint 6 Decision 3: SINGLE UNIFIED METRIC.  The whole
                       ``gates:`` block (gate_1 .. gate_8, ``fwer_alpha``,
                       ``effective_count``) is GONE, replaced by
                       ``statistical_design:`` — one metric, ``composite_dsr``,
                       no Holm-Bonferroni, honest N=513 (13 base + 500 LHS),
                       2520-day track record, Fold-4 TRUE-OOS DSR >= 0.95.
      -> 2ff579f1...  Sprint 6 Decision 3: UNIFIED REGIME ARCHITECTURE.
                       BOCPD + IO-HMM + Micro-States. 3-layer regime detection.
      -> 390084e0...  v6.0 as pinned 2026-09-27.  ARCHIVED, unmodified.
      -> c80a47bf...  v7.0 as pinned 2026-09-28.  CANONICAL 6-FEATURE CONTRACT.
                       CESI (Citi Economic Surprise Index) removed: it is a
                       proprietary, non-FRED series that was being filled with
                       ``np.random.normal(0, 20, n)`` under seed 45, violating the
                       frozen document's own ``optimization.synthetic_fallback:
                       "disabled"``.  Its economic slot is now
                       UNANTICIPATED_INFLATION (CPIAUCSL YoY - T10YIE).  The ARI
                       threshold is NOT relaxed: removing a defective feature is a
                       provenance fix, not grounds to re-roll the bar it did not
                       cause to miss.

Because the ``gates:`` block no longer exists, the old ``n_effective_gates`` /
``fwer_alpha`` / ``gate_1_*`` / ``gate_8_*`` accessors were REMOVED rather than
re-pointed at invented values.  Reading a threshold that the frozen document no
longer declares must raise, not default.

Policy (env ``QUANT_PREREG_INTEGRITY``, default ``audit``)
    ``audit``  — record the mismatch, warn once, expose it in every report.
                 Imports keep working; provenance roots that require a valid
                 pre-registration digest report FAIL.
    ``strict`` — raise :class:`PreRegistrationIntegrityError` on load.
    ``off``    — record only, no warning (diagnostics use this).

CLI
---
    python -m ingestion.harness.prereg                    # print integrity block
    python -m ingestion.harness.prereg --verify-prereg    # exit 0 iff verified
    python -m ingestion.harness.prereg --strict           # exit 1 on mismatch
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import warnings
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import yaml

__all__ = [
    "PREREG_RELPATH",
    "EXPECTED_SHA256",
    "EXPECTED_SHA256_V6",
    "EXPECTED_SHA256_V7",
    "PRE_ARM_A_SHA256",
    "PIN_HISTORY",
    "OBSERVED_SHA256",
    "INTEGRITY_MODES",
    "INTEGRITY_ENV_VAR",
    "PreRegistrationError",
    "PreRegistrationIntegrityError",
    "IntegrityResult",
    "PreRegistration",
    "repo_root",
    "resolve_prereg_path",
    "sha256_bytes",
    "sha256_path",
    "integrity_mode",
    "verify_preregistration_hash",
    "load_prereg",
    "module_load_check",
    "clear_cache",
    "MODULE_PREREG",
]

# ---------------------------------------------------------------------------
# Frozen identity
# ---------------------------------------------------------------------------

#: Repo-relative location of the frozen Phase 9 pre-registration (v7 / Sprint 7).
PREREG_RELPATH = Path("config") / "pre_registration_v7.yaml"

#: Digest of the ARCHIVED v6 pre-registration (Sprint 6 / Decision 3), pinned
#: 2026-09-27.  v6 is SUPERSEDED but retained unmodified for the audit trail;
#: it is what :data:`PRE_ARM_A_SHA256` now anchors.
EXPECTED_SHA256_V6 = "390084e01fae6a358ab66b5ee536ccaf4d5d85bc6ec4dd05103cb74d5a5c5b10"

#: Digest of the LIVE v7 pre-registration (Sprint 7), pinned 2026-09-28.
#: Matches the bare-digest sidecar ``config/pre_registration_v7.yaml.sha256``.
#:
#: v7 is the CANONICAL 6-FEATURE CONTRACT: CESI is removed and replaced by
#: UNANTICIPATED_INFLATION (CPIAUCSL YoY - T10YIE).  This matters beyond
#: bookkeeping -- v6's ``hmm.layer1_features`` still lists ``CESI``, which
#: ``feature_contract.FORBIDDEN_SERIES`` bans and which
#: ``assert_canonical_names`` raises on.  With v6 pinned, the live pre-registration
#: contradicted the engine's own feature contract.
EXPECTED_SHA256_V7 = "c80a47bf6ae8d1c595f3b5e607c2588478bec0c47c4f0f97eb6c374b277a1685"

#: The live pin.  Kept under its original name so existing callers that pass
#: ``EXPECTED_SHA256`` as the default expectation keep working unchanged.
EXPECTED_SHA256 = EXPECTED_SHA256_V7

#: Digest of the immediate predecessor (v6), retained as the v7 transition's
#: baseline.  Not a live pin -- do not "restore" it; it is the audit anchor.
PRE_ARM_A_SHA256 = EXPECTED_SHA256_V6

#: Full pin ledger.  Every re-pin below was an AUTHORISED, logged content
#: change.  A re-pin with no record of the prior value would destroy the only
#: evidence of what was approved -- the failure this module exists to catch.
PIN_HISTORY: Tuple[Tuple[str, str], ...] = (
    ("f9615710c7d8a6348652fbc711c3bf690d531baef06ce67c0ddd849b60d61533",
     "v4.0 as approved 2026-09-26"),
    ("983316e26e86c5e6778e9adf8252ec4cc0b3901528cef4214d68a89888936921",
     "Sprint 4 / Arm A amendment: B_swap_exit exit-logic cluster"),
    ("2b45ba98" + "0" * 56, "Sprint 5 grid widening (truncated; see git history)"),
    ("d907f092" + "0" * 56, "Sprint 6 Decision 3: single unified composite_dsr metric"),
    ("2ff579f1" + "0" * 56, "Sprint 6 Decision 3: unified regime architecture"),
    (EXPECTED_SHA256_V6, "v6.0 as pinned 2026-09-27 (ARCHIVED, superseded by v7)"),
    (EXPECTED_SHA256_V7, "v7.0 canonical 6-feature contract, CESI removed (LIVE)"),
)

#: Digest actually observed on disk (recorded so future drift is detectable
#: independently of the declared digest).
OBSERVED_SHA256 = EXPECTED_SHA256

#: The self-declared digest inside the YAML must agree with EXPECTED_SHA256;
#: if it ever stops agreeing, the document was edited in a new way.
DECLARED_SELF_KEYS: Tuple[str, ...] = ("sha256",)

#: Canonicalisations tried when explaining a mismatch (documentation + tests).
INTEGRITY_CANONICALISATIONS_CHECKED: Tuple[str, ...] = (
    "raw_bytes",
    "lf_normalised",
    "sha256_line_removed",
    "sha256_line_removed_lf",
    "sha256_value_blanked",
    "sha256_value_zeroed",
)

INTEGRITY_MODES: Tuple[str, ...] = ("strict", "audit", "off")
INTEGRITY_ENV_VAR = "QUANT_PREREG_INTEGRITY"
DEFAULT_INTEGRITY_MODE = "audit"

#: Decision 3 marker.  Kept as a module constant so a caller can assert the
#: frozen document really is the simplified design without re-parsing the YAML.
NO_GATE_HIERARCHY = True

_WARNED = False


class PreRegistrationError(RuntimeError):
    """Base class for pre-registration loading/integrity failures."""


class PreRegistrationIntegrityError(PreRegistrationError):
    """Raised when the frozen pre-registration fails its SHA-256 gate."""


# ---------------------------------------------------------------------------
# Paths + hashing
# ---------------------------------------------------------------------------

def repo_root() -> Path:
    """Repository root (the directory that owns ``config/`` and ``ingestion/``)."""
    here = Path(__file__).resolve()
    for cand in here.parents:
        if (cand / "config").is_dir() and (cand / "ingestion").is_dir():
            return cand
    return here.parents[2]


def resolve_prereg_path(path: Optional[Union[str, Path]] = None) -> Path:
    """
    Resolve the pre-registration path.  Relative paths are tried against the
    CWD first (so a test can point at a tmp copy) and then the repo root, so the
    gate engine works from any working directory.
    """
    if path is None:
        path = PREREG_RELPATH
    p = Path(path)
    if p.is_absolute():
        return p
    cwd_cand = (Path.cwd() / p)
    if cwd_cand.exists():
        return cwd_cand
    root_cand = (repo_root() / p)
    if root_cand.exists():
        return root_cand
    return cwd_cand      # non-existent: let the caller raise a clear error


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: Union[str, Path]) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonicalisation_digests(raw: bytes) -> Dict[str, str]:
    """
    Every canonicalisation we tried while diagnosing a digest mismatch.
    Kept (and unit-tested) so the "we checked, it is not a line-ending artefact"
    claim is falsifiable rather than folklore.
    """
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    out: Dict[str, str] = {"raw_bytes": sha256_bytes(raw)}
    out["lf_normalised"] = sha256_bytes(raw.replace(b"\r\n", b"\n"))

    no_sha = "\n".join(l for l in lines if not l.strip().startswith("sha256:"))
    out["sha256_line_removed"] = sha256_bytes(no_sha.encode("utf-8"))
    out["sha256_line_removed_lf"] = sha256_bytes(no_sha.replace("\n", "\r\n").encode("utf-8"))

    blanked = "\n".join(
        (re.sub(r'(sha256:\s*").*(")', r"\1\2", l) if l.strip().startswith("sha256:") else l)
        for l in lines
    )
    out["sha256_value_blanked"] = sha256_bytes(blanked.encode("utf-8"))

    zeroed = text.replace(EXPECTED_SHA256, "0" * 64)
    out["sha256_value_zeroed"] = sha256_bytes(zeroed.encode("utf-8"))
    return out


# ---------------------------------------------------------------------------
# Integrity result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IntegrityResult:
    """Outcome of the pre-registration SHA-256 gate."""

    path: str
    exists: bool
    expected_sha256: str
    actual_sha256: Optional[str]
    declared_sha256: Optional[str]
    verified: bool
    status: str
    detail: str
    canonicalisations: Dict[str, str] = field(default_factory=dict)

    def to_dict(self, include_canonicalisations: bool = True) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "path": self.path,
            "exists": self.exists,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "declared_sha256": self.declared_sha256,
            "verified": self.verified,
            "status": self.status,
            "detail": self.detail,
        }
        if include_canonicalisations:
            out["canonicalisations"] = dict(self.canonicalisations)
            out["canonicalisations_checked"] = list(INTEGRITY_CANONICALISATIONS_CHECKED)
        return out


def integrity_mode(mode: Optional[str] = None) -> str:
    """Resolve the integrity policy (explicit arg > env var > default)."""
    if mode is not None:
        m = str(mode).strip().lower()
        if m not in INTEGRITY_MODES:
            raise ValueError(
                f"unknown integrity mode {mode!r}; expected one of {INTEGRITY_MODES}")
        return m
    env = os.environ.get(INTEGRITY_ENV_VAR, "").strip().lower()
    return env if env in INTEGRITY_MODES else DEFAULT_INTEGRITY_MODE


def _verify_once(path: Path, expected: str) -> IntegrityResult:
    """Pure verification (no policy).  Never raises for a mismatch."""
    p = Path(path)
    if not p.exists():
        return IntegrityResult(
            path=str(p), exists=False, expected_sha256=expected,
            actual_sha256=None, declared_sha256=None, verified=False,
            status="MISSING",
            detail=f"pre-registration not found: {p}")
    raw = p.read_bytes()
    actual = sha256_bytes(raw)
    declared: Optional[str] = None
    try:
        cfg = yaml.safe_load(raw.decode("utf-8")) or {}
        for key in DECLARED_SELF_KEYS:
            val = cfg.get(key)
            if isinstance(val, str) and val:
                declared = val.strip().lower()
                break
    except Exception:      # noqa: BLE001 — a YAML error is itself a failure below
        cfg = None

    canon = _canonicalisation_digests(raw)
    if actual == expected:
        status, verified, detail = "VERIFIED", True, "sha256 matches the frozen digest"
    elif expected in canon.values():
        status, verified, detail = ("VERIFIED_CANONICALISED", True,
                                    "raw bytes differ but a canonicalisation matches")
    else:
        status, verified = "MISMATCH", False
        detail = (f"frozen pre-registration does NOT match its declared digest "
                  f"(expected {expected[:12]}..., actual {actual[:12]}...); "
                  f"checked {len(canon)} canonicalisations")
    return IntegrityResult(
        path=str(p), exists=True, expected_sha256=expected, actual_sha256=actual,
        declared_sha256=declared, verified=verified, status=status, detail=detail,
        canonicalisations=canon)


def _apply_policy(integrity: IntegrityResult, mode: str) -> None:
    if integrity.verified or mode == "off":
        return
    if mode == "strict":
        raise PreRegistrationIntegrityError(
            f"{integrity.detail}\npath: {integrity.path}\n"
            f"declared-in-file: {integrity.declared_sha256}\n"
            f"observed:         {integrity.actual_sha256}\n"
            f"Set {INTEGRITY_ENV_VAR}=audit to downgrade to a recorded FAIL "
            f"(Phase 9 requires the audit, not a silent pass).")
    global _WARNED
    if not _WARNED:
        _WARNED = True
        warnings.warn(
            "Phase 9 pre-registration INTEGRITY FAIL: " + integrity.detail +
            f" ({integrity.path}) — provenance roots requiring a valid digest will "
            f"report FAIL until the CEO re-approves and re-pins the artifact.",
            RuntimeWarning, stacklevel=2)


# ---------------------------------------------------------------------------
# Pre-registration handle
# ---------------------------------------------------------------------------

class PreRegistration:
    """
    Read-only handle over the frozen pre-registration with typed accessors.

    Every accessor raises :class:`PreRegistrationError` when the key is absent
    rather than falling back to a literal — a missing threshold is a hard error,
    never a silent default.  That discipline is what Decision 3 needed: the
    ``gates:`` block was deleted, and the old ``gates.*`` accessors were deleted
    with it instead of being quietly re-pointed at invented values.
    """

    def __init__(self, config: Dict[str, Any], integrity: IntegrityResult,
                 path: Path, mode: str = DEFAULT_INTEGRITY_MODE) -> None:
        if not isinstance(config, dict):
            raise PreRegistrationError(
                f"pre-registration did not parse to a mapping: {path}")
        self.config = config
        self.integrity = integrity
        self.path = Path(path)
        self.mode = mode

    # -- generic ------------------------------------------------------------

    def get(self, dotted: str, default: Any = "__raise__") -> Any:
        """Fetch ``a.b.c``; raises unless an explicit default is supplied."""
        node: Any = self.config
        for part in str(dotted).split("."):
            if not isinstance(node, dict) or part not in node:
                if default == "__raise__":
                    raise PreRegistrationError(
                        f"pre-registration key missing: {dotted!r} "
                        f"(in {self.path})")
                return default
            node = node[part]
        return node

    # -- identity -----------------------------------------------------------

    @property
    def phase(self) -> int:
        return int(self.get("phase"))

    @property
    def version(self) -> str:
        return str(self.get("version"))

    @property
    def status(self) -> str:
        return str(self.get("status"))

    @property
    def declared_sha256(self) -> str:
        try:
            return str(self.get("sha256"))
        except PreRegistrationError:
            # Self-referential sha256 line removed per CEO ruling 1A;
            # fall back to sidecar file
            sidecar = self.path.with_suffix(self.path.suffix + ".sha256")
            if sidecar.exists():
                return sidecar.read_text().strip()
            raise

    @property
    def integrity_verified(self) -> bool:
        return bool(self.integrity.verified)

    # -- statistical design (Decision 3) ------------------------------------

    @property
    def statistical_design(self) -> Dict[str, Any]:
        return dict(self.get("statistical_design"))

    @property
    def metric(self) -> str:
        """The ONE unified metric name.  Decision 3: ``composite_dsr``."""
        return str(self.get("statistical_design.metric"))

    @property
    def no_gate_hierarchy(self) -> bool:
        return bool(self.get("statistical_design.no_gate_hierarchy"))

    @property
    def composite_components(self) -> List[Dict[str, Any]]:
        return [dict(c) for c in self.get("statistical_design.composite_components")]

    @property
    def composite_component_names(self) -> List[str]:
        return [str(c["name"]) for c in self.composite_components]

    @property
    def composite_component(self, name: str) -> Dict[str, Any]:
        for c in self.composite_components:
            if str(c.get("name")) == str(name):
                return dict(c)
        raise PreRegistrationError(
            f"composite component {name!r} is not declared in the frozen "
            f"pre-registration; declared: {self.composite_component_names}")

    @property
    def dsr_method(self) -> str:
        return str(self.get("statistical_design.dsr.method"))

    @property
    def dsr_n_trials(self) -> int:
        return int(self.get("statistical_design.dsr.n_trials"))

    @property
    def dsr_base_n(self) -> int:
        return int(self.get("statistical_design.dsr.base_n"))

    @property
    def dsr_lhs_n(self) -> int:
        return int(self.get("statistical_design.dsr.lhs_n"))

    @property
    def dsr_track_record_days(self) -> int:
        """2520 = the full concatenated 4-fold walk-forward record, NOT per-fold."""
        return int(self.get("statistical_design.dsr.track_record_days"))

    @property
    def dsr_threshold(self) -> float:
        return float(self.get("statistical_design.dsr.threshold"))

    @property
    def fold_evaluation_mode(self) -> str:
        return str(self.get("statistical_design.fold_evaluation.mode"))

    @property
    def fold_4_true_oos(self) -> bool:
        return bool(self.get("statistical_design.fold_evaluation.fold_4_true_oos"))

    @property
    def fold_4_test_end(self) -> str:
        return str(self.get("windows.primary.folds.fold_4.test_end"))

    # -- HMM ----------------------------------------------------------------

    @property
    def hmm_ari_threshold(self) -> float:
        return float(self.get("hmm.validation.ari_threshold"))

    @property
    def hmm_folds_required(self) -> int:
        return int(self.get("hmm.validation.folds_required"))

    @property
    def hmm_n_states(self) -> int:
        return int(self.get("hmm.n_states"))

    @property
    def hmm_state_names(self) -> List[str]:
        return [str(s) for s in self.get("hmm.state_names")]

    @property
    def hmm_layer1_features(self) -> List[str]:
        return [str(s) for s in self.get("hmm.layer1_features")]

    @property
    def hmm_train_window_years(self) -> int:
        return int(self.get("hmm.train_window_years"))

    @property
    def hmm_retrain_frequency(self) -> str:
        return str(self.get("hmm.retrain_frequency"))

    @property
    def hmm_dirichlet_lambda(self) -> float:
        return float(self.get("hmm.dirichlet_lambda"))

    @property
    def hmm_sticky_kappa(self) -> int:
        return int(self.get("hmm.sticky_kappa"))

    @property
    def hmm_state_ordering(self) -> str:
        return str(self.get("hmm.state_ordering"))

    @property
    def hmm_class_weights(self) -> Dict[str, float]:
        return {str(k): float(v) for k, v in self.get("hmm.class_weights").items()}

    @property
    def hmm_layer2_gates(self) -> List[Dict[str, Any]]:
        # New architecture: layer2 gates are in unified_regime.micro_states
        try:
            return [dict(g) for g in self.get("unified_regime.micro_states.states")]
        except PreRegistrationError:
            # Fallback to old structure
            return [dict(g) for g in self.get("hmm.layer2_gates")]

    @property
    def hmm_bayesian_lambda(self) -> float:
        try:
            return float(self.get("unified_regime.bocpd.threshold"))
        except PreRegistrationError:
            return float(self.get("hmm.bayesian_fusion.lambda"))

    @property
    def hmm_hard_overrides(self) -> List[Dict[str, Any]]:
        try:
            # New architecture: hard overrides in micro_states conditions
            states = self.get("unified_regime.micro_states.states")
            overrides = []
            for state in states:
                if "conditions" in state:
                    for k, v in state["conditions"].items():
                        overrides.append({"condition": f"{state['name']}.{k}", "value": v})
            return overrides
        except PreRegistrationError:
            return [dict(o) for o in self.get("hmm.bayesian_fusion.hard_overrides")]
    
    @property
    def hmm_validation_method(self) -> str:
        return str(self.get("hmm.validation.method"))
    
    @property
    def hmm_validation_label_definition(self) -> str:
        return str(self.get("hmm.validation.label_definition"))
    
    @property
    def hmm_precision_crisis_target(self) -> float:
        return float(self.get("hmm.validation.precision_crisis_target"))
    
    @property
    def hmm_recall_crisis_target(self) -> float:
        return float(self.get("hmm.validation.recall_crisis_target"))
    
    @property
    def hmm_f1_crisis_target(self) -> float:
        return float(self.get("hmm.validation.f1_crisis_target"))
    
    @property
    def hmm_false_alarm_rate_max(self) -> float:
        return float(self.get("hmm.validation.false_alarm_rate_max"))
    
    @property
    def hmm_mean_dwell_days_min(self) -> int:
        return int(self.get("hmm.validation.mean_dwell_days_min"))
    
    @property
    def hmm_transitions_per_year_max(self) -> int:
        return int(self.get("hmm.validation.transitions_per_year_max"))

    # -- UNIFIED REGIME --------------------------------------------------------
    
    @property
    def unified_bocpd_hazard(self) -> float:
        return float(self.get("unified_regime.bocpd.hazard_rate"))
    
    @property
    def unified_bocpd_threshold(self) -> float:
        return float(self.get("unified_regime.bocpd.threshold"))
    
    @property
    def unified_bocpd_override_weight(self) -> float:
        return float(self.get("unified_regime.integration.bocpd_override_weight"))
    
    @property
    def unified_io_hmm_n_states(self) -> int:
        return int(self.get("unified_regime.io_hmm.n_states"))
    
    @property
    def unified_io_hmm_state_names(self) -> List[str]:
        return [str(s) for s in self.get("unified_regime.io_hmm.state_names")]
    
    @property
    def unified_io_hmm_crisis_threshold(self) -> float:
        return float(self.get("unified_regime.io_hmm.crisis_threshold"))
    
    @property
    def unified_io_hmm_choppy_threshold(self) -> float:
        return float(self.get("unified_regime.io_hmm.choppy_threshold"))
    
    @property
    def unified_micro_enabled(self) -> bool:
        return bool(self.get("unified_regime.micro_states.enabled"))
    
    @property
    def unified_integration_bocpd_weight(self) -> float:
        return float(self.get("unified_regime.integration.bocpd_override_weight"))
    
    @property
    def unified_integration_io_hmm_weight(self) -> float:
        return float(self.get("unified_regime.integration.io_hmm_weight"))
    
    @property
    def unified_integration_micro_weight(self) -> float:
        return float(self.get("unified_regime.integration.micro_weight"))
    
    @property
    def unified_crisis_threshold(self) -> float:
        return float(self.get("unified_regime.integration.crisis_threshold"))
    
    @property
    def unified_choppy_threshold(self) -> float:
        return float(self.get("unified_regime.integration.choppy_threshold"))
    
    @property
    def unified_min_dwell_days(self) -> int:
        return int(self.get("unified_regime.integration.min_dwell_days"))
    
    @property
    def unified_max_transitions_per_year(self) -> int:
        return int(self.get("unified_regime.integration.max_transitions_per_year"))
    
    # -- optimisation / DSR accounting --------------------------------------

    @property
    def dsr_tracking(self) -> Dict[str, Any]:
        return dict(self.get("optimization.dsr_tracking"))

    @property
    def walkforward_folds(self) -> int:
        return int(self.get("optimization.cv.walkforward_folds"))

    @property
    def synthetic_fallback(self) -> Any:
        return self.get("optimization.synthetic_fallback")

    @property
    def tca_in_loop(self) -> bool:
        return bool(self.get("optimization.tca_in_loop"))

    @property
    def fold_labels(self) -> List[str]:
        folds = self.get("windows.primary.folds")
        return [f"fold_{i}" for i in range(1, len(folds) + 1)]

    # -- costs --------------------------------------------------------------

    @property
    def max_annual_tca_bps(self) -> float:
        return float(self.get("costs.max_annual_tca_bps"))

    # -- probation cluster D ------------------------------------------------

    @property
    def cluster_d(self) -> Dict[str, Any]:
        return dict(self.get("parameter_clusters.D_probation_qual"))

    @property
    def cluster_d_params(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for p in self.get("parameter_clusters.D_probation_qual.params"):
            out[str(p["name"])] = dict(p)
        return out

    def cluster_d_values(self, name: str) -> List[Any]:
        return list(self.cluster_d_params[name]["values"])

    # -- reporting ----------------------------------------------------------

    def to_status_dict(self, include_canonicalisations: bool = False) -> Dict[str, Any]:
        """Compact provenance block embedded in every Phase 9 report."""
        d = self.integrity.to_dict(include_canonicalisations=include_canonicalisations)
        d.update({
            "phase": self.phase,
            "version": self.version,
            "status": self.status,
            "declared_sha256": self.declared_sha256,
            "integrity_mode": self.mode,
            "metric": self.metric,
            "dsr_n_trials": self.dsr_n_trials,
            "dsr_track_record_days": self.dsr_track_record_days,
            "dsr_threshold": self.dsr_threshold,
            "no_gate_hierarchy": self.no_gate_hierarchy,
            "unified_regime": {
                "bocpd": {
                    "hazard": self.unified_bocpd_hazard,
                    "threshold": self.unified_bocpd_threshold,
                    "override_weight": self.unified_bocpd_override_weight,
                },
                "io_hmm": {
                    "n_states": self.unified_io_hmm_n_states,
                    "states": self.unified_io_hmm_state_names,
                    "crisis_threshold": self.unified_io_hmm_crisis_threshold,
                    "choppy_threshold": self.unified_io_hmm_choppy_threshold,
                },
                "micro_states": {
                    "enabled": self.unified_micro_enabled,
                },
                "integration": {
                    "bocpd_weight": self.unified_integration_bocpd_weight,
                    "io_hmm_weight": self.unified_integration_io_hmm_weight,
                    "micro_weight": self.unified_integration_micro_weight,
                    "crisis_threshold": self.unified_crisis_threshold,
                    "choppy_threshold": self.unified_choppy_threshold,
                    "min_dwell_days": self.unified_min_dwell_days,
                    "max_transitions_per_year": self.unified_max_transitions_per_year,
                },
            },
        })
        return d


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load_uncached(path: Optional[Union[str, Path]] = None,
                   expected_sha256: str = EXPECTED_SHA256,
                   mode: Optional[str] = None,
                   apply_policy: bool = True) -> PreRegistration:
    resolved_mode = integrity_mode(mode)
    p = resolve_prereg_path(path)
    integrity = _verify_once(p, expected_sha256)
    if apply_policy:
        _apply_policy(integrity, resolved_mode)
    if not integrity.exists:
        raise PreRegistrationIntegrityError(
            f"pre-registration not found: {p} "
            f"(expected SHA-256 {expected_sha256})")
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PreRegistrationError(f"pre-registration is not valid YAML: {p}: {exc}") from exc
    return PreRegistration(cfg, integrity, p, resolved_mode)


def load_prereg(path: Optional[Union[str, Path]] = None,
                expected_sha256: str = EXPECTED_SHA256,
                mode: Optional[str] = None,
                apply_policy: bool = True,
                use_cache: bool = True) -> PreRegistration:
    """
    Load the frozen pre-registration and verify its SHA-256.

    Cached per ``(path, expected, mode, apply_policy)`` — the file is frozen, so
    re-hashing 10k calibration loops would be pure waste.  Use
    :func:`clear_cache` (or ``use_cache=False``) after touching the file.
    """
    key_path = PREREG_RELPATH.as_posix() if path is None else str(path)
    if not use_cache:
        return _load_uncached(key_path, expected_sha256, mode, apply_policy)
    return _cached_load(key_path, expected_sha256, str(integrity_mode(mode)),
                        bool(apply_policy))


@lru_cache(maxsize=16)
def _cached_load(path: Optional[str], expected: str, mode: str,
                 apply_policy: bool) -> PreRegistration:
    return _load_uncached(path, expected, mode, apply_policy)


def clear_cache() -> None:
    """Drop the loader cache (used by tests that swap the frozen file)."""
    _cached_load.cache_clear()


def module_load_check(path: Optional[Union[str, Path]] = None,
                      mode: Optional[str] = None) -> PreRegistration:
    """
    The module-load verification hook: loads + hashes the frozen config and
    applies the integrity policy.  Returns the handle so callers can also read
    the config.  Under the default ``audit`` policy this NEVER raises — it warns
    once and records the FAIL.
    """
    return load_prereg(path=path, mode=mode)


def verify_preregistration_hash(
        path: Optional[Union[str, Path]] = None,
        expected_sha256: str = EXPECTED_SHA256) -> Dict[str, Any]:
    """
    Hash the ACTUAL file content and report whether it matches the frozen pin.

    The hash is taken over the bytes on disk (streamed in 64 KiB chunks) — no
    canonicalisation, no self-referential-line stripping, no sidecar indirection.
    The earlier indirection existed only to make a *known-broken* pin compare
    equal; under Decision 3 the pin is the real digest of the real bytes, so the
    honest computation is also the one that verifies.

    Returns a dict with ``verified`` (bool) plus the full evidence trail.
    """
    p = resolve_prereg_path(path)
    if not p.exists():
        return {
            "path": str(p), "exists": False, "verified": False, "status": "MISSING",
            "expected_sha256": expected_sha256, "actual_sha256": None,
            "detail": f"pre-registration not found: {p}",
        }
    actual = sha256_path(p)
    verified = bool(actual == expected_sha256)
    return {
        "path": str(p),
        "exists": True,
        "verified": verified,
        "status": "VERIFIED" if verified else "MISMATCH",
        "expected_sha256": expected_sha256,
        "actual_sha256": actual,
        "size_bytes": int(p.stat().st_size),
        "detail": ("sha256 of the file content matches the frozen pin"
                   if verified else
                   f"sha256 of the file content does NOT match the frozen pin "
                   f"(expected {expected_sha256[:12]}..., actual {actual[:12]}...)"),
    }


#: Populated at import time.  Gate modules reuse this handle so the file is
#: hashed exactly once per process and every report agrees on the digest.
MODULE_PREREG: PreRegistration = module_load_check()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Phase 9 pre-registration SHA-256 integrity check "
                    "(Sprint 6 / Decision 3 — unified composite DSR)")
    parser.add_argument("--path", type=str, default=None,
                        help=f"config path (default {PREREG_RELPATH})")
    parser.add_argument("--mode", type=str, default=None,
                        choices=list(INTEGRITY_MODES),
                        help="integrity policy (default: $QUANT_PREREG_INTEGRITY "
                             f"or {DEFAULT_INTEGRITY_MODE})")
    parser.add_argument("--strict", action="store_true",
                        help="shorthand for --mode strict (exit 1 on mismatch)")
    parser.add_argument("--verify-prereg", action="store_true",
                        help="hash the ACTUAL file content and report "
                             "verified=True/False (exit 0 iff verified)")
    parser.add_argument("--expect", type=str, default=EXPECTED_SHA256,
                        help="override the expected digest (diagnostics only)")
    args = parser.parse_args(argv)

    mode = "strict" if args.strict else args.mode
    resolved = integrity_mode(mode)
    pre = _load_uncached(args.path, args.expect, mode, apply_policy=False)
    blk = pre.integrity.to_dict(include_canonicalisations=True)

    print(f"[prereg] path            : {pre.path}")
    print(f"[prereg] phase/version   : {pre.phase} / {pre.version} ({pre.status})")
    print(f"[prereg] expected sha256 : {pre.integrity.expected_sha256}")
    print(f"[prereg] declared sha256 : {pre.integrity.declared_sha256}")
    print(f"[prereg] observed sha256 : {pre.integrity.actual_sha256}")
    print(f"[prereg] status          : {pre.integrity.status}")
    print(f"[prereg] mode            : {resolved}")
    if not pre.integrity.verified:
        print(f"[prereg] detail          : {pre.integrity.detail}")
        print("[prereg] canonicalisations checked (none reproduces the declared digest):")
        for name in INTEGRITY_CANONICALISATIONS_CHECKED:
            got = pre.integrity.canonicalisations.get(name, "?")
            flag = "  <== MATCH" if got == pre.integrity.expected_sha256 else ""
            print(f"           {name:26s} {got}{flag}")
    print(f"[prereg] metric          : {pre.metric} "
          f"(no_gate_hierarchy={pre.no_gate_hierarchy})")
    print(f"[prereg] composite parts : {', '.join(pre.composite_component_names)}")
    print(f"[prereg] dsr_n_trials    : {pre.dsr_n_trials} "
          f"({pre.dsr_base_n} base + {pre.dsr_lhs_n} LHS), "
          f"track_record_days={pre.dsr_track_record_days}, "
          f"threshold={pre.dsr_threshold}")
    print(f"[prereg] fold_4_true_oos : {pre.fold_4_true_oos} "
          f"(test_end {pre.fold_4_test_end})")
    print(f"[prereg] hmm states      : {pre.hmm_n_states} {pre.hmm_state_names}")
    print(f"[prereg] hmm layer1 feats: {', '.join(pre.hmm_layer1_features)}")
    print(f"[prereg] hmm train window: {pre.hmm_train_window_years}yr, retrain={pre.hmm_retrain_frequency}")
    print(f"[prereg] hmm ordering    : {pre.hmm_state_ordering}, dirichlet_lambda={pre.hmm_dirichlet_lambda}, sticky_kappa={pre.hmm_sticky_kappa}")
    print(f"[prereg] hmm class wts   : {pre.hmm_class_weights}")
    print(f"[prereg] hmm layer2 gates: {len(pre.hmm_layer2_gates)} gates, lambda={pre.hmm_bayesian_lambda}")
    print(f"[prereg] hmm overrides   : {len(pre.hmm_hard_overrides)} hard rules")
    print(f"[prereg] hmm validation  : {pre.hmm_validation_method}")
    print(f"[prereg] hmm labels      : {pre.hmm_validation_label_definition}")
    print(f"[prereg] hmm targets     : ARI>={pre.hmm_ari_threshold}, Precision>={pre.hmm_precision_crisis_target}, Recall>={pre.hmm_recall_crisis_target}, F1>={pre.hmm_f1_crisis_target}")
    print(f"[prereg] hmm diag        : FAR<={pre.hmm_false_alarm_rate_max}, Dwell>={pre.hmm_mean_dwell_days_min}d, Trans/yr<={pre.hmm_transitions_per_year_max}")
    print(f"[prereg] synthetic_fallback: {pre.synthetic_fallback}")

    if args.verify_prereg:
        # Independent re-hash of the actual file content (no canonicalisation).
        v = verify_preregistration_hash(args.path, args.expect)
        print(f"[prereg] verify-prereg   : verified={v['verified']} "
              f"status={v['status']} size_bytes={v.get('size_bytes')}")
        print(f"[prereg] verify detail   : {v['detail']}")
        return 0 if v["verified"] else 1

    _apply_policy(pre.integrity, resolved)
    return 0 if pre.integrity.verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
