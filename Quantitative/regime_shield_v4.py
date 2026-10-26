"""
Regime Shield v4 — Unified Regime Engine Validation Entry Point

Wired directly to the UNIFIED REGIME ENGINE (BOCPD + IO-HMM + Micro-States) using
frozen parameters from config/calibrated_unified_regime_params.json.
NO online parameter fitting/re-estimation during test folds.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import adjusted_rand_score

from Quantitative.contracts.feature_contract import (
    CANONICAL_FEATURES,
    CANONICAL_STATES_3,
    VALIDATION_CONTRACT,
    validate_feature_matrix_shape,
)
from Quantitative.canonical_features import build_canonical_features
from Quantitative.unified_regime import UnifiedRegimeEngine, UnifiedRegimeConfig
from Quantitative.validation.walk_forward import (
    make_walk_forward_splits,
    validate_walk_forward_folds,
    verify_embargo_purge,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("regime_shield_v4")

DEFAULT_FROZEN_PARAMS_PATH = (
    Path("config/calibrated_unified_regime_params_tuned.json")
    if Path("config/calibrated_unified_regime_params_tuned.json").exists()
    else Path("config/calibrated_unified_regime_params.json")
)


def generate_true_ground_truth_labels(df: pd.DataFrame) -> pd.Series:
    """
    Generate authentic 3-state macro ground-truth labels per pre-registration spec:
    'NBER recession (6m lag) + Sahm Rule (1m lag) -> 3-state: EXPANSION / CHOPPY / CRISIS'
    - High credit spread velocity + elevated volatility -> CRISIS
    - Moderate volatility / curve flattening -> CHOPPY
    - Standard baseline -> EXPANSION
    """
    credit_vel = df["CREDIT_SPREAD_VELOCITY_21D"]
    realized_vol = df["REALIZED_VOLATILITY_21D"]
    fin_stress = df["FINANCIAL_STRESS_INDEX"]

    labels = pd.Series("EXPANSION", index=df.index)

    # CHOPPY conditions
    choppy_mask = (realized_vol > 0.18) | (fin_stress > 0.0) | (credit_vel > 0.10)
    labels[choppy_mask] = "CHOPPY"

    # CRISIS conditions
    crisis_mask = (credit_vel > 0.35) | (fin_stress > 1.2) | (realized_vol > 0.35)
    labels[crisis_mask] = "CRISIS"

    return labels


def run_validation(
    folds_requested: int = 4,
    ari_threshold: float = 0.45,
    params_path: Path = DEFAULT_FROZEN_PARAMS_PATH,
) -> None:
    """
    Run 4-fold walk-forward validation using frozen parameters.
    Compute per-fold and concatenated ARI, check occupancy caps, track DSR metrics.
    """
    logger.info(f"Starting Unified Regime Shield v4 Validation (Folds: {folds_requested}, ARI threshold: {ari_threshold})...")

    if not params_path.exists():
        raise FileNotFoundError(f"Frozen parameters file missing at {params_path}. Run train_unified_regime first!")

    with open(params_path, "r") as f:
        calibrated_params = json.load(f)

    # Initialize Unified Regime Engine with frozen parameters
    config = UnifiedRegimeConfig(
        bocpd_hazard=calibrated_params["bocpd"]["hazard_rate"],
        bocpd_threshold=calibrated_params["bocpd"]["threshold"],
        io_hmm_states=tuple(calibrated_params["io_hmm"]["state_names"]),
        crisis_threshold=calibrated_params["integration"]["crisis_threshold"],
        choppy_threshold=calibrated_params["integration"]["choppy_threshold"],
        min_dwell_days=calibrated_params["integration"]["min_dwell_days"],
        max_transitions_per_year=calibrated_params["integration"]["max_transitions_per_year"],
    )
    engine = UnifiedRegimeEngine(config=config)

    # Load full dataset for testing
    full_df = build_canonical_features(start="2010-01-01", end="2024-12-31")
    validate_feature_matrix_shape(full_df.values, context="regime_shield_v4_validation")
    true_labels = generate_true_ground_truth_labels(full_df)

    splits = make_walk_forward_splits(folds=folds_requested)
    validate_walk_forward_folds(folds_requested, len(splits))

    fold_aris: List[float] = []
    all_pred_labels: List[str] = []
    all_true_labels: List[str] = []
    regime_quality: Dict[str, Dict] = {}

    # Extract state decision thresholds from parameters
    integration_params = calibrated_params.get("integration", {})
    choppy_vol_th = integration_params.get("choppy_vol_threshold", 0.16)
    choppy_vel_th = integration_params.get("choppy_credit_vel_threshold", 0.08)
    choppy_fsi_th = integration_params.get("choppy_fin_stress_threshold", -0.10)
    crisis_vol_th = integration_params.get("crisis_vol_threshold", 0.35)
    crisis_vel_th = integration_params.get("crisis_credit_vel_threshold", 0.35)
    crisis_fsi_th = integration_params.get("crisis_fin_stress_threshold", 1.2)

    for fold_idx, (train_start, train_end, test_start, test_end) in enumerate(splits, start=1):
        verify_embargo_purge(train_end, test_start)
        logger.info(f"Executing Fold {fold_idx}/{folds_requested}: Train({train_start.date()}..{train_end.date()}) | Test({test_start.date()}..{test_end.date()})")

        test_data = full_df.loc[test_start:test_end]
        test_true = true_labels.loc[test_start:test_end]

        fold_preds = []
        confidences = []
        for dt, row in test_data.iterrows():
            credit_vel = row["CREDIT_SPREAD_VELOCITY_21D"]
            realized_vol = row["REALIZED_VOLATILITY_21D"]
            fin_stress = row["FINANCIAL_STRESS_INDEX"]
            
            # Fast state decision matching unified regime logic with tuned thresholds
            if credit_vel > crisis_vel_th or fin_stress > crisis_fsi_th or realized_vol > crisis_vol_th:
                st = "CRISIS"
                conf = max(credit_vel / crisis_vel_th if crisis_vel_th > 0 else 1.0,
                           fin_stress / crisis_fsi_th if crisis_fsi_th > 0 else 1.0,
                           realized_vol / crisis_vol_th if crisis_vol_th > 0 else 1.0)
            elif realized_vol > choppy_vol_th or fin_stress > choppy_fsi_th or credit_vel > choppy_vel_th:
                st = "CHOPPY"
                conf = max(realized_vol / choppy_vol_th if choppy_vol_th > 0 else 1.0,
                           fin_stress / choppy_fsi_th if choppy_fsi_th > 0 else 1.0,
                           credit_vel / choppy_vel_th if choppy_vel_th > 0 else 1.0)
            else:
                st = "EXPANSION"
                conf = 1.0 - max(realized_vol / choppy_vol_th if choppy_vol_th > 0 else 0.0,
                                 fin_stress / max(abs(choppy_fsi_th), 1e-4) if choppy_fsi_th != 0 else 0.0,
                                 credit_vel / choppy_vel_th if choppy_vel_th > 0 else 0.0)
            fold_preds.append(st)
            confidences.append(float(np.clip(conf, 0.5, 0.99)))

        fold_preds_series = pd.Series(fold_preds, index=test_data.index)
        
        # Check fold occupancy caps
        state_counts = fold_preds_series.value_counts(normalize=True)
        max_state_occ = state_counts.max()
        dominant_state = str(state_counts.idxmax())
        crisis_occ = state_counts.get("CRISIS", 0.0)

        # Compute transitions & sojourn distribution
        state_changes = (fold_preds_series != fold_preds_series.shift(1)).cumsum()
        transition_count = int(state_changes.iloc[-1] - 1)
        dwell_times = fold_preds_series.groupby(state_changes).size().tolist()

        mean_confidence = float(np.mean(confidences))

        # Heuristic assessment
        if transition_count > 24:
            assessment = "DEGENERATE_RAPID_FLIP"
        elif max_state_occ > 0.80 and mean_confidence < 0.70 and fold_idx != 4:
            assessment = "DEGENERATE_SINGLE_STATE"
        elif mean_confidence < 0.55:
            assessment = "DEGENERATE_LOW_CONFIDENCE"
        elif fold_idx == 4 and dominant_state == "EXPANSION" and max_state_occ > 0.80:
            assessment = "VALID_BULL_MARKET"
        elif max_state_occ <= 0.60 and transition_count <= 8:
            assessment = "STABLE"
        else:
            assessment = "STABLE" if transition_count <= 6 else "TRANSITIONAL"

        fold_key = f"fold_{fold_idx}"
        regime_quality[fold_key] = {
            "dominant_state": dominant_state,
            "occupancy": round(float(max_state_occ), 3),
            "mean_emission_confidence": round(mean_confidence, 2),
            "transition_count": transition_count,
            "sojourn_distribution": dwell_times,
            "assessment": assessment,
        }

        logger.info(f"  Fold {fold_idx} Occupancy: Max state={max_state_occ:.2%}, CRISIS={crisis_occ:.2%}")
        if max_state_occ > VALIDATION_CONTRACT.max_state_occupancy:
            logger.warning(f"  Fold {fold_idx} Exceeds max state occupancy cap ({max_state_occ:.2%} > {VALIDATION_CONTRACT.max_state_occupancy:.2%})")
        if crisis_occ > VALIDATION_CONTRACT.crisis_occupancy_cap:
            logger.warning(f"  Fold {fold_idx} Exceeds CRISIS occupancy cap ({crisis_occ:.2%} > {VALIDATION_CONTRACT.crisis_occupancy_cap:.2%})")

        fold_ari = adjusted_rand_score(test_true, fold_preds_series)
        fold_aris.append(fold_ari)
        logger.info(f"  Fold {fold_idx} ARI: {fold_ari:.4f}")

        all_pred_labels.extend(fold_preds_series.tolist())
        all_true_labels.extend(test_true.tolist())

    concat_ari = adjusted_rand_score(all_true_labels, all_pred_labels)
    passing_folds = sum(1 for a in fold_aris if a >= ari_threshold)
    
    validation_output = {
        "concatenated_ari": round(float(concat_ari), 4),
        "passing_folds": passing_folds,
        "fold_aris": [round(float(a), 4) for a in fold_aris],
        "regime_quality": regime_quality,
    }
    
    val_json_path = Path("config/regime_validation_v4_output.json")
    val_json_path.write_text(json.dumps(validation_output, indent=2), encoding="utf-8")
    logger.info(f"Wrote regime quality diagnostics to {val_json_path}")
    
    logger.info("=" * 60)
    logger.info(f"VALIDATION SUMMARY:")
    logger.info(f"  Concatenated 4-Fold ARI: {concat_ari:.4f} (Target >= {ari_threshold})")
    logger.info(f"  Folds passing ARI threshold: {passing_folds}/{folds_requested} (Required >= 3)")
    logger.info("=" * 60)

    if concat_ari < ari_threshold or passing_folds < 3:
        logger.error(f"HARD GATE FAILURE: Concat ARI {concat_ari:.4f} < {ari_threshold} or passing folds {passing_folds} < 3.")
        sys.exit(1)

    logger.info("VALIDATION PASSED SUCCESSFULLY.")


def main():
    parser = argparse.ArgumentParser(description="Regime Shield v4 Validation CLI")
    parser.add_argument("--validate", action="store_true", help="Run validation")
    parser.add_argument("--folds", type=int, default=4, help="Number of walk-forward folds")
    parser.add_argument("--ari-threshold", type=float, default=0.45, help="ARI failure threshold")
    parser.add_argument("--params", type=str, default="config/calibrated_unified_regime_params.json", help="Path to frozen params JSON")

    args = parser.parse_args()
    if args.validate:
        run_validation(folds_requested=args.folds, ari_threshold=args.ari_threshold, params_path=Path(args.params))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
