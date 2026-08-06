# Backtest Accuracy Implementation Plan (B-20260804-002)

CEO-ruled Tier 3 (full debate after this plan). Fixes the 7 documented
degradations plus the deeper point-in-time (PIT) gaps the 2026 research surfaced.
Grounding sources: SiftingIO (corporate-action math), AlphaEdge/StockFit (PIT
IC evidence, delisting handling), Alpha Learning (timestamp tripwires), Tiingo,
OpenBB, edgartools, fja05680/sp500, fredapi/ALFRED.

## 0. Tier-3 debate record + CEO ruling (2026-08-04)

Debate artifacts: `_drafts/position-A-B-20260804-002.md`,
`position-B-B-20260804-002.md`, `disagreement-map-B-20260804-002.md`,
`synthesis-B-20260804-002.md` (S-20260804-002). Incorporated here in depth so
the CEO can review the full debate in one place.

**Position A (big-pickle) — approve the full 7-phase rebuild as written.**
- Verified in code: static `DIVIDEND_YIELDS` (sleeves.py:52) accrues
  shares*p*y/252 daily (fee_sim3.py:294) - a 2026-dated yield map cannot
  truthfully price 2018-2026 dividend income; dual raw/adjusted series with real
  ex-date events (P2) is the only honest execution math, and it kills the silent
  same-day-fill look-ahead too.
- Verified: `_nasdaq_crosscheck` (fee_sim3.py:677) runs in `main()` only, not
  `run_sim_discovery()` (fee_sim3.py:823); routing it through a shared
  `validate_prices()` gate hit by EVERY runner plus the S1 DEGRADED ledger makes
  fallbacks visible, satisfying the Provenance-everywhere invariant the current
  silent NA/price-proxy path violates.
- Build order is right: P1-P6 fix inputs, P7 re-runs identical strategy code, so
  any % delta is attributable to data alone; S5 then honestly answers whether
  B-20260804-001's directional read (incl. 72/102 bear) survives, and the
  auditor no-hardcoding gate keeps the re-run from being gamed into a pass.
- RISKS: new API surfaces (fredapi/ALFRED, edgartools, Tiingo/Stooq/OpenBB)
  carry key/rate/blocking risk (pinned parquet cache + checksums mitigate); a
  large P7 delta creates pressure to "correct" results - only the pre-registered
  success bars + conductor gate hold the line.
- BLIND-SPOTS: did not verify today's FRED/Tiingo key availability or quotas;
  did not run edgartools/fredapi locally; did not audit full run_sim_discovery
  internals beyond the crosscheck gap; did not test sqlite/parquet cache on
  Windows path-length limits.

**Position B (gemini-planner) — reject 7-phase as scope-creep; minimal 3-phase
patch (#1 FRED, #4 empty basket, #3 cross-check wiring only).**
- 100% PIT accuracy is an infinite data-integrity sink that delays the critical
  return-max decision while chasing diminishing returns on a Discovery-level
  backtest.
- S1 ("zero silent degradations") is practically unfalsifiable without defining
  exhaustive error boundaries across 4+ external APIs.
- A full rebuild relies heavily on OpenBB/FRED/EDGAR availability, risking
  immediate blockers if API keys or rate limits fail; targeted patches minimize
  external dependency surface.
- RISKS: the 3-phase patch might leave minor forward-looking biases in secondary
  signals; the delta-vs-degraded report may underestimate survivorship impact if
  the full universe rebuild is skipped.
- BLIND-SPOTS: did not verify the CEO possesses valid rate-limit-free keys for
  Tiingo/EDGAR/FRED; did not review how deeply embedded the degraded logic is in
  fee_sim3.py.

**Disagreement map (big-pickle).**
- DISAGREE-ON: scope (7-phase vs 3-phase); S1 falsifiability; external-dependency
  risk; survivorship/universe rebuild; decision-delay (correctness-first vs
  unblock-the-decision-first).
- CONSENSUS-ON: #1 (FRED) and #4 (basket-empty) are the materially-moving
  degradations; #3 (cross-check wiring) must be fixed; data-layer only; zero
  strategy-logic changes; re-run reports deltas honestly; external key
  availability is a shared risk.
- RESOLUTION-PATH: define S1 operationally (explicit source list + DEGRADED
  boundaries) before ruling; probe FRED/EDGAR/Tiingo key availability + quotas
  now to decide scope feasibility; test whether the 72/102-bear read and the
  buy-more thesis can be trusted on a 3-phase patch alone - if not, full rebuild
  is the floor.

**hermes synthesis (S-20260804-002) — hybrid.**
- RECOMMENDATION: execute Position A's full 7-phase rebuild, gated on a
  pre-build API probe (FRED/Tiingo/EDGAR keys + quotas verified within 1 hour);
  if any key is blocked, fall back to Position B's 3-phase patch for THAT source
  only, with a DEGRADED ledger entry, and proceed - never halt the whole build
  for one missing API.
- SUBSTANTIVE disagreements: scope (B's patch leaves survivorship + look-ahead
  intact, failing S1/S5 and making the delta report unreliable); external-API
  dependency risk (shared blind spot; the single decision-gate). STYLISTIC: S1
  falsifiability framing (both agree on the operational fix).
- RATIONALE: the CEO's stated requirement is full accuracy with all six success
  criteria pre-registered; Position A is the only path that honestly answers
  whether the buy-more thesis survives corrected data. The API-probe gate
  converts B's blocking risk into a scoped fallback rather than a veto.
- RISK: API key failure on FRED or Tiingo blocks P2 and delays the rebuild by
  1-3 days; DEGRADED-ledger fallback must be pre-wired before build starts.

**CEO RULING: APPROVE hybrid (D-20260804-002).** Execute the full 7-phase
data-layer rebuild (P1-P7) gated on a <=1-hour API probe before P2; per-source
DEGRADED fallback if a key is unavailable (never full stop); then Discovery
re-run with % deltas and re-evaluated success bars. Build order below is the
execution contract.

## 1. The 7 degradations -> fixes

| # | Problem | Fix | Tool/library |
|---|---------|-----|-------------|
| 1 | FRED unreachable; macro used HYG/LQD price-proxy; 72/102 bear | Free FRED API key; `fredapi` with ALFRED vintages (first-print not final-revised); explicit source tag per decision; keep price-proxy ONLY as tagged DEGRADED fallback | `fredapi`, `openbb-fred`, ALFRED REST |
| 2 | SEC EDGAR/CIK unreachable; XBRL cross-check = NA | `edgartools` (rate-limit aware, caching, `Company().get_facts().as_of(date)` PIT); SEC companyfacts bulk JSON w/ proper User-Agent, disk-cached to parquet | `edgartools` (MIT), SEC data.sec.gov |
| 3 | Nasdaq cross-check only in `main()`, not discovery path | Wire `_nasdaq_crosscheck()` into `run_sim_discovery()` via a shared `validate_prices()` gate called by EVERY runner | existing `fetch_nasdaq` + Tiingo/Stooq |
| 4 | Basket empty 2018-2020 (5y audit window) | Candidate history from Tiingo (1962+) / SEC XBRL dividend facts so the audit window is populated from 2018; PIT S&P500 constituents incl. delisted tickers | Tiingo EOD, `fja05680/sp500` |
| 5 | yfinance 429 rate-limit | Pinned disk cache (parquet + checksum + data-version stamp); bulk one-time ingestion; retry/backoff; multi-provider fallback so a single 429 never degrades a run | Tiingo/Stooq/OpenBB behind one interface |
| 6 | Static `DIVIDEND_YIELDS` map | Real ex-date dividend events per ticker (amount + ex-date + record date) accruing at ex-date into cash; split factors applied to share counts | yfinance dividends + Tiingo corporate-actions feed |
| 7 | Stale baseline-test-results.json | Auto-refresh baseline as part of the conductor gate | pytest + gate script |

## 2. Deeper PIT/accuracy architecture (the real "100%")

1. **Dual price series** (SiftingIO rule): returns/signals/vol on split+dividend-
   ADJUSTED series; execution/position-sizing on UNADJUSTED bars with explicit
   corporate actions (split -> multiply shares; dividend -> credit cash at
   ex-date; adjusted volume = raw/split-factor). Keep factor series for audit.
   Add the **AAPL Aug-2020 split test** + ex-date drop tests as CI trips.
2. **PIT join keys**: fundamentals keyed by `dateFiled`/accession, never fiscal
   period-end (AlphaEdge: naive IC 5.38% vs PIT 2.88%). FRED rows carry
   `available_from`; macro reads only `available_from <= t`. Moat/cashflow gate
   (valuation_alpha/moat_gate.py) gets filing-date, not as-of, data.
3. **Survivorship**: universes built from PIT membership + delisted/renamed
   tickers retained; force-liquidate a holding missing >10 trading days at a
   write-off price (AlphaEdge BUG-002 pattern).
4. **Fill timing**: decisions computed on close N fill at open N+1 (no
   same-close fills - current sim's silent look-ahead).
5. **Traceability gate**: every run writes a data-status ledger (per-source:
   FRED, EDGAR, yfinance, Tiingo/Stooq, Nasdaq, dividend feed) into the results
   meta/registry. Any fallback marks the run DEGRADED. Violates "Provenance
   everywhere" if a degradation is ever silent again.
6. **Tripwires** (Alpha Learning): forward-fill audit on reference fields;
   time-shift sensitivity check; distributional sanity gates on ingest.

## 3. GitHub / data tools to use

- **edgartools** (dgunning/edgartools, MIT) - SEC EDGAR + XBRL, PIT `as_of`,
  rate-limit + cache built-in. Fixes #2 and PIT fundamentals.
- **fja05680/sp500** (and chinobing mirror) - PIT S&P500 constituents since
  1996 incl. delisted/changed symbols (AAL-199702, AAMRQ, ...). Fixes universe
  survivorship.
- **OpenBB Platform** (openbb-fred, openbb-sec, openbb-tiingo, openbb-yfinance)
  - ONE interface, per-endpoint provider priority lists, `--adjustment
  {splits_and_dividends,unadjusted,splits_only}`. Fixes multi-source fallback
  and dual-series cleanly.
- **Tiingo** (free EOD key; 1962+, error-checked, survivorship-adjusted, raw +
  adjusted, dedicated Splits and Distribution corporate-action feeds). Fixes
  #4/#5/#6 and adds a 2nd/3rd price source.
- **Stooq** (free CSV, no key) - zero-cost second feed for cross-check.
- **fredapi** + ALFRED vintages (free key) - fixes #1 with PIT macro.
- **pandas/pyarrow/parquet** + sqlite WAL (db/ layer) - pinned reproducible cache.

## 4. Build order (data layer only; NO strategy-logic changes)

1. P1 Data layer: multi-provider price fetch (yfinance + Tiingo/Stooq + Nasdaq)
   behind one `fetch_all` interface; dual raw/adjusted columns + factor series.
2. P2 Corporate actions: dividends/splits ingestion; replace static yield map;
   split/ex-date CI tests.
3. P3 Macro PIT: fredapi + ALFRED; source-tagged state; DEGRADED ledger.
4. P4 EDGAR PIT: edgartools dividend/moat facts keyed to filing dates.
5. P5 Universe: PIT S&P500 constituents + delisted retention; basket populated
   from 2018.
6. P6 Fill timing: decision N -> fill N+1; tripwire + look-ahead audit tests.
7. P7 Discovery re-run: all 8 strategies; % delta vs degraded; success bars
   re-evaluated; S1-S6 verified; conductor gate + baseline refresh.

## 5. Expected honesty check

Corrected data will NOT simply improve numbers: the 72/102 "bear" read may flip
to fewer bear months, the buy-more basket becomes live from 2018, and fees get
real dividend events. The re-run's job is to REPORT the delta, not to make the
pivot pass. If the pivot still fails T1 on accurate data, that is the answer.
