# `scripts/` — Operational Scripts, Diagnostics & Run Logs

## Roadmap Position
Pipeline stage 8 (operations): one-off runs, schedulers, and diagnostic/fix
tooling. This folder was reorganized by Workstream B — ad-hoc debug/fix scripts
now live in `scripts/diagnostics/`, checkpoint/log-writing runs in
`scripts/runs_logs/`. **Pre-existing `py_compile` failures:** `temp_pop.py`
(0xff bytes) and `write_v1_final.py` (null bytes) — junk stubs, untouched.

## Folder Summary
- `scripts/diagnostics/` (34 files): `check_*` / `debug_*` / `fix_*` scripts
  used during development (`check_data*.py`, `debug_load*.py`,
  `fix_prereg*.py`, `fix_gate_engine.py`, `fix_walkforward_hmm.py`, ...).
- `scripts/runs_logs/` (16 files): run checkpoints and result writers
  (`run_backtest.py`, `run_bt.py`, `write_results.py`, `log_decisions.py`,
  `log_regime_v6_checkpoint.py`, `create_fold_artifacts.py`, ...).
- Top-level: production-adjacent runners (`build_master_universe_v2.py`,
  `build_pit_universe.py`, `run_pit_collection.py`, `run_phase4_dual_window_3arm.py`,
  `sentinel_pipeline.py`, `consensus_pipeline.py`, schedulers (`fred_scheduler.py`,
  `scheduler.py`), scrapers, and platform scripts (`.plist`, `.sh`, `.ps1`).

## Skeleton
```
scripts/
  diagnostics/   # 34 check_*/debug_*/fix_* scripts
  runs_logs/     # 16 run/checkpoint/write scripts
  build_master_universe_v2.py  build_pit_universe.py  run_pit_collection.py ...
  fred_scheduler.py  scheduler.py  sentinel_pipeline.py  consensus_pipeline.py ...
```

## Files (key)

| File | Purpose |
|---|---|
| `diagnostics/fix_gate_calibration.py` | gate-calibration fix tooling |
| `diagnostics/fix_prereg_hash.py` | pre-reg hash repair |
| `runs_logs/run_backtest.py` | backtest runner (walk-forward) |
| `runs_logs/create_fold_artifacts.py` | fold artifact generation |
| `build_master_universe_v2.py` | master universe build (v2) |
| `build_pit_universe.py` | PIT universe build |
| `run_pit_collection.py` | PIT collection run |
| `sentinel_pipeline.py` | sentinel monitoring pipeline runner |

## Entrypoints
```bash
python -m scripts.build_master_universe_v2 --run
python -m scripts.runs_logs.run_backtest --config <yaml>
python -m scripts.diagnostics.fix_prereg_hash
```

## Dependencies
Everything (these are the ops touchpoints). Diagnostics/runs_logs entries
reference the moved modules via fully-qualified imports — kept intact by the
Workstream B sweep (verified: no .py imports a moved module).

## Efficient Pathways
- `full_pipeline`: use `runs_logs` for checkpointed runs so results are
  reproducible; treat `diagnostics` as throwaway (never import from them).