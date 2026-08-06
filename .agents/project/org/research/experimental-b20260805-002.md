# B-20260805-002 — Experimental layers (NOT production)

- Run: 2026-08-05 21:25 on branch feature/b-20260805-002
- FRED: PRICE FALLBACK (run used HYG/LQD price-proxy regime fallback)
- All results are RESEARCH ONLY; nothing here feeds the live pipeline.

## Phase 2 — reward metric

Headline: RM-REWARD (3y window, lambda=0.3, tc=10bp) vs RM-ML-ADAPTIVE-HIGH (same feed, raw return objective).

| strategy | total | SPY | ann | Sharpe | maxDD | OOS excess | trades |
|---|---|---|---|---|---|---|---|
| RM-ML-ADAPTIVE-HIGH | +240.5% | +238.2% | +11.2% | 1.05 | -20.1% | -16.2% | 65 |
| RM-REWARD (3y, l=0.3) | +240.6% | +238.2% | +11.2% | 1.05 | -20.1% | -16.1% | 65 |
| RM-FINAL (Final bar) | +242.5% | +238.2% | +11.1% | 1.22 | -11.7% | -15.1% | 48 |

Double-descent capacity curve (complexity = trailing re-fit window; lambda=0.3):

| capacity (trailing window) | train ann excess vs SPY | OOS ann excess vs SPY |
|---|---|---|
| 126d | +0.6% | -3.4% |
| 252d | +0.6% | -2.9% |
| 504d | +0.4% | -2.9% |
| 756d | +0.4% | -2.7% |
| 1008d | +0.5% | -2.8% |

- Train-only capacity rule picks the simplest window at max train excess (126d); largest OOS excess is at 756d (-2.7%).

## Phase 3 — Bayesian posterior P(strategy beats SP500)

Prior Beta(11,9) (mean 0.55) updated on non-overlapping 1Y windows.

| strategy | wins | losses | posterior mean | 90% CI | P(p>0.5) |
|---|---|---|---|---|---|
| RM-STATIC | 6 | 10 | 0.47 | 0.34-0.61 | 37% |
| RM-ML-ADAPTIVE-HIGH | 5 | 11 | 0.44 | 0.31-0.58 | 25% |
| RM-FINAL (Final bar) | 5 | 11 | 0.44 | 0.31-0.58 | 25% |
| BASELINE SPY | 0 | 16 | 0.31 | 0.19-0.44 | 1% |

## Phase 4 — Monte Carlo forward sim (state-conditional bootstrap)

RM-FINAL vs SPY, 6m horizon, N=2000 paths, current regime = bull.

| metric | RM-FINAL | SPY |
|---|---|---|
| median 6m return | +8.7% | +10.0% |
| P(beat SPY) | 44% | - |
| 5% VaR | +0.0% | -0.8% |
| worst path | -4.7% | -10.7% |

Calibration gate (backtest-first, n=114 historical 6m windows): realized in 90% band 84% (target 90%), in 50% band 45% (target 50%).

## Phase 5 — Markov Peter-Lynch states (proxy classification)

Lynch states per name/month from momentum/vol/dividend (proxy for SEC fundamentals).

Transition matrix (aggregate regime):

```
           DEFENSIVE  GROWTH  MIXED
DEFENSIVE      0.992   0.000  0.008
GROWTH         0.000   0.636  0.364
MIXED          0.231   0.088  0.681
```

Stationary distribution: {'DEFENSIVE': 0.957, 'GROWTH': 0.008, 'MIXED': 0.034}

Signal test (does aggregate state predict forward 6m excess vs SPY?): defensive share rho=-0.332 p=0.002; entropy rho=+0.426 p=0.000; n=88.

---
Experimental only. No pipeline wiring. See impl-plan-B-20260805-002.md.

## Executive verdict (did the strategy "work"?)

| Layer | Verdict | Why |
|---|---|---|
| Phase 2 reward metric | **No-op as designed** | Rewarding `alpha vs SPY` changes nothing: SPY's ann return is a constant offset in the optimizer, so the argmax is identical (RM-REWARD +240.6% vs ADAPTIVE +240.5%, same 65 trades). Trade reduction comes from the event-driven hold gate (RM-FINAL: 48 trades, maxDD -11.7%), NOT from a weight-space penalty. |
| Phase 2 double descent | **Overfit confirmed** | Every capacity loses to SPY OOS on annualized excess (-2.7 to -3.4%). OOS best at the LONGEST window (756-1008d), worst at the shortest (126d) — the "underfit regime" of the double-descent curve. The full-window win comes from regime/buy-more/bond-sleeve structure, not the ML within-equity split. |
| Phase 3 Bayesian | **Honest answer to "confidence"** | Evidence (5W-11L on non-overlapping 1Y windows) overrides the 0.55 prior: posterior P(p>0.5) = **25%** for RM-FINAL. The strategy beats SPY on risk-adjusted + long-horizon terms, NOT on majority-of-1Y-windows. The prior must be set lower, or the "beat SPY" definition must be long-horizon. |
| Phase 4 Monte Carlo | **Works, mildly under-dispersed** | 6m forward: RM-FINAL median +8.7% vs SPY +10.0%, P(beat) 44%. Calibration gate caught under-dispersion (90% band covers 84%, 50% band 45%) — bands are too narrow; widen shocks before trusting any forward band. |
| Phase 5 Markov | **Partial signal, degenerate matrix** | The Lynch-proxy classification saturates: stationary distribution is 95.7% DEFENSIVE (dividend-heavy basket), so the transition matrix is near-absorbing and useless for "prepare." But entropy-to-forward-excess is significant (rho +0.43, p<0.001): high state diversity precedes higher 6m excess — INVERTS the "low-entropy = high statistics = prepare" hypothesis. Needs SEC-fundamental classification to be actionable. |

### Bottom line
- The strategy's edge is NOT the ML/reward machinery — it is the **regime structure + bear buy-more + bond sleeve + event-driven fee gate** (RM-FINAL). Those are already in the pipeline.
- The experimental layers that HELP: the **Bayesian posterior** (kills unfalsifiable confidence — currently says only 25% P(beat SPY) per 1Y window) and the **MC calibration gate** (catches under-dispersion). The **Markov entropy signal** is a genuine new finding worth a fundamentals-grounded follow-up.
- The layers that DON'T help as specified: reward-as-alpha (degenerate), double-descent ML split (overfits OOS), Markov transition matrix (degenerate proxy).
- Recommended: do NOT wire any of this into the live pipeline. If the CEO wants Phase 2 pursued, the only lever that matters is the **hold/no-trade band with a turnover budget** (already approximated by RM-FINAL's fee discipline).