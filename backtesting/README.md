# `backtesting/` — Walk-Forward Harness & Statistical Backtests

## Roadmap Position
Pipeline stage 2.5 (backtest kernel): the statistical harness beneath the gates —
walk-forward IC/Sharpe/hit-rate, regime-independence chi-square, drift detection.
The production 3-arm backtests themselves run from `ingestion/harness/`.
**See `backtesting/DEEP_DIVE.md` for details.**

## Folder Summary
Small, dependency-light folder: the house backtest driver (`backtest.py` with
VaR/CVaR and Ulcer/max-DD blocks), the regime × win/loss independence gate
(`chi_square.py` with Fisher's-exact fallback), a distribution-drift monitor,
and the reduced metric kit (`metrics_extra.py`). All backtests execute through
the `fee_sim3` fee engine by default.

## Skeleton
```
backtesting/
  backtest.py        # walk-forward harness
  chi_square.py      # regime x win/loss gate (v2)
  drift_detection.py # distribution shift monitor
  metrics_extra.py   # sortino/calmar/win_rate
  oos_evaluation.py  # Out-of-Sample (OOS) purged evaluation & stress engine
  __init__.py
```

## Files

| File | Purpose |
|---|---|
| `backtest.py` | walk-forward config; IC/Sharpe/hit-rate; `_compute_var_cvar`, `_compute_ulcer_maxdd`; `ENGINE_MAP` → fee_sim3 |
| `chi_square.py` | regime×outcome contingency chi-square; Fisher fallback (expected <5); DEFAULT_REGIMES (bull 2023-24, bear 2020+2022) |
| `drift_detection.py` | rolling train-vs-live distribution drift |
| `metrics_extra.py` | sortino, calmar, win_rate, monthly_returns (ANNUALIZE=252) |
| `oos_evaluation.py` | Purged Out-Of-Sample partition (3-mo embargo), dynamic square-root slippage, delisting injection, null asset-shuffling, PBO & DSR |

## Entrypoints
```bash
python -m backtesting.backtest --config <walkforward.yaml>
python -m backtesting.chi_square --run
python -m backtesting.drift_detection --run
```

## Dependencies
`diversification/fee_sim3.py` (fee engine), `config/gates_v2.yaml` (alpha /
Bonferroni family), runner layer in `scripts/runs_logs/`.

## Efficient Pathways
- `full_pipeline`: leaf-level validation always passes through here before gates
- `three_arm_backtest`: composite-level runs go through `ingestion/harness`; this
  folder is for leaf/statistical backtests and drift checks