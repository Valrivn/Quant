# `execution/` — TCA Simulation

## Roadmap Position
Pipeline stage 5 (execution): transaction-cost analysis. `tca_simulator_v2.py`
is the production TCA model (slippage + fees + market impact); `tca_simulator.py`
is the v1 predecessor kept as history. TCA output feeds the `tca_bps` component
of the composite gate (`≤ 150 bps`).

## Folder Summary
Two-file folder. The v2 simulator is the reference for `TXN_COST_BPS = 10/side`
used in `ingestion/harness/three_arm_runner.py` and for the rolling `tca_bps`
series in `data/backtests/rolling_metrics_*_primary.parquet`.

## Skeleton
```
execution/
  __init__.py  tca_simulator.py  tca_simulator_v2.py
```

## Files

| File | Purpose |
|---|---|
| `tca_simulator_v2.py` | production TCA (slippage, fees, impact) |
| `tca_simulator.py` | v1 historical simulator (read-only) |

## Entrypoints
`python -m execution.tca_simulator_v2 --run` (exact CLI per module docstring).

## Dependencies
`diversification/fee_sim3.py` (fee model), `data/backtests/` (fills/trades).

## Efficient Pathways
- `three_arm_backtest`: TCA bps is a frozen component (≤150) of the composite DSR
- `full_pipeline`: execution costs land in `rolling_metrics_*` for gate evaluation