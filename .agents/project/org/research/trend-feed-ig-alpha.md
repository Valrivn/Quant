# IG vs Traditional Alpha Comparison — D-20260807-001 (Rx)

**Verdict artifact:** `trend-feed-ig-alpha.md` (Run 2 appended)
**Runner:** discovery.backtest_alpha (leaf) + per-name/pool backtest
**Date:** 2026-08-07 (UTC)
**Status:** research-only; no production contact; no commit

## Run 2 — per-name + combined pool (CEO ask)

Observed both lanes with the standard qual+quant gate, then ran each name
individually followed by the combined equal-weight pool. Prices live via
yfinance; FF5 factors + SP500 live. Window 2019-01-01..2026-07-31; FF5 alpha
is the trailing 252-trading-day residual (annualized), excess/IR vs SPY on the
same aligned window.

### STEP 1+2 — Gate pass-through

| lane | status | n_pass | reason |
|------|--------|--------|--------|
| IG | unfed | 0 | no IG feed on disk (video stub locked, no ig_* table) — never fabricated |
| traditional | no_pass | 0 | every name `qual:avoid;quant:no_alpha_data` |

Both lanes run the SAME gates (per D-20260807-001). The screen — not the
channel — is the binding constraint: 0/11 of the scraper cohort clears either
gate this run, matching the frozen P1 census recording.

### STEP 3 — per-name alpha (trailing 252d FF5 residual, annualized)

| ticker | FF5 alpha (ann) | alpha_t | excess vs SPY | IR |
|--------|------------------|---------|---------------|-----|
| AAPL | +37.5% | +1.69 | +17.4% | 0.85 |
| GOOGL | +42.2% | +1.55 | +13.2% | 0.59 |
| INTC | +11.6% | +0.17 | +6.9% | 0.16 |
| JPM | -17.0% | -0.67 | +7.5% | 0.36 |
| AMZN | -19.2% | -0.72 | +4.3% | 0.17 |
| MSFT | -24.3% | -0.84 | +8.7% | 0.46 |
| AVGO | -23.7% | -0.59 | +31.4% | 0.96 |
| META | -38.7% | -1.09 | +11.0% | 0.33 |
| AMD | -40.6% | -0.68 | +42.2% | 0.91 |
| NVDA | -30.7% | -1.15 | +50.3% | 1.27 |
| TSLA | -67.9% | -1.39 | +39.4% | 0.71 |

### STEP 4 — combined equal-weight pool

| measure | value |
|---------|-------|
| FF5 alpha (ann) | -14.2% (t = -0.95) |
| excess vs SPY | +21.1%/yr |
| information ratio | 1.39 |
| n_obs | 252 |

**Reading:** the pool beat SPY by ~21%/yr on the window, but that excess is
statistically weak (alpha t = -0.95, IR ~1.4 not significant) and the FF5
residual alpha is negative — the superior excess is mostly factor/beta/regime
exposure, not genuine stock-picking alpha.

### Honesty notes (Run 2)

- IG lane: **no result** — no IG feed exists on disk, so there is no IG
  cohort to backtest yet. No fabricated candidates, no fabricated alpha.
- Traditional gate: 0 pass-through (as in Run 1). Individual + pool backtests
  below are run on the raw scraper cohort so the CEO can compare whenever the
  gate unblocks; they are NOT pass-cohort results.
- Only AAPL/GOOGL are individually positive on FF5 alpha and neither is
  significant (|t|<2).

## Next steps (ranked)

1. Unblock the quant gate (`no_alpha_data`) — attach real alpha_3y_ann +
   fundamentals metrics so pass-through can clear. Single highest-leverage item.
2. Provide an IG-derived candidate list (P1 evidence / live feed) so the IG
   lane is seeded, then re-run: pass-cohort vs pass-cohort alpha comparison.
3. Re-run the same per-name + pool table on pass cohorts for both lanes; that
   is the direct answer to D-20260807-001.

## Artifact records

- discovery/backtest_alpha.py — compare_cohorts + run_each_alpha / report_each
  (offline-safe, injectable fetchers)
- tests/test_discovery_backtest_alpha.py — 9 offline tests
- Full suite: 1014 passed, 18 skipped before this run.

<hr>

## Original Run 1 (2026-08-07, recorded earlier)

`compare_lanes(ig_tickers=None, traditional_limit=20)` with live fetchers:
identical lane structure — IG `unfed`, traditional `no_pass` (0/10:
`qual:avoid;quant:no_alpha_data`). Consistent with P1 census 0/500 pass.
No alpha was fabricated in either lane. Not a blocker: the producing step is
the screen, not the IG channel.