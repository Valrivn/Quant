# Stage 3 — Persistence Readiness Confirmation + Live Backtest Support

Run: House of Quant 4-Stage Dynamic Value & Regime Shield pipeline hand-off.
Author: Big Pickle (persistence / strategy-store support)
Date: 2026-09-17 21:30 UTC (report written while live backtest PID 3048 still running)
Status: **PERSISTENCE READY** — write paths green, read paths green, output schema defined.
Prerequisite report: `center/backfill_integration_verification_20260917.md` (Stage 2).
Peer: gemini-worker (manifest fix DONE, 4stage mode wired into `real_backtest.py`, Stage-4 `regime_shield` in progress).

---

## 0. Executive summary

The persistence layer is confirmed ready for 4-stage backtest write/read. Stage-1 fundamentals
(valuation_fundamentals.parquet, 57 cols / 83,265 rows / 2,179 tickers) read clean for Stage-1 joins;
all four strategy-store write paths and the read surface (ASOF/PIT, confidence propagation, black-swan
window alignment, stage-gate factor join) passed verification against a production-schema temp clone —
using the **real, live schema in `data/harness/factors.duckdb`**, never writes to prod. Backtest output
contract defined at `data/backtests/_schema.json` + append-only `runs.jsonl` registry.

Live: the running 4-stage backtest (PID 3048, `real_backtest.py --strategy 4stage --strata S7 S8`) is
crunching (CPU 38.7→41.9s over ~5 min, WS ~530 MB). It computes regime/gates/blacksave in-memory but has
**no persistence calls wired** — the four strategy tables remain empty (0 rows) and nothing lands in
`data/backtests/` until a persistence pass is added Discussions. All issues below are REPORTED, not auto-fixed
(no schema edits to the shared DB; no writes while the live process holds the DB).

---

## 1. Deliverable 1 — Strategy Store WRITE paths: VERIFIED (temp clone, real schema)

Temp clone carries the same DDL as `data/harness/factors.duckdb` (checked by hashing schema shape); zero
writes hit production. All four write APIs are **DataFrame-based**:

| Write fn (signature) | Result |
|---|---|
| `write_regime_state(df, provenance)` | 3 rows round-trip; PIT `read_regime_at` returns row; future obs_date → `PITValidationError`. |
| `write_grassroots_signals(df, provenance)` | Revision-safe (latest row wins per date); high outputs at min_confidence=medium; low dropped; future date → error. |
| `write_black_swan_events(df, provenance)` | 2 events round-trip; per-row retrieval_timestamp honored; window alignment 2/2 (Mar), 2 (Mar..Jun). **iv_price dropped silently — see §4 issue (a).** |
| `write_stage_gate_decisions(df, provenance)` | 4-stage rows (stages 1..4) round-trip; filed_date > rebalance → error; join to REAL 57-col parquet PIT-clean: roic_cap_rd=0.4474, omr=0.428, wacc=0.0530 (2026-05-01 anchor). |

`export_table` → parquet + `.provenance.json` sidecar with **matching SHA-256** for all four tables.
`PITValidationError` raised correctly on lookahead everywhere.

## 2. Deliverable 2 — Factor Store READ paths: VERIFIED

- `read_factor(..., min_confidence="medium")`: confidence propagation None→3, medium→2, high→1 rows.
- `hype_p95` low-only rows excluded from the core read surface.
- **ASOF no-lookahead confirmed**: factor rows observed 2024-01-15 see 01-12 SHOCK, NOT 01-19 EXPANSION; planted 01-08 rows ASOF to 01-05 EXPANSION. `query_regime_conditional` honors PIT.
- `query_stage_gate_factors`: 4 gate rows joined on the real parquet; filed<=rebal True; roic_cap_rd=0.6203.
- `align_black_swan_timestamps`: Mar window 1 row; Mar..Jun 2 rows.
- `read_grassroots_snapshot("...", min_confidence="medium")` = 2 (NVDA low dropped).
- **Production read surface snapshot** (read-only, prod DB): Stage-1 83,265 rows / 2,179 tickers / roic_cap_rd 25,184 / omr 54,397 / wacc 82,612; Stage-2 `grassroots_c_t` 0; Stage-3 `hype_p95` 0; Stage-4 lock. Legacy qual factors ALL NULL confidence → see §4 issue (c).

## 3. Deliverable 3 — Backtest output persistence contract: DEFINED

- `data/backtests/_schema.json` — full contract: run registry (`runs.jsonl`), per-run dirs
  (`<run_id>/run.json`, returns/positions/trades/regime_states/stage_gate_decisions/grassroots_signals/
  black_swan_events parquet, each with `.provenance.json` SHA-256 sidecar), column-level DDL for every
  parquet with PIT anchor columns and required flags, PIT rules, and verification checks.
- `data/backtests/runs.jsonl` — append-only registry, created (empty, ready).
- Parquet sides mirror the strategy-store tables 1:1 (regime_states ↔ `regime_state`, stage_gate_decisions ↔
  `stage_gate_decision`, etc.) so backtest state can be diffed against live state with zero ETL.

## 4. Issues encountered — REPORTED (no auto-fix)

- **(a) Silent column drop — HIGH.** `write_black_swan_events` DDL has no `iv_price` / `trigger_price`
  columns. Backtest-supplied `iv_price` (Stage-4 IV-based limit) is silently DROPPED. Closest existing
  col is `entry_price_limit`. → Recommend mapping `iv_price` → `entry_price_limit` at the writer, or add a
  column to the DDL (schema change belongs to the schema owner, not this task).
- **(b) Signature mismatch vs dispatch — HIGH.** Dispatch/doc lists SCALAR write signatures; the real API is
  DataFrame-based `write_*_states(df, provenance)`. Scalar calls → `TypeError`. Any caller built against the
  documented scalar contract will crash on contact. → Report only; writer contract in
  `_schema.json > strategy_store_sync` documents the true DataFrame signatures.
- **(c) ALL-NULL mapping_confidence on legacy factor_panel — MEDIUM.** All 562,343 rows have
  `mapping_confidence = NULL`. The core read filter (`!= 'low'`) drops NULLs, so legacy qual factors —
  gh_commits_90d (7,300), ats_posted_90d (728), cfpb_complaints_90d (364), nhtsa_complaints_90d (84),
  patent_citations_1y (44) — are INVISIBLE at `min_confidence="medium"`. → Recommend a one-time backfill
  from QUAL_FACTOR_CATALOG defaults (deferred, requires schema-owner approval).
- **(d) Strategy tables empty (pending backtest writes) — EXPECTED, not a fault.** All four strategy tables
  are 0 rows because the running backtest computes strategy state in-memory and never persists. Nothing
  landed in `data/backtests/` yet either. Confirmed by live monitor: tables still 0 at 21:27:05 and 21:30.
- **(e) No lock contention observed.** Monitor ran repeated read-only queries against prod DB while PID
  3048 held it; no DuckDB lock errors, no deadlock. Single-writer, WAL-compatible.

## 5. Artifacts produced this session

- `data/backtests/_schema.json` (12,672 B, 2026-09-17 21:28)
- `data/backtests/runs.jsonl` (empty registry, 21:28)
- Validation scripts (temp, run against temp clones — prod untouched):
  `stage3_write_path_validation.py`, `stage3_read_path_validation.py`, `monitor_db.py` (all exit 0).

## 6. Live support log (backtest PID 3048)

```
21:25:22  PID 3048 launched: real_backtest.py --strategy 4stage --strata S7 S8 (miniconda python)
21:26:00  Prod DB snapshot: all 4 strategy tables 0 rows; factor_panel 562,343 / NULL conf 562,343.
21:27:05  Monitor: no strategy-store writes yet; no lock contention; CPU growing.
21:30:00  PID 3048 alive, CPU 38.7→41.9s, WS ~534 MB (Stage-4 NumPy heavy). Tables still 0.
expected  Post-run: aggregate real_backtest_report.json only (no strategy-store rows, no data/backtests/)
          until a persistence pass is wired. Persistence layer is ready to accept writes when the
          engine is updated per the _schema.json contract.
```

## 7. Next steps (recommended, for schema owner / integration)

1. Wire a persistence pass into `real_backtest.py` writing per `_schema.json` (regime_states →
   `write_regime_state`, gates → `write_stage_gate_decisions`, etc.) AFTER the in-memory phase; prod tables
   fill + `data/backtests/<run_id>/` artifacts land.
2. Resolve (a) `iv_price` mapping/DDL; (b) document true DataFrame signatures; (c) backfill
   `mapping_confidence` for legacy qual factors.
3. Re-run AFTER persistence wired: verify strategy tables non-empty and `data/backtests/<run_id>/`
   sidecar SHA-256s match.
