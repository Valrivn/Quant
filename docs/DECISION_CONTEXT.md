# Objective Decision Context — 7 Architectural Rulings Required
**Date:** 2026-10-06 | **Author:** Orchestrator (Nemotron Brain) | **Evidence Sources:** Direct code inspection

---

## DECISION 1: Config Namespace — Where Should Arm A Refactor Parameters Live?

### Objective Evidence

| Source | Finding |
|--------|---------|
| `config/pre_registration_v7.yaml` (844 lines) | **FROZEN & APPROVED** — Contains the canonical Phase 9 v7 contract. SHA-256 `c80a47bf6ae8d1c595f3b5e607c2588478bec0c47c4f0f97eb6c374b277a1685`. Pinned at `ingestion/harness/prereg.py:128`. **844 lines with massive duplication** — the `statistical_design` and `UNIFIED REGIME ARCHITECTURE` blocks are copy-pasted **3 times** (lines 69-128, 130-164, 191-225), likely from a bad merge. |
| `config/arm_a_refactor_prereg.yaml` (238 lines) | **DRAFT_AWAITING_CEO_APPROVAL** — Contains vol targeting, composite reweight, turnover controls, graded de-risking, tail hedge, cost model, and `factor_data_contracts` with explicit gap analysis. Explicitly states: "This file ADDS the Arm A refactor parameter set... It does NOT supersede, modify, re-pin or replace v7." |
| `ingestion/harness/prereg.py` | The pin verification reads `pre_registration_v7.yaml` by path. Any namespaced change to v7 must update the pin hash. |
| CEO Decision 2026-10-04 (Audit Ledger) | Recommendation was: **"extend_v7"** — extend v7 with arm_a namespace. |

### Trade-offs

| Option | Pros | Cons |
|--------|------|------|
| **A: Extend v7 with `arm_a:` namespace** | Single frozen artifact; v7 SHA-256 remains the sole pin; factor_data_contracts audit stays attached to the canonical pre-reg; aligns with CEO 2026-10-04 recommendation | Must re-pin SHA-256 (changes the digest); 844-line file already has duplication that must be cleaned first; Arm A plan is still DRAFT so it contaminates an APPROVED file's status |
| **B: Keep separate `arm_a_refactor_prereg.yaml`** | Clean DRAFT/APPROVED separation; v7 digest untouched and verified; refactor can be iterated without touching frozen artifact | Two pre-reg files to track; risk of drift between them; CLI consumers must know which file owns which param |

### Orchestrator Recommendation
**Option B for now, with a documented migration path.** The v7 file is APPROVED and its digest is load-bearing (verified by prereg.py, provenance_audit.py, phase9d_emit.py). Arm A is DRAFT — mixing a DRAFT parameter set into an APPROVED file obscures provenance. On CEO approval of Arm A, the arm_a params should be ratified into v8 (a new versioned file `pre_registration_v8.yaml`), not mutate v7 in place. This preserves the audit trail.

---

## DECISION 2: Embargo Authority — Single Source of Truth?

### Objective Evidence

| Source | Finding |
|--------|---------|
| `regime/embargo.py` (78 lines) | `ReEntryEmbargo` is a **full in-memory state machine**: `state` ('inactive'/'active'/'cooling_down'), `days_in_state`, `confirmation_date`. `update()` increments counters. |
| `regime/regime_state.py` (398 lines) | `RegimeState` is a **persisted mirror** with its own `embargo_state`, `embargo_days_remaining`, `days_since_confirmation`. `advance_day()` and `clear_event()` mutate these. `sync_from_embargo()` copies from the embargo instance. |
| `regime/regime_state.py:45-60` (docstring) | **Explicitly documents the dual-truth problem:** "Two copies of a state machine is two answers to 'where are we in the cooldown?'" The rule adopted: RegimeState = persisted mirror/reporting source; ReEntryEmbargo = transition logic. |
| `regime/embargo.py:51-57` (BUG) | `self.days_in_state += 1` happens **every day while active**, then `if self.days_in_state >= self.max_days: self.state = 'cooling_down'`. But in cooling_down (line 60), `self.days_in_state += 1` again — so the counter is not reset on the active→cooling transition. The min/max comparison mixes state durations. |
| `regime/embargo.py:72` (BUG) | `days_remaining = max(0, self.min_days - self.days_in_state)` — uses `days_in_state` which counts across both 'active' and 'cooling_down' phases, so `days_remaining` is wrong during the cooling phase. |

### Trade-offs

| Option | Pros | Cons |
|--------|------|------|
| **A: RegimeState fully authoritative; Delete ReEntryEmbargo class** | Eliminates dual-truth bug; single state machine; position-level embargo in PositionState; matches CEO 2026-10-04 recommendation | Requires rewriting embargo logic into RegimeState; PositionState.parquet needs an embargo_remaining column; must update three_arm_runner integration |
| **B: Keep dual truth (current)** | No immediate code change | Two answers to "where are we in cooldown"; restart loses ReEntryEmbargo in-memory state; confirmed BUG in counter logic means embargo timing is incorrect |

### Orchestrator Recommendation
**Option A.** The current dual-truth architecture has two confirmed bugs (counter not reset on transition, days_remaining computed from wrong counter). `RegimeState` already owns persistence (atomic JSON writes, survives restart). Move the per-asset embargo into `PositionState` (which already persists to Parquet), and let `RegimeState` own the portfolio-level embargo state machine. Delete `ReEntryEmbargo` class entirely. Fix the counter-reset bug by construction.

---

## DECISION 3: Damodaran Data Refresh — Automated vs Manual?

### Objective Evidence

| Source | Finding |
|--------|---------|
| `config/industry_beta.yaml` (527 lines) | Already populated with 60+ industries, unlevered betas, and `default_ratings.tiers` (14 ICR→rating→spread tiers). Has `update.source` field pointing to `https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/ratings.htm` and `update.refresh_cadence: monthly`, `update.last_updated: '2026-09-01'`. Has `combined_checksum` field. |
| `config/damodaran_erp.yaml` (15 lines) | `default_erp: 0.050`, `historical_erp` 2018-2026. Manually populated. No checksum field. |
| `ingestion/wacc_engine_v2.py` | Consumes both configs. `DamodaranSectorBeta._load()` reads `config/industry_beta.yaml`. `SyntheticRatingTable` reads `config/industry_beta.yaml:default_ratings`. Both have `candidate_path` fields for staging. |
| No `scripts/refresh_damodaran.py` | Referenced in comments but **does not exist** in the repo. No automation. |
| `wacc_engine_v2.py:105` | `SECTOR_BETA_CFG = "config/sector_beta.yaml"` — points to a different file than `industry_beta.yaml`. Possible mismatch. |

### Trade-offs

| Option | Pros | Cons |
|--------|------|------|
| **A: Monthly automated fetch (cron/scheduled)** | Always fresh; no manual step; matches `refresh_cadence: monthly` in config | NYU Stern URL may block automated requests (User-Agent needed); checksum validation adds complexity; silent staleness if fetch fails and no one checks |
| **B: Manual trigger via `scripts/refresh_damodaran.py`** | Human-in-the-loop audit; checksum verification before commit; no silent failures; auditable | Requires someone to run it; can go stale if neglected; script doesn't exist yet and must be written |

### Orchestrator Recommendation
**Option B, with a quarterly reminder.** The configs already have `candidate_path` staging fields and checksum fields — the design intent is manual-verified refresh. Automated fetch from an academic site is fragile and the checksums need human validation. Write `scripts/refresh_damodaran.py` that: (1) downloads to candidate file, (2) computes SHA-256, (3) diffs against current, (4) writes only on explicit `--apply` flag, (5) updates `last_updated` and checksum. Run quarterly or when Damodaran publishes new annual data.

---

## DECISION 4: Valuation Z-Score — Which Methodology?

### Objective Evidence

| Source | Finding |
|--------|---------|
| User spec (ARCHITECTURAL_CONTEXT.md) | Gate 1: "Z_valuation ≤ -1.0 or fall below the 10th percentile floor of its 10,000-pass Monte Carlo DCF distribution" |
| `ingestion/wacc_engine_v2.py:709-714` | `iv_nowcast = analytical_duration_adjustment(float(iv_base), float(d_wacc_val), float(wacc) - wacc_baseline)` with a `0.01` floor. This produces a single-point IV nowcast, NOT a Monte Carlo distribution. |
| `ingestion/factor_computer.py` | Computes ROIC, WACC, ICR, FCF_Yield, FCF_Margin — but **no valuation z-score, no Monte Carlo DCF, no price-to-IV distribution**. |
| `config/arm_a_refactor_prereg.yaml:152-238` (`factor_data_contracts`) | `eva_spread` partial, `ocf_yield_revision` NOT computable (missing market cap, EPS estimates), `dividend_carry_coverage` partial (missing share count). **No valuation_zscore factor declared at all.** |
| `Quantitative/iv_nowcast.py` | Referenced by wacc_engine_v2. Contains `analytical_duration_adjustment` — a deterministic formula, not Monte Carlo. |

### Trade-offs

| Option | Pros | Cons |
|--------|------|------|
| **A: Trailing 5-yr Monte Carlo DCF mean/std** | Matches spec exactly; 5-yr captures full cycle; Monte Carlo gives distribution not point estimate | Requires building a 10k-pass Monte Carlo DCF engine (doesn't exist); computationally heavy; 5-yr lookback may be too short for some regimes |
| **B: Historical P/E or EV/EBITDA percentile** | Simpler; data available in factor_computer; fast | Different metric than spec; P/E distorted by cyclicality; not a z-score vs DCF IV; would deviate from pre-reg |

### Orchestrator Recommendation
**Option A, but build it minimally.** The spec is explicit: "Z ≤ -1.0 vs trailing 5-year valuation mean OR below 10th percentile of 10,000-pass Monte Carlo DCF." The honest implementation is a Monte Carlo DCF using the existing `wacc_engine_v2` outputs (WACC_v2, IV_baseline, duration) as inputs, with 10,000 passes perturbing WACC, terminal growth, and revenue growth. Store the distribution, compute z-score and P10. This must be built as a new module `valuation/monte_carlo_dcf.py`. Do NOT substitute P/E percentile and call it the pre-registered factor.

---

## DECISION 5: EVA Probability = 100% — How Computed?

### Objective Evidence

| Source | Finding |
|--------|---------|
| User spec | Gate 2: "EVA Prob = 100%" — "its downstream Monte Carlo Economic Value Added probability must be 100%" |
| `ingestion/factor_computer.py:364-381` | Computes ROIC = NOPAT / Capital_Base. NOPAT = (EBIT + R&D_Amort) × (1 - τ). This gives a point ROIC, not an EVA distribution. |
| `ingestion/wacc_engine_v2.py` | Computes WACC_v2, ke_v2, kd_pre_tax_v2. No EVA computation. |
| `Quantitative/iv_nowcast.py` | Analytical duration adjustment only. No EVA probability. |
| Monte Carlo infrastructure | **Does not exist in the codebase.** The `factor_data_contracts` acknowledge 45% of the directional alpha block is NOT computable due to missing data (ocf_yield_revision, eva_spread needs sector map, dividend_carry_coverage needs share count). |

### Trade-offs

| Option | Pros | Cons |
|--------|------|------|
| **A: Full 10k-pass Monte Carlo, require EVA > 0 in all paths** | Binary 100% is unambiguous; matches spec literally | Computationally heavy; requires Monte Carlo engine; 100% is extremely strict — almost no real firm passes; may make the gate permanently blocking |
| **B: Analytical approximation** | Fast; gives a probability; can threshold at 95% or 99% if 100% is too strict | Deviates from spec's literal "100%"; "approximation" of a Monte Carlo probability is epistemologically weaker |

### Orchestrator Recommendation
**Option A with a documented relaxation valve.** Implement the full 10,000-pass Monte Carlo as specified. However, add a **documented diagnostic threshold**: log the actual EVA probability (e.g., 98.7%) alongside the binary 100% pass/fail. If the 100% gate blocks ALL assets for 3+ consecutive months, escalate to CEO to explicitly relax to a pre-registered 99% threshold — do NOT silently lower it. The spec's "100%" is the frozen contract; any relaxation is a CEO ruling.

---

## DECISION 6: Defensive Assets — Add Gold (GLDM)?

### Objective Evidence

| Source | Finding |
|--------|---------|
| User spec | "It determines total equity exposure versus defensive buffers (Treasury bills like BIL/SHY and gold GLDM)" — explicitly names GLDM |
| `config/arm_a_refactor_prereg.yaml:55` | `cash_instruments: ["BIL", "SHY"]` — NO GLDM |
| `config/arm_a_refactor_prereg.yaml:117` | `cash_allocation: ["BIL", "SHY"]` in graded_derisking |
| `regime/derisking.py:26` | `cash_instruments` defaults to `["BIL", "SHY"]` |
| `regime/regime_state.py:118` | `cash_instruments: List[str] = field(default_factory=lambda: ["BIL", "SHY"])` |
| `portfolio/allocator.py:316` | `apply_vol_targeting(..., cash_instruments: List[str] = ["BIL", "SHY"])` |
| Price data for GLDM | No GLDM in price cache or universe. Would need to add GLDM to `data/master_prices/` and to any cash-instrument return series used in backtests. |

### Trade-offs

| Option | Pros | Cons |
|--------|------|------|
| **A: Add GLDM → `["BIL", "SHY", "GLDM"]`** | Matches spec; gold adds inflation/crisis diversification beyond duration; 3-way split reduces single-instrument risk | Requires GLDM price data (not in universe); adds a return series to every cash-allocation calc; backtests currently run on 2 instruments so adding a 3rd changes historical attribution; more instruments = more data quality risk |
| **B: Keep BIL/SHY only** | No data changes; backtests remain comparable to Sprint 9 results; simpler attribution | Deviates from spec's explicit "and gold GLDM" mention; no inflation hedge in defensive sleeve |

### Orchestrator Recommendation
**Option A, with data guard.** The spec explicitly names GLDM. Add it, but: (1) verify GLDM price data exists in `data/master_prices/` before enabling; (2) if absent, fetch from the price cache pipeline and add to the universe; (3) update the defensive split to equal-weight 3-way (BIL/SHY/GLDM) for de-risking; (4) re-run the 3-arm backtest and compare to Sprint 9 baseline (Arm A: -1.24%/-0.13) — GLDM should improve the MaxDD during 2020/2022 if the defensive sleeve is active. If GLDM data is unavailable, fall back to Option B and document.

---

## DECISION 7: Rebalance Frequency — Quarterly vs Monthly?

### Objective Evidence

| Source | Finding |
|--------|---------|
| `config/arm_a_refactor_prereg.yaml:86` | `calendar_rebalance: "quarterly"` |
| `ingestion/harness/three_arm_runner.py:29` | "Rebalance: day 1 of each month, using data dated <= prior month-end (PIT)." — **Currently MONTHLY** |
| `config/arm_a_refactor_prereg.yaml:57` | `turnover_controls.max_monthly_turnover: 0.10` — references monthly cap |
| `ingestion/harness/three_arm_runner.py:90` | `MAX_TURNOVER = 0.30` — 30% one-way per rebalance, no frequency specified |
| Arm A plan (Tomorrow.md §9.2) | "Execution Controls: <10%/mo turnover, 15% hysteresis deadband, 0.8z swap gap, quarterly calendar, 60-day dwell" |
| `market_event_detection.graded_derisking` | Day 3/5/10 steps — these fire on calendar days after trigger confirmation, independent of rebalance frequency. A monthly rebalance means de-risking may not execute until the next month-start. |

### Trade-offs

| Option | Pros | Cons |
|--------|------|------|
| **A: Quarterly (4/yr)** | Matches arm_a_refactor_prereg; lower cost drag (~8-16 bps/yr vs 24-48 bps/month×12); 60-day dwell rule aligns with quarterly; 15% hysteresis deadband filters noise | Slower reaction to regime shifts; de-risking steps (Day 3/5/10) may not execute until next quarter-start — contradicts the aggressive 3/5/10 de-risking spec; CRISIS could persist through a quarterly boundary |
| **B: Monthly (12/yr)** | More responsive; de-risking steps can execute; matches current three_arm_runner behavior; tighter alignment between trigger confirmation and portfolio adjustment | Higher turnover drag; 60-day dwell = ~2 months, so effectively bi-monthly hold; more frequent trading = more slippage; cost model estimates 8-16 bps/yr assume 4-8 trades/yr which is closer to quarterly |

### Orchestrator Recommendation
**This is a genuine tension. Recommend Option A (Quarterly) for the structural rebalance, but with a documented EXCEPTION for market-event de-risking.** The graded de-risking steps (Day 3/5/10) must be able to execute intra-quarter when a regime trigger is confirmed. Solution: Quarterly for signal-driven rebalancing, PLUS an event-driven exception that overrides the quarterly calendar when `market_event_triggered` is true. This means: regular quarter-start rebalance, BUT immediate de-risking execution on trigger confirmation. Document this as the frozen policy and update `arm_a_refactor_prereg.yaml:calendar_rebalance` to `"quarterly_with_event_override"`.

---

## SUMMARY TABLE — FINAL CEO RULINGS (2026-10-06)

| # | Decision | Final Ruling |
|---|----------|--------------|
| 1 | Config namespace | **B — Keep `arm_a_refactor_prereg.yaml` separate as DRAFT; migrate to `pre_registration_v8.yaml` on approval. v7 pin stays load-bearing.** |
| 2 | Embargo authority | **Hierarchical: RegimeState = macro clock + global gate; PositionState = per-asset `embargo_days_remaining` (default 60d), decremented by RegimeState; ReEntryEmbargo class DELETED; re-entry needs BOTH gate open AND timer==0, unless high-bar override (Z_val≤-1.0, ICR≥5.0).** |
| 3 | Damodaran refresh | **Automated dynamic download every 2 months, version-stamped + checksum + logged.** |
| 4 | Valuation Z-score | **Monte Carlo DCF, macro-FRED-driven (credit spread Δ, inflation, 10Y rate, ERP perturbations).** |
| 5 | EVA probability | **Modal-path EVA via KDE over 10k MC paths; gate passes if modal EVA>0 AND P(EVA>0)≥0.99 AND ROIC−WACC≥2%. MC seed logged for bit-identical reproducibility.** |
| 6 | GLDM | **Add GLDM; source daily adj closes mid-2018→present via Yahoo/AlphaVantage to `data/master_prices/GLDM.parquet`; pre-2018 splice from GOLDPMGBD228NLBM or IAU net of 0.10% ER.** |
| 7 | Rebalance | **Quarterly structural; emergency override FIRED BY hybrid RegimeState (BOCPD P(r_t=0)>0.85 OR spread blowout >0.5bps/day), GATED by valuation agreement check — suppressed when portfolio in deep-discount moats (Z_val≤-1.0 AND ICR≥5.0), proceeds when rich (Z_val>0 or ICR<3.0).** |

**All rulings logged to Audit Ledger (2026-10-06). Ready to execute parallel build.**

**Please rule on each (A/B/C), and I will log your decision, dispatch parallel implementation, and run `/sleep` to snapshot.**