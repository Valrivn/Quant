# `tests/` — Test Suite

## Roadmap Position
Cross-cutting: verification of every layer. `pytest --collect-only` on the repo
root **hangs (pre-existing)**; scope to `tests/` instead:
`python -m pytest tests --collect-only -q -p no:cacheprovider` (~22s).

## Folder Summary
~150 test modules plus `tests/validation/` subfolder (composite, data gates,
probation, provenance, wacc, walkforward). Two **pre-existing** collection
errors, unrelated to the Workstream B reorganization:
`tests/test_hmm_dwell.py` and `tests/test_phase9_core.py` — both
`ImportError: cannot import name 'FactorHMMv3' from 'Quantitative.regime_shield_v3'`
(see `Quantitative/DEEP_DIVE.md` §5).

## Skeleton
```
tests/
  conftest.py  quick_test.py
  validation/            # test_composite_score.py test_walkforward_gates.py ...
  test_backtest.py  test_chi_square.py  test_metrics_extra.py
  test_regime_shield_v2.py  test_har_abs_vol.py  test_swap_barrier_v3.py ...
  test_workstream_b_config_composite.py  test_zero_synthetic_cesi_removal.py ...
```

## Files (key)

| File | Purpose |
|---|---|
| `validation/test_composite_score.py` | monthly composite builder tests (P0B) |
| `validation/test_walkforward_gates.py` | walk-forward gate tests |
| `test_chi_square.py` | chi-square gate tests |
| `test_workstream_b_config_composite.py` | references **root** `gate_engine.py` — why it stays |
| `test_zero_synthetic_cesi_removal.py` | enforces CESI ban |
| `test_hmm_dwell.py` / `test_phase9_core.py` | PRE-EXISTING FactorHMMv3 import errors |

## Entrypoints
```bash
python -m pytest tests --collect-only -q -p no:cacheprovider   # quick collect
python -m pytest tests/validation/test_composite_score.py -q   # scoped run
```

## Dependencies
All packages. Workstream A owns `test_planning_gate.py`,
`test_efficient_paths.py`, `test_dispatch_prompts.py` — do not touch.

## Efficient Pathways
- `gate_audit`: run the composite + zero-synthetic tests before quoting any gate
- `provenance_audit`: `tests/validation/test_provenance_audit.py`