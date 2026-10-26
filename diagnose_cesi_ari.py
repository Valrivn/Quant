"""READ-ONLY DIAGNOSTIC — Phase 9 regime v6, 2026-09-28.

Quantifies the ARI cost of the synthetic CESI placeholder and the fold-count
defect in Quantitative/regime_shield_v3.py. Does NOT mutate the pre-registered
engine: build_layer1_features is monkeypatched in-process only.

Context: CESI is currently i.i.d. Gaussian noise
(regime_shield_v3.py:829-832 -> np.random.normal(0, 20, len(dates)), seed 45),
which violates the standing "no synthetic data fallbacks" constraint.
"""
import json
import logging

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO)

import Quantitative.regime_shield_v3 as rsv3

_ORIG_BUILD = rsv3.build_layer1_features
_ORIG_VALIDATE = rsv3.validate_two_layer_regime_oos


def _drop_cols(frame: pd.DataFrame, cols) -> pd.DataFrame:
    keep = [c for c in frame.columns if c not in cols]
    return frame[keep]


def run_variant(name, neutralize_cesi=False, folds=4):
    def patched(dates):
        out = _ORIG_BUILD(dates)
        if neutralize_cesi:
            # Constant column -> std()==0 -> standardized to 0.0 by
            # build_layer1_features, removing the noise dimension while
            # preserving the hard-coded 6-column contract in retrain().
            out = out.copy()
            out["CESI"] = 0.0
        return out

    rsv3.build_layer1_features = patched
    try:
        res = _ORIG_VALIDATE(folds=folds, ari_threshold=0.45)
    finally:
        rsv3.build_layer1_features = _ORIG_BUILD
    return {
        "variant": name,
        "cesi_treatment": "neutralized_constant" if neutralize_cesi else "synthetic_noise_seed45",
        "folds_requested": folds,
        "fold_ari": [round(a, 4) for a in res.get("fold_ari_scores", [])],
        "concat_ari": round(res.get("concatenated_ari", float("nan")), 4),
        "passing_folds": res.get("passing_folds"),
        "status": res.get("status"),
        "overall_pass": res.get("overall_pass"),
    }


if __name__ == "__main__":
    results = []
    results.append(run_variant("baseline_6feat"))
    results.append(run_variant("cesi_neutralized", neutralize_cesi=True))
    payload = {
        "diagnostic": "phase9_regime_v6_cesi_ari_cost",
        "date": "2026-09-28",
        "note": "read-only; engine not mutated; synthetic CESI per regime_shield_v3.py:829-832",
        "results": results,
    }
    with open("archives/phase9_regime_v6_20260928/cesi_ari_diagnostic.json", "w") as f:
        json.dump(payload, f, indent=2)
    print(json.dumps(payload, indent=2))
