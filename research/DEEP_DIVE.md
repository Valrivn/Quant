# DEEP DIVE — `research/` (Survival Surfaces, Lifecycle, Alpha Decay)

> Workstream B documentation. Phase 9/10 research: the survival-surface family
> (P(survival) curves by cohort and phi-bucket), lifecycle sieving, fragility
> alpha, and the parameter-grid machinery that fed the calibrated gates.

## 1. `survival_surface.py` — cohort survival accounting

Core predicates (used by both `build_survival_surface.py` and the boundary
checks in `validation/boundary_hitrate.py`):

- `is_compounding`: `roic > after_tax_cost_of_debt` (debt cost defaults to
  **0.08** when unavailable). This is the "still economically compounding"
  yes/no for each name in each period.
- `had_operational_break(ticker, date)`: per-category operational-break detector:
  - **ALL categories**: `net_income < 0` for 2 consecutive quarters, or
    `interest_coverage_ratio < 1.0`.
  - **STALWART / SLOW_GROWER**: `FCFE / dividends_paid < 1.0` (dividends not
    covered by free cash flow to equity).
  - **FAST_GROWER**: `sbc_expense / float > 0.05` (dilution alarm).
  - **CYCLICAL**: `inventory_delta + capex > 2σ` of the rolling sum (capacity
    overbuild).
- Survivors = cohorts that never hit a break; the surface is
  `S(t) = P(no break | cohort, phi_bucket, t)`.

## 2. `build_survival_surface.py` — phi-bucket surfaces

- Buckets (frozen): `phi_bins = [-inf, 1, 2, 3, 5, inf]` with labels
  `['[0,1)', '[1,2)', '[2,3)', '[3,5)', '[5,inf)']` — phi is the quality/reinv
  multiple from the valuation layer.
- Categories: `FAST_GROWER, STALWART, CYCLICAL, SLOW_GROWER`.
- **Fallback curve** for cohorts with < 2 rows:
  `s_t = max(1 − 0.05·t, 0.1)` — 5% per-year decay, floored at 10%. The floor is
  a design decision: sparse cohorts never claim zero survival.
- Outputs feed `validation/boundary_hitrate.py` (monotonicity + tail-cohort
  hit-rate) and the portfolio's survival adjustment
  (`Quantitative/survival_adjustment.py`).

## 3. Lifecycle & fragility alpha

- `lifecycle_sieve.py` + `lifecycle_backtest.py`: sieve the universe by
  lifecycle stage (birth → growth → maturity → decline) using the company
  classifier (`Quantitative/company_classifier.py`) and backtest each stage's
  incremental alpha.
- `fragility_alpha.py`: alpha attributable to *fragility* features (high
  operating leverage, customer concentration, tight cash runway) — the reverse
  side of the Arm C risk-penalty decomposition in `three_arm_runner.py`
  (`1 − 0.3(cash<18mo) − 0.2(cust_conc>30%) − 0.2(insider<5%) − 0.1(no 13F)`).
- `reverse_engineer_multibaggers.py` + `top_performer_similarity.py`: given
  ex-post winners, find which pre-fact features separated them (similarity
  scoring) — used to sanity-check that the *causal* features in the composite are
  the ones that mattered, not an artifact.

## 4. Grid search & attribution

- `parameter_grid_search.py` + `ml_parameter_optimization.py`: the grid that
  produced the Sprint 5 "grid widening" (`2b45ba98...` PIN entry) and the
  calibrated params in `config/calibrated_unified_regime_params(_tuned).json`.
- `phase10_attribution.py`: factor/regime attribution on the Phase 10 backtest
  output (`data/backtests/attribution_20261008/`).
- `phase9_full_pipeline.py`: end-to-end Phase 9 pipeline driver (survival →
  surface → adjustment → composite).
- `run_alpha.py` / `run_similarity.py`: CLI drivers for alpha and similarity
  analyses.

## 5. Design notes & referents

- `phase9_design_spec_20260925.md`, `executive_summary_20260925.md`,
  `next_steps_analysis_20260925.md`: the Phase 9 design/executive docs (also
  archived under `archives/phase8_sprint_continuation_20260925/`).
- `v1_intrinsic_backtest.py`: V1 intrinsic-value backtest driver
  (correlates with `valuation/v1_intrinsic_screen.py` and
  `valuation_alpha/`); `test_controls.py` holds the falsification controls for
  the research outputs.
- The panel builder `build_phi_panel.py` assembles the phi panel consumed by
  `build_survival_surface.py`; the panel data lands in
  `data/research/panels/` and `data/research/lifecycle/`.

## 6. Entrypoints

```bash
python -m research.build_phi_panel --run               # phi panel assembly
python -m research.build_survival_surface --run        # S(t) surfaces per phi-bucket
python -m research.phase9_full_pipeline --run          # survival -> composite end-to-end
python -m research.lifecycle_backtest --run            # stage-separated alpha backtest
python -m validation.boundary_hitrate --run            # boundary checks ON these surfaces
```

## 7. Key files quick-map

| File | Highlights |
|---|---|
| `survival_surface.py` | `is_compounding` (roic>0.08 debt default), `had_operational_break` per category (ICR<1, 2-q NI<0, FCFE/dividend<1, SBC/float>0.05, inventory+capex>2σ) |
| `build_survival_surface.py` | phi bins [-inf,1,2,3,5,inf]; fallback `s_t=max(1−0.05t, 0.1)` for cohorts<2 rows |
| `lifecycle_sieve.py` / `lifecycle_backtest.py` | stage sieving + stage-separated alpha |
| `fragility_alpha.py` | fragility features ↔ Arm C risk-penalty mirror |
| `parameter_grid_search.py` / `ml_parameter_optimization.py` | Sprint 5 grid widening, calibrated params |
| `phase10_attribution.py` | attribution on `data/backtests/attribution_20261008/` |

## 8. Cross-references

- Survival consumers: `Quantitative/survival_adjustment.py`,
  `validation/boundary_hitrate.py`
- Universe/company stage: `ingestion/persistence/merged_universe.py`,
  `Quantitative/company_classifier.py`
- Data: `data/research/panels/`, `data/research/lifecycle/`,
  `data/research/cpcv_results.parquet`, `data/research/multibagger_report.json`
- Archives: `archives/phase8_sprint_continuation_20260925/`,
  `archives/phase9_debate_20260926/` (position A/B + disagreement map + synthesis)