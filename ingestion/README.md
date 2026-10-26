# `ingestion/` — Data Lake, Loaders, Provenance, and the Unified Gate Harness

## Roadmap Position
Pipeline stage 1: raw + curated data intake (SEC/XBRL, FRED/ALFRED, qualitative
lanes) with **PIT discipline and provenance hashing**, then the Sprint 6
harness (three-arm construction + composite-DSR gate) that sits between data
and validation.

## Folder Summary
Split into `persistence/` (merged-universe persistence) and `harness/`
(pre-registration-compliant gate engine, three-arm runner, PIT validator,
factor/strategy stores). The top-level modules are the loaders and builders:
`fred_alfred.py`, `sec_xbrl_full_extractor.py`, `universe_builder.py`,
`wacc_engine_v2.py`, `orc_pit_*`, scrapers (`apewisdom_scraper.py`,
`ats_rss_loader.py`, etc.), and the provenance chain
(`pit_provenance.py`, `trade_audit.py`).

## Skeleton
```
ingestion/
  harness/            # Sprint 6: gate_engine, three_arm_runner, prereg, feature_gate, pit_validator, factor_store, strategy_store
  persistence/        # merged_universe.py
  fred_alfred.py      # FRED/ALFRED ingestion
  pit_provenance.py   # PIT + provenance hashing
  universe_builder.py # master universe construction
  wacc_engine_v2.py   # WACC panel (v2)
  sec_*               # SEC/XBRL extractors, parsers, mappers
  ... (loaders/scrapers)
```

## Files (top-level, abridged)

| File | Purpose |
|---|---|
| `harness/` | see `ingestion/harness/DEEP_DIVE.md` |
| `persistence/merged_universe.py` | merged PIT universe writer/reader |
| `fred_alfred.py` | FRED/ALFRED series ingestion (see `config/fred_schedule.json`) |
| `pit_provenance.py` | provenance + PIT sidecar generation |
| `universe_builder.py` | master universe build (see `scripts/build_master_universe_v2.py`) |
| `wacc_engine_v2.py` | WACC by sector/firm (feeds `ROIC_WACC_Spread`) |
| `sec_xbrl_full_extractor.py` | full XBRL fact extraction (see `data/sec_facts/`) |
| `sec_concept_mapper.py` | XBRL concept → canonical metric mapping (see `config/xbrl_concept_map.json`) |
| `beta_calculator.py`, `sector_stats.py`, `factor_computer.py` | factor/risk panels |
| `price_cache.py` | price cache (see `data/prices/price_cache.parquet`) |
| `quality_thresholds.py` | quality threshold defaults |
| `trade_audit.py` | trading-activity audit |
| `unfunded_watchlist.py` | cash-runway watchlist (Arm C risk penalty input) |

## Entrypoints
```bash
python -m ingestion.harness.three_arm_runner   # arms A/B/C monthly PIT construction
python -m ingestion.harness.prereg             # frozen config + integrity gate
python -m validation.gate_calibration (see validation/README.md)
```

## Dependencies
`Quantitative/contracts/feature_contract.py` (canonical features, forbidden
series), `config/pre_registration_v7.yaml` (frozen), `config/xbrl_concept_map.json`,
`data/` (parquet panels + DuckDB stores).

## Efficient Pathways
- `universe_build`: `universe_builder.py` + `persistence/merged_universe.py`
- `provenance_audit`: `pit_provenance.py` + `data/provenance/manifest.jsonl`
- `three_arm_backtest`: `harness/three_arm_runner.py` (Arms A/B/C) → `validation/composite_score.py`
- `gate_audit`: `harness/gate_engine.py` + `harness/prereg.py` (SHA-256 pin)