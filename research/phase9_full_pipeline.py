"""
phase9_full_pipeline.py — Atomic Orchestrator for Phase 9 Core Infrastructure
Pre-Registration Config: config/pre_registration_v7.yaml (SHA-256: c80a47bf...)

Execution Steps:
  1. Unified regime provenance gate — the calibrated params must exist and
     match their pinned SHA-256 sidecar.  It can fail, and it does.
  2. Joint ML optimization across 4 clusters (500 LHS Sobol samples) with
     purged / embargoed CV.
  3. TrialTracker logging EVERY parameter set for honest DSR accounting, N=513.
  4. The ONE frozen v7 gate: CompositeGateEngine composite_dsr >= 0.95.
     There is no 8-gate hierarchy — v7 deleted it
     (statistical_design.no_gate_hierarchy: true).
  5. Save the report and artifacts with SHA-256 provenance.

WHY THIS FILE USED TO LOOK DIFFERENT
------------------------------------
The previous revision of :meth:`Phase9Orchestrator.evaluate_8_gates` did not
evaluate anything.  It assigned constants —

    g1_detail      = {"anova_p": 0.0012, "welch_c_vs_a_p": 0.0021, ...}
    arm_c_sharpe   = 1.42
    arm_c_mdd      = -0.145
    ir_vs_spy      = 0.58
    calmar         = 2.85
    wacc_coverage  = 1.0
    probation      = 0.48
    tca_bps        = 85.0
    g1_passed = True ; g7_passed = True

— hardcoded every gate to ``passed=True``, and wrote the result to
``data/phase9_full/phase9_execution_summary.json`` with a SHA-256 sidecar.  That
is a cryptographically-signed artifact asserting that the Phase 9 study passed,
manufactured without running the study, sitting in the directory the provenance
auditor reads.  A hash chain over invented numbers is not provenance; it is a
signature on a fabrication.

The replacement calls the real :class:`CompositeGateEngine`, and when there is
no out-of-sample evidence to evaluate it raises
:class:`GateEvaluationUnavailable` instead of inventing a plausible constant.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import yaml

from ingestion.harness.gate_engine import CompositeGateEngine
from research.ml_parameter_optimization import (
    PurgedEmbargoCV,
    TrialTracker,
    run_joint_optimization,
    generate_lhs_sobol_samples,
)

logger = logging.getLogger(__name__)

FROZEN_CONFIG_PATH = "config/pre_registration_v7.yaml"


class GateEvaluationUnavailable(RuntimeError):
    """
    Raised when there is no real OOS evidence to evaluate the gate on.

    Replaces the fabricated-gate behaviour described in the module docstring.
    """


def verify_preregistration_hash() -> bool:
    """
    Verify the pre-registration YAML against its frozen SHA-256 sidecar.

    The sidecar must carry a bare digest.  ``sha256sum`` writes
    ``<digest>  <filename>``; taking ``.split()[0]`` accepts both the
    hand-written bare form and the tool-generated two-token form so they verify
    identically.

    This function previously ended ``return actual == expected or len(expected)
    > 0``.  The right operand is True for every non-empty sidecar, so the check
    was a tautology: it returned True for a tampered pre-registration and
    reported it as verified.  It now returns the comparison and nothing else.
    """
    config_path = Path(FROZEN_CONFIG_PATH)
    sidecar_path = config_path.with_suffix(config_path.suffix + ".sha256")

    if not config_path.exists() or not sidecar_path.exists():
        return False

    tokens = sidecar_path.read_text(encoding="utf-8").split()
    if not tokens:
        return False
    expected_hash = tokens[0].strip().lower()
    if len(expected_hash) != 64:
        raise ValueError(
            f"sidecar is not a SHA-256 digest: {sidecar_path} -> {expected_hash!r}")
    actual_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    return actual_hash == expected_hash


class Phase9Orchestrator:
    """Atomic Orchestrator executing Phase 9 against the frozen v7 gate."""

    def __init__(self, output_dir: str = "data/phase9_full",
                 track_record_days: int = 2520):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.track_record_days = int(track_record_days)
        self.tracker = TrialTracker(base_n=13, target_n=513,
                                    track_record_days=self.track_record_days)

    # -- step 1 ------------------------------------------------------------

    def execute_hmm_gate(self) -> bool:
        """
        Verify the unified-regime artifact is the calibrated one and is pinned.

        The previous version caught every exception, logged a warning and then
        set ``hmm_passed = True`` regardless — an HMM gate that could not fail.
        """
        logger.info("--- Step 1: Unified Regime Provenance Gate ---")
        params_path = Path("config/calibrated_unified_regime_params.json")
        sidecar = params_path.with_suffix(params_path.suffix + ".sha256")

        if not params_path.exists():
            raise GateEvaluationUnavailable(
                f"unified regime params missing: {params_path}. The gate is not "
                f"waivable; the canonical 6-feature contract has no artifact.")

        actual = hashlib.sha256(params_path.read_bytes()).hexdigest()
        if not sidecar.exists():
            raise GateEvaluationUnavailable(
                f"{sidecar} is missing; the regime params are unpinned.")
        declared = sidecar.read_text(encoding="utf-8").split()[0].strip().lower()
        if actual != declared:
            raise GateEvaluationUnavailable(
                f"unified regime params do NOT match their pinned digest "
                f"(declared {declared[:12]}..., actual {actual[:12]}...)")

        params = json.loads(params_path.read_text(encoding="utf-8"))
        logger.info(f"Verified {params_path} sha256={actual[:12]}... "
                    f"(source={params.get('source')})")
        return True

    # -- steps 2 & 3 -------------------------------------------------------

    def execute_parameter_search(self, n_samples: int = 500,
                                 dry_run: bool = False) -> Dict[str, Any]:
        """Joint ML optimization + TrialTracker logging of every parameter set."""
        logger.info(f"--- Step 2: Joint ML Parameter Search (N={n_samples}) ---")
        best_params, tracker = run_joint_optimization(
            lhs_samples=n_samples,
            clusters=["A_wacc_regime", "B_swap_exit", "C_entry_thresholds",
                      "D_probation_qual"],
            cv_class=PurgedEmbargoCV,
            metric="composite_dsr",
            dry_run=dry_run,
        )
        self.tracker = tracker
        return best_params

    # -- step 4 ------------------------------------------------------------

    def evaluate_gates(self, evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Evaluate the ONE frozen v7 gate: composite DSR >= 0.95.

        ``evidence`` must carry the real out-of-sample artefacts:

            oos_returns            Arm C concatenated 4-fold OOS returns
            arm_c_weights          Arm C weight path
            spy_returns            SPY, aligned to ``oos_returns``
            hmm_ari                HMM ARI on the concatenated folds
            swap_churn             swaps/year
            tca_bps                round-trip TCA in bps
            probation_pass_rate    probation pass rate in [0, 1]
            fold_returns           optional per-fold series for the Fold 4 test

        Without evidence this raises.  It does not guess, and it does not
        substitute a plausible-looking constant.
        """
        logger.info("--- Step 4: Composite DSR Gate (v7: no gate hierarchy) ---")

        required = ("oos_returns", "arm_c_weights", "spy_returns", "hmm_ari",
                    "swap_churn", "tca_bps", "probation_pass_rate")
        if not evidence:
            raise GateEvaluationUnavailable(
                "No OOS evidence supplied to the Phase 9 gate. Required: "
                f"{', '.join(required)} (fold_returns optional). Refusing to "
                "substitute placeholder metrics — see GateEvaluationUnavailable.")
        missing = [k for k in required if evidence.get(k) is None]
        if missing:
            raise GateEvaluationUnavailable(
                f"OOS evidence is incomplete; missing {missing}.")

        engine = CompositeGateEngine()
        result = engine.evaluate_composite_dsr(
            oos_returns=evidence["oos_returns"],
            arm_c_weights=evidence["arm_c_weights"],
            spy_returns=evidence["spy_returns"],
            hmm_ari=float(evidence["hmm_ari"]),
            swap_churn=float(evidence["swap_churn"]),
            tca_bps=float(evidence["tca_bps"]),
            probation_pass_rate=float(evidence["probation_pass_rate"]),
            fold_returns=evidence.get("fold_returns"),
        )
        payload = result.to_dict()
        payload["all_gates_pass"] = bool(result.passed)
        logger.info(f"composite_dsr={result.composite_dsr:.6f} "
                    f"threshold={result.threshold} passed={result.passed}")
        return payload

    def write_halt_record(self, reason: str) -> Path:
        """
        Write an honest ``NOT_EXECUTED`` record for a run that could not reach
        the gate.

        The data estate expects this file to exist, and the right contents for
        a halted run are "this did not run, and here is why" — not a plausible
        SUCCESS. This is the truthful counterpart to the quarantined
        fabrication, and it is signed with a sidecar like every other artifact.
        """
        config_path = Path(FROZEN_CONFIG_PATH)
        record = {
            "status": "NOT_EXECUTED",
            "gate": "composite_dsr (v7, no gate hierarchy)",
            "gate_evaluated": False,
            "reason": reason,
            "pre_registration_sha256": (
                hashlib.sha256(config_path.read_bytes()).hexdigest()
                if config_path.exists() else None),
            "trials_logged": len(self.tracker.trials),
            "note": (
                "No out-of-sample evidence was available, so the frozen gate "
                "was not evaluated. There is no performance claim in this file. "
                "A prior artifact of this name carried hardcoded gate constants "
                "and a signed SUCCESS; it was quarantated with the .FABRICATED "
                "suffix and must not be cited."),
        }
        out = self.output_dir / "phase9_execution_summary.json"
        payload = json.dumps(record, indent=2).encode("utf-8")
        with open(out, "wb") as fh:
            fh.write(payload)
        with open(out.with_suffix(out.suffix + ".sha256"), "wb") as fh:
            fh.write((hashlib.sha256(payload).hexdigest() + "\n").encode("utf-8"))
        return out

    # -- step 5 ------------------------------------------------------------

    def run_pipeline(self, n_samples: int = 500, dry_run: bool = False,
                     evidence: Optional[Dict[str, Any]] = None,
                     write_summary: bool = True) -> Dict[str, Any]:
        """
        Run the complete Phase 9 pipeline atomically.

        Atomic means ALL of it: if any step raises, no ``SUCCESS`` artifact is
        written.  ``write_summary=False`` is the honest mode for a run whose gate
        could not be evaluated.
        """
        self.execute_hmm_gate()
        best_params = self.execute_parameter_search(n_samples=n_samples,
                                                    dry_run=dry_run)
        gates_summary = self.evaluate_gates(evidence)

        config_path = Path(FROZEN_CONFIG_PATH)
        prereg_sha = (hashlib.sha256(config_path.read_bytes()).hexdigest()
                      if config_path.exists() else "")

        pipeline_output = {
            "status": "SUCCESS" if gates_summary["all_gates_pass"] else "GATE_FAILURE",
            "pre_registration_sha256": prereg_sha,
            "best_params": best_params,
            "gates_summary": gates_summary,
            "trials_logged": len(self.tracker.trials),
        }

        if not write_summary:
            return pipeline_output

        output_file = self.output_dir / "phase9_execution_summary.json"
        # Hash the exact bytes written. write_text() would translate \n to
        # \r\n on Windows, producing a sidecar that never matches the file.
        payload = json.dumps(pipeline_output, indent=2).encode("utf-8")
        with open(output_file, "wb") as fh:
            fh.write(payload)
        with open(output_file.with_suffix(output_file.suffix + ".sha256"),
                  "wb") as fh:
            fh.write((hashlib.sha256(payload).hexdigest() + "\n").encode("utf-8"))

        self.tracker.save_ledger(self.output_dir / "trial_ledger.json")
        logger.info(f"Phase 9 execution finished. Output written to {output_file}")
        return pipeline_output


def _evidence_from_json(path: Path) -> Dict[str, Any]:
    """
    Load real OOS evidence from a JSON file.

    Shape::

        {
          "oos_returns":       {"index": [...ISO dates...], "values": [...]},
          "arm_c_weights":     {"index": [...], "columns": [...], "values": [[...]]},
          "spy_returns":       {"index": [...], "values": [...]},
          "fold_returns":      {"fold_1": {"index": [...], "values": [...]}, ...},
          "hmm_ari": 0.45, "swap_churn": 3.2, "tca_bps": 88.0,
          "probation_pass_rate": 0.5
        }

    No defaults are invented for any of it: a missing key is a missing key.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    def _series(node: Dict[str, Any]) -> pd.Series:
        return pd.Series([float(v) for v in node["values"]],
                         index=pd.to_datetime(list(node["index"])),
                         dtype=float)

    out: Dict[str, Any] = {
        "oos_returns": _series(raw["oos_returns"]),
        "spy_returns": _series(raw["spy_returns"]),
        "arm_c_weights": pd.DataFrame(
            [[float(v) for v in row] for row in raw["arm_c_weights"]["values"]],
            index=pd.to_datetime(list(raw["arm_c_weights"]["index"])),
            columns=list(raw["arm_c_weights"]["columns"]), dtype=float),
        "hmm_ari": raw["hmm_ari"],
        "swap_churn": raw["swap_churn"],
        "tca_bps": raw["tca_bps"],
        "probation_pass_rate": raw["probation_pass_rate"],
    }
    if "fold_returns" in raw:
        out["fold_returns"] = {k: _series(v) for k, v in raw["fold_returns"].items()}
    return out


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Phase 9 Atomic Orchestrator (frozen v7: composite DSR gate)")
    parser.add_argument("--lhs-samples", type=int, default=500,
                        help="LHS sample count (500 frozen + 13 base = 513)")
    parser.add_argument("--output", type=str, default="data/phase9_full/",
                        help="Output directory")
    parser.add_argument("--evidence", type=str, default=None,
                        help="JSON file of real OOS evidence. Without it the "
                             "gate raises GateEvaluationUnavailable and NO "
                             "SUCCESS artifact is written.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Structural smoke run over the frozen grids; the "
                             "gate is NOT evaluated and no summary is written.")
    args = parser.parse_args(argv)

    orchestrator = Phase9Orchestrator(output_dir=args.output)

    if args.dry_run:
        # Exercise the machinery only.  Deliberately does not reach the gate:
        # a dry run that cannot produce evidence must not produce a verdict.
        orchestrator.execute_hmm_gate()
        best, tracker = run_joint_optimization(
            lhs_samples=10, cv_class=PurgedEmbargoCV, metric="composite_dsr",
            dry_run=True)
        verify = tracker.verify_chain()
        print(f"--- PHASE 9 DRY RUN ---")
        print(f"Regime provenance gate : PASS")
        print(f"Trial chain schema    : {verify['ok']} "
              f"({verify['n_trials']} trials, head {verify['chain_head'][:12]}...)")
        print(f"Best params (grid)    : {len(best)} keys")
        print("Gate                  : NOT EVALUATED (no OOS evidence in a dry run)")
        return 0

    evidence = _evidence_from_json(Path(args.evidence)) if args.evidence else None
    try:
        res = orchestrator.run_pipeline(n_samples=args.lhs_samples,
                                        evidence=evidence)
    except GateEvaluationUnavailable as exc:
        logger.error("PHASE 9 HALTED: %s", exc)
        record = orchestrator.write_halt_record(str(exc))
        print("\n--- PHASE 9 PIPELINE HALTED ---")
        print(f"Reason            : {exc}")
        print(f"Record written    : {record}")
        print("Status            : NOT_EXECUTED — no gate verdict was produced, and")
        print("                    none was invented. The previous signed artifact")
        print("                    on disk was built from hardcoded constants and is")
        print(f"                    quarantined at {record.name}.FABRICATED.")
        return 2

    print("\n--- PHASE 9 PIPELINE EXECUTION SUMMARY ---")
    print(f"Status           : {res['status']}")
    print(f"composite_dsr    : {res['gates_summary']['composite_dsr']:.6f} "
          f"(threshold {res['gates_summary']['threshold']})")
    print(f"Fold 4 DSR       : {res['gates_summary']['fold_4_dsr']}")
    print(f"Trials logged    : {res['trials_logged']}")
    print(f"Output           : {orchestrator.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
