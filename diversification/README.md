# `diversification/` — Sleeves, Risk Models & the fee_sim3 Engine

## Roadmap Position
Pipeline stage 4 (sleeve allocation): the diversified sleeves layer. `fee_sim3.py`
is the **house fee engine** referenced by `backtesting/backtest.py`
(`ENGINE_MAP`), `execution/`, and the three-arm runner — every backtest without
an explicit engine defaults to it.

## Folder Summary
`macros_state.py`/`markov_momentum.py` condition sleeves; `risk_minimizer.py` /
`return_max.py` / `optimistic.py` are the sleeve optimizers; `sleeves.py` is the
sleeve decomposition; `allocator.py`, `backtest.py`, `backtest_diagnostics.py`,
`report.py` run and report sleeve backtests; `datastore.py`, `master_data.py`,
`trading_calendar.py`, `pit_credit.py` supply data; `fee_sim3.py` is the cost
engine; `dividend_audit.py` audits dividend coverage.

## Skeleton
```
diversification/
  sleeves.py risk_minimizer.py return_max.py opportunistic.py markov_momentum.py macro_state.py
  fee_sim3.py backtest.py backtest_diagnostics.py allocator.py report.py datastore.py ...
```

## Files (key)

| File | Purpose |
|---|---|
| `fee_sim3.py` | house fee/slippage engine (default for all backtests) |
| `sleeves.py` | sleeve decomposition of the portfolio |
| `risk_minimizer.py` | minimum-variance sleeve |
| `markov_momentum.py` | regime-conditioned momentum sleeve state |
| `macro_state.py` | macro conditioning for sleeve mix |

## Entrypoints
`python -m diversification.backtest --run` (exact CLI per module docstring).

## Dependencies
`execution/tca_simulator_v2.py`, `portfolio/allocator.py`, `data/master_prices/`.

## Efficient Pathways
- `three_arm_backtest`: ALWAYS route costs through `fee_sim3.py` — never flat-bps substitutes
- `full_pipeline`: sleeves own the final capital split across arms