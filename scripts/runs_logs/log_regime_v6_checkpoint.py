"""College audit logger — Phase 9 regime architecture decisions, 2026-09-28.

Appends to logs/college/2026-09-28/college.jsonl using the same record schema
as the existing college trail (meta / ts / level / logger / message / line / func).

Note: config.logging_config.setup_college_file() referenced by log_final.py no
longer exists in that module, so this script installs the JsonlHandler directly.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path("logs/college/2026-09-28")
LOG_FILE = LOG_DIR / "college.jsonl"

RESERVED = {
    "meta", "ts", "level", "logger", "message", "line", "func",
    "name", "args", "exc_info", "exc_text", "stack_info", "levelname",
    "pathname", "filename", "module", "msecs", "relativeCreated",
    "thread", "threadName", "process", "processName", "taskName", "message",
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
        "phase9_regime_v6_checkpoint",
        date="2026-09-28",
        summary=(
            "Unblocked Quantitative/hmm_regime.py (indentation), re-ran regime OOS "
            "validation, and quantified three architectural defects. Gate result: FAIL."
        ),
    )

    emit(
        lg,
        "blocker_resolved",
        component="Quantitative/hmm_regime.py",
        defect="Mixed 4/8-space indentation broke module import (IndentationError).",
        locations=[
            "build_macro_features: if/else bodies for DGS10-DGS2 curve + CESI guard",
            "synthetic_features: for-loop body, multi-line call continuations",
        ],
        resolution="Rewrote both function bodies with consistent 4-space function-body indent.",
        verification="python -m py_compile Quantitative/hmm_regime.py -> exit 0; "
                    "python -m Quantitative.hmm_regime smoke test -> SHOCK detected, logL=-1495.1",
    )

    emit(
        lg,
        "handoff_ruling_noted",
        component="Quantitative/hmm_regime.py::synthetic_features",
        ruling_from_session=(
            "Realigned the synthetic smoke-test generator to emit the full 12-column "
            "FEATURE_NAMES contract instead of 4 legacy columns."
        ),
        rationale=(
            "Legacy frame exposed only credit_spread/yield_curve/market_ret_21d/"
            "market_vol_21d, but the generator looped over FEATURE_NAMES (12) and "
            "would raise KeyError. Emitting all 12 also keeps _F_CREDIT=3 -> "
            "BAA10Y_spread semantically valid for the smoke path."
        ),
        scope="Smoke test only; does not touch build_macro_features or the validation engine.",
    )

    emit(
        lg,
        "open_defect_feature_contract_drift",
        severity="HIGH",
        components=[
            "Quantitative/hmm_regime.py (GaussianHMM + RegimeDetector + build_macro_features)",
            "Quantitative/regime_shield_v3.py (ClassWeightedGaussianHMM + TwoLayerRegimeEngine)",
        ],
        finding=(
            "Two divergent regime implementations coexist. hmm_regime.FEATURE_NAMES is a "
            "12-feature contract with _F_CREDIT=3 -> BAA10Y_spread, but build_macro_features() "
            "returns 6 columns (BAA10Y, 10Y2Y, CP_TBill, RealizedVol21d, HY_OAS, CESI). "
            "RegimeDetector.retrain() sets _state_order = argsort(mu[:, _F_CREDIT]), so on the "
            "6-column output index 3 is RealizedVol21d, NOT credit spread. State labelling is "
            "therefore ordered by volatility instead of credit."
        ),
        blast_radius=[
            "Quantitative/backtest_harness.py:50,201,336",
            "Quantitative/portfolio_orchestrator.py:36,185,187",
        ],
        note=(
            "regime_shield_v3.py is self-consistent (its own 6-feature list, state order keyed "
            "to BAA10Y as first feature), so the validation path is NOT affected by this drift."
        ),
        status="OPEN — not remediated this session; needs a CEO ruling on the canonical contract.",
    )

    emit(
        lg,
        "open_defect_synthetic_cesi_placeholder",
        severity="HIGH",
        component="Quantitative/regime_shield_v3.py:829-832",
        finding=(
            "CESI — one of the 6 HMM input features — is i.i.d. Gaussian noise: "
            "np.random.seed(45); pd.Series(np.random.normal(0, 20, len(dates)), index=dates). "
            "Comment reads 'NOT AVAILABLE IN FRED CACHE; synthetic placeholder with fixed seed'."
        ),
        constraint_violated=(
            "Standing CEO constraint: no synthetic data fallbacks; all features from real FRED cache."
        ),
        measured_impact={
            "method": "read-only monkeypatch of build_layer1_features; engine not mutated",
            "baseline_6feat": {"fold_ari": [0.0088, 0.0859, -0.0977], "concat_ari": 0.0886},
            "cesi_neutralized": {"fold_ari": [0.0902, 0.0859, -0.0951], "concat_ari": 0.1334},
            "delta_concat_ari": 0.0448,
            "conclusion": (
                "Removing the noise dimension lifts concat ARI by ~0.045 (+50% relative) but "
                "remains far below the 0.45 gate. CESI is a real defect and NOT the primary "
                "cause of the ARI failure."
            ),
        },
        artifact="archives/phase9_regime_v6_20260928/cesi_ari_diagnostic.json",
        status="OPEN — not remediated; removing/replacing CESI is a pre-registration change.",
    )

    emit(
        lg,
        "open_defect_fold_count_off_by_one",
        severity="MEDIUM",
        component="Quantitative/regime_shield_v3.py:663-670, 750-777",
        finding=(
            "validate_two_layer_regime_oos(folds=4) executes only 3 folds. Fold geometry is "
            "train_end=(f+1)*fold_size+252 and test_end=min(n,(f+2)*fold_size+252). With n=1826 "
            "and fold_size=456, fold 3 gets test_start=2076 >= n, so the loop breaks on the "
            "folds_required check. The CLI accepts --folds and silently under-delivers."
        ),
        consequence=(
            "passing_folds is capped at 3, which exactly equals folds_required=3, so the "
            "per-fold half of overall_pass is satisfiable by a single fold coincidence while "
            "the requested 4-fold walk-forward never runs."
        ),
        status="OPEN — needs explicit fold-geometry fix before any ARI claim is credible.",
    )

    emit(
        lg,
        "validation_result",
        component="Quantitative/regime_shield_v3.py",
        command="python -m Quantitative.regime_shield_v3 --validate --folds 4 --ari-threshold 0.45",
        exit_code=1,
        reproducibility="Ran twice; identical numbers both times.",
        label_distribution={
            "full_window": {"EXPANSION": 1166, "SHOCK": 392, "CRISIS": 269},
            "fold_1": {"EXPANSION": 260, "SHOCK": 92, "CRISIS": 104},
            "fold_2": {"EXPANSION": 152, "SHOCK": 191, "CRISIS": 113},
            "fold_3": {"EXPANSION": 181, "SHOCK": 26, "CRISIS": 0},
        },
        fold_ari=[0.0088, 0.0859, -0.0977],
        concat_ari=0.0886,
        ari_threshold=0.45,
        passing_folds="0/3",
        concat_crisis_precision_recall_f1="0.243 / 0.113 / 0.155",
        verdict="FAIL — 0/3 folds pass; concat ARI 0.0886 vs 0.45 required.",
        artifacts=[
            "archives/phase9_regime_v6_20260928/validation_ari_20260928.log",
            "data/regime/two_layer_v1/oos_validation.json",
        ],
        note=(
            "Fold 3 has zero CRISIS labels, making its ARI degenerate and the 3-state problem "
            "ill-posed on that fold. Class imbalance and label sparsity are compounding causes "
            "beyond the three defects above."
        ),
    )

    emit(
        lg,
        "housekeeping",
        action="removed 56 scratch indentation-repair scripts from repo root",
        pattern="fix_indent*.py, fix_if_blocks*.py, fix_final_indent.py",
        post_state="All untracked throwaway artifacts of the indentation repair; "
                   "the real fix lives in Quantitative/hmm_regime.py source.",
    )

    print(f"college events appended -> {LOG_FILE}")
    print(f"records in file: {sum(1 for _ in open(LOG_FILE, encoding='utf-8'))}")
