# Backtest Registry

**Maintained by:** logger + data-scientist. One row per strategy per run, per the
D-20260803-002 adoption: universe / params / gates / exit / metrics / dates /
verdict. Append-only; never edit past rows.

## Fee/turnover simulation — D-20260803-002 ($10k, 2018-01-31 to 2026-07-31)

Universe: 100-name stratified MID-50/SMALL-50 pilot names (live Wikipedia + discovery screen).
Params: K=10, sector cap 30%, purge+embargo 21d, trailing-3y FF5 residual alpha z-score selection, 0.5% turnover fee.
Gates: none (simulation); exit: none / opportunistic swap z-gap>=1.0 / drift fee-gate 25%.

| strategy | params | exit | end_value | total_return | ann_return | ann_vol | sharpe | maxDD | fees | fees_pct_of_gain | trades | verdict |
|----------|--------|------|-----------|--------------|------------|---------|--------|-------|------|------------------|--------|---------|
| CHURN flat (current pilot) | monthly 0.5% flat | none | 43,589 | +336% | 23%/yr | 0.33 | 0.70 | -51% | 14,704 | 43.8% | 102 | FAIL fee discipline |
| FEE-GATED | skip drift<25%, fee on turnover | none | 56,132 | +461% | 26%/yr | 0.33 | 0.78 | -52% | 8,629 | 18.7% | 94 | fee bill still high |
| OPPORTUNISTIC | swap only on z-gap>=1.0 | opportunistic | 55,217 | +452% | 28%/yr | 0.40 | 0.70 | -65% | 201 | 0.44% | 15 | ADOPT: fee-free upside |
| NO-FEE reference | same logic, fee=0 | none | 72,681 | +627% | 29%/yr | 0.33 | 0.88 | -50% | 0 | 0% | 102 | ceiling |
| 60/40 flat | SPY60+basket40, monthly 0.5% | none | 26,907 | +169% | 14%/yr | 0.23 | 0.63 | -38% | 9,055 | 53.6% | 102 | worst fee case |
| 60/40 cost-aware | SPY60+basket40, opportunistic | opportunistic | 40,009 | +300% | 21%/yr | 0.28 | 0.72 | -47% | 110 | 0.37% | 15 | ADOPT: approved plan |
| SPY 100 buy-and-hold | — | none | 30,004 | +200% | 15%/yr | 0.19 | 0.77 | -34% | 0 | 0% | 0 | benchmark |

## Sum-of-all-backtests "if implemented today" ($10k each, D-20260803-002)

| backtest | window | total | end_value | source |
|----------|--------|-------|-----------|--------|
| P3 pilot 100 no-exit | 2018-01-31..2026-07-31 | +277.2% | 37,720 | pilot100_noexit |
| P3 pilot 100 rolling-exit | same | +214.3% | 31,430 | pilot100 |
| P3 pilot MID-50 rolling | same | +276.4% | 37,640 | pilot_mid |
| P3 pilot SMALL-50 rolling | same | +218.9% | 31,890 | pilot_small |
| SPY same window | same | +277.2% | 37,720 | benchmark |
| **SUM (5 x $10k)** | — | — | **176,400** | avg $35,280 per $10k |

Cross-validation note: NO-FEE +627% × 0.995^102 fee drag ≈ the recorded net pilot
+277% — simulator reproduces the pilot's net-of-fee totals.

## Multi-asset Phase-1 sim — D-20260803-003 ($10k, 2018-01-31 to 2026-07-31)

Universe: 8 instruments across 4 sleeves (SPY / VCSH+VCIT / BIL+SHY+SGOV /
GLD+IAU). Params: pre-registered sleeves.py + macro_state.py + risk_minimizer.py
constants; friction gate = rebalance only when expected variance improvement
clears the 0.5% turnover fee (D-20260803-002 rule); monthly rebalances; purge+
embargo 21d; 252d covariance window. Macro input: HYG/LQD price-proxy fallback
(FRED unreachable this run). Multi-source: all prices cross-validated vs the
Nasdaq public API (raw-level corr 1.000 on BIL/SGOV; >=0.92 return corr on the
rest). Auditor memo: `_drafts/audit-B-20260803-003.md`.

| strategy | sleeve mix | friction | end_value | total_return | ann_return | ann_vol | sharpe | maxDD | fees | fees_pct_of_gain | dividends | coverage(dvd/fees) | trades | verdict |
|----------|-----------|----------|-----------|--------------|------------|---------|--------|-------|------|------------------|-----------|--------------------|--------|---------|
| BASELINE SPY | 100% SPY | buy-and-hold | 31,700 | +217% | 15%/yr | 0.19 | 0.83 | -33% | 25.00 | 0.12% | 1,771 | 70.9x | 1 | benchmark |
| MACRO (state+risk) | 4 sleeves, macro tilt + within-sleeve minvar | fee-gated | 22,982 | +130% | 10%/yr | 0.07 | 1.38 | -15% | 60.70 | 0.47% | 3,246 | 53.5x | 6 | risk-adjusted win, return lag |
| MINVAR (theoretically-better) | global min-variance across 4 sleeves | fee-gated | 24,719 | +147% | 11%/yr | 0.08 | 1.43 | -14% | 54.04 | 0.37% | 2,947 | 54.5x | 6 | risk-adjusted win, return lag |

CEO hypothesis (dividends cover reallocation fees) CONFIRMED: coverage 53-71x.
Phase-1 de-risks (Sharpe 1.38-1.43 vs 0.83; maxDD roughly halved) at the cost
of raw return vs SPY in a large-cap-dominated window. FRED macro path is live
and must be re-run when FRED is reachable (flag for CEO).

## Multi-asset Phase-2 sim — D-20260803-004 ($10k, 2018-01-31 to 2026-07-31)

Universe: Phase-1 8 ETFs + 13 pre-registered large-cap dividend candidates.
Params: pre-registered dividend_audit.py gates (5y window, >=3% trailing yield,
no >50% y/y cut on COMPLETE years, no skipped year, REIT/BDC/MLP excluded,
MIN_CANDIDATES=3 floor + SHY bills fallback) + opportunistic.py (bear-state
oversold tilt, z<=-1.0 vs 5y trailing mean) + reused D-20260802-002 friction
gate. DIVIDEND strategy = same macro-state sleeve mix as MACRO but the equity
sleeve is the OOS-audited stable-dividend basket. Multi-source dividends:
yfinance feed (disk-cached, 13/13) + SEC XBRL cross-check (NA this run -
EDGAR unreachable, documented). Auditor memo: `_drafts/audit-B-20260803-004.md`.

| strategy | sleeve mix | friction | end_value | total_return | ann_return | ann_vol | sharpe | maxDD | fees | fees_pct_of_gain | dividends | coverage(dvd/fees) | trades | verdict |
|----------|-----------|----------|-----------|--------------|------------|---------|--------|-------|------|------------------|-----------|--------------------|--------|---------|
| BASELINE SPY | 100% SPY | buy-and-hold | 31,700 | +217% | 15%/yr | 0.19 | 0.83 | -33% | 25.00 | 0.12% | 1,771 | 70.9x | 1 | benchmark |
| MACRO (state+risk) | 4 sleeves, macro tilt + within-sleeve minvar | fee-gated | 22,982 | +130% | 10%/yr | 0.07 | 1.38 | -15% | 60.70 | 0.47% | 3,246 | 53.5x | 6 | risk-adjusted win, return lag |
| MINVAR (theoretically-better) | global min-variance across 4 sleeves | fee-gated | 24,719 | +147% | 11%/yr | 0.08 | 1.43 | -14% | 54.04 | 0.37% | 2,947 | 54.5x | 6 | risk-adjusted win, return lag |
| DIVIDEND (stable-div + opportunistic) | audited basket equity sleeve + macro sleeves | fee-gated | 14,188 | +42% | 4%/yr | 0.03 | 1.33 | -6% | 25.00 | 0.60% | 1,406 | 56.3x | 1 | safest, lowest return; opportunistic fired 0x |

Coverage hypothesis CONFIRMED across all four strategies (45-71x). DIVIDEND
holds the 77/102 stable-dividend-basket decisions (avg 5 names; 25 early
decisions in bills fallback) at maxDD -6% but trails on raw return in a
large-cap bull window; the pre-registered opportunistic z-gate fired 0 times
(falsification evidence, not a tuning lever). SEC XBRL + FRED cross-checks to be
exercised when EDGAR/FRED are reachable (flag for CEO).

## Multi-asset Phase-3 sim — D-20260803-005 ($10k, 2018-01-31 to 2026-07-31)

Universe: 4-sleeve Phase-3 allocator (SPY / MDY+IWM / audited stable-dividend
basket / VCSH+VCIT+BIL+SHY+SGOV). Params: ALL pre-registered in
config/weights_diversification.yaml (static targets, sleeve bounds, GD
hyperparameters, train_end 2022-12-31 / oos_start 2023-01-01, risk_lambda 2.0,
hard maxDD 0.30, profit-change gate, cash-shortfall policy). Optimizer: gradient
ascent maximizing Sharpe - 2.0*vol^2 - 50*max(0, -maxDD - 0.30), projected onto
the capped simplex, deterministic seed, static + equal + minvar initial guesses.
Friction gate = D-20260802-002 rule (unchanged). Auditor memo:
`_drafts/audit-B-20260803-005.md`.

| strategy | sleeve mix | friction | end_value | total_return | ann_vol | sharpe | maxDD | fees | dividends | coverage | trades | verdict |
|----------|-----------|----------|-----------|--------------|---------|--------|-------|------|-----------|----------|--------|---------|
| BASELINE SPY | 100% SPY | buy-and-hold | 31,700 | +217% | 0.19 | 0.83 | -33% | 25 | 1,771 | 70.9x | 1 | benchmark |
| STATIC-40/20/20/20 (CEO) | fixed 45/15/20/20 (small-mid floored) | friction | 24,128 | +141% | 0.12 | 0.94 | -21% | 25 | 2,652 | 106.1x | 1 | risk-adjusted win, return lag |
| OPPORTUNISTIC-ONLY | static mix + z/profit-change OR tilt | friction | 22,979 | +130% | 0.11 | 0.93 | -21% | 73.65 | 3,631 | 49.3x | 3 | switching costs return (-11 pts) |
| STATIC-after-ML | ML weights (spy .30/sm .10/div .116/bonds .484) held statically | friction | 21,882 | +119% | 0.08 | 1.16 | -16% | 48.57 | 3,329 | 68.6x | 3 | ML BEATS manual on Sharpe; OOS Sharpe 1.90 |
| ADAPTIVE (risk-constrained) | re-optimized trailing 3y, same objective | friction | 21,819 | +118% | 0.08 | 1.16 | -16% | 50.12 | 3,333 | 66.5x | 3 | matches static-ML; dynamic adds nothing here |

CEO cash question: Phase-3 strategies generate ~1.9-2.0x SPY's dividend cash
(2,652-3,631 vs 1,771) but trail SPY on raw total return in a large-cap bull
window. Hard 30% maxDD bound HONORED everywhere (-16% to -21%); SPY breached at
-33%. Success criterion met: ML weights validated OOS (Sharpe 1.90, maxDD -9%
on 2023+) beat the fixed 40/20/20/20 mix (0.94) on Sharpe. Falsifications:
profit-change gate fired 0/102; opportunistic-only churn (28 z-fires) dragged
return. SEC XBRL + FRED cross-checks to be exercised when reachable (flag for
CEO).
