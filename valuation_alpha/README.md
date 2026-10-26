# `valuation_alpha/` — Alpha Pipeline on the V1 Valuation Layer

## Roadmap Position
Pipeline stage 3 (alpha generation): the full valuation→alpha pipeline — data
store (`datastore/`), CIK/universe resolution (`universe/`), ratio/reinvestment/
moat gates, selection, and reporting. Distinct from `valuation/` (the V1 kernel);
this is the production alpha application.

## Folder Summary
`alpha.py` orchestrates selection; `datastore/` (xbrl_financials, prices,
factors) is the PIT store; `universe/` (roster, discovery, cik_resolver)
constructs the investable list; `moat_gate.py`, `reinvestment.py`, `ratios.py`,
`stats.py`, `exit.py`, `young_firm_valuation.py` form the analysis toolkit;
`pipeline.py` is the end-to-end driver; `report.py` + `engine.py` produce
evaluation reports (see `opencode_scripts/output/` and `center/` results).

## Skeleton
```
valuation_alpha/
  datastore/    # xbrl_financials.py, prices.py, factors.py
  universe/     # roster.py, discovery.py, cik_resolver.py
  alpha.py  pipeline.py  engine.py  selection.py  report.py
  moat_gate.py  reinvestment.py  ratios.py  stats.py  exit.py  young_firm_valuation.py  discovery_screen.py
```

## Files (key)

| File | Purpose |
|---|---|
| `alpha.py` | alpha orchestration |
| `pipeline.py` | end-to-end valuation→alpha pipeline |
| `moat_gate.py` | moat presence gate |
| `reinvestment.py` | reinvestment-ratio analysis |
| `young_firm_valuation.py` | young-firm (pre-profit) valuation |
| `datastore/xbrl_financials.py` | PIT XBRL financial store access |
| `universe/roster.py` | investable universe roster |

## Entrypoints
```bash
python -m valuation_alpha.pipeline --run
python -m opencode_scripts.eval_googl_valuation_alpha   # example evaluation (see opencode_scripts/)
```

## Dependencies
`valuation/` (V1 kernel), `Quantitative/` (WACC, survival), `data/sec_facts/`,
`config/xbrl_concept_map.json`, `data/pit_fundamentals/`.

## Efficient Pathways
- `universe_build`: `universe/discovery.py` + `universe/roster.py`
- `full_pipeline`: `pipeline.py` is the production driver
- `gate_audit`: `moat_gate.py` output feeds Arm C moat condition
  (`ROIC_cap_rd > WACC + 0.02 AND OMR > 0.5`)