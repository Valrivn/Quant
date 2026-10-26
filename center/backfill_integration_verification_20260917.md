# BACKFILL INTEGRATION VERIFICATION REPORT — STAGE 2 (big-pickle-worker)

**Date**: 2026-09-17 (verified through 2026-09-18T01:20+00:00)
**Worker**: big-pickle-worker (Workstream B — persistence / schema / integration verification)
**Scope**: Verify gemini-worker's backfill integrates cleanly into the persistence layer.
**Constraint respected**: No strategy logic touched. No persistence artifacts modified. Reporting only.

---

## Verdict

| System | Status | Gate |
| :--- | :--- | :--- |
| 1. Valuation Fundamentals parquet (57-col Stage-1 spec) | **GREEN** | PASS |
| 2. Factor Store DuckDB sync (mapping_confidence, min_confidence) | **GREEN** | PASS |
| 3. Strategy Store tables + PIT-safe read surfaces | **GREEN** (schema ready; tables awaiting Stage-2/3/4 writes) | PASS |
| 4. Provenance Audit | **RED — FAIL** (3 finding classes; no hash corruption, see §4) | FAIL |
| 5. Universe Builder integration | **GREEN** (all thresholds met) | PASS |

**4-Stage backtest readiness: AMBER — persistence layer verified green; provenance manifest +
audit must be reconciled before a fully green audit gate. Two data-coverage observations
also flagged (§6) that do not block the backtest but constrain its universe.**

---

## 1. Valuation Fundamentals Parquet Integration — PASS

Verified on `data/pit_fundamentals/valuation_fundamentals.parquet` (rebuilt by backfill batch 2026-09-17 21:07 local, provenance sidecar concurrent).

- **Schema**: exactly **57 columns** — 48 base valuation columns + the full 9-column Stage-1 migration set appended (`rd_cap_asset, rd_amort_exp, ebit_cap_rd, nopat_cap_rd, invested_capital_cap_rd, roic_cap_rd, omr, cost_of_equity_est, wacc_estimate`). No column dropped or reordered (append-only migration, idempotency confirmed via `python -m scripts.migrate_valuation_stage1 --check`: all 9 present, sidecar_ok=true, 57 cols, 95,417 rows).
- **Rows**: 95,417; `filed_date` nulls = **0**; anchor range 2009-04-15 → 2026-09-15 (all ≤ retrieval 2026-09-18, zero lookahead).
- **Stage-1 column population on rows**:
  - 100% non-null: `rd_cap_asset, rd_amort_exp, ebit_cap_rd, invested_capital_cap_rd, cost_of_equity_est`
  - 99.4% non-null: `nopat_cap_rd`, `wacc_estimate`
  - 57.1% non-null: `omr` (1,248 tickers) — requires revenue, by design
  - 20.0% non-null: `roic_cap_rd` (596 tickers) — gated to R&D-positive names by design
- **SHA-256 sidecar**: recomputed file hash `526f18aa…d256347` **matches** sidecar exactly; sidecar row_count (95,417) and date_range match the file. Atomic temp-file replacement convention respected.
- **Read path**: `query_stage_gate_factors` joins `stage_gate_decision` → `read_parquet(valuation_fundamentals.parquet)` on (ticker, filed_date); returns widened columns including all nine Stage-1 fields (verified on temp DB against the production parquet).

**No schema drift in the fundamentals parquet.**

## 2. Factor Store DuckDB Sync — PASS

Verified against `data/harness/factors.duckdb` (shared harness DB) and module self-test.

- `factor_panel` (562,343 rows) gained the `mapping_confidence` column via idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS` — existing rows untouched (legacy rows carry NULL; NULLs are conservatively excluded by `min_confidence ∈ {high, medium}` reads, which is the safe direction).
- `read_factor(factor_name, min_confidence=…)` verified on new qualitative factors:
  - `grassroots_c_t`: default confidence `medium`; per-row grades accepted ({high, medium, low} mix) and correctly filtered — `min_confidence='medium'` drops low, `'high'` keeps high only.
  - `hype_p95`: catalogue guard enforces `('low',)` only; `mapping_confidence='high'` rejected with ValueError (Tier-3 ApeWisdom rule).
- **`mapping_confidence` populates for new qualitative loader outputs**: verified end-to-end on a temp DB (write → read with confidence filters → distribution). Catalogue name guards (`QUAL_FACTOR_CATALOG`, `SOURCE_CONFIDENCE_RULES`) are enforced on write.
- FactorStore module self-test passes; strategy-store migration is idempotent (all v1–v4 applied 2026-09-18T01:07:13Z, re-run is a no-op).
- Note: 6 legacy qualitative factors in `factor_panel` (`ats_posted_90d`, `cfpb_complaints_90d`, `gh_commits_90d`, `nhtsa_complaints_90d`, `patent_citations_1y`) still carry NULL `mapping_confidence` from before the migration. New loads re-grade them; nothing to fix in the store.

## 3. Strategy Store Tables — PASS (schema) / AWAITING DATA (tables)

- All four tables created and accepting writes: `regime_state`, `grassroots_signal`, `black_swan_event`, `stage_gate_decision` (schema migrations v1–v4, `strategy_schema_version`=4/4, applied 2026-09-18T01:07:13Z).
- Write paths verified on temp DB: `write_regime_state` (probability-sum + state checks, PIT `obs_date ≤ retrieval_timestamp`), `write_grassroots_signals` (required `mapping_confidence` grade, PIT check, revision-safe PK), `write_black_swan_events` (tier check, per-row `retrieval_timestamp` honored), `write_stage_gate_decisions` (stage 1–4 check, `filed_date ≤ rebalance_date` PIT anchor).
- **PIT-safe read surfaces verified** (temp DB, populated data):
  - `read_regime_at(trade_date)` — latest regime with `obs_date ≤ trade_date`; returns None before first obs; no lookahead.
  - `read_grassroots_snapshot(trade_date, min_confidence)` — latest revision per ticker at last signal date ≤ trade_date; correct high/medium/low filtering; empty before first signal.
  - `align_black_swan_timestamps(window_start, window_end)` — drops events whose `retrieval_timestamp > window_end` (knowable-inside-window semantics) — verified dropping a late-retrieved TIER2 event and including it when the window widens.
  - `query_regime_conditional` (ASOF join factor_panel → regime_state), `query_stage_gate_factors`, `export_table` + `.provenance.json` sidecar (SHA verified) all green.
- Production tables are **empty (0 rows each)** — they are schema-ready; Stage-2/3/4 data writers have not yet run in this backfill wave. Reads on empty tables return clean empty/None (no errors).
- `tests/test_strategy_store.py`: **15/15 passed** (incl. PIT enforcement, lookahead rejection, stage-1 migration on a real copy).

## 4. Provenance Audit — **FAIL** (no corruption; coverage + heuristic artifacts)

Command: `python -m validation.provenance_audit --run --data-root data/` → report `data/validation/provenance_audit_report.json`, status **FAIL**, ran 2026-09-18T01:15Z.

| Check | Result |
| :--- | :--- |
| SHA-256 vs manifest | **0 mismatches** across all manifest-covered files; coverage 16/1567 (1551 no manifest entry) |
| PIT compliance | 13/15 files clean; **2 violations** (both new universe files — see A) |
| mapping_confidence | **10/10 PASS** — distribution {high: 9,168, medium: 7,174, low: 2,681} |
| Provenance schema (§8) | 14/1567 complete (1553 incomplete) |
| Survivorship | includes_delisted=True, delisted=107, pit_correct=True, is_active_false=53 |

Findings (reported, NOT auto-fixed):

- **[PROV-01 / PROV-02 — MEDIUM] Manifest coverage gap (1551 files)**. The audited estate has grown to 1,567 parquets while `manifest.jsonl` covers 35 unique paths. Breakdown: 1,548 × `data/source/yfinance/<TICKER>/prices.parquet` (created 2026-09-15 pre-backfill, no manifest entries, no sidecars) + 3 × `data/pit_fundamentals/*.parquet` — including the backfill-rebuilt `valuation_fundamentals.parquet`, which **has a complete §8-compliant sidecar but no manifest entry**. The universe-chain manifest entries written by the backfill (`pit_universe_2018_2026.parquet`, `monthly_reconstitution.parquet`, `price_cache.parquet`) are also §8-incomplete (missing `pit_timestamp_column`, `entity_key`, `transformations`, `date_range`). Remediation path exists (`scripts/backfill_provenance_manifest.py --run`) — not executed under the no-auto-fix constraint.
- **[LOOK-01 — WARN] PIT heuristic flag on the 2 new universe files.** `pit_universe_2018_2026.parquet` (111,000 rows) and `monthly_reconstitution.parquet` (6,000 rows) carry dates after the retrieval stamp (2026-09-18). These are **planned Oct–Dec 2026 trade dates on the 2018-01-01→2026-12-31 backtest schedule**, not lookahead leakage (schedule built 2026-09-18, prices through 2025-01-06). The audit's anchor heuristic treats `date` as an availability anchor for these files; the condition is inherent to a pre-built forward-dated trade schedule and does not affect the 2018–2024 primary window.
- **[SEL-01 — HIGH] Pre-existing, documented selection-pool bias.** Universe candidate pool ranked by 2026 market cap (`master_universe.yaml`), not historical PIT membership; `shares_outstanding` synthetic. This finding predates the backfill (declared in the audit's own bias checklist and Data_Strategy §9) and is the single high-severity flag driving status=FAIL. It is a research-design acceptance decision, not a persistence defect.
- No hash mismatches anywhere → **no corruption introduced** by the backfill.

## 5. Universe Builder Integration — PASS

Verified after gemini-worker's `universe_builder --rebuild --top-1500 --start 2018-01-01 --end 2026-12-31` (two runs; final deterministic output 2026-09-18T01:18Z, SHA-identical across runs).

- **Daily PIT universe (`pit_universe_2018_2026.parquet`)**: 3,490,500 rows; **avg 1,711.4 tickers/month** (min 1,500, max 1,885) across 108 months (2018-01-31 → 2026-12-31) — **≥1,500/month PASS**.
- **`price_stale_flag` = 0.0015%** (53 rows, all tail-2024 for the 4 flagged names) — **<1% PASS**.
- **`illiquid_flag` = 0.082%** — **<15% PASS**.
- **Delisting alignment**: `delisting_events.parquet` — 107 SEC 8-K events, **104 distinct `delist_filed` dates**, all `form='8-K'` (SEC_EDGAR 8K_1.0). All 4 delisted tickers are present in the PIT universe with market-cap rows before their delist-filed dates (pit_correct=True). Post-delist tail flagged `is_active=False` (53 rows) — the survivorship surface is materially improved vs. the 9/13 state (0 inactive rows). Note: the 4 names (ABBV/BRK-B/CAT/CVX) are keyword-triggered 8-K "delisting/bankruptcy or material-agreement" events, not actual delistings (audit SURV caveat).
- **Price cache**: rebuilt 2026-09-18T01:17Z, 3,536,714 rows / 2,000 tickers, 2018-01-01 → 2025-01-06, 0 stale, SHA-verified in manifest.

## 6. Data Coverage Observations (integration surfaces, not schema drift)

1. **Fundamentals × universe coverage: 66.7%.** `valuation_fundamentals.parquet` covers 1,333 of 2,000 universe tickers (1,252 with revenue, 596 with `roic_cap_rd`). Materially better than the 9/15 estate (145 tickers with 3+ years) but ~667 universe names still lack fundamentals and will fail Stage 1 by absence (conservative default, no lookahead). Stage-1 gate should treat missing fundamentals as `passed=False` / skip with a logged reason.
2. **Price horizon vs universe horizon: 2025-01-06 vs 2026-12-31.** The daily universe extends ~2 years past the price cache end. Rows beyond the price horizon are not stale-flagged (flag computed at build time). For the primary 2018–2024 window this is immaterial; any 2025–2026 simulation leg must not assume live prices for those dates.

---

## Batch-by-batch consolidation

| Batch (observed) | Artifacts | Result |
| :--- | :--- | :--- |
| B1 — XBRL facts extraction (pre-wave, 9/15) | `raw_xbrl_facts.parquet` foundation | Verified as basis of B2 |
| B2 — Fundamentals + strategy schema (9/17 21:07Z+1 local) | `valuation_fundamentals.parquet` + sidecar; strategy store v1–v4 | **GREEN** (schema/57-col/SHA/PIT) |
| B3 — Universe rebuild (9/17 21:13 + 21:18 local) | `pit_universe_2018_2026.parquet`, `monthly_reconstitution.parquet`, `price_cache.parquet`, manifest entries | **GREEN** (all 4 thresholds) |
| B4 — Stage-2/3/4 data writers (grassroots_c_t, hype_p95, regime, black-swan, stage-gate) | Not yet observed — factor/strategy store paths pre-verified | **READY** (write/read surfaces green on temp DB) |

## Recommendations to CEO (no code changed)

1. Run `scripts/backfill_provenance_manifest.py --run` (or the §8 backfill equivalent) then re-run the audit — targets PROV-01/PROV-02; the remaining FAIL drivers are the documented SEL-01 acceptance item and the future-dated-trade-schedule heuristic (LOOK-01), which are decision items, not defects.
2. Confirm Stage-1 gate missing-fundamentals semantics (fail-closed) before the 4-stage backtest; expect an effective Stage-1-eligible universe ≈ 1,250–1,330 names.
3. Decide the 2025–2026 universe tail treatment (exclude from simulation or accept flat-price approximation).