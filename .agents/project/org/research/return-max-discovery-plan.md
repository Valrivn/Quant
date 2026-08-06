# Implementation Plan — Return-Max Pivot (B-20260804-001, Discovery)

Date: 2026-08-04. Discovery backtest executed with all params pre-registered
(config/weights_diversification.yaml `return_max` block, invariant 4). This plan
is the CEO's requested deliverable: the logistics + niche details of
implementing the "buy-more quality stocks in bear / ride momentum in bull"
allocator, plus the honest backtest verdict.

## 1. Verdict (what the backtest says)

$10k, 2018-01-31 -> 2026-07-31, monthly decisions, turnover-proportional fees,
OOS stable-dividend audit, Markov momentum, crisis de-risk, event-driven fees.

| strategy | end $ | total return | Sharpe | maxDD | fees | trades |
|---|---|---|---|---|---|---|
| BASELINE SPY | 31,700 | +217% | 0.83 | -33% | 25 | 1 |
| STATIC-after-ML (P3 ref) | 21,882 | +119% | 1.16 | -16% | 49 | 3 |
| RM-STATIC (rules) | 26,933 | +169% | 1.03 | -20% | 1,181 | 64 |
| RM-ML-STATIC | 26,802 | +168% | 1.02 | -20% | 1,138 | 64 |
| RM-ML-ADAPTIVE-HIGH | 26,841 | +168% | 1.00 | -20% | 1,382 | 65 |
| RM-ML-ADAPTIVE-LOW | 27,144 | +171% | 0.99 | -20% | 1,493 | 68 |
| RM-GUARD (downside engine) | 25,579 | +156% | 0.98 | -20% | 1,408 | 65 |
| RM-FINAL (event-driven) | 27,016 | +170% | 1.16 | -12% | 1,332 | 48 |

Success bars (all three fail on the "beat SPY" leg and the fee leg):
- Test 1 (beat SPY raw total cash): **FAIL** - best RM-ML-ADAPTIVE-LOW +171% vs SPY +217%.
- Test 2 (beat SPY AND Sharpe > 0.70 AND maxDD < 40%): risk bars pass easily
  (Sharpe ~1.0-1.16, maxDD -12% to -20%), but the beat-SPY leg fails.
- Final (Test-2 bars AND fees < $200): **FAIL** - fees $1,138-$1,493 (7-9% of gains).

Directional finding: the return-max pivot closed ~half the gap to SPY (+119% ->
+171%) while keeping drawdown ~60% lower than SPY. It is a real improvement in
total cash generated vs every prior Phase-1/2/3 strategy, but it does NOT beat
SPY in this large-cap bull window. The verdict for the CEO: worth further
refinement, NOT worth a Tier-3 blueprint adoption as built.

## 2. What was built (discovery modules)

- `diversification/markov_momentum.py` - 2-state Markov chain (P(up|current)
  from trailing 252d), threshold-gated tilt. Fired in all 30 bull decisions.
- `diversification/return_max.py` - return-max objective (ann_mean -
  dd_penalty*breach over the 40% bar) + projected gradient optimizer over the
  equity complex + in-sample static fit + trailing adaptive re-fit.
- `diversification/fee_sim3.py` - `run_sim_discovery()` / `main_discovery()`,
  bear-buy-more rule, bull momentum overweight, crisis pulse, event-driven
  fee discipline, and a new `Portfolio.run(gate="turnover")` execution mode.
- `tests/test_diversification_discovery.py` - 23 offline tests.

## 3. Logistics (build order for any follow-up)

1. Keep all knobs in `return_max` config (invariant 4) - nothing hardcoded.
2. Sim, not live: this stays a backtest until the CEO rules. No funds move.
3. Data: yfinance + Nasdaq cross-check + FRED (down this run, price-proxy used)
   + SEC XBRL (down this run, cross-check skipped as documented degradation).

## 4. Niche details (the gotchas that changed the answer)

- **Variance fee gate blocks return-max**: `Portfolio.run` traded only when a
  rebalance LOWERED expected variance enough to clear the fee. Return-max
  targets move toward HIGHER variance, so the gate blocked every rebalance and
  all variants froze at their first allocation (that is why a 1-trade artifact
  showed identical results on the first run). Fix: `gate="turnover"` (trade when
  target differs by >= 5% turnover; fees charged honestly). Backward compatible;
  Phase-1/2/3 default unchanged.
- **Empty basket starves the buy-more**: the OOS audit needs a 5y window, so the
  basket is empty until ~2020. In bear 2018-2020 the "buy-more" 60% went to the
  SHY bills fallback, i.e. the CEO's buy-more bought BILLS, not stocks. The
  idea only becomes live once the audit admits names (2020+).
- **The price-fallback classifier over-says "bear"**: FRED unreachable this run;
  the HYG/LQD price proxy flagged 72/102 months bear. With E(bear)=0.85 the
  portfolio sits ~85% in the buy-more rule most of the window and misses bull
  participation. The regime read is a first-order driver of the result.
- **Momentum tilt fired 30/30 bull decisions** but after renormalization its
  weight impact is diluted across 3-7 members; no A/B control was run to
  isolate its contribution (flagged as a follow-up ablation).
- **The 21-day crisis pulse armed only 2 months** (COVID): it fled to bills just
  before the V-recovery (cost ~1.2pp vs no engine) and MISSED the 2022 slow bear
  (a grind, never -10% in 21 days), so maxDD stayed -20% in both. The pulse
  design is falsified as the downside engine; a slower DD/level-based circuit
  breaker (e.g., portfolio DD > 35% or SPY below 200d SMA for N weeks) is the
  candidate replacement.
- **Fees are the Final-bar killer**: 48-68 trades = $1,138-$1,493. Hitting the
  $200 bar means ~8 trades total. Event-driven trading (RM-FINAL: trade only on
  state transitions / crisis arm-disarm / quarterly boundary) cut trades to 48
  but per-trade turnover stayed large (full reallocation on state flips).
  Options: asymmetric tolerance band around targets, or rebalance to the 60/40
  hybrid every 6 months instead of full reallocation.
- **Diversification drag is structural**: 15-25% bonds + defensive dividend
  names (KO/PG/CL/TGT...) lag megacap SPY in a bull run. Beating SPY raw return
  here requires either concentrating into SPY when its momentum is strong
  (already the bull rule) or a different window (2018-2026 is a large-cap bull;
  the CEO's edge likely shows in a flat/bear window).
- **Static-ML fit uses future data by design**: RM-ML-STATIC fits on all data
  <= 2022-12-31 then holds (same as Phase-3 STATIC-after-ML). OOS segment
  (2023+) validated at Sharpe 1.52, maxDD -10% - no overfit in the segment.

## 5. Recommended next step (if the CEO wants to pursue)

Promote to Tier 3 with a MODIFY brief: (a) replace the 21-day pulse with a
portfolio-DD / 200d-SMA circuit breaker; (b) add a momentum A/B control; (c) cap
turnover per state flip (fee budget ~8 trades); (d) re-run in a non-bull
sub-window (e.g., 2022) to test the bear thesis where the CEO's edge is
expected. Only then does a blueprint ruling make sense.
