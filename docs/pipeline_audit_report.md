# Pipeline Audit Report — RS-07 Provenance & Bias Pre-Flight

- **Date:** 2026-09-13
- **Scope:** Full data estate under `data/` (91 tracked parquet files), Phase-4 dual-window 3-arm pipeline, gate registry.
- **Method:** `validation/provenance_audit.py` (rewritten, CLI: `--run`, `--check-manifest`, `--check-pit`) + static code review of ingestion/backtest paths.
- **Artifacts:** `data/validation/provenance_audit_report.json` (machine-readable), this report.
- **Runbook refs:** `.agents/general/org/runbook.md`, `.agents/project/org/blueprint.md`, `Data_Strategy.md` §6–§9.

## Executive Summary — verdict: **DO NOT EXECUTE Phase-4 backtest yet**

The audit produced **4 HIGH, 5 MEDIUM/WARN, 1 informational** findings. Three HIGH findings
(SURV-01 synthetic delistings, SEL-01 2026-ranked candidate pool, CONT-01 missing ML
contamination guard) still block a defensible run. IMPUTE-01 (RPO imputation leak) was
**already remediated in this audit** by changing `compute_rpo_growth()` in
`scripts/run_phase4_dual_window_3arm.py` to drop `rpo_is_imputed=True` rows and re-basing
the primary window from 2014–2024 (imputation-dependent) to **2018–2024 (RPO-clean)**.

GO / NO-GO:

| Gate | Status |
| --- | --- |
| INGEST-1 (uniq rows all sources) | ✅ Pass (0 dup rows across tracked files) |
| INGEST-2 (nulls+imputations registered) | ⚠️ Partial — imputation flags exist, OOS-invalidated usage removed |
| INGEST-3 (as-of propagation) | ⚠️ WARN — 3 PIT violations (see Lookahead); price cache OK |
| UNIVERSE-1 (PIT cap) | ✅ Pass (delisted names capped at delist date; 0 mcap ≤ 0 post-cap rows inside traded range) |
| UNIVERSE-2 (index base) | ⚠️ WARN — reconstitution slate extends to 2026-12 (planned dates), 2018–2019 absent |
| BIAS-1 (survivorship) | ⚠️ WARN — events are synthetic/patterned (74 × same date) |
| BIAS-2 (contamination guard) | ❌ **FAIL** — `research/reverse_engineer_multibaggers.py` does not exist |
| GATE-1 (frozen registry) | ✅ Pass (gates_v2.yaml frozen, sha256 matches dry-run provenance) |
| **Phase-4 EXECUTE gate** | ❌ **NO-GO** until SURV-01 / SEL-01 / CONT-01 remediated |

## 1. SHA-256 Integrity (manifest)

- Source of truth: `data/provenance/manifest.jsonl` (append-only; latest entries win).
- Coverage: manifest attests **15 files**; the estate holds **91 tracked parquets**.

| Result | Count | Files |
| --- | --- | --- |
| ✅ Passed | 6 | `prices/price_cache`, `universe/*` (4), `provenance/qual_coverage_matrix` |
| ❌ Hash mismatch | 9 | **ALL 9 data/qual/*.parquet** — files were re-written (columns appended, e.g. `nhtsa_recalls_90d`, `patent_small_entity_flag`, `sha256`) after their manifest entries were appended |
| ⛔ No manifest entry | 76 | `data_lake/fred` + `alfred` (75), `data/factors/*` (2), `data/fred_cache`, `data/pit_fundamentals`, `data/universe/pit_universe_2014_2024` |

- Manifest-append discipline is functioning (6/6 attested-but-unchanged files match), but
  **re-writes are not re-attested**: any rebuild must append a new manifest line (see §8 REC-01).
- First byte evidence of the 9 mismatches: e.g. `gh_velocity.parquet` expected
  `40e8e447…` vs actual `5bfb96d2…`, `amazon_reviews` expected `bacdc91d…` vs actual `8230178a…`
  (full pairs in `provenance_audit_report.json`).

## 2. PIT Compliance (no lookahead)

15/18 scoped files passed. Anchor columns used: `filed_date` / `created_at` / `grant_date` /
`DateReceived` / `posted_at` / `delist_filed` / `rebalance_date` / `date`.

| File | Violation | Rows | Assessment |
| --- | --- | --- | --- |
| `universe/pit_universe_2020_2026.parquet` | `future_dates_relative_to_retrieval` | 110,679 | **HIGH** — dates run through 2026-12-31 while the file was built 2026-09-12/13; extended window is pre-projected, not observed |
| `universe/monthly_reconstitution.parquet` | future rebalance dates | 5,604 | LOWSAME — planned 2025-2026 reconstitution slate; 2020-2021 PRIMARY-window rows are clean |
| `qual/ats_mapping.parquet` | `retrieval_timestamp` bulk-stamped on run day | 14 | LOW — mapping table, stamp reflects fetch, not usage |

Key clean files (verified): `prices/price_cache.parquet` (window 2020-01-01 → 2025-01-02,
`price_stale_flag` = quote_age>2, 0 stale), universe 2018–2024, rpo (filed_date ✓),
mda_tone (filed_date ✓), GH/ATS/patents/CFPB/NHTSA (availability anchors ✓).

### Survivorship check (consistency with §7.4)

- `universe_includes_delisted=true`, `delisted_count=74`, `pit_correct=true`
  (all 74 delisted names appear in `pit_universe_2018_2024` with caps at `delist_filed`).
- **`is_active=False` rows = 0 across all PIT universes** — the delisting path never
  exercised; delisted names are absent post-cap rather than flagged inactive (same outcome,
  weaker bookkeeping).
- `market_cap <= 0` rows inside 2018–2024 universe: **739** (data-quality defect, see
  REC-04).

## 3. Mapping Confidence (qualitative estate)

- 10/10 `data/qual/*.parquet` carry `mapping_confidence`; **100% of rows declare `high`**
  (coverage matrix agrees, 1,128 rows all high).
- Assessment: over-declared — heuristic mappings (ATS career-URL tokens, App Store titles,
  Apewisdom handles) cannot all be high-confidence. **MAP-01 (WARN): downgrade rules and
  `medium/low` grading required before qual tilts are priced.**

## 4. Provenance Schema (10-field contract)

- Complete: **4/19** scoped files (`price_cache`, universes 2018–2024 / 2020–2026,
  `qual_coverage_matrix`).
- Incomplete: **15 files** — missing fields cluster on: `source_version`
  (universe/rpo/mda_tone + all qual), `pit_timestamp_column` (rpo, mda_tone, gh_velocity,
  ats_velocity, ats_mapping), `transformations` (rpo, mda_tone, ats_mapping, inst_ownership,
  patents_signals), `entity_key` (put vs pyvar vs cik inconsistent). Exact per-file
  `missing_fields` map in the JSON report.
- `factors/rpo.parquet` + `factors/mda_tone.parquet` carry sidecars
  (`.provenance.json`) but no manifest entry; sidecars+vault merge logic is in place.

## 5. Bias Checklist (9/9 audited)

| # | Bias | Status | Evidence | Mitigation / State |
| --- | --- | --- | --- | --- |
| 1 | **Survivorship** | ⚠️ WARN | 74 delisting events **all dated 2021-06-30** (`universe_builder.py` synthetic: every-20th CIK); 0 `is_active=False` rows; candidate pool excludes pre-2026 dead names | PIT caps correct for included names; **REPLACE synthetic events with real SEC 8-K delistings** (REC-02) |
| 2 | **Lookahead** | ⚠️ WARN | 110,679 future rows in `pit_universe_2020_2026`; 5,604 future reconstitution dates | PIT anchors verified on all factor/price files; re-tag extended-window rows as `as_of_projection` (REC-05) |
| 3 | **Forward fill** | ✅ PASS | `price_cache.py` verified: `ffill(limit=2)`, `price_stale_flag=quote_age>2`; 0 stale rows; no ffill across PIT gaps in factor estate | — |
| 4 | **Selection** | ⚠️ WARN | `master_universe.yaml` = 2,552 tickers **ranked by 2026 market cap** (BRK-B $1.08T); synthetic shares/price via `hash()` (PYTHONHASHSEED-dependent!) | Deterministic `SHA-256`-derived synthetic regression needed; historical membership source required (REC-03) |
| 5 | **Mapping** | ✅ PASS | All qual files carry `mapping_confidence`; entity_key present | Over-declared at 100% high (MAP-01) |
| 6 | **Imputation** | ❌ HIGH ➜ **FIXED in driver** | 260 imputed RPO rows inside 2018–2024 (47% of window rows); `rpo_imputed` validation FAIL (OOS R²=0.157); old driver used `rpo_imputed` for ALL rows | `compute_rpo_growth()` now drops `rpo_is_imputed=True`; primary window rebased to 2018–2024 RPO-clean |
| 7 | **Gate integrity** | ✅ PASS | `config/gates_v2.yaml` frozen v2, sector_relative, sha256 `0351b8591fa9cd2864324f80f10316cdb9bb022eea3e0a0794da15a4b214c84b` matches `dry_run_gates_rs05.json`; pre-registered 2026-09-12T23:43Z | NB: calibration decision = **needs_review** (10,000 null portfolios, seed 20260909) |
| 8 | **Data snooping** | ✅ PASS | Manifest build order: universe 2026-09-12T23:36–23:39Z **before** qual 2026-09-13T00:54–00:57Z; no factor used before universe freeze | — |
| 9 | **Contamination** | ❌ HIGH | `research/` directory absent — `research/reverse_engineer_multibaggers.py` (walk-forward CPCV, embargo ≥ 12mo, LASSO+RF train/test split) **never implemented** | **REQUIRED before any ML track runs** (REC-06) |

## 6. Data-Readiness Inventory (status after this audit's reorg)

| Estate | Location (post-audit) | Status |
| --- | --- | --- |
| Prices cache | `data/prices/price_cache.parquet` | ✅ 1,832,430 rows, 2020-01-01..2025-01-02, 0 stale |
| PIT universes | `data/universe/` | ⚠️ extended-window future rows; mcap≤0 739 rows |
| Delistings | `data/universe/delisting_events.parquet` | ❌ synthetic (74 × 2021-06-30) |
| Quant factors (SEC) | `data/factors/rpo.parquet`, `mda_tone.parquet` | ⚠️ RPO imputation excluded at driver level; mda_tone = 2026 sample only |
| Qual estate | `data/qual/` (10 files + coverage matrix) | ⚠️ hashes un-attested (9/9), 100%-high mapping |
| Raw archives | `data/source/gh_archive/`, `data/source/gdelt/` | ✅ **moved this audit** (was root-level `data/gh_archive`, `data/gdelt`); `sprint1_parallel_ingest.py` writers updated |
| Legacy factor dups | `data/archive/factors_legacy/` (10 pairs + sidecars) | ✅ **moved this audit**; canonical reads now `data/qual/` |
| Backtest artifacts | `data/backtests/` | ✅ created (empty, ready) |
| Auxiliary (documented) | `data_lake/`, `sec_facts/`, `pit_fundamentals/`, `fred_cache/`, `gdelt→source/` | ⛔ outside manifest scope (76/91 un-attested) |

**Driver sync:** `scripts/run_phase4_dual_window_3arm.py` updated — primary **2018-01-01 →
2024-12-31 (RPO-clean)**, secondary **2020-01-01 → 2026-12-31 (extended)**; reads
`data/qual/{gh_velocity,ats_velocity,patents_signals,cfpb_velocity,nhtsa_signals,
inst_ownership}.parquet` with canonical attribute columns
(`gh_commits_90d`, `ats_posted_90d`, `patent_citations_1y`, `cfpb_complaints_90d`,
`nhtsa_complaints_90d`); `compute_rpo_growth` emits clean rows only.

**Other latent defects surfaced (outside this audit's fix scope):**
`ingestion/harness/three_arm_runner.py` default `load_data_from_store()` returns **synthetic
data**; `data/harness/factors.duckdb` holds only 5 factors (2023 sample cohort) — the
production wiring from real store → runner is still missing.

## 7. Bias Mitigations Applied

1. **IMPUTE-01 remediated** — RPO-clean factor path + window re-baseline (driver change,
   verified import-clean; full backtest re-run is Phase-4 execution, out of this audit's scope).
2. **Canonical tree enforced** — `data/source/`, `data/archive/factors_legacy/`,
   `data/backtests/` created; consumers updated (`sprint1_parallel_ingest.py` L244/L303,
   `Quantitative/entry_timing.py`+`consumer_signal.py` already read `data/source/`).
3. **Audit tooling hardened** — `validation/provenance_audit.py`: repo-relative manifest
   matching, schema waiver for non-qual files, tz-naive/aware-safe comparisons, 5-file
   self-test green.

## 8. Recommendations / Open Items (blockers in bold)

1. **REC-01 (PROV-01, PROV-02, SHA):** append fresh manifest entries for every rebuilt
   file; backfill `source_version`, `pit_timestamp_column`, `transformations`, `entity_key`
   on the 15 incomplete files.
2. **REC-02 (SURV-01):** replace synthetic delistings with real SEC 8-K delisting events;
   set `is_active=False` on PIT universes at cap date.
3. **REC-03 (SEL-01):** source a historical (2018-01 PIT) membership + market-cap list;
   replace `hash()`-seeded synthetic shares/prices with deterministic SHA-256-derived values.
4. **REC-04 (universe mcap):** purge/repair the 739 `market_cap <= 0` rows before any
   cap-weighted strategy reads the universe.
5. **REC-05 (LOOK-01):** tag `pit_universe_2020_2026` / `monthly_reconstitution` extended
   rows with `as_of_projection` so PIT checks skip projections explicitly.
6. **REC-06 (CONT-01):** implement `research/reverse_engineer_multibaggers.py`
   (walk-forward CPCV, embargo ≥ 12mo) before the ML track — hard blocker.
7. **REC-07 (IMPUTE-01, driver):** gate calibration decision is `needs_review`; re-run
   the 3-arm backtest under the updated RPO-clean windows and re-score gates on real data.

---

*Generated by `validation/provenance_audit.py --run --data-root data/` (SHA-256, PIT,
mapping_confidence, provenance_schema, survivorship, bias_flags) on 2026-09-13.*