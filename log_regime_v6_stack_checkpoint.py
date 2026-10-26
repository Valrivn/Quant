"""College audit logger — Phase 9 regime v6 3-LAYER STACK bring-up, 2026-09-28.

Companion to log_regime_v6_checkpoint.py. Records the defects found while
bringing the frozen v6 architecture (BOCPD -> IO-HMM -> Micro-States) to a
runnable state, and the one structural gap that remains open.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path("logs/college/2026-09-28")
LOG_FILE = LOG_DIR / "college.jsonl"

RESERVED = {
    "meta", "ts", "level", "logger", "message", "line", "func", "name", "args",
    "exc_info", "exc_text", "stack_info", "levelname", "pathname", "filename",
    "module", "msecs", "relativeCreated", "thread", "threadName", "process",
    "processName", "taskName",
}


class JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "line": record.lineno,
            "func": record.funcName,
        }
        for key, value in record.__dict__.items():
            if key not in RESERVED and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, default=str)


def build_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("quant_audit")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        handler.setFormatter(JsonlFormatter())
        logger.addHandler(handler)
    return logger


def emit(logger: logging.Logger, event: str, **fields) -> None:
    logger.info(f"AUDIT_EVENT: {event} | {fields}", extra={"event": event, **fields})


if __name__ == "__main__":
    lg = build_logger()

    emit(
        lg,
        "architectural_finding_validation_path_is_not_v6",
        severity="CRITICAL",
        finding=(
            "The ARI=0.0886 validation result does NOT measure the frozen v6 architecture. "
            "Quantitative/regime_shield_v3.py (the --validate CLI) imports only "
            "load_fred_cache and RegimeDetector from hmm_regime, and runs its own "
            "ClassWeightedGaussianHMM / TwoLayerRegimeEngine. It imports NONE of "
            "Quantitative.bocpd, Quantitative.io_hmm, Quantitative.micro_states, or "
            "Quantitative.unified_regime. The v6 3-layer stack was never exercised."
        ),
        prereg_version_actually_loaded="v5 (regime_shield_v3.py:642)",
        prereg_version_frozen_and_pinned="v6 (ingestion/harness/prereg.py:98 PREREG_RELPATH, :104 EXPECTED_SHA256)",
        prereg_v6_sha256="390084e01fae6a358ab66b5ee536ccaf4d5d85bc6ec4dd05103cb74d5a5c5b10",
        implication=(
            "The 0.0886 figure is evidence about a v5-era single-layer class-weighted HMM. "
            "It is NOT evidence that the v6 BOCPD/IO-HMM/Micro-States design fails. No v6 "
            "ARI number exists yet."
        ),
        status="OPEN — requires a validation entry point wired to unified_regime.",
    )

    emit(
        lg,
        "prereg_naming_inconsistency_v6",
        severity="LOW",
        component="config/pre_registration_v6.yaml",
        finding=(
            "The same hmm block names the middle state inconsistently: "
            "hmm.state_names = ['EXPANSION','CHOPPY','CRISIS'] while "
            "hmm.class_weights keys and hmm.validation.label_definition both use 'SHOCK'. "
            "unified_regime.py and io_hmm use CHOPPY; regime_shield_v3.py and the label "
            "generator use SHOCK."
        ),
        status="OPEN — cosmetic for now, but a latent mismatch for any ARI/confusion-matrix "
               "join keyed on state name.",
    )

    emit(
        lg,
        "blocker_resolved",
        component="Quantitative/unified_regime.py::initialize",
        defect=(
            "UnboundLocalError: 'IOHMMConfig'. IOHMMConfig was first referenced at the "
            "IO-HMM construction site, then re-imported inside the function body further "
            "down, so Python scoped the name as a function-local and the earlier use "
            "bound to nothing."
        ),
        secondary_defect=(
            "bocpd_cfg was built from self.config and then discarded in favour of a "
            "hard-coded BOCPDConfig(hazard_rate=0.01, threshold=0.95), so the "
            "config-driven BOCPD parameters had no effect."
        ),
        resolution=(
            "Removed the in-function re-imports; both components now build from "
            "self.config (bocpd_hazard/bocpd_threshold, io_hmm_states). The config "
            "defaults are identical to the previously hard-coded values, so this is "
            "behaviour-preserving while removing the drift risk."
        ),
        verification="BOCPD and IO-HMM both initialize; engine proceeds to decision logic.",
    )

    emit(
        lg,
        "blocker_resolved",
        component="Quantitative/micro_states.py",
        defect="NameError: name 'softmax' is not defined (used at line 201, never imported).",
        resolution="Added 'from scipy.special import softmax', matching the convention in io_hmm.py:25.",
        verification="Micro-state classification now returns STAGFLATION for the current snapshot.",
    )

    emit(
        lg,
        "blocker_resolved",
        component="Quantitative/unified_regime.py::_compile_actions",
        defect=(
            "NameError: name 'macro_name' is not defined. Callers pass macro_map (an int) "
            "but the body tested macro_name against the strings 'CRISIS' and 'CHOPPY'."
        ),
        resolution=(
            "Resolve the name from the index inside the method via "
            "tuple(self.config.io_hmm_states)[macro_map], falling back to 'CRISIS' "
            "out of range."
        ),
        verification="Actions now compile; CRISIS/CHOPPY branches reachable.",
    )

    emit(
        lg,
        "blocker_resolved",
        component="Quantitative/unified_regime.py::_compute_io_hmm_inputs",
        defect=(
            "TypeError: missing 1 required positional argument 'data'. update() called it "
            "with no args. The value it returns is also never consumed — the live path uses "
            "_get_latest_io_inputs() instead."
        ),
        resolution=(
            "Made 'data' optional (falls back to self._load_market_data()) and documented "
            "that the return is a batch helper, not the live path. Did NOT rewire the live "
            "path — see open_defect_io_hmm_never_fitted."
        ),
        status="Resolved for the crash; the dead-code/training gap is logged separately.",
    )

    emit(
        lg,
        "blocker_resolved",
        component="Quantitative/unified_regime.py::_get_latest_io_inputs",
        defect=(
            "IO-HMM belief was [nan nan nan]. Root cause: "
            "curve_level = (DGS10 - DGS2).iloc[-1] / 100.0 subtracts two Series whose last "
            "observation dates differ (DGS10 ends 2026-09-10, DGS2 ends 2026-09-24). Pandas "
            "aligns on the index UNION, so every date past the shorter leg is NaN and "
            ".iloc[-1] selects that NaN. The NaN propagated through the emission log-probs "
            "into the forward step and softmax."
        ),
        resolution=(
            "Insert .dropna() before .iloc[-1] so the value is taken from the last date where "
            "BOTH legs are known. This is also the PIT-correct choice: it does not ffill a "
            "stale leg forward to manufacture a curve reading."
        ),
        verification="IO-HMM belief is now finite and normalised: [0.59996368, 0.30000454, 0.10003178].",
        prior_reading="IO-HMM: [nan nan nan]",
    )

    emit(
        lg,
        "v6_stack_smoke_result",
        command="python -m Quantitative.unified_regime",
        result="RAN TO COMPLETION",
        current_snapshot={
            "regime_name": "EXPANSION/STAGFLATION",
            "confidence": 0.75,
            "macro_state": "EXPANSION",
            "micro_state": "STAGFLATION",
            "bocpd_alarm": False,
            "bocpd_p_r0": 0.010,
            "io_hmm_belief": [0.59996368, 0.30000454, 0.10003178],
            "actions": {
                "macro_action": "HOLD",
                "micro_action": "ROUTE_CASH_TO_GOLD",
                "wacc_adjustment_bps": 0,
            },
        },
        compile_gate="All 6 v6 modules py_compile clean: hmm_regime, unified_regime, "
                     "micro_states, io_hmm, bocpd, regime_shield_v3.",
        caveat=(
            "This is a single-snapshot smoke test, NOT a validation. It proves the v6 stack "
            "executes end-to-end; it says nothing about ARI or predictive power."
        ),
        note="STAGFLATION as the Tier-2 micro-state alongside an EXPANSION macro state is "
             "internally odd and is flagged for review, not endorsed.",
    )

    emit(
        lg,
        "open_defect_io_hmm_never_fitted",
        severity="CRITICAL",
        component="Quantitative/io_hmm.py::IOHMM",
        finding=(
            "IOHMM exposes only compute_velocity_features, get_current_state, initialize, "
            "update. There is no fit/train method. trans_base, emit_mean and emit_cov are "
            "set once in initialize() and never estimated from data; only state_counts, "
            "emit_sums and emit_sums2 accumulate, from single-observation updates."
        ),
        consequence=(
            "The v6 Tier-1 'IO-HMM Wind-Vane' is a fixed-prior online filter, not a learned "
            "Input-Output HMM. The observed belief [0.600, 0.300, 0.100] is one forward step "
            "from the default prior and is not evidence of learned regime structure. Any ARI "
            "computed on this component would be measuring the prior, not the model."
        ),
        also_noted=(
            "update() also contains a no-op transition-count block (io_hmm.py:239-243) whose "
            "comment concedes it does not track the previous state, so the online transition "
            "learning path is inert even for the sufficient statistics that do accumulate."
        ),
        status="OPEN — implementing a fitting procedure is a substantive design task and "
               "requires pre-registration alignment before any v6 ARI is claimed.",
    )

    emit(
        lg,
        "checkpoint_state",
        date="2026-09-28",
        runnable=[
            "Quantitative/hmm_regime.py (import + smoke test)",
            "Quantitative/regime_shield_v3.py --validate (v5-era engine, ARI 0.0886 FAIL)",
            "Quantitative/unified_regime.py (v6 3-layer stack, smoke test)",
        ],
        not_yet_runnable_or_untested=[
            "v6 ARI validation — no entry point wires unified_regime into the fold loop",
            "IO-HMM fitting — no train method exists",
        ],
        pre_registration_untouched=True,
        pre_registration_authoritative="v6 / 390084e01fae6a358ab66b5ee536ccaf4d5d85bc6ec4dd05103cb74d5a5c5b10",
        headline=(
            "Both the v5 and v6 regime paths now execute. Neither is validated. "
            "Five runtime defects fixed; six findings logged, four still open."
        ),
    )

    print(f"college events appended -> {LOG_FILE}")
    print(f"records in file: {sum(1 for _ in open(LOG_FILE, encoding='utf-8'))}")
