# `Qualitative/` — Qualitative Research: Psychological Lines + Scrapers

## Roadmap Position
Pipeline stage 1 (qualitative intake): alternate-data research — the
`psychological/` package (scrapers, sentiment training, scoring) and the
`scraper/` package (browserless infra). Outputs feed
`Quantitative/quality_composite.py` and Arm B's qualitative factors.

## Folder Summary
`psychological/` holds the four-lane research machine: `scrapers/` (reddit,
instagram, glassdoor, finbert, github, velocity), `sentiment_training/`
(distill-reddit, gemini labeling, model training), `four_lane_pipeline.py`,
`qualitative_scoring.py`, `nlp_engine.py`, `signal_matrix.py`, `orchestrator.py`,
plus behavioral/monte-carlo layers. `scraper/` holds the browserless infra
(Dockerfile, `engine.py`, `run_scraper.py`, `hybrid_orchestrator.py`,
`risk_detector.py`, fintech_clients incl. apewisdom + stocktwits).
`Qualitative Blueprint.md` is the design doc.

## Skeleton
```
Qualitative/
  psychological/    # scrapers/ sentiment_training/ four_lane_pipeline.py qualitative_scoring.py ...
  scraper/          # engine.py run_scraper.py Dockerfile.browserless fintech_clients/ ...
  Qualitative Blueprint.md
```

## Files (key)

| File | Purpose |
|---|---|
| `psychological/four_lane_pipeline.py` | four-lane research pipeline driver |
| `psychological/qualitative_scoring.py` | qualitative composite scoring |
| `psychological/sentiment_training/train_reddit_model.py` | reddit sentiment model training |
| `scraper/hybrid_orchestrator.py` | scraper orchestration |
| `scraper/engine.py` | scraper engine (browserless) |
| `scraper/fintech_clients/apewisdom.py` | ApeWisdom client (Tier-3 contrarian, ≤5% weight) |

## Entrypoints
```bash
python -m Qualitative.psychological.four_lane_pipeline --run
python -m Qualitative.scraper.run_scraper --run
```

## Dependencies
`config/fintech_credentials.yaml`, `config/reddit_credentials.yaml`,
`config/weights_sentiment_bars.yaml`, `data/qual/` (parquet + provenance sidecars),
`data/sentiment_training_cache/`.

## Efficient Pathways
- `universe_build`: qualitative lanes gate early names; ApeWisdom is contrarian-only (MAP-01)
- `full_pipeline`: qualitative factors feed Arm B composite (missing → 0.0 neutral fill)
- `provenance_audit`: every `data/qual/*.parquet` carries a `.provenance.json` sidecar