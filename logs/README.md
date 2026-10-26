# `logs/` — Runtime Logs, Run State & College Diaries

## Roadmap Position
Cross-cutting (observability): stdout/stderr captures, scheduler run state, and
the `college/` dated diaries kept by the research agents. Git-ignored (except as
noted). Content is scratch — never cite a number from a log; cite the
`data/validation` artifacts instead.

## Folder Summary
`college/` holds dated subfolders (2026-09-06 … 2026-10-10) of session/decision
diaries; the top level holds pipeline logs (`quant_pipeline.log`,
`supervised_pipeline_run.log`, `fred_pipeline.log`, `gdelt_run.log`,
`harvest_run.log`, `ig_loop_*.log`, `stream_guard.log`, `supervisor.log`),
run-state JSON (`consensus_status.json`, `fred_health_check.json`,
`supervised_pipeline_status.json`), and the Workstream-B moved artifacts
(`bt_out.log`, `bt_result.json`, `bt_result_opp.json`, `bt_run.log`,
`debug.log`, `out.log`, `stack.log`, `test.log`, `discovered_sources.log`).

## Skeleton
```
logs/
  college/              # dated decision diaries
  *.log  *.err          # pipeline stdout/stderr captures
  bt_result.json  bt_result_opp.json  consensus_status.json  fred_health_check.json ...
```

## Files (key)

| File | Purpose |
|---|---|
| `college/` | dated agent diaries (intent/decisions) |
| `quant_pipeline.log` | main pipeline log |
| `supervised_pipeline_run.log(.err)` | supervised-run capture |
| `bt_result.json` | backtest result artifact (moved from root) |

## Entrypoints
None (scratch space). Consumed by no production code.

## Dependencies
Writers: `scripts/` schedulers and runners.

## Efficient Pathways
- `full_pipeline`: if a result appears ONLY in a log, it isn't evidence —
  path through `data/validation` + provenance before quoting.