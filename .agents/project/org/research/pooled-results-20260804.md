# Pooled Results — Every Historical Test on the Corrected Data Layer (D-20260804-002)

Date: 2026-08-04. Re-ran `run_sim()` (P1/P2), `run_sim_phase3()` (P3), and
`run_sim_discovery()` on the corrected data layer (real ex-date dividend events
instead of the static `DIVIDEND_YIELDS` daily accrual) and pooled all results.
Authoritative command: `python -m diversification.fee_sim3 pool`.

## 1. DATA CHALLENGES REPORT (degradations explicit — S1, never silent)

| metric | status | detail |
|---|---|---|
| FRED macro (BAA10Y, gold) | DEGRADED | FRED unreachable this run; HYG/LQD price-proxy fallback. No ALFRED vintages (fredapi not installed) — macro state is not PIT. |
| SEC EDGAR (2nd dividend source) | DEGRADED | EDGAR reachable earlier in session (HTTP 200) but the probe timed out this run; XBRL cross-check skipped (CIK resolution unavailable). yfinance dividends are the sole dividend source. |
| Dividend source (S4/S6) | PARTIAL | REAL ex-date events for 19/21 tickers (SPY, BIL, SHY, SGOV, VCSH, VCIT + 13 candidates). Static `DIVIDEND_YIELDS` fallback still applied to IWM, MDY. |
| Survivor-free universe (S4) | LIMITED | yfinance history ends at today's constituents; delisted names absent. Basket membership is expanding-window OOS, but the price universe itself is survivorship-biased (no fja05680/sp500 PIT list yet). |
| Fill discipline (S3/P6) | OK | Execution at first trading day strictly after the calendar rebalance date; signals at month-end. No same-close fills. |
| Multi-source prices (S2) | CHECKING | Nasdaq cross-check ran (below); gold cross-check skipped (FRED down). |

Multi-source price integrity (yfinance vs Nasdaq, daily return corr):
SPY 0.999, GLD 1.000, VCIT 0.986, SHY 0.921, VCSH 0.968, IAU 0.948,
BIL 0.228 (raw-level corr 1.000, rel diff 0.0000), SGOV 0.028 (raw-level
corr 1.000, rel diff 0.0000). BIL/SGOV return-corr noise is the documented
bill-fund artifact; the raw-level reconciliation is exact.

## 2. POOLED RESULTS (corrected data layer)

$10k; turnover-proportional fees; monthly decisions; dividends = REAL ex-date
events (static fallback only for IWM/MDY).

| phase | strategy | end $ | total ret | Sharpe | maxDD | fees | dividends | trades | Δ vs degraded |
|---|---|---|---|---|---|---|---|---|---|
| P1/P2 | BASELINE SPY | 33,816 | +238% | 0.91 | -31% | 25 | 3,887 | 1 | +6.7% |
| P1/P2 | MACRO (state+risk) | 26,432 | +164% | 1.61 | -14% | 62 | 5,807 | 4 | — |
| P1/P2 | MINVAR | 27,018 | +170% | 1.57 | -13% | 50 | 4,954 | 3 | — |
| P1/P2 | DIVIDEND | 15,166 | +52% | 1.64 | -5% | 25 | 2,385 | 1 | — |
| P3 | BASELINE SPY | 33,816 | +238% | 0.91 | -31% | 25 | 3,887 | 1 | +6.7% |
| P3 | STATIC-40/20/20/20 | 26,277 | +163% | 1.08 | -20% | 25 | 4,800 | 1 | — |
| P3 | OPPORTUNISTIC-ONLY | 26,666 | +167% | 1.13 | -20% | 70 | 7,123 | 2 | — |
| P3 | STATIC-after-ML | 24,012 | +140% | 1.36 | -15% | 25 | 5,532 | 1 | — |
| P3 | ADAPTIVE | 23,947 | +139% | 1.36 | -15% | 25 | 5,497 | 1 | — |
| DISC | BASELINE SPY | 33,816 | +238% | 0.91 | -31% | 25 | 3,887 | 1 | +6.7% |
| DISC | STATIC-after-ML (P3 ref) | 24,012 | +140% | 1.36 | -15% | 25 | 5,532 | 1 | +9.7% |
| DISC | RM-STATIC | 34,440 | +244% | 1.27 | -20% | 1,353 | 9,690 | 64 | +27.9% |
| DISC | RM-ML-STATIC | 34,233 | +242% | 1.26 | -20% | 1,301 | 9,617 | 64 | +27.7% |
| DISC | RM-ML-ADAPTIVE-HIGH | 34,050 | +240% | 1.23 | -20% | 1,583 | 9,227 | 65 | +26.9% |
| DISC | RM-ML-ADAPTIVE-LOW | 34,319 | +243% | 1.21 | -20% | 1,710 | 9,005 | 68 | +26.4% |
| DISC | RM-GUARD (Test-2 bar) | 32,464 | +225% | 1.22 | -20% | 1,607 | 8,876 | 65 | +26.9% |
| DISC | RM-FINAL (Final bar) | 34,246 | +242% | 1.43 | -12% | 1,523 | 9,238 | 48 | +26.8% |

## 3. RE-EVALUATED SUCCESS BARS vs degraded (B-20260804-001)

SPY total return rose +217% -> +238% (+6.7%); that delta is entirely the real
dividend accrual ($3,887 vs $1,771 static = +$2,116). The RM suite rose
+225..+244% (+26-28% vs degraded): real dividends on the equity complex more
than double the static-map estimate ($8,876-9,690 vs $3,955-4,274).

- Test 1 (beat SPY raw total cash): **now PASSES** — RM-STATIC +244% beats
  SPY +238% (degraded verdict: all FAILED).
- Test 2 (beat SPY AND Sharpe > 0.70 AND maxDD < 40%): **now PASSES** for
  RM-STATIC / RM-ML-STATIC / RM-ML-ADAPTIVE-LOW / RM-FINAL.
- Final (Test-2 bars AND fees < $200): **still FAILS** — fees $1,301-$1,710
  (5-7% of gains). Fee budget unchanged; the fee problem is structural, not
  data-driven.

## 4. Code changes this run (data layer only; zero strategy-logic changes)

- `diversification/fee_sim3.py`: `Portfolio.__init__` gains `div_hist`;
  `run()` accrues real ex-date dividend events (lump-sum, shares held), falling
  back to the static map only for tickers without real history. New
  `_fetch_div_hist()` covers every traded ticker. New `data_status()` and
  `pool_all_results()` (`python -m diversification.fee_sim3 pool`).
- `tests/test_diversification_phase1.py`: +2 regression tests for ex-date
  accrual (event lump vs static drip; event dominates static yield).
- `center/baseline-test-results.json`: refreshed (925 passed / 18 skipped /
  0 failed, 943 collected, 0.981).

## 5. Gate

Conductor: 925 passed / 18 skipped / 0 failed in 159.64s (0.981). Zero new
failures. Gate PASS.
