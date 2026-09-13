# Tomorrow.md — Master Plan & Execution Agenda (Updated 2026-09-10)

## Executive Summary
- **Phase 4 Backtest Completed**: Dual-window 3-arm backtest FAILED (2/10 gates primary, 1/10 secondary). Root cause: Universe collapse (27 → 13 tradable) causing Arm A singleton screen in 71/72 months → Arm C ≡ Arm A.
- **Tier-3 Debate Completed (B-20260910-001)**: Council debate between Position A (statistical purity) and Position B (pragmatic execution) resulted in **CEO Ruling: EXECUTIVE OVERHAUL**.
- **Data Scientist Sanity Check Completed**: 3 directives evaluated — all **CONDITIONAL** with binding remedial actions (RS-01 through RS-06).
- **Next Build Mandate**: Fix survivorship bias, PIT leakage, gate re-registration, and rebuild pipeline for Top 1,000 dynamic PIT universe.

---

## 1. What Was Accomplished Today (2026-09-10)

### ✅ Tier-3 Council Debate & CEO Ruling
- **Brief**: `B-20260910-001` — Pipeline overhaul for universe expansion, moat-first adaptive screening, reverse-engineering track.
- **Position A** (Statistical Purity): `position-A-B-20260910-001.md`
- **Position B** (Pragmatic Execution): `position-B-B-20260910-001.md`
- **Disagreement Map**: `disagreement-map-B-20260910-001.md`
- **Synthesis**: `synthesis-B-20260910-001.md`
- **CEO Ruling (EXECUTIVE OVERHAUL)**: `ceo-ruling-B-20260910-001-modified.md`

### ✅ Data Scientist Sanity Check
- **Directive 1 (Universe Expansion)**: CONDITIONAL — Survivorship bias risk; requires dynamic PIT top-1000 reconstitution
- **Directive 2 (Moat-First Adaptive Screening)**: CONDITIONAL — PIT leakage risk; requires strict SEC filing dates & qualitative timestamps
- **Directive 3 (Reverse Engineering CPCV)**: CONDITIONAL — Selection bias risk; requires walk-forward training + embargo ≥ horizon
- **6 Binding Remedial Actions (RS-01 to RS-06)** logged in `Data_Strategy.md §13.3`

### ✅ Documentation Updated
- `Data_Strategy.md` v1.1 → v1.2 with Decision Log entries, backtest results, and sanity check evaluation (§13)
- All debate artifacts archived in `.agents/project/org/decisions/_drafts/`

---

## 2. Agenda for Tomorrow (2026-09-11): Remedial Fixes & Pipeline Rebuild

Tomorrow's execution focuses on implementing the **6 Binding Remedial Actions (RS-01 to RS-06)** before re-running the backtest.

### 🔧 Task 1: Dynamic PIT Universe Builder (RS-01)
- **File**: `ingestion/universe_builder.py`
- **Change**: Replace static 27-name universe with dynamic monthly top-1,000 by market cap reconstitution using SEC EDGAR historical index files + delisting 8-Ks.
- **Requirement**: Include delisted/bankrupt firms in historical $U_t$; max 2-day forward fill for prices; drop illiquid symbols.

### 🔧 Task 2: Strict PIT Timestamp Enforcement (RS-02)
- **Files**: `ingestion/sec_rpo_parser.py`, `ingestion/sec_mda_parser.py`
- **Change**: All fundamental ratios (ROIC, WACC, ICR, FCF Yield) must use `FILING_DATE` from SEC 10-Q/10-K, NOT `period_end`.
- **Qualitative Loaders**: Add `retrieval_timestamp` provenance to every row in Phase 2/3 loaders (RS-03).

### 🔧 Task 3: Price Cache Layer Fix (RS-04)
- **File**: `ingestion/universe_builder.py` / new price cache module
- **Change**: Reduce Yahoo Finance forward fill from 5 days → **max 2 trading days**.
- **Add**: `price_stale_flag` column (True if quote age > 48 hours) for PIT validator to reject.

### 🔧 Task 4: Gate Re-Registration (RS-05)
- **File**: `ingestion/harness/gate_engine.py`
- **Change**: Re-register Gates 3 & 4 (Welch C vs A, Welch C vs B) with new sector-relative threshold hypothesis.
- **Rationale**: Screen changed from absolute (ROIC>10%) to adaptive (sector quantile top 30%) → old gates invalid.

### 🔧 Task 5: Reverse-Engineering Research Module (RS-06)
- **File**: `research/reverse_engineer_multibaggers.py` (NEW)
- **Requirements**:
  - Walk-forward training on $U_t$ at time $t$ (no post-hoc top 10% selection)
  - CPCV with embargo ≥ forward horizon (e.g., 12 months for 12-month returns)
  - LASSO + Random Forest feature selection on 10-year outperformers
  - Max 40% empirical weight in production hybrid

### 🔧 Task 6: Re-Run Dual-Window 3-Arm Backtest
- After RS-01 to RS-05 complete, dispatch `gemini-worker` + `bigpickle-worker` to execute:
  - Primary Window (2014–2024) with Top 1,000 dynamic PIT universe
  - Secondary Window (2018–2024) with RPO-clean data
  - All 10 pre-registered gates (updated)

---

## 3. Dispatch Commands for Tomorrow

```bash
# 1. Update Universe Builder with Dynamic PIT Top-1000
python -m ingestion.universe_builder --rebuild --top-1000 --start 2014-01-01 --end 2024-12-31

# 2. Validate PIT Timestamp Enforcement on SEC Loaders
python -m ingestion.sec_rpo_parser --validate-pit
python -m ingestion.sec_mda_parser --validate-pit

# 3. Test Price Cache with 2-Day Forward Fill Limit
python -m ingestion.universe_builder --test-price-cache --max-forward-fill 2

# 4. Re-register Gates 3 & 4 in Gate Engine
python -m ingestion.harness.gate_engine --re-register-gates 3,4 --sector-quantile

# 5. Run Gate Calibration on New Gates (10,000 null portfolios)
python -m validation.gate_calibration --run --n 10000

# 6. Execute Full Backtest Re-Run (after RS-01 to RS-05 complete)
python -m ingestion.harness.three_arm_runner --start 2014-01-01 --end 2024-12-31 --window primary
python -m ingestion.harness.three_arm_runner --start 2018-01-01 --end 2024-12-31 --window secondary
```

---

## 4. Parallel Research Track (Non-Blocking)

```bash
# Reverse-engineering multi-bagger factors (walk-forward + CPCV)
python -m research.reverse_engineer_multibaggers --train-window 2014-2024 --embargo-months 12 --max-empirical-weight 0.4
```

---

## 5. Debate & Evaluation Archive (For Reference)

All artifacts from B-20260910-001 preserved in `.agents/project/org/decisions/_drafts/`:
- `brief-B-20260910-001.md`
- `position-A-B-20260910-001.md`
- `position-B-B-20260910-001.md`
- `disagreement-map-B-20260910-001.md`
- `synthesis-B-20260910-001.md`
- `ceo-ruling-B-20260910-001-modified.md`

Data Scientist sanity check embedded in `Data_Strategy.md §13`.

---

*Updated 2026-09-10 — Pipeline Remedial Fixes & Rebuild Agenda Ready for Tomorrow's Build*