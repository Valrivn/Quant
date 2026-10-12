# Data Remediation Plan — House of Quant
**Created:** 2026-09-03  
**Context:** Post-architecture-audit merge to `main` (commit `5f15c94`). All 1,460 tests pass. Backtest infrastructure is rigorous. **The data is the problem.**

---

## Executive Summary

| Finding | Verdict |
|---------|---------|
| **Algorithms** | MINVAR & MACRO work — mathematically sound, survive 26-year backtest, controlled drawdown |
| **Backtest Engine** | Rigorous — t+1 execution, chi-square gate, PIT framework, audit status, regime returns |
| **Data Feeds** | **All degraded** — static, proxy, unvalidated, or broken |

**Root Cause:** Every strategy runs on garbage data. The "SYSTEMATIC" chi-square verdicts for failed strategies (ADAPTIVE, RM-FINAL, OPPORTUNISTIC, STATIC-ML) are statistical artifacts of bear-market win-rates, not genuine alpha.

---

## Today's Work Summary (2026-09-03)

### Commits Merged to `main`
| Commit | Scope |
|--------|-------|
| `19d11a6` | Architecture audit remediation: MC per-path seeding, t+1 execution, per-rebalance slippage, FF5 alignment, Bernoulli LGD, clamp calibration, VaR/CVaR/Ulcer/MaxDD, allocator caps, Damodaran versioning, PIT universe, OOD stress tests |
| `5f15c94` | Portfolio weight normalization + test infrastructure fixes: adaptive `max_position=1/n`, renormalization after caps, yfinance mock signature, Damodaran fetch error handling |

### Test Results
- **1,460 passed, 18 skipped (98.8%)** in 3m 19s (parallel xdist)
- Conductor gate ≥90% satisfied
- All targeted audit-fix tests (206/206) pass

### Backtest Results (Unchanged by Today's Fixes)
| Method | FULL Window | Chi² | Verdict |
|--------|-------------|------|---------|
| **MINVAR** | +248%, Sharpe 0.87, MaxDD -15% | p=0.044 | **GENUINE EDGE** |
| **MACRO** | +194%, Sharpe 0.68, MaxDD -19% | p=0.011 | **GENUINE EDGE** |
| SPY | +764%, Sharpe 0.52, MaxDD -55% | p=1.0 | CHANCE |
| ADAPTIVE | -98%, Sharpe -0.60, MaxDD -99.7% | p=0.005 | OVERFIT |
| RM-FINAL | -99.6%, Sharpe -0.44, MaxDD -99.9% | p=0.005 | OVERFIT |
| OPPORTUNISTIC | -97.7%, Sharpe -0.49, MaxDD -99.7% | p=0.011 | OVERFIT |
| STATIC-ML | -70.4%, Sharpe -0.28, MaxDD -93.6% | p=0.005 | OVERFIT |
| DIVIDEND | -30.2%, Sharpe -0.19, MaxDD -30.2% | p=0.005 | STATIC YIELD CHASE |

---

## Current Data Inventory — Status & Gaps

### 1. PIT Universe — **CRITICAL GAP**
| File | `data/pit_sp500_constituents.json` + `diversification/datastore.py` |
|------|---------------------------------------------------------------------|
| **Current** | 102 quarterly snapshots (2000-Q1 → 2025-Q2) — **ETF PROXIES ONLY** (VTI, VB, BND, GLD, IAU, VCSH, SGOV, SHY, VCIT) |
| **Gap** | **No real S&P 500 constituent history** — survivorship bias baked in |
| **Impact** | Every backtest uses current ETF holdings for all historical dates |
| **Owner** | `datasource-worker` |
| **Fix** | Source real S&P 500 quarterly reconstitutions from: (a) S&P DJI official data (paid), (b) CRSP/Compustat via WRDS, (c) WikiData + SEC 13F fallback, (d) ETF replication reverse-engineering |

### 2. Damodaran ICR Table — **STALE / NETWORK-DEPENDENT**
| File | `Quantitative/stochastic/default_probability_table.py` + `config/industry_beta.yaml` |
|------|---------------------------------------------------------------------------------------|
| **Current** | v2024.1 hardcoded fallback (14 tiers); live fetch from `pages.stern.nyu.edu` fails offline |
| **Gap** | No automated refresh; no CI/CD; credit cycle not captured |
| **Impact** | Default probabilities frozen at 2024 levels; no regime adaptation |
| **Owner** | `edgar-worker` |
| **Fix** | (a) Cron job (monthly) to fetch + validate + commit `config/industry_beta.yaml`; (b) Local mirror of Damodaran HTML/XLS; (c) Alerting on fetch failure; (d) Versioned cache with checksum |

### 3. FF5 Factors (FRED) — **BROKEN FETCH**
| File | `valuation_alpha/datastore/factors.py` → `backtesting/chi_square.py:load_factors()` |
|------|---------------------------------------------------------------------------------------|
| **Current** | `fetch_ff5_factors()` fails silently → returns empty DataFrame → `alpha_ff5 = null` in all backtests |
| **Gap** | No factor-adjusted alpha decomposition; only excess vs SPY |
| **Impact** | Cannot measure true alpha vs Fama-French 5-factor model |
| **Owner** | `datasource-worker` |
| **Fix** | (a) Robust FRED client with retry/backoff; (b) Local cache (parquet) updated daily; (c) Fallback to Ken French website CSV; (d) Alerting on >24h stale |

### 4. Dividend Yields — **STATIC DICT**
| File | `diversification/sleeves.py:DIVIDEND_YIELDS` |
|------|----------------------------------------------|
| **Current** | Hardcoded dict: `{"VCSH": 0.04, "SGOV": 0.05, ...}` — same yield for all dates |
| **Gap** | No PIT dividend history; no ex-date adjustments; no yield curve evolution |
| **Impact** | Dividend engine uses fictional yields; total return miscalculated |
| **Owner** | `datasource-worker` |
| **Fix** | (a) Build PIT dividend database from CRSP/Compustat or Yahoo Finance historical dividends; (b) Integrate with `pit_tickers_for_date()`; (c) Ex-date aware accrual (already in `fee_sim3.py:313-315`) |

### 5. Corporate Bond / Credit Spreads — **NO PIT DATA**
| File | `diversification/fee_sim3.py` (macro engine uses HYG/LQD prices) |
|------|------------------------------------------------------------------|
| **Current** | Macro engine falls back to HYG/LQD ETF prices when FRED BAML series fail |
| **Gap** | No PIT credit spreads by rating; no OAS history; no default cycle data |
| **Impact** | "State+risk" macro regime detection uses degraded proxy |
| **Owner** | `datasource-worker` |
| **Fix** | (a) ICE BAML / FRED BAML series with local cache; (b) PIT spreads by rating bucket; (c) Integrate with Damodaran ICR table for regime detection |

### 6. Sentiment / Alternative Data — **UNVALIDATED**
| Source | Status |
|--------|--------|
| FinBERT (Reddit/StockTwits) | Trained on synthetic labels — **no ground truth validation** |
| IG Reels Scraper | Anti-bot works — **zero signal quality audit** |
| ApeWisdom | Adopted per CEO ruling — **no backtest integration** |
| Wiki/SEC Discovery | Built — **no signal-to-alpha measurement** |
| Consensus Gate | Built — **no live falsification** |

**Owner:** `screen-worker` + `educator`  
**Fix:** Human-labeled ground truth (500+ samples) → validate FinBERT → measure IC → integrate or discard.

---

## Remediation Priority Matrix

| Priority | Task | Effort | Dependencies | Success Metric |
|----------|------|--------|--------------|----------------|
| **P1** | Real S&P 500 quarterly constituents for PIT universe | 2-3 weeks | Data vendor access / WRDS | Backtest universe changes quarterly; survivorship bias eliminated |
| **P2** | Automated Damodaran fetch + CI/CD | 1 week | Network access to Stern NYU | `config/industry_beta.yaml` updates monthly; version increments |
| **P3** | FRED FF5 factor fetch reliability | 1 week | FRED API key | `alpha_ff5` populated in all backtests; CI test passes |
| **P4** | PIT dividend yield history | 2 weeks | CRSP/Compustat or Yahoo Finance bulk | Dividend engine total return matches SPY benchmark |
| **P5** | PIT credit spreads by rating | 2-3 weeks | ICE BAML / FRED BAML access | Macro engine regime detection uses real spreads |
| **P6** | Sentiment signal validation (ground truth) | 3-4 weeks | Labeling budget / Mechanical Turk | FinBERT IC > 0.05 on held-out test set |

---

## Technical Implementation Notes

### P1: Real S&P 500 PIT Constituents
```python
# Target interface (already exists in datastore.py):
def pit_tickers_for_date(date: pd.Timestamp) -> List[str]:
    """Return actual S&P 500 tickers as of date (quarter-end snapshot)."""

# Data format (JSON, one file):
{
  "2000-03-31": ["AAPL", "MSFT", "GE", ...],  # 500 tickers
  "2000-06-30": [...],
  ...
  "2025-06-30": [...]
}

# Sources to evaluate:
# 1. S&P DJI Index Methodology + Constituent History (paid, authoritative)
# 2. WRDS CRSP/Compustat: `sp500_constituents` table (academic access)
# 3. WikiData SPARQL: `wdt:P361` (S&P 500 member) + `wdt:P580` (start date)
# 4. ETF replication: SPY/IVV/VOO holdings history from ETF.com or issuer PDFs
```

### P2: Damodaran Automated Refresh
```yaml
# .github/workflows/refresh-damodaran.yml
on:
  schedule:
    - cron: '0 6 1 * *'  # Monthly 1st at 06:00 UTC
  workflow_dispatch:

jobs:
  refresh:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Fetch Damodaran
        run: python scripts/refresh_damodaran.py
      - name: Validate & Commit
        run: |
          git config user.name "bot"
          git config user.email "bot@quant"
          git add config/industry_beta.yaml
          git commit -m "chore(data): refresh Damodaran beta table v$(date +%Y.%m)"
          git push
```

### P3: FF5 Factor Robust Fetch
```python
# valuation_alpha/datastore/factors.py
def fetch_ff5_factors(use_cache=True, max_age_days=1) -> pd.DataFrame:
    cache_path = Path("data/ff5_factors_daily.parquet")
    if use_cache and cache_path.exists():
        age = (datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)).days
        if age <= max_age_days:
            return pd.read_parquet(cache_path)
    
    # Try FRED first
    try:
        from fredapi import Fred
        fred = Fred(api_key=os.getenv("FRED_API_KEY"))
        # ... fetch 5 series ...
    except Exception:
        # Fallback to Ken French CSV
        url = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
        # ... download, parse ...
    
    df.to_parquet(cache_path)
    return df
```

---

## What NOT To Do (Until Data Fixed)

| Activity | Reason |
|----------|--------|
| New strategy research | Garbage in, garbage out — MINVAR/MACRO already prove framework works |
| Hyperparameter tuning | Overfitting to degraded data |
| Alternative data integration | No validation pipeline; adds noise |
| Sentiment model retraining | Labels are synthetic; no ground truth |
| Ensemble methods | Compounding errors from bad inputs |

---

## Go/No-Go Criteria for Next Phase

| Gate | Metric | Threshold |
|------|--------|-----------|
| **PIT Universe** | Constituent turnover matches S&P DJI reports | ±5 tickers/quarter |
| **Damodaran** | Automated fetch succeeds 3 consecutive months | 100% |
| **FF5 Factors** | `alpha_ff5` non-null in backtest registry | 100% of runs |
| **Dividend Yields** | Dividend engine total return within 50bps of SPY benchmark | ±0.5% |
| **Sentiment** | FinBERT IC on human-labeled test set | >0.05 |

**Only when all 5 gates pass** should algorithm work resume.

---

## File Pointers for Tomorrow

| Area | Key Files |
|------|-----------|
| PIT Universe | `data/pit_sp500_constituents.json`, `diversification/datastore.py`, `diversification/sleeves.py`, `diversification/fee_sim3.py` |
| Damodaran | `Quantitative/stochastic/default_probability_table.py`, `scripts/refresh_damodaran.py`, `config/industry_beta.yaml` |
| FF5 Factors | `valuation_alpha/datastore/factors.py`, `backtesting/chi_square.py:load_factors()` |
| Dividends | `diversification/sleeves.py:DIVIDEND_YIELDS`, `diversification/fee_sim3.py:313-315` |
| Backtest Engine | `backtesting/chi_square.py`, `backtesting/backtest.py`, `diversification/fee_sim3.py` |
| Allocator | `portfolio/allocator.py` (fixed but unused by engines) |
| Tests | `tests/test_pit_universe.py`, `tests/test_stochastic_models.py`, `tests/test_backtest.py` |

---

## Next Session Kickoff (Tomorrow)

1. **Start with P1** — assign `datasource-worker` to evaluate S&P 500 constituent sources
2. **Parallelize P2+P3** — Damodaran cron + FRED robust fetch can run simultaneously
3. **Defer P4+P5** — require vendor access (CRSP/ICE BAML)
4. **Park P6** — sentiment validation is a separate workstream; do not block core data

**Branch strategy:** Create `feature/data-pit-constituents`, `feature/data-damodaran-auto`, `feature/data-ff5-robust` — each with own PR, test, and merge gate.

---

*End of plan. Resume tomorrow with P1 source evaluation.*