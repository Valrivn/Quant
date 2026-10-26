# `valuation/` — Intrinsic Valuation (V1 kernel + EVA modal gate)

## Roadmap Position
Pipeline stage 3 (valuation): the V1 intrinsic-value kernel plus the modal-EVA
quality gate. Feeds `valuaction_alpha` and the survival-adjusted intrinsic values
used by Arm A ranking (`ROIC_WACC_Spread`, `FCF_Yield`).

## Folder Summary
`v1_intrinsic_screen.py` is the V1 screen; `monte_carlo_dcf.py` provides DCF
distributions; `eva_modal.py` computes the **modal EVA gate**: the mode of the
EVA distribution (KDE) must be > 0 AND P(EVA > 0) ≥ 0.99 for a name to pass.
This gate is deliberately strict — a name is only "compounding" when both the
modal outcome and the tail probability agree.

## Skeleton
```
valuation/
  __init__.py  v1_intrinsic_screen.py  monte_carlo_dcf.py  eva_modal.py
```

## Files

| File | Purpose |
|---|---|
| `v1_intrinsic_screen.py` | V1 intrinsic-value screen driver |
| `monte_carlo_dcf.py` | Monte Carlo DCF (distributional value) |
| `eva_modal.py` | modal-EVA gate: `modal_eva > 0` AND `P(EVA>0) ≥ 0.99` |

## Entrypoints
`python -m valuation.v1_intrinsic_screen --run` (exact CLI per module docstring).

## Dependencies
`Quantitative/dcf_nowcaster.py`, `Quantitative/survival_adjustment.py`,
`data/pit_fundamentals/` (PIT financials), `data/fred_cache/` (discount inputs).

## Efficient Pathways
- `universe_build`: valuation layer determines `IV` used in ranking; EVA modal gate filters names
- `full_pipeline`: `v1_intrinsic_backtest.py` in `research/` backtests this layer