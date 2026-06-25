# Reddit Quant Sentiment Pipeline

A modular, production-ready pipeline for scraping Reddit financial sentiment, computing risk signals, and optimizing portfolio weights through Bayesian backtesting.

## Architecture Overview

```
quant-py/
├── config/              # Configuration & constants
│   ├── constants.py     # Shared constants (blacklists, lexicons, entity resolution)
│   ├── reddit_weights.yaml  # Dynamic weight configuration
│   └── weights.py       # Weight loading utilities
├── db/                  # Database layer
│   ├── connection.py    # SQLite connection pool with WAL mode
│   ├── schema.py        # Table definitions & partitioning
│   ├── feature_store.py # Per-category sentiment feature access
│   └── jobs.py          # Aggregation & maintenance jobs
├── scraper/             # Scraping & NLP pipeline
│   ├── engine.py        # VADER + financial lexicon sentiment engine
│   ├── reddit_client.py # API-free Reddit JSON scraper
│   └── risk_detector.py # Z-score based risk narrative detection
├── optimization/        # Weight optimization
│   ├── optuna_search.py # Bayesian optimization with Optuna
│   └── ab_testing.py    # Champion/Challenger A/B testing
├── backtesting/         # Walk-forward backtesting
│   ├── backtest.py      # Monthly rebalancing simulation (IC, Sharpe, Hit Rate)
│   └── drift_detection.py # IC decay monitoring & auto-reoptimization
├── dashboard/           # Streamlit visualization
│   └── tab_sentiment_risk.py # Social Sentiment & Risk tab
├── scripts/             # Automation scripts
│   └── scheduler.py     # APScheduler for cron-like execution
├── run_scraper.py       # Main CLI entry point
└── stream_quant.py      # Main Streamlit dashboard app
```

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

Required packages:
- `nltk`, `pandas`, `numpy`, `scipy`, `yfinance`
- `requests`, `optuna`, `yaml`
- `streamlit`, `plotly` (for dashboard)
- `apscheduler` (for automated scheduling)

### 2. Initialize Database

```bash
python run_scraper.py init-db
```

### 3. Run Pipeline Components

**Full scrape + aggregate (daily):**
```bash
python run_scraper.py scrape
```

**Daily aggregation only:**
```bash
python run_scraper.py aggregate
```

**Walk-forward backtest:**
```bash
python run_scraper.py backtest --lookback 180 --objective information_coefficient
```

**Bayesian weight optimization:**
```bash
python run_scraper.py optimize --trials 50 --metric sharpe
```

**IC drift detection:**
```bash
python run_scraper.py drift --threshold 0.20 --window 60
```

**Purge old data (>30 days):**
```bash
python run_scraper.py purge --days 30
```

### 4. Start Automated Scheduler

```bash
# Run as daemon (runs daily/weekly/bi-weekly on schedule)
python scripts/scheduler.py

# Or run specific job once
python scripts/scheduler.py --once daily
python scripts/scheduler.py --once weekly
python scripts/scheduler.py --once drift
```

### 5. Launch Dashboard

```bash
streamlit run stream_quant.py
```

Navigate to the **"Social Sentiment & Risk"** tab for visualizations.

## Pipeline Cadence

| Frequency | Component | Purpose |
|-----------|-----------|---------|
| **Daily** (6 AM UTC) | VADER + Lexical Contradiction Rules | Clean text, generate raw sentiment scores |
| **Weekly** (Mon 2 AM) | Bayes' Theorem Calculations | Update micro-cap success probabilities on pre-financial events |
| **Bi-weekly** (Tue/Fri 3 AM) | Markov Chain State Transitions | Define macro/micro psychological regime (Apathy, Grassroots Conviction, Euphoria, Panic) |

## Configuration

Edit `config/reddit_weights.yaml` to adjust:
- Subreddit weights within each category
- Category-level blending weights
- Backtesting optimization parameters

The system automatically optimizes these weights via Optuna and stores versions in the database with Champion/Challenger A/B testing.

## Database Schema

Key tables (auto-partitioned monthly):
- `submissions_YYYY_MM` - Raw Reddit posts
- `daily_aggregations` - Per-ticker daily sentiment by category/subreddit
- `risk_signals` - Keyword frequency by risk type
- `composite_scores` - Weighted composite sentiment
- `weight_versions` - Optimization history with metrics
- `sentiment_runs` - Model version tracking

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific test modules
python -m pytest tests/test_nlp.py -v
python -m pytest tests/test_backtest.py -v
```

## Extending the Pipeline

### Add New Subreddits

Edit `config/reddit_weights.yaml`:
```yaml
subreddit_weights:
  macro_geopolitical:
    geopolitics: 0.35
    economics: 0.30
    supplychain: 0.35
    new_subreddit: 0.10  # Add here
```

### Add New Risk Keywords

Edit `config/constants.py`:
```python
RISK_KEYWORDS = {
    "geopolitical": [...],
    "new_category": ["keyword1", "keyword2"],
}
```

### Add Custom Financial Lexicon

Edit `config/constants.py`:
```python
FINANCIAL_LEXICON = {
    "bullish": 2.0,
    "new_term": 1.5,
}
```

## Compliance

See `COMPLIANCE.md` for Reddit Content Policy adherence guidelines.

## License

Internal use only.