# DEEP DIVE — `backtesting/` (Walk-Forward Harness & Statistical Backtests)

> Workstream B documentation. The house backtest layer: sentiment walk-forward
> (IC/Sharpe/hit-rate), the chi-square regime × win/loss gate (with Fisher's
> exact fallback), drift detection, and the reduced-metric extras module.

## 1. `backtest.py` — the sentiment walk-forward harness

- Runs walk-forward backtests with a `WalkForwardConfig` (train/test splits,
  embargo, expanding or rolling window). Key output metrics:
  **IC (rank correlation), Sharpe (annualized, 252), hit rate**.
- Risk blocks computed on the equity/returns series:
  - `_compute_var_cvar` — historical VaR/CVaR at the configured level.
  - `_compute_ulcer_maxdd` — Ulcer Index (RMS of drawdowns) and max drawdown.
- Engine map: `ENGINE_MAP` routes strategy configs to fee models — the default
  house engine is **`fee_sim3`** (`diversification/fee_sim3.py`), not a naive
  flat bps model. Never backtest without a fee_sim3 executor.
- Returns series are monthly for the composite path (84 rows, 2018–2024) and
  daily for leaf-level checks — do not mix cadences when computing DSR-adjacent
  quantities (see `validation/composite_score.py` P0B note).

## 2. `chi_square.py` — regime × win/loss independence gate (house gate v2)

- Contingency table: market regime × (win / win-loss) rows over the strategy's
  trades; **chi-square test of independence** between regime and outcomes.
- **Fisher's exact fallback**: whenever any expected cell count < 5, the chi-square
  approximation is invalid → swap to Fisher's exact test (scipy) and report it.
- `DEFAULT_REGIMES`: bull = 2023–2024; bear = 2020 COVID drawdown + 2022
  tightening. `FULL_WINDOW` = 2000-01-01 → today; `RECENT_WINDOW` = trailing 19
  months (the frozen "recent" lookback used by the composite path).
- Interpretation contract: a *significant* chi-square is a **caution flag**
  (regime-dependence), not a pass: the gate passes only when regime independence
  cannot be rejected at the pre-registered level. Alpha and Bonferroni family are
  defined in `config/gates_v2.yaml`.
- Tests: `tests/test_chi_square.py`.

## 3. `metrics_extra.py` — reduced metric kit

- `sortino` (downside deviation below target, MAR-style with
  `ANNUALIZE = 252`), `calmar` (CAGR / max drawdown), `win_rate`, plus plain
  `period_return` / `monthly_returns` helpers.
- Used by `tests/test_metrics_extra.py` and by runner scripts
  (`scripts/runs_logs/run_backtest.py`) for per-arm reporting. Keep this module
  dependency-light — it is imported by runner scripts whose import graphs were
  part of the Workstream B sweep (see `scripts/README.md`).

## 4. `drift_detection.py` — distribution shift monitor

- Detects drift between rolling train vs live windows (e.g. PSI / KS-style
  comparisons) so a regime shift is caught *during* the backtest rather than in
  post-hoc audit. Feeds `regime/embargo.py` decisions about when to retrain.

## 5. What this folder is NOT

- The production backtests for the 3-arm pipeline run in
  `ingestion/harness/three_arm_runner.py` + `validation/composite_score.py`
  (monthly composite). The full-fat slippage/TCA modelling lives in
  `execution/tca_simulator_v2.py` and `diversification/fee_sim3.py`.
- This folder holds the *statistical harness* used by validation and research,
  not a strategy zoo.

## 6. Entrypoints

```bash
python -m backtesting.backtest --config <walkforward.yaml>     # walk-forward run
python -m backtesting.chi_square --run                         # regime x win/loss gate
python -m backtesting.drift_detection --run                    # drift monitor
```

## 7. Key files quick-map

| File | Highlights |
|---|---|
| `backtest.py` | `WalkForwardConfig`, IC/Sharpe/hit-rate, `_compute_var_cvar`, `_compute_ulcer_maxdd`, `ENGINE_MAP` → fee_sim3 |
| `chi_square.py` | regime×outcome chi-square, Fisher fallback (<5 expected), DEFAULT_REGIMES bull 2023-24 / bear 2020+2022, RECENT_WINDOW 19 mo |
| `drift_detection.py` | rolling distribution-shift detection |
| `metrics_extra.py` | sortino / calmar / win_rate / monthly_returns, ANNUALIZE=252 |

## 8. Cross-references

- Fees: `diversification/fee_sim3.py`; TCA: `execution/tca_simulator_v2.py`
- Gates that consume these stats: `validation/trailing_cv.py` (Sharpe,
  sector-clustered bootstrap), `validation/composite_score.py`,
  `ingestion/harness/gate_engine.py` (component targets)
- Tests: `tests/test_backtest.py`, `tests/test_chi_square.py`,
  `tests/test_metrics_extra.py`
- Runner layer: `scripts/runs_logs/` (run_backtest.py, run_bt.py)