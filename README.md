# Quant-Py

A three-pillar quantitative investment research platform: social-sentiment scraping, alternative data NLP, and Monte Carlo simulation with stochastic risk models — fused into conviction-scored, risk-adjusted portfolio allocations.

## Pipeline Overview

- **Quantitative** — ETF screening (bonds, gold, equities), stochastic risk models (Bernoulli shocks, Markov lifecycle, Poisson black swans), tactical allocation.
- **Qualitative** — Reddit/social sentiment NLP, alternative data scrapers (Glassdoor, G2, GitHub, SEC EDGAR), psychological regime classification, multi-source Bayesian fusion.
- **Monte Carlo engine** — intrinsic-value simulations integrating all three stochastic models with sector shock probabilities (yfinance EBIT + Bayesian shrinkage).
- **Dashboard** — Streamlit app for portfolio overview, sentiment & risk visualization, conviction scoring.

## Quick Start

```bash
pip install -r requirements.txt
python scripts/migrate_db.py        # Initialize database
python scraper/run_scraper.py scrape  # Run full pipeline
streamlit run dashboard/stream_quant.py  # Launch dashboard
python -m pytest tests/ -v          # Run tests
```

## Stochastic Models

1. **Bernoulli Shock Filter** — per-company shock trial via Damodaran ICR→default probability tables, with Balance Sheet Resilience Modifier (M_health) and sector operational shock probability.
2. **Markov Lifecycle Chain** — 6-state corporate lifecycle transitions (FAST_GROWER → ASSET_PLAY) with dynamic transition matrices.
3. **Poisson Black Swan** — systemic event counts via Poisson process with regime-aware lambda scaling from credit spreads.

## Results (2026-08-30)

- **Tests:** `1332 passed · 18 skipped · 0 failed` — 100% pass rate on executed tests.
- **Discovery pipeline (live run):** Thread-B parallel lane drew **1,339 candidates** across same-beta-band peer industries → **517 passing survivors** after the reverse-heatmap screen.
- **Quantitative baseline:** 500-ticker cohort with 3Y annualized FF5 alpha scores (`center/hybrid_quant_500.md`).

<details><summary>Errors, warnings & remaining issues (detail)</summary>

**Test warnings**
- `datetime.utcnow()` deprecation across several modules (needs timezone-aware UTC migration).
- NumPy "Degrees of freedom <= 0" / "invalid value in scalar divide" in DoubleStandardizer audit paths.
- transformers WordPiece tokenizer deprecation.

**Data-source issues (scrape-time, non-fatal)**
- AVGO completeness 0/4 sources (SEC/GitHub/Reddit missing; Glassdoor blocked `403`).
- Wikidata global coverage `DEGRADED` on transient HTTP 504 (degrades cleanly, never crashes).
- GitHub repo 404s (web-UI fallback), Adzuna 401, Nodriver CDP attach failures, Browserless down.

**Remaining issues / roadmap**
- 18 skipped tests (GPU / live-service dependent, not exercised locally).
- PIT certification coverage thin — ~1.9% of crawled edges are dated (`available_as_of`).
- Wikidata↔Damodaran industry label mismatch bridged by an aliases config; mapping needs periodic extension.
- `discovery/wayback_pit.py` (Wayback PIT rating harvester) built + tested but uncommitted.
- `feature/educator-agent` holds uncommitted `db/schema.py` / `db/connection.py` changes awaiting commit decision.

</details>

## Repository Layout

<details><summary>File tree</summary>

```
quant-py/
├── config/                          # YAML configs, constants, lexicons, credentials
│   ├── hybrid_config.yaml           # Master orchestration config (subsectors, tickers, weights)
│   ├── hybrid_weights.yaml          # Source/category weight tuning
│   ├── reddit_weights.yaml          # Subreddit & category weights
│   ├── etf_config.yaml              # ETF pipeline config
│   ├── constants.py                 # Blacklists, financial lexicon, risk keywords, entity resolution
│   ├── weights.py                   # YAML config loader utilities
│   ├── logging_config.py            # Structured logging with rotation
│   └── *_credentials.yaml           # API credentials (Reddit, StockTwits, ApeWisdom)
│
├── db/                              # SQLite database layer (WAL mode)
│   ├── connection.py                # Thread-local connection pool
│   ├── schema.py                    # Table definitions, partitioning, indexes (601 lines)
│   ├── schema_fintech.py            # Fintech API tables + hybrid orchestration schema
│   ├── feature_store.py             # Per-category weighted sentiment feature queries
│   └── jobs.py                      # Daily aggregation, purge old submissions
│
├── scraper/                         # Core scraping & NLP pipeline
│   ├── engine.py                    # VADER + financial lexicon sentiment engine
│   ├── reddit_client.py             # API-free Reddit JSON scraper + PRAW fallback
│   ├── risk_detector.py             # Z-score risk narrative detection
│   ├── hybrid_orchestrator.py       # Reddit + fintech + psychological fusion
│   ├── sec_scraper.py               # SEC EDGAR FastAPI scraper
│   ├── product_intel.py             # G2, Capterra, App Store scrapers
│   └── fintech_clients/             # StockTwits, ApeWisdom API clients + rate limiter
│
├── Qualitative/                     # Qualitative Pillar: alternative data + NLP
│   ├── psychological/               # Signal processing, scrapers, NLP
│   │   ├── monte_carlo.py           # Monte Carlo engine integrating all 3 stochastic models (576 lines)
│   │   ├── four_lane_pipeline.py    # Four-lane qualitative→quantitative fusion (976 lines)
│   │   ├── qualitative_scoring.py   # EMA filters, composites, DoubleStandardizer (931 lines)
│   │   ├── bayesian_calibration.py  # Walk-forward Bayesian cross-validation (1029 lines)
│   │   ├── nlp_engine.py            # VADER + financial lexicon NLP
│   │   ├── velocity_tracker.py      # Mention velocity with rolling windows
│   │   ├── state_machine.py         # Psychological regime classification
│   │   ├── behavioral_feature_store.py  # SQLite-backed feature vectors
│   │   ├── signal_matrix.py         # Contrarian buy/hold/reduce directives
│   │   ├── dcf_floor.py             # DCF intrinsic value floor/ceiling
│   │   ├── data_fusion.py           # Multi-source fusion with provenance
│   │   └── scrapers/                # Reddit, GitHub, Glassdoor, G2, hiring, moat scrapers
│   └── scraper/                     # Legacy scraper + fintech API clients
│
├── Quantitative/                    # Quantitative Pillar: ETF selection, stochastic models
│   ├── stochastic/                  # *** Stochastic risk models ***
│   │   ├── default_probability_table.py  # Damodaran ICR→rating→p_default (14 tiers)
│   │   ├── bernoulli_shock_filter.py     # Per-company shock trial + M_health modifier
│   │   ├── markov_lifecycle.py           # 6-state Markov chain (FAST_GROWER→ASSET_PLAY)
│   │   ├── poisson_blackswan.py          # Poisson process for systemic black swan counts
│   │   ├── sector_shock_data.py          # Bayesian-shrunk sector shock probabilities
│   │   └── generate_sector_shock_data.py # One-time yfinance data generation
│   ├── bonds/                       # Bond ETF screener + credit spread monitor
│   ├── gold_etf/                    # Gold ETF screener + macro valuation
│   ├── dividends/                   # Dividend safety + FCFE protection yield
│   ├── fragility/                   # Sector fragility + power-law overlay
│   ├── funds/                       # Index fund hold-through analysis
│   ├── allocation/                  # Tactical rebalancer + Fidelity order drafts
│   ├── sensitivity/                 # Min-Max sensitivity vectors
│   ├── audit/                       # Data provenance + allocation audit
│   ├── shared/                      # FRED scraper, ETF data fetcher
│   └── company_classifier.py        # Lynch/Damodaran company classification
│
├── optimization/                    # Bayesian weight optimization
│   ├── optuna_search.py             # Optuna hyperparameter search
│   └── ab_testing.py                # Champion/Challenger A/B testing
│
├── backtesting/                     # Walk-forward backtesting
│   ├── backtest.py                  # Monthly rebalancing sim (IC, Sharpe, Hit Rate)
│   └── drift_detection.py           # IC decay monitoring + auto-reoptimization
│
├── dashboard/                       # Streamlit visualization
│   ├── stream_quant.py              # Main dashboard app (889 lines)
│   └── tab_sentiment_risk.py        # Social Sentiment & Risk tab
│
├── opencode_scripts/                # Autonomous agent orchestration
│   ├── antigravity_daemon.py        # Self-healing daemon: AST scan + auto-fix
│   ├── lane_delta_pipeline.py       # Monte Carlo conviction scoring (523 lines)
│   ├── overnight_runner.py          # Parallel lane execution
│   ├── lanes/                       # 8+ parallel worker lanes (alpha→zeta + omega audit)
│   └── *.sh                         # launchd, watchdog, cleanup scripts
│
├── scripts/                         # Automation
│   ├── scheduler.py                 # APScheduler: daily/weekly/bi-weekly jobs
│   ├── migrate_db.py                # Versioned DB migrations
│   └── seed_historical.py           # Synthetic baseline data seeder
│
├── tests/                           # ~43 test files
│   ├── test_stochastic_models.py    # Bernoulli, Markov, Poisson tests (879 lines)
│   ├── test_qualitative_scoring.py  # EMA, composites, Lane Alpha tests (623 lines)
│   ├── test_monte_carlo.py          # Monte Carlo simulation tests
│   ├── test_master_qualitative_audit.py  # Cross-lane audit across 10 tickers
│   └── test_*.py                    # Unit + integration tests for all components
│
├── data/                            # Runtime data
│   ├── reddit_quant.db              # Primary SQLite database
│   ├── sector_shock_probs.json      # Empirical sector shock probabilities
│   ├── sector_ebit_history.csv      # Raw EBIT panel data
│   └── historical_5y_baseline.csv   # 5-year baseline for backtesting
│
├── center/                          # Audit reports & conviction outputs
│   ├── conviction_scores.md         # Asset conviction ratings
│   ├── lane_summary.md              # Lane execution summary
│   └── *.md, *.json                 # Audit reports, scoring metrics
│
├── lane_results/                    # Multi-lane execution outputs
│   └── chats/                       # ~50+ chat transcripts from parallel lanes
│
└── docs/, Instructions/            # Design documents, compliance, architecture
```

</details>

## License

Internal use only.