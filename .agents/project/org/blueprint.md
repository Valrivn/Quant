# Master Blueprint — Quant-Py

**Custodian:** big-pickle. Updated after every T3 ruling and every structural
change. Agents read this file for project facts — do not bake facts into
prompts.

## Mission

Three-pillar quantitative investment research: Reddit/social alternative-data
NLP + stochastic risk modeling (Monte Carlo) + conviction-scored portfolio
allocations, surfaced through a Streamlit dashboard.

## Pillars

1. **Qualitative** — Reddit/social sentiment NLP, alternative-data scrapers
   (Glassdoor, G2, GitHub, SEC EDGAR, StockTwits, ApeWisdom), psychological
   regime state machine, multi-source Bayesian fusion.
2. **Quantitative** — ETF screening (bonds, gold, equities), stochastic models
   (Bernoulli shock filter, Markov lifecycle, Poisson black swan), sensitivity
   analysis, tactical allocation.
3. **Dashboard** — Streamlit app: portfolio overview, sentiment & risk, conviction.

## Module map (top-level)

| Path | Responsibility |
|------|----------------|
| `scraper/` | sentiment engine, reddit client, risk detector, hybrid orchestrator, SEC EDGAR, product intel, fintech clients |
| `Qualitative/psychological/` | monte_carlo, four_lane_pipeline, qualitative_scoring, bayesian_calibration, nlp_engine, velocity_tracker, state_machine, behavioral_feature_store, signal_matrix, dcf_floor, data_fusion |
| `Quantitative/` | stochastic/, bonds/, gold_etf/, dividends/, fragility/, funds/, allocation/, sensitivity/, audit/, shared/, company_classifier |
| `optimization/` | optuna_search, ab_testing (champion/challenger) |
| `backtesting/` | monthly rebalancing sim (IC, Sharpe, Hit Rate), drift detection |
| `dashboard/` | stream_quant.py (+ tab_sentiment_risk) |
| `db/` | SQLite WAL layer: connection, schema (+ fintech), feature_store, jobs |
| `config/` | YAML configs, constants, weights, credentials (git-ignored) |
| `scripts/` | scheduler (APScheduler), migrate_db, seed_historical |
| `tests/` | ~43 test files — the quality gate's raw material |
| `center/` | audit reports + conviction scores |
| `lane_results/` | legacy parallel-lane outputs (superseded) |

## Architecture invariants (T3-grade; never violated without a ruling)

1. **DB-first, WAL mode.** All persistence through `db/` layer; threads use
   thread-local connections. Never open raw connections ad hoc.
2. **Provenance everywhere.** Every fused score carries source provenance;
   audit module can trace any number to its inputs.
3. **Credentials only in `config/*_credentials.yaml`, git-ignored.** Never in
   code, comments, or committed files.
4. **Weight changes go through `config/weights*.yaml`**, not hard-coded constants.
5. **Anything decision-critical ships with tests.** New modules require a test
   file; pass rate target ≥90% (quality-gate.md).
6. **No secrets in logs.** Structured logging via `config/logging_config.py`.

## Stochastic core (non-negotiable math)

- **Bernoulli shock filter:** Damodaran ICR→rating→default-probability (14
  tiers) + balance-sheet resilience modifier M_health + sector shock prob.
- **Markov lifecycle:** 6 states (FAST_GROWER→…→ASSET_PLAY), dynamic matrices.
- **Poisson black swan:** systemic event counts, regime-aware λ from credit spreads.
- Sector shock probs: Bayesian-shrunk from yfinance EBIT (Beta(2,98) prior).

## Data sources

Reddit, StockTwits, ApeWisdom, Glassdoor, G2, Capterra, App Store, GitHub,
SEC EDGAR, FRED, yfinance. Credentials: Reddit, StockTwits, ApeWisdom.

## Open questions / contested areas (escalate to T3)

- Lane fusion weights drifting after regime shifts — revisit via
  bayesian_calibration + drift_detection.
- Whether psychological state_machine should gate allocation or only flag.
- Dashboard load patterns (Streamlit) vs compute cost of MC simulations.

## Decision history

See `.agents/project/org/decisions/index.md` for every CEO ruling. Append a
ruler block here for rulings that change structure or invariants.

```text
## Ruling D-YYYYMMDD-### (T3)
SUMMARY: <what changed>  |  BY: CEO  |  EFFECT: <blueprint delta>
```
