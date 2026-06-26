# Psychological Pillar Implementation - Repository Map & Reference

## Overview
Primary Contrarian Sentiment Engine implementation. Uses Reddit/WSB as primary psychological indicator, with secondary validation from GitHub repository tracking, job scraping, Glassdoor/Comparably employee metrics, and G2/Capterra/App Store product intelligence.

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

### `psychological/scrapers/`
* **`lightweight_scraper.py`**: Shared headless browser footprint utilizing SeleniumBase UC (Undetected ChromeDriver) for anti-bot bypass.
* **`reddit_primary.py`**: Main scraper for comment harvest across designated trading subreddits.
* **`reddit_custom.py`**: old.reddit taxonomy-scoped SeleniumBase harvester.
* **`github_tracker.py`**: GitHub REST API tracker analyzing star velocity, fork acceleration, and commit frequencies.
* **`corp_anonymous.py`**: Adzuna job search API client acting as a hiring volume delta proxy.
* **`corp_audit.py`**: Glassdoor and Comparably scrapers for corporate health/employee sentiment.
* **`hiring_velocity.py`**: JobSpy dual-engine tracking 30-day delta and 1-year job posting Z-scores.
* **`product_intel.py`**: Product health scraping for G2, Capterra, and App Store reviews.
* **`validation_gate.py`**: Confidence decay gate applying mathematical penalties to noisy/stale indicators.
* **`cross_validation.py`**: Multi-layered validation gates:
  * Layer 1: Glassdoor ↔ Comparably (Employee sentiment alignment)
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
| Employee Sentiment | Adzuna Job Search API + public career index (keyless proxy) |
| Historical Backfill | Dual-regime: offline 2021-2026 for training; live Tue/Fri deltas forward |
| Storage | SQLite write-path + periodic Parquet exports for Optuna/backtesting |
| Fintech Integration | Regime-level blending (state machine output ↔ DCF/moat validation) |
| Scraping Engine | Full PRAW replacement with headless SeleniumBase UC to bypass Cloudflare anti-bot mechanisms |

---

## Risk Mitigation

| Risk | Mitigation |
|----------|------------|
| Reddit API rate limits | SeleniumBase UC scraping old.reddit with adaptive delays (12-25s) |
| GitHub API limits | Token authentication (5000 requests/hr) with 1h local cache |
| Adzuna API limits | Free tier caching (24h) + fallback to public careers index scrapers |
| Historical data volume | Partitioned SQLite storage and compressed Parquet serialization |
| Regime false positives | Multi-layered CrossValidationGate requiring alignment before execution |