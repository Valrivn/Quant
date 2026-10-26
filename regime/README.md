# `regime/` — Regime State, De-Risking & Embargo Guards

## Roadmap Position
Pipeline stage 3.5 (risk state): the operational regime layer that listens to
`Quantitative/unified_regime.py` / `regime_shield_v3.py` and translates regime
signals into concrete trading guards (de-risk, embargo, high-bar, market events).

## Folder Summary
`regime_state.py` is the state holder; `derisking.py` (scale-down rules on
CRISIS), `embargo.py` (no-new-positions windows / retrain triggers),
`highbar.py` (elevated entry thresholds in risky regimes) and `market_events.py`
(calendar-based events: FOMC, data releases) apply the guards.

## Skeleton
```
regime/
  __init__.py  regime_state.py  derisking.py  embargo.py  highbar.py  market_events.py
```

## Files

| File | Purpose |
|---|---|
| `regime_state.py` | operational regime state (consumes unified regime output) |
| `derisking.py` | de-risk rules on CRISIS/SHOCK |
| `embargo.py` | position embargoes + retrain triggers (drift-aware) |
| `highbar.py` | elevated entry bars in risky regimes |
| `market_events.py` | scheduled event handling (FOMC, inflation prints) |

## Entrypoints
`python -m regime.regime_state --status` (exact CLI per module docstring).

## Dependencies
`Quantitative/regime_shield_v3.py`, `Backtesting/drift_detection.py`,
`config/regime_thresholds.yaml`.

## Efficient Pathways
- `regime_validation`: regime model → `regime/` guards → validation gates
- `full_pipeline`: guards gate portfolio/execution behavior in real time