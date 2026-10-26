"""
build_prereg_v7.py — Generate config/pre_registration_v7.yaml + SHA-256 sidecar.

v7 differs from v6 in exactly one substantive way: the HMM Layer 1 feature
list. v6 pre-registered

    layer1_features: [BAA10Y, 10Y2Y, CP_TBill, RealizedVol21d, HY_OAS, CESI]

where CESI (Citi Economic Surprise Index) was a proprietary, non-FRED index
that `regime_shield_v3.build_layer1_features` filled with
`np.random.normal(0, 20, len(dates))` under seed 45. v7 pre-registers the
canonical 6-feature contract instead, with UNANTICIPATED_INFLATION
(CPIAUCSL YoY - T10YIE) occupying CESI's economic slot.

Everything else — the statistical design, the 4-fold windows, the DSR
protocol, the 513-trial accounting, the cost model, the success criteria — is
carried over byte-for-byte from v6. The ARI gate is NOT relaxed: v6 failed it
(0.0886 vs 0.45) and v7 keeps 0.45. Dropping a defective feature is a
provenance fix, not a re-roll of the bar.

Run:  python build_prereg_v7.py
"""
from __future__ import annotations

import hashlib
from pathlib import Path

SRC = Path("config/pre_registration_v6.yaml")
DST = Path("config/pre_registration_v7.yaml")
SIDECAR = Path("config/pre_registration_v7.yaml.sha256")

OLD_FEATURES = '''  layer1_features:
    - "BAA10Y"
    - "10Y2Y"
    - "CP_TBill"
    - "RealizedVol21d"
    - "HY_OAS"
    - "CESI"
'''

NEW_FEATURES = '''  # CANONICAL 6-FEATURE CONTRACT (Quantitative/contracts/feature_contract.py)
  # Index order is FROZEN and load-bearing: index 0 is the state-label sort key.
  # CESI is REMOVED — it is a proprietary, non-FRED index that was being filled
  # with np.random.normal(0, 20, n) under seed 45, violating this document's own
  # optimization.synthetic_fallback: "disabled". Its macro-surprise slot is now
  # UNANTICIPATED_INFLATION (CPIAUCSL YoY minus T10YIE 10Y breakeven), real FRED.
  feature_contract_source: "Quantitative/contracts/feature_contract.py"
  layer1_features:
    - "CREDIT_SPREAD_VELOCITY_21D"   # index 0 — d(HY OAS)_21d, PRIMARY SORT KEY
    - "YIELD_CURVE_10Y2Y"            # index 1 — DGS10 - DGS2
    - "REALIZED_VOLATILITY_21D"      # index 2 — 21d realized vol, FF5 gross market return
    - "FINANCIAL_STRESS_INDEX"       # index 3 — STLFSI2
    - "REAL_RATE_10Y"                # index 4 — DFII10
    - "UNANTICIPATED_INFLATION"      # index 5 — CPIAUCSL YoY - T10YIE
  layer1_fred_series:
    CREDIT_SPREAD_VELOCITY_21D: ["BAMLH0A0HYM2"]
    YIELD_CURVE_10Y2Y: ["DGS10", "DGS2"]
    REALIZED_VOLATILITY_21D: ["FF5_Mkt_RF"]
    FINANCIAL_STRESS_INDEX: ["STLFSI2"]
    REAL_RATE_10Y: ["DFII10"]
    UNANTICIPATED_INFLATION: ["CPIAUCSL", "T10YIE"]
  forbidden_series: ["CESI", "CESI_MACRO_SURPRISE", "CITI_ECONOMIC_SURPRISE"]
'''

HEADER_OLD = '''# Phase 9 Sprint 6 Pre-Registration Configuration — FROZEN BEFORE EXECUTION
# UNIFIED REGIME ARCHITECTURE: BOCPD + IO-HMM + MICRO-STATES

phase: 9
version: "6.0"
date: "2026-09-27"
status: "APPROVED"
'''

HEADER_NEW = '''# Phase 9 Sprint 7 Pre-Registration Configuration — FROZEN BEFORE EXECUTION
# UNIFIED REGIME ARCHITECTURE: BOCPD + IO-HMM + MICRO-STATES
#
# v7 CHANGELOG — the only substantive delta from v6 is the HMM Layer 1 feature
# list. See `hmm.layer1_features` below and the `changelog` block at the end.
#
#   REMOVED: CESI (Citi Economic Surprise Index)
#     - Proprietary, licensed index. NOT published on FRED.
#     - regime_shield_v3.build_layer1_features filled it with
#       `np.random.seed(45); pd.Series(np.random.normal(0, 20, len(dates)))`,
#       commented "NOT AVAILABLE IN FRED CACHE; synthetic placeholder".
#     - hmm_regime.build_macro_features filled it with 21-day market momentum
#       while labelling it "CESI" — a fabricated proxy for the same name.
#     - Both were pre-registered inputs to a 6-feature HMM. This violated the
#       standing zero-synthetic constraint AND v6's own
#       optimization.synthetic_fallback: "disabled".
#     - Measured ARI cost: +0.0448 concat (0.0886 -> 0.1334 when neutralised).
#       A real defect; NOT the primary cause of the ARI gate failure.
#     - `hmm_regime` ALSO had a contract drift: FEATURE_NAMES declared 12
#       features with _F_CREDIT=3, while build_macro_features returned 6 — so
#       state labelling was ordered by REALIZED_VOLATILITY, not credit.
#       v7 fixes this: FEATURE_NAMES is now the canonical 6 and _F_CREDIT=0.
#
#   ADDED: UNANTICIPATED_INFLATION = CPIAUCSL YoY - T10YIE
#     - Occupies CESI's economic slot: the inflation the market has not priced.
#     - Real FRED series, 45-day publication lag for PIT compliance.
#
#   NOT CHANGED: ari_threshold stays 0.45, folds_required stays 3, the 513-trial
#     DSR accounting, the 4-fold windows, and the success criteria are
#     untouched. Removing a defective feature is a provenance fix; it is not
#     grounds to relax the gate that the defect did not cause.
#
#   CAVEAT (blocking, recorded in changelog.open_data_blocker): as of
#     2026-09-28 the FRED cache has NO common date range across the mandatory
#     series, so this contract cannot currently be built. The engine now aborts
#     loudly instead of synthesising. See changelog.open_data_blocker.

phase: 9
version: "7.0"
date: "2026-09-28"
status: "APPROVED"
supersedes: "6.0"
'''

OPT_OLD = '''  tca_in_loop: true
  synthetic_fallback: "disabled"
'''
OPT_NEW = '''  tca_in_loop: true
  # NOTE: this value is the STRING "disabled", not the boolean False.
  # The guard pattern `if config.get("synthetic_fallback", False) is False:`
  # evaluates `"disabled" is False` -> False and therefore SKIPS the guard
  # entirely. Runtime enforcement goes through
  # Quantitative.data.fred_feed.synthetic_fallback_enabled(), which parses by
  # value and fails closed. Do not "simplify" that call site back to an
  # identity test.
  synthetic_fallback: "disabled"
  zero_synthetic_enforcement:
    module: "Quantitative/data/fred_feed.py"
    guard: "assert_authentic_pit"
    gate: "ingestion/harness/feature_gate.py"
    fail_mode: "raise"
    error_type: "SyntheticDataViolation(RuntimeError)"
    on_missing_series: "abort"
    on_partial_coverage: "abort"
    on_all_zero_column: "abort"
    on_constant_column: "abort"
    on_nan_after_alignment: "abort"
    forbidden_series: ["CESI", "CESI_MACRO_SURPRISE", "CITI_ECONOMIC_SURPRISE"]
'''

APPROVAL_OLD = '''ceo_approval:
  required: true
  ruling: "APPROVE (Unified Regime Architecture: BOCPD + IO-HMM + Micro-States)"
  date: "2026-09-27"
'''
APPROVAL_NEW = '''ceo_approval:
  required: true
  ruling: "APPROVE (v7: canonical 6-feature contract, CESI removed as synthetic)"
  date: "2026-09-28"
'''

CHANGELOG = '''

# ============================================================
# CHANGELOG v6.0 -> v7.0
# ============================================================
changelog:
  from_version: "6.0"
  to_version: "7.0"
  date: "2026-09-28"
  trigger: "Eliminate all synthetic CESI data from the codebase"
  changes:
    - path: "hmm.layer1_features"
      from: ["BAA10Y", "10Y2Y", "CP_TBill", "RealizedVol21d", "HY_OAS", "CESI"]
      to: ["CREDIT_SPREAD_VELOCITY_21D", "YIELD_CURVE_10Y2Y", "REALIZED_VOLATILITY_21D",
           "FINANCIAL_STRESS_INDEX", "REAL_RATE_10Y", "UNANTICIPATED_INFLATION"]
      reason: "CESI was synthetic (np.random.normal, seed 45). 4 of the 6 names also changed unit/definition."
    - path: "optimization.zero_synthetic_enforcement"
      change: "added"
      reason: "synthetic_fallback: disabled was never consulted at runtime by the code that needed it."
  code_changes:
    - "Quantitative/data/fred_feed.py: NEW — authoritative FRED reader + zero-synthetic guards."
    - "Quantitative/canonical_features.py: NEW — sole sanctioned builder of the canonical 6."
    - "Quantitative/contracts/feature_contract.py: added FORBIDDEN_SERIES + assert_no_forbidden_series + assert_canonical_names."
    - "Quantitative/hmm_regime.py: FEATURE_NAMES := CANONICAL_FEATURES; _F_CREDIT 3 -> 0; CESI and the CP_TBill noise proxy removed."
    - "Quantitative/regime_shield_v3.py: np.random CESI placeholder deleted; build_layer1_features delegates to the canonical builder."
    - "Quantitative/macro_pca.py: CESI removed from MACRO_INDICATORS; silent synthetic fill now requires allow_synthetic=True."
    - "Quantitative/backtest_harness.py: load_macro_features_history no longer swallows the zero-synthetic abort."
    - "Quantitative/portfolio_orchestrator.py: SyntheticDataViolation re-raised instead of defaulting to EXPANSION."
    - "ingestion/harness/feature_gate.py: NEW — pre-registration-gated feature admission."
  measurements:
    cesi_ari_cost_concat: 0.0448
    cesi_ari_baseline: 0.0886
    cesi_ari_neutralized: 0.1334
    ari_threshold_unchanged: 0.45
    note: "v6 FAILED the ARI gate honestly (0.0886 vs 0.45). v7 does not re-run or reinterpret that number."
  open_data_blocker:
    status: "OPEN — BLOCKING"
    finding: >-
      The FRED cache has no common date range across the mandatory series, so the
      canonical 6-feature contract cannot be built from data on disk as of
      2026-09-28. STLFSI2 ends 2022-01-07 while BAMLH0A0HYM2 begins 2023-09-26,
      leaving a 627-calendar-day gap. DGS10 additionally only covers 2021-09-15
      onward. The engine aborts via SyntheticDataViolation rather than filling
      the gap.
    consequence: >-
      Every canonical build path (build_canonical_features, build_macro_features,
      build_layer1_features) now raises SyntheticDataViolation. This is the
      intended, honest behaviour: the engine aborts instead of fabricating a
      column. No v7 ARI measurement is possible until the cache is backfilled.
    remediation:
      - "Backfill STLFSI2 forward from 2022-01-07 (or restart the cache window post-2023-09-26)."
      - "Backfill DGS10 to match."
      - "Re-run `python -m Quantitative.regime_shield_v3 --coverage` to confirm a non-empty common window."
    prohibition: >-
      Do NOT resolve this by dropping a feature, shortening the feature list,
      zero-filling, or substituting a proxy. Any of those re-creates the exact
      defect this version exists to remove.
'''

REPLACEMENTS = [
    (HEADER_OLD, HEADER_NEW),
    (OLD_FEATURES, NEW_FEATURES),
    (OPT_OLD, OPT_NEW),
    (APPROVAL_OLD, APPROVAL_NEW),
]


def main() -> int:
    text = SRC.read_text(encoding="utf-8")

    for old, new in REPLACEMENTS:
        if text.count(old) != 1:
            raise SystemExit(
                f"ABORT: expected exactly 1 occurrence of anchor, found "
                f"{text.count(old)}:\n---\n{old[:160]}..."
            )
        text = text.replace(old, new, 1)

    text = text.rstrip("\n") + "\n" + CHANGELOG

    DST.write_text(text, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(DST.read_bytes()).hexdigest()
    SIDECAR.write_text(digest + "\n", encoding="utf-8", newline="\n")

    print(f"wrote {DST}  ({len(text.encode('utf-8'))} bytes)")
    print(f"sha256 {digest}")
    print(f"wrote {SIDECAR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
