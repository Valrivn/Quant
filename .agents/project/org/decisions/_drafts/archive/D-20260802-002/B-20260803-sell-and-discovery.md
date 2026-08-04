# Brief B-20260803 — Sell Algorithm + Discovery Algorithm (SP400/SP600)

**Author:** big-pickle (blueprint custodian), from the CEO's ruling on D-20260802.
**Status:** **APPROVED** (CEO, 2026-08-02) with hermes-bridge hybrid — regime-
dependent exit, continuous Glassdoor tilt, falsification-first sequencing,
plus PIT cashflow / re-entry cooldown / whipsaw metric / slippage sensitivity.
**Prerequisite context:** the D-20260802 deep review found the engine has zero
out-of-sample FF5 residual alpha, no exit logic, a 40/50 CIK/fundamentals gap,
in-sample selection, and a concentrated tech/semis momentum profile.

---

## PART 1 — SELL ALGORITHM (the missing exit layer)

The engine only re-ranks at monthly rebalance. There is no per-position exit.
The CEO's rule: **exit when a position rises ~15-20% above its highest range,
justified by cashflow and current macrotrends.**

### 1.1 Trigger — regime-dependent take-profit band above the high range
- `high_watermark(t) = max(adj_close over trailing 252 trading days)` per name
  (a rolling 1y range ceiling; configurable window).
- **Regime-dependent band (CEO APPROVED, Position B):**
  - Trending regime (narrow credit spreads, low sector-shock prob): band
    widened to **25-30%** or replaced by a **2.5x ATR trailing stop** from peak.
  - Choppy / regime-shift regime: band tightened to **10-12%** AND **both**
    cashflow + macro confirmation required to exit.
  - Regime from existing `CreditSpreadMonitor` + `SectorShockProbability`
    (sector-level; weekly smoothing to avoid whipsaw).
- Phase exits (recommended for vol): sell 50% at first threshold, remainder at
  the second. Optional companion: trailing stop-loss at -25% from
  high_watermark (protective, not a profit rule — CEO can accept or drop).
- **Ablation required:** regime-conditioned band validated against a
  fixed-band-only baseline in the OOS harness (separate pre-registered branch).
- All band/phase/weight parameters live in `config/weights*.yaml` (invariant 4),
  not hard-coded.

### 1.2 Confirmatory gates — justify the exit (never sell a winner on price alone)
1. **Cashflow leg** (from the XBRL quarterly datastore, `valuation_alpha/ratios.py`
   + `datastore/xbrl_financials.py`): operating cash flow / FCF trend, cash_burn_months,
   interest coverage, debt-to-capital. **POINT-IN-TIME (Position A):** facts
   filtered by `filed_date <= decision_date` (filing lag, not fiscal-end) — the
   "accelerating cashflow → HOLD override" is a lookahead backdoor otherwise.
   Override rate reported; if >30% of trigger-hits are overridden, fail the P3
   gate as suspected leakage.
2. **Macro leg** (existing modules): sector credit regime (`CreditSpreadMonitor`),
   real rates (DFII10), M2 YoY (gold valuation), sector shock probability
   (Bernoulli sector-shock data). Exit is *confirmed* when the sector's macro
   regime turns negative (spreads widening, real rates rising, elevated shock prob).

**Decision matrix (trigger hit = price at/above the regime band):**
| Regime | Cashflow gate | Macro gate | Action |
|---|---|---|---|
| choppy | worsening | negative | **SELL (full)** |
| choppy | worsening | neutral | **SELL (full)** |
| choppy | neutral | negative | **SELL (full)** |
| any | accelerating | neutral | SELL 50%, hold 50% |
| any | accelerating | positive | HOLD (override) |
| trending | — | — | hold to widened band / ATR stop |
| no trigger | — | — | HOLD |

### 1.3 Behavior & integration
- New module `valuation_alpha/exit.py` (class `SellAlgorithm`), a **daily overlay**
  on L1 holdings evaluated between monthly L1 rebalances; wired into the
  `portfolio/allocator.py` daily-weights path and `diversification/backtest.py`
  replay so it is backtestable.
- Exit proceeds roll to cash/short-bills; re-entry is gated (Position A):
  per-name `reentry_cooldown_days=60` AND macro gate neutral/positive before
  re-entry; discovery names must re-pass the quant baseline + Glassdoor tilt.
  Re-entry on a pullback of ≥10% from the sell price, or at the next monthly
  rebalance. **Whipsaw rate (re-entries per name/yr) is a P3 success metric.**

### 1.4 Validation (pre-registered, walk-forward OOS harness)
Extend the 2018-2026 monthly-rebalance OOS sim with the exit overlay. Success:
- Sharpe improves ≥ 0.15 vs no-exit baseline; maxDD reduced ≥ 10%;
- OOS excess return not reduced by more than 20%;
- confirmed-exit hit rate ≥ 60% (exits that avoid a subsequent ≥10% pullback);
- FF5 residual alpha of the exit-augmented portfolio not worse than baseline;
- band-only vs regime-conditioned ablation reported;
- SP600 slippage sensitivity at **1% AND 2%** (Position A).

---

## PART 2 — DISCOVERY ALGORITHM (mid/small-cap universe expansion)

Current universe is 50 tech/control names. The CEO wants growth discovery from
**S&P MidCap 400** (market cap ~$1.8B-$35.5B, median ~$7.5B) and **S&P SmallCap
600** (~$0.7B-$3.2B), that pass the quantitative baseline; since many are
non-tech and cannot be screened by the tech-centric quant stack alone, use
**Glassdoor** as the qualitative gate.

### 2.0 Prerequisite — CIK resolver (fixes the core data gap)
- New `valuation_alpha/universe/cik_resolver.py`: fetch SEC EDGAR
  `company_tickers.json` (public, no auth) on a schedule → ticker→CIK map for
  ~1000+ names; fallback `company_tickers_exchange.json`.
- Persist in `db/` (WAL) + cache companyfacts at ≤10 req/s, incremental.
- **This also fixes the existing 40/50 CIK gap** (only megacaps are mapped today,
  `valuation_alpha/universe/roster.py:44-49`), unblocking the lifecycle/
  fundamentals legs for the whole universe.

### 2.1 Screen 1 — quantitative baseline (reuse L1)
- Run the existing L1 engine metrics on the new universe: FF5 residual alpha
  (1y/3y), excess vs SP500, lifecycle/Markov, peer percentiles, mahalanobis.
- Small-cap weighting: raise the stochastic risk leg — Bernoulli shock filter,
  cash_burn_months, debt-to-capital, interest coverage. **Hard exclusions**:
  cash_burn < 12 months, interest coverage < 1x, mahalanobis beyond a distress
  threshold.
- Liquidity gate (reuse `LiquidityGatekeeper` logic): ADV, min price, no OTC.
  Small caps → backtest slippage raised to ~1%.

### 2.2 Screen 2 — Glassdoor qualitative tilt (the CEO's non-tech filter)
- Use the existing stack: `GlassdoorScraper` + `company_resolver` (glassdoor_slug)
  + `employer_translator` + `cross_validation.validate_layer1_glassdoor_comparably`
  (Glassdoor 0.40 / Indeed 0.30 / Comparably 0.30 weights).
- **Continuous tilt, not a gate (CEO APPROVED, Position B):** Glassdoor composite
  enters L1 ranking as **+0.1 z-score per 0.1 above median** of the discovery
  universe — no name is hard-excluded for low coverage (<40% of SP600 has ≥50
  reviews, so a hard floor would become a large-cap selection filter that
  defeats discovery). Tilt weight in `config/weights*.yaml`.
- Cost control: only fetch Glassdoor for names that pass screens 1 + 3.

### 2.3 Screen 3 — sector classification
- Expand the 6-sector map to full GICS (industrials, materials, REITs, utilities,
  health-care services, etc.) using `Quantitative/company_classifier.py`.
  Discovery names default `bias=False`.

### 2.4 Integration & governance
- Discovery output = expanded candidate pool feeding the SAME L1 ranking → L2
  sleeves → L3 allocator, with the Part-1 exit overlay on the equity leg.
- **Discovery is a screen, never an automatic add.** Every new name entering the
  top-k pool is logged with provenance (CIK source, screens passed, glassdoor
  score, liquidity) — extend `Quantitative/audit/data_provenance_audit.py`.

### 2.5 Validation (pre-registered)
- Same OOS harness on the SP400/SP600 pool + exit overlay. Success:
  OOS excess vs SP500 ≥ +3%/yr; FF5 residual alpha t>2 (or bootstrap CI excluding
  zero); Sharpe ≥ 0.8; small-cap liquidity stress test at 1% slippage.

---

## PART 3 — Falsification-first (council prerequisite, CEO APPROVED)

**Sequencing (Position B, CEO-approved):** the falsification test runs FIRST,
before any SP400/SP600 pilot. Discovery must stand on its own pre-registered
validation (2.5), not inherit credibility from the old universe. Re-run the OOS
sim with **expanding-window alpha (purge-and-embargo, no lookahead)** on the
current 50-name universe. If OOS excess collapses to ≤1% and FF5 alpha stays
zero, the discovery build must be validated standalone. Then run a
**market-cap-stratified 100-name pilot (SP400 vs SP600)** with explicit
survivorship correction (historical index constituents where available).

---

## PART 4 — Phases & gates

| Phase | Deliverable | Gate |
|---|---|---|
| P1 | **CIK resolver + SEC cache + SP400/SP600 universe loader (standalone)** | unit tests + provenance audit |
| P2 | Falsification test (Part 3) FIRST → then quant baseline + liquidity + Glassdoor tilt on a stratified 100-name pilot (SP400 vs SP600, survivorship-corrected) | pilot report |
| P3 | Sell algorithm (`valuation_alpha/exit.py`, regime-dependent) + OOS harness extension (incl. ablation, PIT, whipsaw, slippage 1%/2%) | pre-registered exit metrics |
| P4 | Full pre-registered validation | conductor ≥90%, zero new failures |
| P5 | Wire into pipeline.py + dashboard tab + blueprint + decision record | T3 gate → CEO ruling |

**Sequencing note (CEO APPROVED):** falsification (P2 head) gates the discovery
pilot; P1 runs standalone regardless. **Effort estimate:** P1 ≈ week 1, P2
(falsification + pilot) ≈ week 1-2, P3-P4 ≈ week 2-3, P5 ≈ week 3.
