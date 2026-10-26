# `dashboard/` — Streamlit Risk & Sentiment Tabs

## Roadmap Position
Pipeline stage 7 (monitoring): the live dashboard layer. `stream_quant.py`
is the Streamlit entry; `tab_*` modules provide the risk, sentiment, and
stochastic valuation tabs.

## Folder Summary
Five-file folder. `stream_quant.py` wires the app; `tab_stochastic_risk.py` and
`tab_sentiment_risk.py` render risk surfaces; `tab_valuation_alpha.py` renders
valuation-alpha results; `stochastic_risk_service.py` serves the stochastic
risk data.

## Skeleton
```
dashboard/
  stream_quant.py  tab_sentiment_risk.py  tab_stochastic_risk.py
  tab_valuation_alpha.py  stochastic_risk_service.py
```

## Files

| File | Purpose |
|---|---|
| `stream_quant.py` | Streamlit entrypoint |
| `tab_stochastic_risk.py` | stochastic risk tab |
| `tab_sentiment_risk.py` | sentiment risk tab |
| `tab_valuation_alpha.py` | valuation-alpha tab |
| `stochastic_risk_service.py` | stochastic risk data service |

## Entrypoints
```bash
streamlit run dashboard/stream_quant.py
```

## Dependencies
`Quantitative/stochastic/` (models), `data/regime/regime_daily_nowcast.parquet`,
`data/backtests/`, `center/` artifacts.

## Efficient Pathways
- `full_pipeline`: monitoring reads `nowcast.parquet` + rolling metrics — keep
  schemas stable; dashboard code is presentation-only (no analysis logic).