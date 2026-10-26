# `optimization/` — Hyperparameter Search (A/B + Optuna)

## Roadmap Position
Pipeline stage 2.5 (research support): parameter search over the risk/exit
grid. `optuna_search.py` produced the Sprint 5 grid widening
(corresponding PIN `2b45ba98...`); `ab_testing.py` runs A/B comparisons of
configs (e.g. trailing vs fixed exit on the same folds).

## Folder Summary
Two-file folder supporting `research/parameter_grid_search.py` and
`research/ml_parameter_optimization.py`. All optimized parameters must land in
a `config/calibrated_*.json` with sha256 sidecars before use — optimization
output is never read directly by production code.

## Skeleton
```
optimization/
  __init__.py  ab_testing.py  optuna_search.py
```

## Files

| File | Purpose |
|---|---|
| `ab_testing.py` | A/B config comparisons |
| `optuna_search.py` | Optuna hyperparameter search |

## Entrypoints
`python -m optimization.optuna_search --study <name>` (exact CLI per module docstring).

## Dependencies
`config/pre_registration_*.yaml`, `backtesting/backtest.py` (objective),
`research/parameter_grid_search.py`.

## Efficient Pathways
- `gate_audit`: optimized params are frozen via sha256-pinned calibration JSONs —
  never inline-tuned post-freeze
- `regime_validation`: `calibrated_unified_regime_params_tuned.json` came from here