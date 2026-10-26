# DEEP DIVE — `ingestion/harness/` (Sprint 6 Unified Gate Engine)

> Workstream B documentation. Points to the pre-registration-frozen Sprint 6
> design: a **single composite-DSR gate** replacing the removed 8-gate
> Holm-Bonferroni hierarchy. Source of truth: `config/pre_registration_v7.yaml`.

## 1. What lives here

| Module | Role |
|---|---|
| `prereg.py` | Frozen-artifact loader + SHA-256 integrity gate. Single point of access for `config/pre_registration_v7.yaml`. |
| `gate_engine.py` | `CompositeGateEngine` — implements exactly the frozen statistical design, **and nothing else** (per-gate registry, gate families and `_holm_step_down` were deleted). |
| `three_arm_runner.py` | Arm A (Pure Quant RS-05), Arm B (Pure Qual z-score), Arm C (Hybrid weight overlay) portfolio construction. |
| `feature_gate.py` | Pre-registration-gated admission of new features; zero-synthetic-data rule. |
| `pit_validator.py` | Point-in-time validator + `@pit_validated` decorator. |
| `factor_store.py` | DuckDB factor panel (date, ticker, factor_name, value, provenance_hash). |
| `strategy_store.py` | Versioned PIT-safe strategy-state tables (regime, signal, black swan, stage gates). |

## 2. The frozen statistical design (`prereg.py`, `gate_engine.py`)

`ingestion/harness/prereg.py:EXPECTED_SHA256` pins `config/pre_registration_v7.yaml`
(sha `c80a47bf...`). Load all frozen values through `load_prereg()`; any other path
is a provenance violation.

Frozen design, verbatim (see `validation/gate_calibration.py:9-13`):

```yaml
statistical_design:
  metric: "composite_dsr"
  no_gate_hierarchy: true   # REMOVED: 8-gate Holm-Bonferroni structure
  dsr: {n_trials: 513, track_record_days: 2520, threshold: 0.95}
  fold_evaluation: {mode: "concatenated", fold_4_true_oos: true}
```

- `gate_engine.py:CompositeGateEngine.__init__` **asserts** `no_gate_hierarchy`
  on every construction — a defense against resurrecting the old design.
- All gates that fired **GONE** in v7; the `gates:` block of v6 was replaced by
  `statistical_design: composite_dsr` (`INGESTION/harness/prereg.py` PIN_HISTORY
  entry `d907f092...`).
- `validation/gate_calibration.py` (the Phase 9C null study) calibrates exactly
  this rule: `PASS <=> evaluate_composite_dsr(...) >= 0.95` (and Fold 4
  independently ≥ 0.95 when fold returns are supplied). Nothing else is a
  hypothesis test with an alpha.
- `composite_dsr` = Deflated Sharpe Ratio over the composite score series.
  `DSR_N_TRIALS = 513` (13 base + 500 LHS trials, `gate_engine.py`), a
  2520-day track record, N(0,1) variance of tracker estimate ~ 1/(2520-1).

### Component weights (frozen, `gate_engine.py:COMPONENT_SPECS`)

| Component | Weight | Threshold |
|---|---|---|
| sharpe_rolling_12m | 0.25 | > 1.0 |
| max_dd_rolling_12m | 0.20 | >= -0.20 |
| turnover_mom | 0.15 | <= 0.30 |
| hmm_ari | 0.15 | >= 0.70 |
| swap_churn | 0.10 | < 5.0 |
| tca_bps | 0.10 | <= 150 |
| probation_pass_rate | 0.05 | in [0.35, 0.65] |

`ARM_A_COMPONENT_SPECS` (v7 amendment) uses a sigmoid mapping for Sharpe:
`weight 0.20 * sigmoid(sharpe * 2)` — Arm A's composite is built on the same
monthly framework but with its own spec.

### Dependency rule

`gate_engine.py` imports **only** `numpy`, `pandas`, `ingestion.harness.prereg`,
and `Quantitative.regime_shield_v3` (for the HMM ARI component). Keep it that way:
any component change must go through a pre-registration pin change, not a code edit.

## 3. Three-arm construction (`three_arm_runner.py`)

Runner builds three arms over the PIT master universe:

- **Arm A — Pure Quant, DUAL-TRACK sector-relative RS-05.**
  - Track 1: `Sector_Rank(ROIC_WACC_Spread desc, FCF_Yield desc)`; qualify if
    `sector_percentile <= 25%`; a sector must have ≥ 20 names.
  - Track 2 (Achievers): `FCF_Margin > sector_median_3yr_rolling` **AND**
    `ROIC_WACC_Spread > 2%`.
  - Qualified set Q = T1 ∪ T2. Fallback when |Q| < 20: take top 30% by rank
    (logged as a design change, never silent).
  - Portfolio: rank by FCF_Yield desc, take `top N = min(50, |Q|)`, equal weight.
- **Arm B — Pure Qual, 8-factor composite.**
  - PCA-1 / equal-weight composite over 8 qualitative factors, z-scored at T over
    the full universe; **missing → 0.0 neutral fill** (a deliberate choice —
    not-detectable ≠ bad).
  - `top N = min(50, |U|)` equal weight.
- **Arm C — Hybrid overlay.**
  - `w_T = base_weight × (1 + qual_z_T) × risk_penalty_T × I(ticker in Q_T)`.
  - Tickers outside Q get weight 0.
  - `risk_penalty = 1 - 0.3(cash runway < 18mo) - 0.2(customer conc. > 30%)
    - 0.2(insider < 5%) - 0.1(no 13F sponsor)`.
  - Monthly rebalance on day 1, PIT inputs, 30% turnover cap,
    `TXN_COST_BPS = 10/side` + slippage.

All three arms write monthly rolling metrics to
`data/backtests/rolling_metrics_{a,b,c}_primary.parquet`, consumed by
`validation/composite_score.py` (see `validation/DEEP_DIVE.md`).

## 4. Zero-synthetic and PIT discipline

- `feature_gate.py`: any feature not in the frozen catalog
  (`Quantitative/contracts/feature_contract.py:CANONICAL_FEATURES`,
  `FORBIDDEN_SERIES`) is rejected. `SyntheticFallbackPolicy` **fails closed** —
  synthetic fallback is disabled, period. Known historical bug: the string
  `"disabled"` in YAML evaluated truthy (`""disabled" is False" → False`), making
  the guard a no-op; fixed to a strict enum comparison. CESI is on a permanent
  denylist (was fed by `np.random.normal(0, 20, len(dates))` — a synthetic-data
  violation, see `Quantitative/DEEP_DIVE.md`).
- `pit_validator.py`: `validate_pit(df)` enforces every timestamp column
  `<=` the trade-date column (defaults `"filed"` / `"date"`); raises
  `PITValidationError` otherwise. `@pit_validated` decorator applies it to
  module-level PIT snapshots.
- `factor_store.py`: DuckDB panel `(date, ticker, factor_name, value,
  provenance_hash)` at `data/harness/factors.duckdb`. The factor **date is the
  PIT timestamp**, not the calendar date of the data.
- `strategy_store.py`: four versioned PIT-safe tables in the same DuckDB file —
  `regime_state`, `grassroots_signal`, `black_swan_event`, `stage_gate_decision`;
  each has its own PIT rule (clock time vs decision time).

## 5. Known failure modes & migration notes

- **Composite/DSR coupling**: the DSR is computed on the *composite* the same
  components were tuned on; the Fold-4 independent check (`fold_4_true_oos: true`)
  is the only true out-of-sample brake — never relax the Fold-4 requirement.
- **60% / 20% occupancy caps** and **ARI ≥ 0.45 on ≥ 3 of 4 folds** were Sprint-5
  era requirements; they feed the HMM component in the composite but are no longer
  standalone gates.
- **PIN history** (`prereg.py:PIN_HISTORY`): `f9615710...` (v4.0, 2026-09-26) →
  `983316e2...` (Sprint 4 Arm A: B_swap_exit amendment) → `2b45ba98...` (Sprint 5
  grid widening) → `d907f092...` (Sprint 6: single unified metric). Any further
  change is a CEO decision, not an engineering one.
- **Import path shim**: `validation/gate_calibration.py:84-85` inserts repo root
  in `sys.path` before importing `ingestion.harness.*`. Do not remove.

## 6. Entrypoints

```bash
python -m ingestion.harness.three_arm_runner   # build arms A/B/C (monthly, PIT)
python -m validation.gate_calibration --run --n 10000 --phase9   # null calibration
python -m validation.gate_calibration --test                       # self-test
python -m validation.composite_score        # monthly composite for the 3 arms
```

## 7. Key files quick-map

| File | Highlights |
|---|---|
| `prereg.py` | `EXPECTED_SHA256`, `load_prereg`, `PIN_HISTORY` ledger, frozen YAML load + integrity gate |
| `gate_engine.py` | `CompositeGateEngine`, `deflated_sharpe_ratio`, `DSR_N_TRIALS=513`, `DSR_THRESHOLD=0.95`, `COMPONENT_SPECS`, `ARM_A_COMPONENT_SPECS` |
| `three_arm_runner.py` | Arm A dual-track RS-05, Arm B 8-factor z, Arm C hybrid `base×(1+qual_z)×risk_penalty×I(Q)`, `TXN_COST_BPS=10` |
| `feature_gate.py` | `SyntheticFallbackPolicy` fail-closed, CESI denylist, catalog checks |
| `pit_validator.py` | `validate_pit`, `@pit_validated`, `PITValidationError` |
| `factor_store.py` | `DEFAULT_FACTOR_STORE`, `CONFIDENCE_LEVELS`, `QUAL_FACTOR_CATALOG` (grassroots_c_t, hype_p95; ApeWisdom Tier-3 ≤5% weight, always low) |
| `strategy_store.py` | 4 tables, per-table PIT rules |

## 8. Cross-references

- Frozen config + sha256: `config/pre_registration_v7.yaml(.sha256)`
- Null calibration of the frozen rule: `validation/gate_calibration.py`
- Monthly composite consumer: `validation/composite_score.py`
- Old (removed) design artifacts: `config/pre_registration_v6.yaml`,
  `config/gates_v2.yaml` (read-only history)
- Structural feature contracts: `Quantitative/contracts/feature_contract.py`