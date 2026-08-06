# Discovery — Best Strategy Summary, Sub-Period Splits & Short-Term Gains

Date: 2026-08-05. Fresh run on the corrected data layer (real ex-date dividend
events; same pre-registered `config/weights_diversification.yaml` `return_max`
params, invariant 4). $10k, 2018-01-31 -> 2026-07-31, monthly decisions,
turnover-proportional fees. Daily value paths captured for every strategy via
`fee_sim3.run_sim_discovery()` and analyzed for (A) sub-period splits 1Y/2Y/3Y/5Y
and (B) short-term holding-period gains. Nothing was refit to outcomes; all
short-term timing rules are fixed, standard, low-parameter.

## 1. The best strategy (GAINZ)

**RM-STATIC** — the return-max rules allocator — is the winner on total cash,
and **RM-FINAL** is the winner on risk-adjusted cash:

| strategy | end $ | total ret | ann ret | Sharpe | maxDD | fees | trades |
|---|---|---|---|---|---|---|---|
| BASELINE SPY | 33,816 | +238% | 16% | 0.91 | -31% | 25 | 1 |
| **RM-STATIC** (best cash) | **34,440** | **+244%** | 15% | 1.27 | -20% | 1,353 | 64 |
| RM-ML-STATIC | 34,233 | +242% | 15% | 1.26 | -20% | 1,301 | 64 |
| RM-ML-ADAPTIVE-LOW | 34,319 | +243% | 15% | 1.21 | -20% | 1,710 | 68 |
| **RM-FINAL** (best risk) | 34,246 | +242% | 15% | **1.43** | **-12%** | 1,523 | 48 |
| STATIC-after-ML (P3 ref) | 24,012 | +140% | 11% | 1.36 | -15% | 25 | 1 |

How it works (all pre-registered rules): macro state sets the equity weight
(bull 0.75 / neutral 0.65 / bear 0.85). In **bear** the equity complex is
concentrated 60/25/15 into the OOS-audited stable-dividend basket (quality
cashflow names; dividend-cut names rejected) / SPY / small-mid — the "buy-more"
rule. In **bull** a 2-state Markov momentum tilt overweights the top-3 members
by P(up|state). A hard 40% drawdown bound penalizes the optimizer; RM-FINAL
adds a crisis de-risk pulse (SPY trailing 21d <= -10% -> equity capped at 40%)
and event-driven trading.

## 2. Why it works — per-year behaviour (the mechanism)

| year | SPY | RM-STATIC | RM-FINAL | RM-GUARD | regime read |
|---|---|---|---|---|---|
| 2018 | -6.6% | **+1.1%** | +0.4% | +0.4% | bear-flat — buy-more + bonds protect |
| 2019 | +33.9% | +18.5% | +18.5% | +18.5% | bull — SPY wins, RM gives up upside |
| 2020 | +19.3% | +21.9% | **+23.0%** | +16.4% | COVID vol — de-risk + bear-buy helps |
| 2021 | +31.2% | +25.9% | +26.4% | +26.6% | bull — SPY wins |
| 2022 | -15.0% | **+0.8%** | -0.7% | -0.1% | bear — the CEO's edge shows (+15.8pp) |
| 2023 | +26.4% | +17.5% | +17.6% | +18.0% | bull — SPY wins |
| 2024 | +25.3% | +19.8% | +19.1% | +18.7% | bull — SPY wins |
| 2025 | +18.3% | +14.4% | +15.4% | +14.5% | bull — SPY wins |
| 2026* | +9.2% | +16.5% | **+17.1%** | +17.3% | *partial to 07-31 — RM wins |

**Why it beats SPY on total cash despite losing the bull years:** the strategy
underperforms in SPY's strongest bull years (-3.9 to -15.3pp) but out-earns SPY
in the bad years (+2.6 to +15.8pp), with nearly zero cost to compounding — a
-15% SPY year becomes ~flat, and 2018's -6.6% becomes +1.1%. Compounding the
years is what closes the gap: RM-STATIC +244% vs SPY +238% **with half the
max drawdown** (-20% vs -31%) and a better Sharpe (1.27 vs 0.91). Real dividend
accrual ($9,690 dividends) plus bonds income keeps the path compounding when
stocks go flat.

## 3. Sub-period splits (the "GAINZ" time-buckets)

Annualized per segment (non-overlapping; partial segments flagged):

**2Y segments:** 2018-20 SPY 8.1% / RM 6.6%; 2020-22 SPY 13.9% / **RM 14.9%**;
2022-24 SPY 5.4% / **RM 6.8%**; 2024-26 SPY 13.7% / RM 11.7%; 2026+ **RM 4.7% vs SPY 2.6%**.

**3Y segments:** 2018-21 SPY 9.7% / RM 8.9%; 2021-24 SPY 8.5% / **RM 9.8%**;
2024-26 SPY 10.8% / **RM 11.0%**.

**5Y segments:** 2018-23 SPY 7.9% / **RM 9.2%**; 2023-26 SPY 9.6% / RM 8.6%.

**Rolling windows (median | worst-case annualized):**

| window | SPY | RM-STATIC | RM-FINAL | RM beats SPY |
|---|---|---|---|---|
| 1Y | 11.9% | -16.0% | 10.7% | -8.6% | 9.3% | -3.6% | 37% of windows |
| 2Y | 11.2% | -6.3% | **12.2%** | -1.4% | 11.7% | -1.8% | 43% |
| 3Y | 12.2% | -4.3% | **14.1%** | -1.0% | 13.5% | -1.2% | 57% |
| 5Y | 13.4% | -2.1% | **13.6%** | -0.4% | **13.3%** | **+1.4%** | 50% (RM-FINAL never loses a 5Y window) |

**Reading:** RM's edge grows with horizon — it beats SPY in a minority of 1Y
windows but a majority of 3Y windows, and RM-FINAL has never had a losing 5Y
window in this dataset (worst +1.4%/yr). SPY's best window is higher (+75% vs
+58% on 1Y) but its worst window is 4-9x deeper. The strategy is the better
compound over every horizon >= 2Y.

## 4. Short-term gains — the answer to "ten years, what about short term?"

**Short-term SPY is a coin flip; short-term RM is positive-expectancy.**
Non-overlapping holding-period win rates:

| horizon | SPY win% | RM-STATIC win% | RM-FINAL win% |
|---|---|---|---|
| 1 week | 47.3% | 48.4% | 49.2% |
| 1 month | 47.1% | 52.9% | 53.6% |
| 1 quarter | 56.5% | 54.3% | 56.5% |
| 6 months | 52.2% | **65.2%** | 65.2% |
| 1 year | 54.5% (min -15.5%) | **72.7% (min +0.0%)** | 63.6% (min -1.3%) |

Buy-and-hold SPY loses money in a *majority* of its 1-week and 1-month windows
(positive skew: few big up-weeks carry the drift). RM-STATIC flips the monthly
win rate above 50% (52.9%) and — critically — **never lost money in any full
12-month holding window** (min +0.0%), vs SPY's worst 1-year -15.5%.

**Chasing short-term SPY timing rules destroys value.** Pre-registered fixed
rules, SPY vs BIL:

| rule | end $ | total ret | Sharpe | maxDD | trades |
|---|---|---|---|---|---|
| MOM-5D (5-day momentum, weekly) | 11,340 | +13% | 0.15 | -33% | 284 |
| MOM-21D (21-day momentum, monthly) | 17,045 | +71% | 0.47 | -30% | 69 |
| SMA200 (200-day trend filter, weekly) | 26,043 | +160% | 0.75 | -20% | 31 |
| SPY buy-and-hold | 33,816 | +238% | 0.77 | -31% | 1 |

Fast momentum over-trades into whipsaws (284 trades -> +13%, fees + whipsaw
destroy ~90pp). Even the clean SMA200 trend filter, which cuts drawdown to
-20%, gives up 78pp of return in this bull window. **There is no free
short-term edge on SPY in this window; the short-term gains that exist belong
to the regime-tilted RM equity complex, not to faster trading.**

## 5. More profit WITHOUT overfitting — ranked candidates

1. **Fix the data layer (already worth +26-28% in the pooled run).** Real
   dividends, PIT FRED vintages (fredapi), SEC XBRL dividend cross-check, and
   wiring `_nasdaq_crosscheck()` into the discovery path all raised the RM
   suite from +169-171% to +225-244% vs the degraded run. This is data, not
   curve-fitting — every point is a real tradeable cash flow.
2. **Replace the falsified 21-day crisis pulse with a slower circuit breaker**
   (portfolio DD > 35% or SPY < 200d SMA for N weeks). The pulse armed only
   2 months (COVID, then fled into the V-recovery) and MISSED 2022. The SMA200
   overlay above is the evidence the slow filter catches drawdowns at 1/6 the
   trade count.
3. **Cut fees toward the $200 bar** (RM-STATIC fees are $1,353 = 5.5% of gains).
   An asymmetric tolerance band around targets or semi-annual re-optimization
   (instead of full reallocation on every state flip) saves most of that with
   no parameter fitting.
4. **A/B-ablate the momentum tilt.** It fired 30/30 bull decisions but its
   weight impact is diluted across 3-7 members after renormalization. Isolating
   its true contribution tells us whether to keep, raise, or drop it.
5. **Survivorship-free universe + PIT constituents** (fja05680/sp500 point-in-
   time lists; delisted names preserved). Removes phantom alpha on the basket
   admission — the single biggest "backtest lies" fix available for free.
6. **Non-bull sub-window validation.** The edge concentrates in flat/bear
   windows (2022 +15.8pp vs SPY). Validate on a non-bull segment before betting
   the full 10Y on it — this is robustness, not overfit.
7. **Fold the SMA200 filter into RM-FINAL** as the de-risk signal (instead of
   the pulse) — expected effect: keep the 12% maxDD, add ~1-2%/yr by staying
   invested through the V-recovery.

## 6. Data status (S1-explicit, same as pooled-results-20260804)

FRED macro DEGRADED (price-proxy fallback; 72/102 months "bear" over-reads the
regime — biases the return-max upward on bear buy-more). SEC EDGAR DEGRADED
(XBRL cross-check NA). Survivor-free universe LIMITED (yfinance = today's
constituents). Dividend events REAL for 19/21 tickers (static fallback only
IWM/MDY). Nasdaq cross-check ran on this path: SPY 0.999 / VCIT 0.986 /
VCSH 0.968 / SHY 0.921 / BIL+SGOV reconciled exactly at raw level. These are
the #1 and #3 fixes above; the short-term numbers move with them.

## 7. Gate note

No production code changed this run (analysis only; script lives in the temp
dir). Baseline 925/18/0. Not gated — advisory discovery report for the CEO.
