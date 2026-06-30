# Psychological & Academic Graph Ingestion Architecture (Opus 4.6)

## Overview
Primary Quantitative Portfolio Architecture Engine. Integrates qualitative parameters (Moats, Culture, Leadership) to directly drive quantitative DCF valuation inputs as modulating coefficients. Uses a decoupled two-tier matrix system separating Intrinsic Valuation drivers from Pricing & Momentum indicators.

---

## 4-Lane Parallel Valuation Matrix Integration

To avoid the **"Bermuda Triangle of Valuation"**, the quantitative pipeline strictly segregates intrinsic valuation drivers from pricing/momentum vectors:

```
                            ┌────────────────────────┐
                            │  QUALITATIVE MAPPING   │
                            └───────────┬────────────┘
                                        │
           ┌────────────────────────────┼────────────────────────────┐
           ▼                            ▼                            ▼
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│  Lane 1 (Intrinsic)  │     │   Lane 2 (Pricing)   │     │    Lane 4 (Audit)    │
│  - Culture / Moat    │     │  - HypeComposite     │     │  - Anti-leakage      │
│    Fuzzy Translators │     │    (21-day EMA)      │     │    sentiment sweep   │
└──────────────────────┘     └──────────────────────┘     └──────────────────────┘
```

1. **Lane 1: Intrinsic Valuation Matrix Engine (What to Buy)**:
   * **Narrative-to-Numbers Translation**: Uses `QualitativeProbabilisticTranslator` to map `MoatComposite` into mean and variance for the Competitive Advantage Period ($N_{\text{CAP}}$) distribution shape, preventing step boundary line volatility.
   * **Reinvestment & Capital Efficiency**: Modulates Return on Invested Capital (ROIC) and Expected Fundamental Growth ($g = \text{Reinvestment Rate} \times \text{ROIC}$), capitalizing R&D assets over 3-to-5 year timelines.
   * **Discount Rate Adjustments**: Maps `CultureComposite` and `LeadershipStability` to reduce Equity Risk Premium (ERP) and Cost of Capital, and maps Interest Coverage Ratio to automated Cost of Debt ($R_d$).

2. **Lane 2: Market Mood & Pricing Matrix (When to Buy)**:
   * **Sentiment & Hype Ingestion**: The `HypeComposite` (WSB/Reddit) is isolated to a 21-day half-life EMA to track short-term timing catalysts.
   * **Relative Companion Regressions**: Runs cross-sectional regressions of multiples against Companion Variables (e.g. EV/Sales vs Operating Margin, Price/Book vs ROE) to define Justified Multiples and Pricing Deviation Deltas.

3. **Lane 3: Probabilistic Banding (Monte Carlo Simulation)**:
   * Runs a **10,000-pass Monte Carlo simulation** randomizing Expected Growth ($g$), Operating Margin ($Margin$), and $N_{\text{CAP}}$.
   * Rejects deterministic 1-10 scores, outputting conviction as a probability band (e.g. $P(\text{EVA} > 0 \mid t=5)$).

4. **Lane 4: Framework Audit & Verification Gate**:
   * **Anti-Contamination Checks**: Audits Lane 1 and 2 databases to ensure zero sentiment leakage from forums into intrinsic DCF parameters.
   * **Terminal Stability Rule**: Enforces decay of terminal Year 10 ROIC to the industry baseline cost of capital (WACC).

---

## B2B Knowledge Graph Ingestion Layer (Source C Refactoring)
*   **OpenAlex & Semantic Scholar REST APIs**: Text-mines the academic abstract_inverted_index and citations block. Implements a textual keyword co-occurrence engine to detect structural bottlenecks (e.g. electomigration, substrate packaging thermal limits) before they register in commercial patent applications:
    $$\text{Association}_{\text{Tech-Node}} = \frac{\text{Count}(L_{\text{Corporate Lab}} \cap C_{\text{Technical Failure Key}})}{\text{Total Paper Segment Base}}$$
*   **SEC Form SD (Conflict Minerals)**: Ingests global unredacted tier-3 and tier-4 processing refiners and names.
*   **Customs Manifest Logs & Bills of Lading**: Pulls Harmonized System (HS) Commodity Codes and shipment origin/destination container logs to track real-time supplier cargo weight allocation.

---

## Skeleton Structure & Module Layout

### `psychological/`
* **`__init__.py`**: Core entry point, exporting interfaces, orchestration models, and primary logic classes.
* **`interfaces.py`**: Strong-typed schemas and data structures (TypedDict formats) ensuring interface compliance across metrics.
* **`nlp_engine.py`**: Sentiment engine using VADER with custom financial slang lexicons, bull/bear ratios, and ZeroDivisionError handling.
* **`velocity_tracker.py`**: Tracking of sentiment velocity over 1h, 4h, and 24h rolling windows, calculating Sigma deviations and acceleration.
* **`state_machine.py`**: Logic for transitioning between psychological regimes:
  * `PANIC_CAPITULATION` (contrarian buy authorized)
  * `CROWD_EUPHORIA` (lockdown)
  * `ASYMMETRIC_DIVERGENCE` (moat warning)
  * `APATHY` (idle)
* **`behavioral_feature_store.py`**: SQLite storage for sentiment vectors/regimes and daily Parquet exporter for backtesting compatibility.
* **`data_fusion.py`**: Integrates text vectors, sentiment ratios, and confidence penalties.
* **`signal_matrix.py`**: Multi-state regime mapping integrated with DCF Floor valuations.
* **`dcf_floor.py`**: Stub/interface for advanced intrinsic value and margin of safety calculations.
* **`engineering_guards.py`**: System-wide sanity checks, timezone UTC enforcement, NaN guards, and rate limit rules.
* **`orchestrator.py`**: Coordinates data streams from scrapers, NLP execution, and state machines, feeding outputs to the feature store.

### `scraper/` Core & Extraction Engine
* **`dynamic_extractor.py`**: Template-free semantic extraction engine using Gemini API (`gemini-2.5-flash` / `gemini-1.5-flash`) to dynamically parse structured JSON data from raw HTML/Markdown without rigid CSS selectors.
* **`product_intel.py`**: Product health scraping for G2, Capterra, and App Store reviews.
* **`engine.py`**: Financial sentiment engine with VADER + custom lexicons.
* **`data_fusion.py`**: Signal fusion engine combining fintech indicators and scraper outputs.
* **`health_monitor.py`**: Circuit breaker & health tracking monitoring scraper reliability.
* **`hybrid_orchestrator.py`**: Orchestrates secondary fintech data streams and scrapers.

### `psychological/scrapers/`
* **`lightweight_scraper.py`**: Shared headless browser footprint utilizing SeleniumBase UC (Undetected ChromeDriver) for anti-bot bypass.
* **`reddit_primary.py`**: Main scraper for comment harvest across designated trading subreddits.
* **`reddit_custom.py`**: old.reddit taxonomy-scoped SeleniumBase harvester.
* **`github_tracker.py`**: GitHub REST API tracker analyzing star velocity, fork acceleration, and commit frequencies.
* **`corp_anonymous.py`**: Adzuna job search API client acting as a hiring volume delta proxy.
* **`corp_audit.py`**: Glassdoor (75% anchor), Indeed, and G2 scrapers for corporate health/employee sentiment cross-validation.
* **`hiring_velocity.py`**: JobSpy dual-engine tracking 30-day delta and 1-year job posting Z-scores.
* **`validation_gate.py`**: Confidence decay gate applying mathematical penalties to noisy/stale indicators.
* **`cross_validation.py`**: Multi-layered validation gates:
  * Layer 1: Glassdoor ↔ Indeed ↔ G2 (75/25 weighted employee sentiment alignment)
  * Layer 2: JobSpy ↔ GitHub (Operational/dev activity alignment)
  * Layer 3: Product Intel ↔ Reddit (Public opinion alignment)
  * Layer 4: DCF Floor ↔ Regime (Valuation safety alignment)

---

## Configuration Reference

```yaml
github_mappings:
  NVDA: "nvidia/cuda-samples"
  AMD: "AMD/ROCm"
  MSFT: "microsoft/vscode"
  GOOGL: "google/jax"
  META: "facebookresearch/llama"
  TSLA: "teslamotors/tesla-firmware"

adzuna:
  app_id: "${ADZUNA_APP_ID}"
  app_key: "${ADZUNA_APP_KEY}"
  base_url: "https://api.adzuna.com/v1/api/jobs"
  country: "us"

psychological:
  bullish_terms:
    "call": 1.0
    "moon": 1.5
    "long": 1.0
    "tendies": 1.2
    "diamond hands": 2.0
    "yolo": 1.0
    "undervalued": 1.0
    "bullish": 1.5
    "rip": 1.0
    "green": 0.5

  bearish_terms:
    "put": 1.0
    "crash": 1.5
    "short": 1.0
    "bagholder": 1.5
    "paper hands": 2.0
    "overvalued": 1.0
    "dump": 1.2
    "bearish": 1.5
    "red": 0.5
    "tank": 1.0

  velocity_windows:
    short_hours: 1
    medium_hours: 4
    long_hours: 24

  regime_thresholds:
    panic_ratio: 0.25
    panic_velocity_sigma: 2.0
    euphoria_ratio: 4.0
    euphoria_velocity_sigma: 2.5
    apathy_ratio_min: 0.8
    apathy_ratio_max: 1.5
    apathy_velocity_sigma: 0.5
    asymmetric_employee_sigma: -1.5
    asymmetric_git_velocity_sigma: 1.0

  fusion_weights:
    psychological_regime: 0.60
    fintech_confirmation: 0.25
    quantitative_value: 0.15
```

---

## Database Schema Reference

```sql
-- psychological_vectors
CREATE TABLE psychological_vectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    source_provenance TEXT NOT NULL,
    raw_text TEXT,
    compound_vader REAL,
    bull_bear_ratio REAL,
    bullish_count INTEGER,
    bearish_count INTEGER,
    mention_velocity REAL,
    comment_volume_sigma REAL,
    acceleration REAL,
    employee_sentiment_proxy REAL,
    dev_fork_acceleration REAL,
    metadata_json TEXT
);
CREATE INDEX idx_psych_ticker_time ON psychological_vectors(ticker, timestamp);

-- psychological_regimes
CREATE TABLE psychological_regimes (
    ticker TEXT,
    date TEXT,
    active_regime TEXT,
    contrarian_buy_authorized BOOLEAN,
    confidence_score REAL,
    bull_bear_ratio REAL,
    velocity_sigma REAL,
    employee_sentiment_proxy REAL,
    dev_velocity REAL,
    fintech_confirmation_score REAL,
    quantitative_value_signal REAL,
    PRIMARY KEY (ticker, date)
);
```

---

## Key Architectural Decisions

| Decision | Resolution |
|----------|------------|
| GitHub Mapping | Centralized in `config/hybrid_config.yaml` under `github_mappings` |
| Employee Sentiment | Glassdoor (75% anchor) + Indeed & G2 (25% cross-validation pillar) + Adzuna Job Search API |
| Historical Backfill | Dual-regime: offline 2021-2026 for training; live Tue/Fri deltas forward |
| Storage | SQLite write-path + periodic Parquet exports for Optuna/backtesting |
| Fintech Integration | Regime-level blending (state machine output ↔ DCF/moat validation) |
| Scraping Engine | Headless SeleniumBase UC + **Gemini LLM Dynamic Extractor** (`dynamic_extractor.py`) for template-free parsing |
| Repository Organization | Structured modular layout isolating `dashboard/`, `scraper/`, `tests/`, and `opencode_scripts/lanes/` |
| Verification & Health | 310+ automated tests passing across cross-validation layers, state machines, and dynamic extractors |

---

## Risk Mitigation

| Risk | Mitigation |
|----------|------------|
| Reddit API rate limits | SeleniumBase UC scraping old.reddit with adaptive delays (12-25s) |
| GitHub API limits | Token authentication (5000 requests/hr) with 1h local cache |
| Adzuna API limits | Free tier caching (24h) + fallback to public careers index scrapers |
| Historical data volume | Partitioned SQLite storage and compressed Parquet serialization |
| Regime false positives | Multi-layered CrossValidationGate requiring alignment before execution |
| Website Template Drift | Dynamic Gemini LLM extraction converting raw DOM into structured JSON |