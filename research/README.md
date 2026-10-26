# `research/` — Survival Surfaces, Lifecycle & Alpha Research

## Roadmap Position
Pipeline stage 2.5-3 (research layer): Phase 9/10 empirical research that feeds
the validation gates — survival surfaces, lifecycle sieve, fragility alpha,
parameter grids. **See `research/DEEP_DIVE.md` for predicates and bins.**

## Folder Summary
`survival_surface.py` defines the cohort-survival predicates
(`is_compounding`, `had_operational_break` per category);
`build_survival_surface.py` builds P(survival) surfaces over the frozen
phi-bucket bins `[0,1) [1,2) [2,3) [3,5) [5,inf)` with a sparse-cohort fallback
`max(1−0.05t, 0.1)`. The lifecycle/fragility modules measure stage-separated
alpha; the grid modules produced the Sprint 5 calibrated parameters.

## Skeleton
```
research/
  survival_surface.py  build_survival_surface.py  build_phi_panel.py
  lifecycle_sieve.py   lifecycle_backtest.py      fragility_alpha.py
  parameter_grid_search.py  ml_parameter_optimization.py  phase10_attribution.py
  phase9_full_pipeline.py   v1_intrinsic_backtest.py
  run_alpha.py  run_similarity.py  test_controls.py
  *.md   # phase9 design spec + executive summaries
```

## Files (key)

| File | Purpose |
|---|---|
| `survival_surface.py` | `is_compounding` (roic>debt cost, default 0.08); `had_operational_break` per category (ICR<1.0, 2-q NI<0, FCFE/dividends<1.0, SBC/float>0.05, inventory+capex>2σ) |
| `build_survival_surface.py` | phi-bin surfaces; fallback `max(1−0.05t, 0.1)` for cohorts <2 rows |
| `lifecycle_sieve.py` / `lifecycle_backtest.py` | lifecycle-stage separation + stage alpha |
| `fragility_alpha.py` | fragility feature alpha (mirror of Arm C risk penalties) |
| `parameter_grid_search.py` | Sprint 5 grid widening (`2b45ba98...` PIN) |
| `phase10_attribution.py` | attribution on `data/backtests/attribution_20261008/` |

## Entrypoints
```bash
python -m research.build_phi_panel --run
python -m research.build_survival_surface --run
python -m research.phase9_full_pipeline --run
python -m research.lifecycle_backtest --run
```

## Dependencies
`Quantitative/company_classifier.py`, `Quantitative/survival_adjustment.py`,
`validation/boundary_hitrate.py`, data under `data/research/`.

## Efficient Pathways
- `universe_build`: `build_phi_panel.py` assembles the phi panel used by the valuation layer
- `full_pipeline`: `phase9_full_pipeline.py` is the survival→composite driver
- `gate_audit`: survival surfaces are the object of `boundary_hitrate.py`