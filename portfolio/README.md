# `portfolio/` — Portfolio State, Allocation & Reporting

## Roadmap Position
Pipeline stage 4 (portfolio): the current-holdings state machine, the tactical
allocator output, and reporting. Downstream of the three-arm runner; upstream of
execution/TCA.

## Folder Summary
`allocator.py` applies the arm weights to the current state; `position_state.py`
maintains per-name state (entry, exits, probation, swaps); `report.py` renders
portfolio reports; `llm_guide.py` is the LLM-facing portfolio explainer.

## Skeleton
```
portfolio/
  __init__.py  allocator.py  position_state.py  report.py  llm_guide.py
```

## Files

| File | Purpose |
|---|---|
| `allocator.py` | tactical allocation application |
| `position_state.py` | position lifecycle state (entry/exit/swap) |
| `report.py` | portfolio reporting |
| `llm_guide.py` | LLM-facing guidance for explaining holdings |

## Entrypoints
`python -m portfolio.report` (exact CLI per module docstring).

## Dependencies
`diversification/sleeves.py` (sleeve decomposition), `execution/tca_simulator_v2.py` (fills), `regime/` (risk state).

## Efficient Pathways
- `full_pipeline`: portfolio state is the handoff from arms → execution