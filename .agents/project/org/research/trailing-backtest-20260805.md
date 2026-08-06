# B-20260805-002 — Trailing backtest vs SP500

- Run: 2026-08-05 21:02
- FRED: PRICE FALLBACK (FRED unreachable -> HYG/LQD price-proxy fallback)
- Data ends: 2026-07-30 00:00:00
- Bar (pre-registered): total return > SPY AND (maxDD <= SPY OR Sharpe >= SPY) AND >50% rolling-1Y win rate (2Y window only)

## Trailing 1Y (last 252 trading days; trailing to 2026-07-30)

| strategy | ret | SPY ret | excess | ann | Sharpe | maxDD | BAR |
|---|---|---|---|---|---|---|---|
| BASELINE SPY | +18.4% | +18.4% | +0.0% | +17.6% | 1.54 | -7.5% | FAIL |
| STATIC-after-ML (P3 ref) | +13.7% | +18.4% | -4.6% | +13.1% | 2.12 | -3.8% | FAIL |
| RM-STATIC | +21.7% | +18.4% | +3.3% | +20.1% | 2.33 | -5.6% | PASS |
| RM-ML-STATIC | +21.8% | +18.4% | +3.4% | +20.2% | 2.32 | -5.6% | PASS |
| RM-ML-ADAPTIVE-HIGH | +22.1% | +18.4% | +3.7% | +20.4% | 2.26 | -5.6% | PASS |
| RM-ML-ADAPTIVE-LOW | +22.5% | +18.4% | +4.1% | +20.8% | 2.21 | -5.6% | PASS |
| RM-GUARD (Test-2 bar) | +22.1% | +18.4% | +3.7% | +20.4% | 2.26 | -5.6% | PASS |
| RM-FINAL (Final bar) | +22.8% | +18.4% | +4.4% | +21.0% | 2.37 | -5.6% | PASS |

## Trailing 2Y (last 504 trading days; trailing to 2026-07-30)

| strategy | ret | SPY ret | excess | ann | Sharpe | maxDD | 1Y-win rate | BAR |
|---|---|---|---|---|---|---|---|---|
| BASELINE SPY | +40.1% | +40.1% | +0.0% | +18.0% | 1.21 | -16.3% | 0% (n=252) | FAIL |
| STATIC-after-ML (P3 ref) | +28.2% | +40.1% | -11.9% | +12.7% | 1.71 | -7.7% | 0% (n=252) | FAIL |
| RM-STATIC | +39.3% | +40.1% | -0.8% | +17.1% | 1.65 | -9.7% | 30% (n=252) | FAIL |
| RM-ML-STATIC | +38.5% | +40.1% | -1.6% | +16.9% | 1.61 | -9.8% | 28% (n=252) | FAIL |
| RM-ML-ADAPTIVE-HIGH | +39.5% | +40.1% | -0.6% | +17.3% | 1.52 | -10.6% | 33% (n=252) | FAIL |
| RM-ML-ADAPTIVE-LOW | +40.6% | +40.1% | +0.4% | +17.8% | 1.44 | -11.9% | 44% (n=252) | FAIL |
| RM-GUARD (Test-2 bar) | +39.5% | +40.1% | -0.6% | +17.3% | 1.52 | -10.6% | 33% (n=252) | FAIL |
| RM-FINAL (Final bar) | +40.8% | +40.1% | +0.6% | +17.8% | 1.57 | -10.6% | 38% (n=252) | FAIL |

## Reading

- **Trailing 1Y: the program beats SP500 on return AND risk.** RM-FINAL +22.8% vs +18.4% (+4.4pp excess), Sharpe 2.37 vs 1.54, maxDD -5.6% vs -7.5%. Every RM variant passes the full bar.
- **Trailing 2Y: beats on risk-adjusted terms, ties on raw return, loses the sub-window-majority leg.** RM-FINAL +40.8% vs +40.1% (+0.6pp), Sharpe 1.57 vs 1.21, maxDD -10.6% vs -16.3%. Best rolling-1Y win rate is 44% (ADAPTIVE-LOW) — all below the 50% bar.
- The pre-registered ">50% of rolling-1Y windows won" leg FAILS for every strategy over trailing 2Y. The trailing 2Y is bull-dominated; SPY's full beta wins sub-windows while the program's edge (defense + buy-more) only shows on risk-adjusted total return.
- P3 reference (STATIC-after-ML) badly lags on return in both windows (-4.6pp / -11.9pp), confirming the return-max pivot.

## Decision point for the CEO

The brief's "beats SP500" bar as pre-registered fails on the 2Y window-majority leg for everyone. Options: (a) keep the leg but measure it on rolling-3Y windows (prior research: RM-FINAL never loses a 5Y window, beats SPY in ~57% of 3Y windows); (b) drop the sub-window-majority leg and keep "return >= SPY AND Sharpe >= SPY AND maxDD <= SPY" on both windows; (c) keep as-is and treat only trailing 1Y as the PASS window.
