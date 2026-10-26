# `validation/` — Falsification-First Gate Stack

## Roadmap Position
Pipeline stage 3 (gatekeeper): every production claim must survive this folder
before it may be quoted. Null calibrations, nested CV, sector-clustered
bootstrap gates, churn gates, vol-consistency gates, and the monthly composite
that feeds the Sprint 6 composite DSR. **See `validation/DEEP_DIVE.md` for the
numbers and claim map.**

## Folder Summary
`composite_score.py` builds the monthly 84-row composite (from
`data/backtests/rolling_metrics_{a,b,c}_primary.parquet`); `trailing_cv.py`
runs the nested CV comparing trailing fractal exits vs fixed exits
(sector-clustered paired bootstrap, α = 0.0071 Bonferroni); `gate_calibration.py`
is the Phase 9C null study (Dirichlet(1) zero-alpha portfolios, 10k runs);
`swap_gates_v3.py`, `vol_gates.py`, `boundary_hitrate.py` enforce churn, vol
consistency and survival-boundary constraints; the remaining `*_gates.py` /
`*_audit.py` files are the supporting gate family.

## Skeleton
```
validation/
  composite_score.py  gate_calibration.py  trailing_cv.py  boundary_hitrate.py
  swap_gates_v3.py    vol_gates.py
  hmm_gates.py  hmm_gates_v3.py  probation_gates.py  spike_guards.py  wacc_gates.py
  dcf_gates.py  data_gate_gates.py  qual_coverage_audit.py  provenance_audit.py  ...
```

## Files (key)

| File | Purpose |
|---|---|
| `composite_score.py` | monthly composite builder (fixes P0B daily-1760 defect) |
| `gate_calibration.py` | Phase 9C null calibration of the frozen v7 composite-DSR rule |
| `trailing_cv.py` | nested CV trailing-vs-fixed exit, sector-clustered bootstrap |
| `swap_gates_v3.py` | RS-25 churn gates (≤5 swaps/yr, ≤0.30 turnover) |
| `vol_gates.py` | multifractal vol consistency (HAR_abs/HAR_sq/Wavelet_db4, ≥0.70) |
| `boundary_hitrate.py` | survival monotonicity + tail hit-rate |
| `hmm_gates_v3.py` | regime occupancy bounds (60%/20%, ARI ≥ 0.45 on ≥3/4 folds) |

## Entrypoints
```bash
python -m validation.composite_score                 # monthly composite
python -m validation.gate_calibration --run --n 10000 --phase9
python -m validation.trailing_cv --run
python -m validation.swap_gates_v3 --run
python -m validation.vol_gates --test
python -m validation.boundary_hitrate --run
```

## Dependencies
`ingestion/harness/gate_engine.py`, `ingestion/harness/prereg.py`,
`Quantitative/har_abs_vol.py`, `Quantitative/multifractal_vol.py`,
`research/survival_surface.py`, frozen configs under `config/pre_registration_*.yaml`.

## Efficient Pathways
- `gate_audit`: `gate_calibration.py` (null rate) → `composite_score.py` → `ingestion/harness/gate_engine.py`
- `three_arm_backtest`: `composite_score.py` is the mandatory consumer of the 3-arm runner
- `regime_validation`: `vol_gates.py` cross-checks the fractal-vol stack