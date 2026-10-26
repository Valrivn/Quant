# `discovery/` — Universe Discovery: Frontier, Consensus, Sentinel

## Roadmap Position
Pipeline stage 1.5 (discovery): sourcing new names before they enter the
universe — wiki-based frontier screens, consensus lanes, EDGAR ingestion, and
the Sentinel four-lane monitoring system. Feeds `universe_builder.py` and the
qualitative lanes.

## Folder Summary
Top-level modules cover the discovery stack: `frontier.py` / `wiki_frontier.py`
(each-company screens), `consensus/` (multi-source consensus engine: collectors,
config, engine, pipeline, store), `sentinel/` (altdata/ sec/ enrich/ github
lanes under a governor/orchestrator/queue), `edgar_ingestion/`, plus scanners
(`census.py`, `damodaran.py`, `gdelt.py`, `job_boards.py`, `video_sources.py`,
`structured_sources.py`), taxonomy tools (`taxonomy_loader.py`,
`sector_xbrl_chains.py`), and the ledger/ranker (`ledger.py`, `ranker.py`).

## Skeleton
```
discovery/
  consensus/ sentinel/ edgar_ingestion/
  frontier.py wiki_frontier.py census.py damodaran.py gdelt.py job_boards.py
  taxonomy_loader.py sector_xbrl_chains.py ledger.py ranker.py ...
```

## Files (key)

| File | Purpose |
|---|---|
| `consensus/engine.py` | multi-source consensus scoring driver |
| `sentinel/orchestrator.py` | four-lane monitoring orchestrator |
| `frontier.py` / `wiki_frontier.py` | frontier-name screens |
| `ledger.py` / `ranker.py` | discovery ledger + ranking |
| `sector_xbrl_chains.py` | XBRL sector concept chains (config + docs in `docs/data/`) |
| `damodaran.py` | Damodaran data access (config + sha256 in `config/`) |
| `gdelt.py` | GDELT event ingest |
| `wayback_pit.py` | Wayback Machine PIT collection |

## Entrypoints
```bash
python -m discovery.consensus.pipeline --run
python -m discovery.sentinel.orchestrator --run
python -m discovery.wiki_frontier --run
```

## Dependencies
`config/sentinel.yaml`, `config/frontier_edges.yaml`, `config/weights_discovery.yaml`,
`ingestion/` (loaders), `data/mappings/` (ticker mappings).

## Efficient Pathways
- `universe_build`: discovery frontier → consensus → `ingestion/universe_builder.py`
- `full_pipeline`: sentinel monitors signals after names enter the universe
- `gate_audit`: `sector_xbrl_chains.py` supplies structural concept chains for XBRL validation