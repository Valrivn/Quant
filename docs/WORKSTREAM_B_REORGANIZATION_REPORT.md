# Workstream B — File Organization & Documentation Sweep (Final Report)

**Date:** 2026-10-11 · **Scope:** repository organization + documentation only.
All code changes remain owned by Workstream A.

## 1. Objective

Reorganize the repo's loose files and document every folder, without editing any
`*.py`, any frozen `config/` artifact, `.agents/**`, `commands/**`, `data/**`,
or `logs/**`. Two hard rules: (a) never break imports, (b) never move a file that
another module references by path without a ruling.

## 2. Work completed

### 2.1 File moves (no code edits)

| Group | Destination | Count | Method |
|---|---|---|---|
| Founding briefs / plans | `startup/` | 5 | `git mv` (tracked) + `Move-Item` (untracked) |
| Ad-hoc `check_*`/`debug_*`/`fix_*` | `scripts/diagnostics/` | 34 | `Move-Item` |
| Run/checkpoint/write helpers | `scripts/runs_logs/` | 16 | `Move-Item` |
| Context/decision markdown | `docs/` | 8 | `git mv` (tracked) |
| Stray notes | `docs/_misc/` | 1 | `Move-Item` |
| Logs + result json | `logs/` | 9 | `Move-Item` |
| Artifacts | `data/artifacts/` | 3 | `Move-Item` |
| Arm C output | `data/backtests/arm_c/` | 1 | `Move-Item` |

New directories created: `startup/`, `scripts/diagnostics/`,
`scripts/runs_logs/`, `docs/_misc/`, `data/artifacts/`, `data/backtests/arm_c/`,
`ops/` (docs only).

### 2.2 Import-safety verification

- **Reference scan** (`refscan_out.json`): zero `.py` files import any moved
  module. Scripts that were moved were verified to be leaf executables.
- **`py_compile` sweep** of all 50 moved `.py` files: 48 pass; 2 fail — both
  **pre-existing junk stubs** unchanged by this work:
  - `scripts/runs_logs/temp_pop.py` (0xFF bytes)
  - `scripts/runs_logs/write_v1_final.py` (null bytes)

### 2.3 Documentation added

- **5 DEEP_DIVE.md**: `ingestion/harness/`, `Quantitative/`, `validation/`,
  `backtesting/`, `research/` — formulas, frozen constants, data contracts,
  known failure modes, entrypoints, cross-references.
- **28 folder README.md** (template: Roadmap Position · Folder Summary ·
  Skeleton · Files table · Entrypoints · Dependencies · Efficient Pathways):
  `startup, ops, ingestion, Quantitative, validation, backtesting, research,
  portfolio, regime, valuation, valuation_alpha, execution, optimization,
  discovery, diversification, Qualitative, center, dashboard, docs, scripts,
  tests, config, data, logs, archives, db, commands, opencode_scripts`.
- **Root `README.md`** rebuilt as the MASTER ROADMAP: ordered pipeline table with
  status labels, folder index, root-keepers/ruling queue, core entrypoints,
  Efficient Pathways table, known-state section.

## 3. Verification evidence

| Check | Result |
|---|---|
| New DEEP_DIVE.md on disk | 5 / 5 |
| New folder README.md on disk | 28 / 28 |
| Root README rebuilt | ✅ |
| Scoped `pytest --collect-only` | **2266 tests collected, 2 errors** |
| Collection errors | exactly the 2 pre-existing `FactorHMMv3` import failures (`tests/test_hmm_dwell.py`, `tests/test_phase9_core.py`) |
| New `ModuleNotFound` from moved files | **none** |
| Working tree | docs untracked; `README.md` modified; moves staged/renamed as expected |

Repo-wide `pytest --collect-only` still hangs — **pre-existing**; the scoped
`tests/` collect (~18s) is the supported path.

## 4. Rulings requested (files left in place)

Moving these would break a path reference or cross-module dependency:

- `reddit_quant.db` (203 MB) — `db/connection.py:6` pins `DB_PATH = "reddit_quant.db"`; ~40 modules depend on it.
- Root `gate_engine.py` — imported by `tests/test_workstream_b_config_composite.py:29`.
- `sprint1_parallel_ingest.py` — imported by 3 discovery modules.
- `log_regime_v6_stack_checkpoint.py` / `log_regime_v6_verification_checkpoint.py` — interdependent pair.
- Candidate scripts with no bucket: `real_backtest.py`, `walkforward_backtest.py`, `verify_universe.py`, `alpha_analysis.py`, `diagnose_cesi_ari.py`, `build_prereg_v7.py`, `build_valuation_table.py`, `extract_raw_xbrl.py`.
- `test_workstream_a.py` (Workstream A), `etf_selection_cache.json`, `college_append.jsonl`, `pip_err.txt`, `8/`, `lane_results/`.

Root keepers (by design): `README.md`, `opencode.json`, `.env`,
`skills-lock.json`, `agent_orchestrator.py`, `startup.py`, `reddit_quant.db`,
`gate_engine.py`. Root-level `harness/` and `Instructions/` are off-list (left).

## 5. Notes

- `data/README.md` is git-ignored (the `data/` tree is ignored except
  `data/validation` and `data/provenance`) — the file exists on disk and is
  intentionally not committed.
- `ops/README.md` documents a **not-yet-created** Workstream A package by
  filename only; Workstream A owns its contents.
- Distinct file: `docs/WORKSTREAM_B_REPORT.md` (UTF-16) is the project's own
  "3-Arm Backtest Re-run with P0 Fix" report — unrelated to this sweep and left
  untouched.

## 6. Conclusion

The sweep is complete and import-clean. The only outstanding items are the
rulings in §4 and the two pre-existing collection errors, all of which lie
outside Workstream B's change surface.