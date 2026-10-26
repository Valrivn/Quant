# DEEP DIVE — `validation/` (Falsification-First Gate Stack)

> Workstream B documentation. This folder is where the strategy is *disproven*:
> null calibrations, nested CV, sector-clustered bootstrap tests, churn gates,
> multifractal vol consistency, survival monotonicity, and the composite-score
> builder that feeds the Sprint 6 composite DSR.

## 1. `composite_score.py` — monthly composite for the 3-arm backtest

- Consumes `data/backtests/rolling_metrics_{a,b,c}_primary.parquet` and builds a
  **monthly** composite-series DataFrame (one row per `rebalance_date` per arm;
  ~84 rows per arm over 2018–2024, matching `windows.primary`).
- **P0B defect (fixed)**: the pre-existing daily builder produced 1,760 rows per
  arm, inflating sample size and corrupting DSR variance estimates
  (1/(N−1) term). The monthly builder restores the correct 84-row cadence.
- Output columns include: `rebalance_date, arm, sharpe_rolling_12m,
  max_dd_rolling_12m, turnover_mom, ari_concat, swaps_annualized, tca_bps,
  probation_rate`. The composite is what `ingestion.harness.gate_engine`
  Deflated-Sharpe-ratios; per-component thresholds live in
  `gate_engine.py:COMPONENT_SPECS`.

## 2. `trailing_cv.py` — Nested CV: Trailing Fractal Exit vs Fixed Exit (pre-reg v2)

Outer loop is a **walk-forward expanding window**: 36 train / 12 test / 3 embargo
months, 4 folds. Inner loop is time-series CV. Exit logic is **frozen**:

- Trailing exit fire condition:
  `price_t <= EMA^τ_t · (1 − k · σ_fractal_t(τ))` with
  `σ_fractal(τ) = σ̂_daily · √τ` from the HAR-|r| forecast **built strictly on
  data ≤ t−1** (`validation/vol_gates.py` supplies the forecast).
- Fixed exit: `price_t <= entry · (1 − 1/floor_iv_mult)`; `floor_iv_mult = 1.15`,
  regime targets `EXP 1.20 / CRISIS 1.05`.
- Frozen flags: `fractal_trailing true`, `peak_detection false`,
  `new_structure_hold false`.
- **Primary gate** `trailing_beats_fixed`: paired, sector-clustered bootstrap
  (cluster = sector) on the Sharpe-difference distribution.
  p = fraction of bootstrap means ≤ 0.
  Pass iff **p < 0.0071** (Bonferroni across the 7 primary gates: 0.05/7) —
  **per sector**. This is the alpha-0.0071 number, and it is a *bootstrap
  p-value*, not a Wald/Student p.
- DataFrame contract: `config/gates_v2.yaml`; frozen by `pre_registration_v2.yaml`.

## 3. `boundary_hitrate.py` — survival surface boundary checks

- Loads survival curves from `research/build_survival_surface.py`; asserts the
  empirical survival curve is monotone non-increasing; validates hit-rate on the
  tail cohort (S(t) ≥ 90% monotone cohorts only).
  `break_points.parquet` records where the survival curve breaks.
- `oos_hit_rate` is currently a **hardcoded mock (0.62)** until the Phase 10 OOS
  walk completes — treat any quoted OOS hit rate as provisional until then.

## 4. `swap_gates_v3.py` — Bidirectional Swap Barrier Churn Gates (pre-reg v3)

- Barrier params: `C_bps = 150`, `t_target_months = 24`, `γ = 0.5`,
  `ε = 1e-6`, `moat_penalty_bps = 100`,
  `moat_condition: "ROIC_cap_rd > WACC + 0.02 AND OMR > 0.5"`.
- Bidirectional: both entry-over- and exit-into-barrier swaps count.
  `cash_aware_reentry true`, `stop_loss_reentry_bypass true`.
- Churn limits: `max_swaps_per_year = 5`, `max_annual_turnover = 0.30`.
- Sub-gates the suite reports: `swap_barrier_churn`, `moat_protection_efficacy`,
  `bidirectional_sanity`, `cash_trap_audit` (months idling in cash > 15% trigger
  the cash-trap check).
- CLI: `python -m validation.swap_gates_v3 --run` / `--test`.

## 5. `vol_gates.py` — Multifractal Volatility Consistency Gate (v2, §12.7.5)

- Three volatility estimators must agree: `HAR_abs`, `HAR_sq`, `Wavelet_db4`
  (Daubechies-4), **min correlation 0.70**.
- HAR on |r| with lags (1, 5, 22); `σ = |r̂| · √(π/2)`.
- Wavelet: D4, max depth J=9, reflection boundary; level-7 detail at 128-day
  window; leaders `l(j,k) = max(|d[j,k−1]|, |d[j,k]|, |d[j,k+1]|)` over levels
  1..4 with exponential weighting, half-life 0.33.
- Fractal scaling: `σ_fractal(τ) = σ̂_daily · √τ` for τ ∈ {5,10,20}.
- **Falsification controls** (documented, go/no-go): GARCH(1,1) × regime
  multiplier generator passes at 0.78; IID Gaussian null passes at 0.05 —
  showing the gate is neither trivially passable nor impossible.
- CLI: `python -m validation.vol_gates --test`.

## 6. `gate_calibration.py` — Phase 9C NULL STUDY (the number to quote)

- Calibrates the **frozen v7 rule only**:
  `PASS <=> CompositeGateEngine.evaluate_composite_dsr(...) >= 0.95` (Fold 4
  independent ≥ 0.95 if fold returns supplied). No 8-gate family exists anymore —
  inventing alpha/8 would violate pre-registration.
- **Null DGP**: ~1,500-name universe (`universe_size_target`), monthly rebalance,
  primary window `2018-01-01 .. 2024-12-31` (84 months), every name zero-alpha
  with market-like factor loading, weights = fresh `Dirichlet(1)` draw each month
  (no selection skill, no persistence). Resulting book is ~β=1 passive — the
  adversarial case.
- Defaults: `N_NULL_DEFAULT = 10_000`, `SEED = 20260909`,
  output `data/validation/gate_calibration_report.json` (+ `exit_logic_calibration.json`).
- **Claim map** (read before quoting): `decision_rule` = the composite-DSR pass
  rate (the only false-positive rate answering "how often does the frozen gate
  fire on a no-skill strategy?", compared vs nominal 1/n_trials).
  `threshold_reference_only` = per-metric thresholds in
  `success_criteria.fold_4_test` (Sharpe, drawdown, IR, ARI, Calmar, swap churn,
  TCA, probation) — reported for power planning, **no alpha attached**.
  `declared_assumption` = structural compliance components (WACC coverage,
  provenance chain), null rates are modelling assumptions, never measurements.
- CLI: `python -m validation.gate_calibration --run --n 10000 --phase9`;
  smoke `--n 500`; `--exit-logic` for trailing/fixed exit comparison.

## 7. Supporting gates (read-only context)

- `hmm_gates.py` / `hmm_gates_v3.py`: regime-state occupancy bounds
  (60% / 20% caps by state; ARI ≥ 0.45 on ≥ 3/4 folds — v4-era gates, now feeding
  the HMM ARI component rather than standing alone).
- `probation_gates.py`: probation-pass rate corridor [0.35, 0.65] —
  frozen into `gate_engine.py:COMPONENT_SPECS`.
- `spike_guards.py`, `wacc_gates.py`, `dcf_gates.py`, `data_gate_gates.py`,
  `qual_coverage_audit.py`, `final_audit_v3.py`, `provenance_audit.py`,
  `lifecycle_audit.py`, `rpo_imputation_test.py`, `similarity_significance.py`,
  `glassdoor_ats_granger.py`, `fundamental_coverage_audit.py`,
  `validate_merged_universe.py`, `phase9d_emit.py`.

## 8. Entrypoints

```bash
python -m validation.composite_score                 # monthly composite (3 arms)
python -m validation.trailing_cv --run               # nested CV, trailing vs fixed exit
python -m validation.swap_gates_v3 --run             # RS-25 churn gates
python -m validation.vol_gates --test                # multifractal consistency gate
python -m validation.gate_calibration --run --n 10000 --phase9   # null calibration
python -m validation.boundary_hitrate --run          # survival boundary + hit rate
```

## 9. Key files quick-map

| File | Highlights |
|---|---|
| `composite_score.py` | monthly 84-row composite; fixes P0B daily-1760 defect; feeds composite DSR |
| `trailing_cv.py` | nested CV, sector-clustered paired bootstrap, α=0.0071 Bonferroni, trailing vs fixed exit |
| `boundary_hitrate.py` | monotone S(t) ≥ 90% cohorts; break_points; OOS hit rate mock 0.62 |
| `swap_gates_v3.py` | churn: ≤5 swaps/yr, ≤0.30 turnover; cash-trap audit; moat condition |
| `vol_gates.py` | HAR_abs/HAR_sq/Wavelet_db4 corr ≥ 0.70; falsification 0.78 / 0.05 |
| `gate_calibration.py` | Phase 9C null: 1500 names, Dirichlet(1), 84 months, 10k runs, seed 20260909 |

## 10. Cross-references

- Frozen configs: `config/pre_registration_v2.yaml` (trailing CV),
  `config/pre_registration_v3.yaml` (swap gates), `config/pre_registration_v4.yaml`
  (250 bps barrier), `config/pre_registration_v7.yaml` (composite DSR),
  `config/gates_v2.yaml(.sha256)`, `config/gate_calibration_phase9_v7.json(.sha256)`
- Calibrated engine: `ingestion/harness/gate_engine.py`
- Vol/cv inputs: `Quantitative/har_abs_vol.py`, `Quantitative/multifractal_vol.py`
- Survival inputs: `research/survival_surface.py`,
  `research/build_survival_surface.py`
- Tests: `tests/validation/test_*`, `tests/test_zero_synthetic_cesi_removal.py`