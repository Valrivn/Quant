# Data Strategy — Qualitative Alpha Pipeline (Dual-Window, Free-First)

**Version:** 2.0 | **Date:** 2026-09-11 | **Status:** ACTIVE — Binds all ingestion, validation, and backtest execution | **Rebuild Phase: Phase 0 Specs → Phase 1 Dispatch**

---

## 1. Strategic Objectives

| Objective | Success Metric |
|-----------|----------------|
| **Dual-Window Backtest** | **Primary: 2018-2024 (7yr, RPO-clean, ASC 606 mandated)** + **Extended: 2020-2026 (7yr, full qual coverage)** — two-tier window design |
| **Free-First Mandate** | Zero paid APIs; all sources ToS-cleared, PIT-timestamped |
| **Synthetic Data Validation** | RPO imputation **EXCLUDED** per CEO ruling (2026-09-11); 2014-2017 RPO dropped, primary window starts 2018 |
| **3-Arm Attribution** | Pure Quant (A) vs Pure Qual (B) vs Hybrid (C) — all three required; Arm C conviction over full universe with neutral qual fill |
| **Universe Target** | **~1,500 PIT names**: Top-1,000 by PIT market cap (Russell 1000-equivalent) + next-500 mid/micro-cap by PIT market cap, monthly reconstitution, survivorship-free via SEC EDGAR historical indexes + delisting 8-Ks |
| **Qualitative Coverage** | Strict PIT: 2014-2024 backtest uses only data available at *t*; 2020-2026 extended window uses full qual coverage (separate report) |
| **Gate Integrity** | Pre-registered Bonferroni α=0.0071; Gates 3 & 4 re-registered for sector-relative screen **before** re-run with 10k null calibration |

---

## 2. Data Source Registry (Graded A-F)

### Tier 1 — Core (Zero Risk, Permanent, PIT-Clean)

| Source | Signal | Window | Access | Grade | Notes |
|--------|--------|--------|--------|-------|-------|
| **SEC EDGAR (companyfacts.zip + RSS)** | MD&A tone, RPO, Contract Liabilities, 13F, 8-K | 1994→ / 2009→ (XBRL) | Bulk zip + RSS | **A+** | Statutory mandate; `filing_date` = PIT anchor |
| **GH Archive (BigQuery)** | GitHub commit/fork/star velocity | 2011-02→ | BigQuery free tier (1TB/mo) | **A+** | Event timestamps immutable; tech/small-cap coverage |
| **NHTSA Complaints/Recalls API** | Auto/EV quality & safety signals | 1995→ | Free REST API | **A** | Sector-specific; batch nightly |
| **CFPB Complaints API** | FinTech/lending consumer friction | 2012-06→ | Free REST API | **A-** | Narratives removed 2026; use complaint velocity only |
| **USPTO PatentsView (ODP Bulk)** | Innovation/IP moat (citations, grants) | 1976→ | ODP bulk download | **B+** | Grant lag 18-36mo; small-entity flag for micro-cap |

### Tier 2 — Stable (Low Risk, Requires Engineering)

| Source | Signal | Window | Access | Grade | Notes |
|--------|--------|--------|--------|-------|-------|
| **App Store / Play Store (scrapers)** | Consumer app sentiment, velocity | 2008→ | `app_store_scraper` + proxy pool | **B** | Apple rate-limits aggressive; proxy rotation mandatory |
| **ATS RSS (Greenhouse/Lever)** | Hiring velocity (behavioral, not opinion) | 2015→ | RSS feeds (public) | **B** | **Blocker:** No central registry — seed from SEC 10-K career URLs |
| **SEC 13F (Bulk XML)** | Institutional conviction (45-day lag) | 1999→ | SEC bulk quarterly zip | **B** | Parse locally with DuckDB; avoid WhaleWisdom scrape |
| **Amazon Reviews 2023 (McAuley Lab)** | Product sentiment, review velocity | 1996-2023 | HuggingFace static | **A** | Brand mapping required; verified purchase filter |

### Tier 3 — Experimental (Gated, Validate Before Core Entry)

| Source | Signal | Window | Access | Grade | Gate |
|--------|--------|--------|--------|-------|------|
| **Glassdoor (lallantop/glassdoor HF)** | Employee sentiment (text only) | 2008→ | Static dump | **C+** | Mixed-effects Granger vs ATS; Dip test + FinBERT — **GATED OUT** (failed 2026-09-10) |
| **Reddit/StockTwits (ApeWisdom scrape)** | Mention volume, momentum | 2018→ | Free scrape | **D** | Contrarian overflow only (max 5% weight) |
| **Earnings Transcripts (Company IR)** | CEO tone (audio → Whisper) | 2010→ | Manual/IR pages | **C** | S&P 500 only; small-cap sparse |

### Excluded (Paid / ToS Risk / Redundant)

- ❌ Quiver Quant (free tier = 0.05 calls/ticker/day → noise)
- ❌ CRSP/Compustat ($25k+; SEC EDGAR provides same universe PIT-free)
- ❌ G2/Capterra (no free API, anti-scrape)
- ❌ FMP/earningscall APIs (freemium; bulk requires paid)

---

## 3. Dual-Window Backtest Design (Updated 2026-09-11)

### Primary Window: 2018-01-01 → 2024-12-31 (7 Years, ~1,750 Trading Days) — **RPO-Clean Core**

| Property | Value |
|----------|-------|
| **Regimes Covered** | 2020 COVID, 2022 Inflation (2 full regimes) |
| **RPO Status** | 100% Actual (ASC 606 mandated) — **no imputation** (2014-2017 dropped per CEO ruling) |
| **Statistical Power** | Min detectable ΔSharpe ≈ 0.075 (80% power, α=0.0071 Bonferroni) |
| **Arms Tested** | A (Quant), B (Qual), C (Hybrid) — all three |
| **Universe** | Dynamic PIT top-1000 + 500 mid/micro monthly reconstitution (~1,500 names) |
| **Qual Factors** | Strict PIT: only data available at *t*; missing → neutral z-score (0) |

### Extended Window: 2020-01-01 → 2026-12-31 (7 Years, ~1,750 Trading Days) — **Full Qual Coverage**

| Property | Value |
|----------|-------|
| **Regimes Covered** | 2022 Inflation, 2023-2024 normalization, 2025-2026 forward (emerging) |
| **RPO Status** | 100% Actual |
| **Statistical Power** | Min detectable ΔSharpe ≈ 0.075 (80% power, α=0.0071) |
| **Purpose** | Qualitative signal validation with full data coverage (GH, ATS, App Store, Patents, etc.) |
| **Universe** | Same dynamic PIT construction extended to 2026 |
| **Qual Factors** | Full coverage for all Tier 1-2 sources; reported as **separate attribution report** |

### Gate Thresholds (Pre-Registered, Bonferroni-Corrected)

| Gate | Threshold | α (per test) |
|------|-----------|--------------|
| Chi-Square Contingency | p < 0.05 | 0.0071 |
| ANOVA (3-arm means) | p < 0.05 | 0.0071 |
| Welch C vs A | p < 0.0071 | 0.0071 |
| Welch C vs B | p < 0.0071 | 0.0071 |
| OOS Sharpe (Arm C) | > 1.0 | — |
| Max Drawdown (Arm C) | < -20% | — |
| Information Ratio vs SPY | > 0.3 | — |
| Turnover Drag | < 30% | — |
| **Probabilistic Sharpe (PSR)** | > 0.95 | — |
| **Calmar Ratio** | > 2.0 | — |

**Gate Re-Registration (Binding):** Gates 3 & 4 (Welch C vs A, Welch C vs B) **must be re-registered with sector-relative thresholds** before re-run. Calibration on 10,000 null portfolios required. See §10 Decision Log entries 2026-09-11.

### Gate Thresholds (Pre-Registered, Bonferroni-Corrected)

| Gate | Threshold | α (per test) |
|------|-----------|--------------|
| Chi-Square Contingency | p < 0.05 | 0.0071 |
| ANOVA (3-arm means) | p < 0.05 | 0.0071 |
| Welch C vs A | p < 0.0071 | 0.0071 |
| Welch C vs B | p < 0.0071 | 0.0071 |
| OOS Sharpe (Arm C) | > 1.0 | — |
| Max Drawdown (Arm C) | < -20% | — |
| Information Ratio vs SPY | > 0.3 | — |
| Turnover Drag | < 30% | — |
| **Probabilistic Sharpe (PSR)** | > 0.95 | — |
| **Calmar Ratio** | > 2.0 | — |

---

## 4. Synthetic Data Validation Framework (Critical Path)

### RPO Imputation (2014-2017) — **EXCLUDED PER CEO RULING 2026-09-11**

**Ruling:** OOS R² = 0.157 (FAIL vs 0.80 bar). Primary window redefined to 2018-2024 (RPO-clean). 2014-2017 RPO signal dropped; power loss documented.

**Archive (for reference only):**
**Proxy Construction:**
```
RPO_imputed_t = Deferred_Revenue_t + Contract_Liabilities_t
```
Both available in SEC XBRL from 2009 (tags: `DeferredRevenue`, `ContractLiabilities`)

**Validation Protocol (Out-of-Sample) — NOT RUN:**
1. **Training Period:** 2018-2021 (4 years, actual RPO reported)
2. **Test Period:** 2022-2024 (3 years, held-out)
3. **Model:** `RPO_actual = β₀ + β₁·DeferredRev + β₂·ContractLiab + ε`
4. **Pass Criteria:**
   - Out-of-sample R² > 0.80
   - Residuals: Shapiro-Wilk p > 0.05 (normal), Durbin-Watson ≈ 2 (no autocorr)
   - Coefficient stability: |β_train - β_test| / β_train < 0.15

**PIT Flagging (Mandatory for any future imputation):**
Every imputed row carries `rpo_is_imputed = True` + `imputation_model_version`. Harness filters or weights by flag.

### Glassdoor → ATS Correlation Test — **GATED OUT (FAIL 2026-09-10)**

**Hypothesis:** Glassdoor sentiment (FinBERT on Pros/Cons) Granger-causes ATS hiring velocity (or vice versa)

**Design:**
- **Panel:** Quarterly 2014-2024 (44 quarters)
- **Entities:** Companies with both Glassdoor reviews (n≥10/qtr) AND ATS feed active
- **Model:** Mixed-effects Granger (lags 1-4 quarters)
  ```
  ATS_vel_it = α_i + Σ_{k=1}^4 γ_k ATS_vel_i,t-k + Σ_{k=1}^4 δ_k Glassdoor_sent_i,t-k + ε_it
  Glassdoor_sent_it = α_i + Σ_{k=1}^4 γ_k Glassdoor_sent_i,t-k + Σ_{k=1}^4 δ_k ATS_vel_i,t-k + ε_it
  ```
- **Multiple Testing:** Bonferroni across 4 lags × 2 directions = 8 tests → α = 0.00625
- **Pass Criteria:** δ_k jointly significant (F-test p < 0.00625) AND directionally consistent

**Outcome Mapping:**
| Result | Action |
|--------|--------|
| Glassdoor → ATS significant | Retain Glassdoor in Core (Phase 3) |
| ATS → Glassdoor significant | ATS only; Glassdoor = lagging indicator |
| Neither significant | Drop Glassdoor; ATS only |
| Both significant (feedback) | Use composite; weight by effect size |

---

## 5. Universe Construction (SEC EDGAR Native — Dynamic PIT Top-1500)

### Source Files
1. `company-tickers.json` — Current mapping: CIK ↔ Ticker ↔ Exchange ↔ Name
2. `companyfacts.zip` — All XBRL facts for every filer (historical)
3. **Historical index files** — Quarterly snapshots of active filers (SEC EDGAR)
4. **Delisting 8-Ks** — Form 8-K Item 1.01/1.02/2.01/3.02/5.01/5.03/8.01 for PIT delist dates

### Build Process
```python
# Skeleton logic (executable target for RS-01)
def build_pit_universe(start_date, end_date):
    # 1. Load all companyfacts — each fact has `filed` (PIT) and `end` (period)
    facts = load_companyfacts()
    
    # 2. Load historical index files — quarterly active filer lists
    #    Each index entry: (cik, ticker, index_date, action)
    hist_index = load_historical_indexes()
    
    # 3. Load delisting 8-Ks — PIT delist_filed dates
    delists = load_delisting_8ks()
    
    # 4. For each rebalance month T (month-end):
    #    a. Active universe_T = {cik | index_date <= T < delist_filed}
    #    b. Market cap_T = shares_outstanding_T × close_price_T
    #       - shares_outstanding: from most recent 10-Q/K filed <= T (XBRL: CommonStockSharesOutstanding)
    #       - close_price: Yahoo/AlphaVantage free, cached, max 2-day forward fill
    #    c. Rank by market cap_T → Top 1000 (large) + next 500 (mid/micro)
    #    d. Sector assignment: from most recent 10-K Item 1 (sic/naics) filed <= T
    #       Map to GICS Level 1 (11 sectors) + custom rollup (Hardware/Software/Consumer/Industrial/Healthcare/Energy/Financial)
    
    # 5. Daily PIT expansion: for each trading day T, universe_T = monthly_reconstitution[month(T)]
    #    Delist removal on delist_filed date (not announcement date)
    
    # 6. Price quality flags:
    #    - price_stale_flag = (quote_age > 2 trading days)
    #    - illiquid_flag = (avg_dollar_vol_20d < $100k)
    #    Exclude flagged from tradable set
    
    return pit_universe  # DataFrame: date, cik, ticker, sector, cap_bucket, is_active, price_stale_flag, illiquid_flag
```

### Output
- `data/universe/pit_universe_2018_2024.parquet` — Daily PIT universe (primary window)
- `data/universe/pit_universe_2020_2026.parquet` — Daily PIT universe (extended window)
- `data/universe/delisting_events.parquet` — Every delist/merger/bankruptcy with filed date
- `data/universe/monthly_reconstitution.parquet` — Top-1500 list per rebalance month with market cap, sector
- Provenance: `source=SEC_EDGAR`, `retrieval_date`, `companyfacts_version`, `index_version`

### Qualitative Entity Mapping (Heuristic, Per CEO Ruling)
| Source | Entity Key | Mapping Method | Confidence Flag |
|--------|------------|----------------|-----------------|
| GH Archive | org/repo → ticker | Fuzzy org name ↔ CIK company name + ticker crosswalk | `mapping_confidence ∈ {high, medium, low}` |
| USPTO PatentsView | assignee → ticker | Assignee name ↔ CIK + manual alias table for top-500 | `mapping_confidence` |
| ATS RSS | Greenhouse/Lever token → ticker | Career URL from SEC 10-K Item 1/7/8 → token → ticker | `high` (direct) |
| NHTSA/CFPB | make/model/product → ticker | Keyword match + sector filter + manual review for ambiguous | `mapping_confidence` |
| App Store / Amazon | app_id/brand → ticker | Brand ↔ ticker mapping table (maintained) | `high` for mapped, `low` for heuristic |

All qualitative loader outputs **must** include `mapping_confidence` column. PIT validator rejects `low` confidence rows for core arms; `medium` allowed with 0.5 weight.

---

## 6. 3-Arm Harness Skeleton (Architecture — Updated 2026-09-11)

### Arm A — Pure Quant Screen (Sector-Relative, Dual-Track)

**Sector Taxonomy:** GICS Level 1 (11 sectors) + custom rollup for screen:
- `Hardware` (Semiconductors, Hardware, Electronic Equipment)
- `Software` (Software, IT Services)
- `Consumer` (Consumer Staples, Consumer Discretionary, Retail)
- `Industrial` (Industrials, Materials, Capital Goods)
- `Healthcare` (Healthcare Equipment, Biotechnology, Pharma)
- `Energy` (Energy, Oil & Gas)
- `Financial` (Banks, Insurance, Diversified Financials, Real Estate)

**Track 1 — Mainstream (Top 25% per Sector):**
```
Universe_T → Factor_Compute_T(ROIC, WACC, ICR, FCF_Yield, ROIC_WACC_Spread)
   → Sector_Rank_T(ROIC_WACC_Spread desc, FCF_Yield desc) 
   → Screen(Sector_Percentile <= 25% AND min_names_per_sector >= 20)
   → Qualified_Mainstream_T
```

**Track 2 — Profit-Margin Achievers (Absolute Thresholds):**
```
Universe_T → Factor_Compute_T(FCF_Margin, ROIC, ROIC_WACC_Spread)
   → Screen(FCF_Margin > sector_median_3yr_rolling AND ROIC_WACC_Spread > 2%)
   → Qualified_Achievers_T
```

**Union Qualified Set:**
```
Qualified_Set_T = Qualified_Mainstream_T ∪ Qualified_Achievers_T
```
*If union < 20 names → relax to top-30% sector percentile (logged as design change)*

**Arm A Portfolio:**
```
Qualified_Set_T → Rank(FCF_Yield desc) → Top N (N = min(50, |Qualified_Set_T|)) → Equal Weight
```

### Arm B — Pure Qual Composite (Strict PIT, Neutral Fill)

```
Universe_T → Qual_Features_T(
    RPO_Growth_YoY (2018+ only),
    GH_Velocity_90d,
    ATS_Hiring_Velocity_90d,
    App_Review_Velocity_90d,
    Patent_Citation_Accel_1y,
    CFPB_Complaint_Velocity_90d,
    NHTSA_Complaint_Velocity_90d,
    MD&A_Tone_Score,
    Glassdoor_Sentiment (EXCLUDED — gated out)
) 
→ For each feature: z-score cross-section at T; **missing → neutral (0.0)**
→ PCA (1st component) OR Equal-Weight Composite → Rank → Top N → Equal Weight
```

### Arm C — Hybrid Two-Tier (Full Universe Conviction Sizing)

```
Tier 1 (Quant Gate): Universe_T → Arm_A_Screen → Qualified_Set_T
Tier 2 (Qual Rank): Universe_T → Arm_B_Qual_Score → Qual_Zscore_T (full universe, neutral fill)
Conviction_Size_T = Base_Weight × (1 + Qual_Zscore_T) × Risk_Penalty_T × I(ticker ∈ Qualified_Set_T)

Risk_Penalty_T = 1.0 
  - 0.3 if Cash_Runway_Months < 18
  - 0.2 if Customer_Concentration > 30%
  - 0.2 if Insider_Ownership < 5%
  - 0.1 if No_13F_Sponsor
  - 0.0 otherwise
```

*Names outside Qualified_Set_T get `I(ticker ∈ Qualified_Set_T) = 0` → zero weight. This preserves the quant gate while allowing qual tilt within the gate.*

### Rebalance & Execution
- **Frequency:** Monthly (day 1 of month, using data available at prior month-end)
- **Turnover Control:** Max 30% portfolio turnover per rebalance
- **Transaction Costs:** 10 bps per side + slippage model (execution-strategist validated)

---

## 7. Ingestion Pipeline Architecture (Skeleton — Updated 2026-09-11)

### Phase 0: Specs & Pre-Flight (Week 0) — **COMPLETE BEFORE DISPATCH**
```
specs/
├── universe_pit_v2.md              # Dynamic PIT top-1500 monthly reconstitution
├── sector_screen_v2.md             # GICS + custom rollup, dual-track thresholds
├── gate_reregistration_v2.md       # Gates 3 & 4 sector-relative pre-commitment
└── ml_track_v2.md                  # Walk-forward CPCV, symbolic rule extraction
```

### Phase 1: SEC EDGAR Bulk + Universe + Harness Core (Week 1-2)
```
ingestion/
├── sec_bulk_loader.py          # companyfacts.zip → Parquet (facts, filings, concepts)
├── sec_mda_parser.py           # 10-K/10-Q Item 7 → MD&A text + Loughran-McDonald + FinBERT (FILING_DATE PIT)
├── sec_rpo_parser.py           # XBRL tags: DeferredRevenue, ContractLiabilities, RPO (2018+) — FILING_DATE PIT
├── sec_13f_parser.py           # Bulk XML quarterly → holdings panel (CIK, ticker, shares, filed_date)
├── universe_builder.py         # **RS-01** Dynamic PIT top-1500 monthly reconstitution + price cache
├── price_cache.py              # **RS-04** Yahoo/AlphaVantage free, 2-day forward fill max, price_stale_flag
└── harness/
    ├── factor_store.py         # DuckDB-backed factor panel (date, ticker, factor, value, provenance)
    ├── pit_validator.py        # Enforces filed_date <= trade_date; rejects lookahead; checks price_stale_flag
    ├── gate_engine.py          # 10 gates (7 primary + PSR + Calmar) with Bonferroni α=0.0071
    └── three_arm_runner.py     # Orchestrates A/B/C with identical universe_T
```

### Phase 2: Alt Data Core (Week 2-3)
```
ingestion/
├── gh_archive_loader.py        # BigQuery → commit/fork/star velocity by org/repo → ticker map (mapping_confidence)
├── nhtsa_loader.py             # API → complaints/recalls by make/model → ticker map (mapping_confidence)
├── cfpb_loader.py              # API → complaint velocity by product/company → ticker (mapping_confidence)
├── ats_mapper.py               # SEC 10-K Item 1/7/8 career URLs → Greenhouse/Lever tokens
├── ats_rss_loader.py           # RSS feeds → job_postings velocity (posted/closed/active) — retrieval_timestamp
└── patentsview_loader.py       # ODP bulk → citations/grants by assignee → ticker map (mapping_confidence)
```

### Phase 3: Consumer/Behavioral (Week 3-4)
```
ingestion/
├── app_store_loader.py         # Proxy-rotated scraper → reviews/ratings/velocity by app_id → ticker (retrieval_timestamp)
├── amazon_reviews_loader.py    # HF McAuley 2023 → brand mapping → review velocity/sentiment (review_date)
├── sec_13f_loader.py           # Bulk XML (already parsed) → inst_ownership panel (filed_date)
└── apewisdom_scraper.py        # Reddit/StockTwits mention volume → top 500 tickers weekly (retrieval_timestamp)
```

### Phase 4: Validation (Parallel Week 3-4)
```
validation/
├── rpo_imputation_test.py      # **ARCHIVED** — "Excluded per CEO ruling 2026-09-11"
├── glassdoor_ats_granger.py    # **ARCHIVED** — "Gated out (FAIL 2026-09-10)"
├── gate_calibration.py         # **RS-05** Verify NEW gate thresholds on 10,000 null portfolios
├── provenance_audit.py         # SHA-256 every Parquet; verify PIT compliance; check mapping_confidence
└── qual_coverage_audit.py      # **NEW** Matrix: (ticker, month, source) → has_data; drives window logic
```

### Phase 5: Backtest Execution (Week 4)
```
# Primary window (2018-2024)
python -m ingestion.harness.three_arm_runner --start 2018-01-01 --end 2024-12-31 --window primary

# Extended window (2020-2026) — separate report
python -m ingestion.harness.three_arm_runner --start 2020-01-01 --end 2026-12-31 --window extended
```

---

## 8. Data Contracts (Provenance Schema)

Every Parquet output **must** include:

```json
{
  "source": "SEC_EDGAR|GH_ARCHIVE|NHTSA|CFPB|USPTO|ATS_RSS|APP_STORE|AMAZON|APEWISDOM|GLASSDOOR",
  "retrieval_timestamp": "2026-09-09T14:32:00Z",
  "source_version": "companyfacts_20260901|gharchive_202609|...",
  "pit_timestamp_column": "filed|created_at|date_received|grant_date|review_date|posted_date",
  "entity_key": "cik|ticker|org|assignee|app_id|brand",
  "transformations": ["FinBERT_sentiment", "Loughran_McDonald", "velocity_90d_zscore", "imputed_rpo_flag"],
  "sha256": "a1b2c3...",
  "row_count": 123456,
  "date_range": {"min": "2014-01-01", "max": "2024-12-31"},
  "mapping_confidence": "high|medium|low"    // NEW: for qualitative entity mappings
}
```

**Universe Parquet additionally includes:**
```json
{
  "price_stale_flag": "boolean",
  "illiquid_flag": "boolean",
  "reconstitution_month": "YYYY-MM",
  "sector_gics": "GICS Level 1 code",
  "sector_custom": "Hardware|Software|Consumer|Industrial|Healthcare|Energy|Financial"
}
```

---

## 9. Risk Register & Mitigations (Updated 2026-09-11)

| Risk | Likelihood | Impact | Mitigation | Status |
|------|------------|--------|------------|--------|
| SEC bulk zip format change | Low | High | Version pin `companyfacts.zip`; automated schema diff alert | Active |
| ATS RSS feed changes/breaks | Medium | High | Dual vendor (Greenhouse+Lever); fallback to careers page scrape | Active |
| App Store IP ban | High | Medium | Residential proxy pool (50+ IPs); exponential backoff; cache 7 days | Active |
| GH Archive BigQuery quota | Low | Medium | Local mirror of 50GB tech subset; incremental daily sync | Active |
| **RPO imputation fails OOS** | **Certain** | **High** | **EXECUTED: Drop 2014-2017 RPO; primary window 2018-2024 only; power loss documented** | **Resolved** |
| Glassdoor correlation spurious | High | Low | Gate keeps Glassdoor experimental regardless | **Gated Out** |
| 13F 45-day lag unusable for real-time | Certain | Medium | Use only for quarterly conviction confirmation; not entry signal | Active |
| Small-cap price data gaps | Medium | Medium | Yahoo Finance free + AlphaVantage free (500/day) → cache; forward-fill max 2 days; `price_stale_flag` | **Updated** |
| **Delisted price data unavailable (free)** | **High** | **High** | **Flag delisted with `price_stale_flag` post-delist; exclude from tradable; document survivorship bias** | **Accepted** |
| Qualitative entity mapping errors | Medium | Medium | Heuristic mapping with `mapping_confidence` flag; `low` rejected from core arms | **New** |
| Gate re-registration invalidates pre-registration | Medium | High | Freeze new thresholds in code+config BEFORE re-run; 10k null calibration report attached | **Active** |
| Sector-relative screen changes thesis | Low | Medium | Document as design change; dual-track (mainstream + achievers) preserves absolute quality path | **Active** |
| Extended window (2020-2026) not a true backtest | Certain | Medium | Report as "qual coverage validation" not "backtest"; no gate claims | **Active** |

---

## 10. Decision Log (Binding)

| Decision | Ruling | Date |
|----------|--------|------|
| Backtest Arms | 3-Arm (A/B/C) mandatory | 2026-09-09 |
| Primary Window (Original) | 2014-2024 (10yr) | 2026-09-09 |
| Secondary Window | 2018-2024 (7yr, RPO-clean) | 2026-09-09 |
| Bonferroni α | 0.0071 per gate (7 gates) | 2026-09-09 |
| RPO Imputation | DeferredRev + ContractLiab; OOS test required | 2026-09-09 |
| Glassdoor | Experimental only; Granger vs ATS gate | 2026-09-09 |
| Quiver Quant | EXCLUDED (paid/rate-limited) | 2026-09-09 |
| ApeWisdom | Contrarian overflow only (top 500, weekly) | 2026-09-09 |
| Small-Cap Universe | SEC EDGAR native (free, PIT) | 2026-09-09 |
| Ingestion | Parallel phases (not waterfall) | 2026-09-09 |
| Universe Expansion | Top 1000 US Equities + Yahoo/AlphaVantage price cache | 2026-09-10 |
| Moat Screening | Moat-First ($\text{ROIC}-\text{WACC}>0$ / Qual Vel) + Sector Quantiles ($N \ge 20$) | 2026-09-10 |
| Reverse-Engineering | Parallel CPCV ML research track (max 40% weight) | 2026-09-10 |
| **Primary Window Redefined** | **2018-2024 (RPO-clean); 2014-2017 dropped** | **2026-09-11** |
| **Extended Window Added** | **2020-2026 (full qual coverage); separate report** | **2026-09-11** |
| **Universe Target** | **~1,500 PIT names: Top-1000 + 500 mid/micro monthly reconstitution** | **2026-09-11** |
| **Screen Architecture** | **Sector-relative dual-track: Mainstream (top-25% sector) ∪ Achievers (FCF Margin > sector median 3yr + ROIC-WACC > 2%)** | **2026-09-11** |
| **Gate 3 & 4 Re-registration** | **Sector-relative thresholds; freeze before re-run; 10k null calibration** | **2026-09-11** |
| **Qualitative PIT Policy** | **Strict PIT: missing → neutral (0.0); 2014-2024 uses available-only; 2020-2026 full coverage** | **2026-09-11** |
| **Entity Mapping** | **Heuristic with `mapping_confidence ∈ {high, medium, low}`; `low` excluded from core** | **2026-09-11** |
| **ML Track Design** | **Walk-forward expanding window on $U_t$; embargo ≥ horizon; LASSO+RF → symbolic rules; max 40% weight** | **2026-09-11** |
| **Arm C Conviction** | **Full universe qual z-score with neutral fill; zero weight outside quant gate** | **2026-09-11** |

---

## 11. Next Actions (Dispatch Order — Updated 2026-09-11)

### Phase 0: Specs Complete (This Week)
1. **You** — Approve `specs/sector_screen_v2.md` (dual-track thresholds, sector map, min-N)
2. **You** — Approve `specs/gate_reregistration_v2.md` (Gates 3/4 sector-relative pre-commitment)
3. **Me** — Write `specs/universe_pit_v2.md` (already embedded in §5 above)
4. **Me** — Write `specs/ml_track_v2.md` (walk-forward CPCV, symbolic extraction)

### Phase 1: Parallel Dispatch (Week 1-2)
1. **`gemini-worker`** — Phase 1 Ingestion Core:
   - `sec_bulk_loader.py`, `universe_builder.py` (RS-01), `price_cache.py` (RS-04)
   - `sec_mda_parser.py`, `sec_rpo_parser.py` (RS-02: FILING_DATE strict)
   - `sec_13f_parser.py`, `harness/factor_store.py`, `harness/pit_validator.py`
2. **`bigpickle-worker`** — Phase 1 Schemas & Gates:
   - `harness/gate_engine.py` (RS-05: sector-relative Gates 3/4 + 10k calibration)
   - `harness/three_arm_runner.py` (new Arm A/B/C logic per §6)
   - `sec_rpo_parser.py`, `sec_mda_parser.py` (supporting parsers)

### Phase 2: Qualitative Pipeline (Week 2-3) — *Dispatch as Phase 1 completes*
- `gemini-worker`: `gh_archive_loader.py`, `nhtsa_loader.py`, `cfpb_loader.py`, `ats_mapper.py`, `ats_rss_loader.py`, `patentsview_loader.py`
- `gemini-worker`: `app_store_loader.py`, `amazon_reviews_loader.py`, `sec_13f_loader.py`, `apewisdom_scraper.py`

### Phase 3: Validation & Backtest (Week 3-4)
- `bigpickle-worker`: `validation/gate_calibration.py` (10k null on NEW gates)
- `gemini-worker`: `validation/provenance_audit.py`, `validation/qual_coverage_audit.py`
- **Both**: Backtest re-run commands (Primary 2018-2024, Extended 2020-2026)

### Phase 4: ML Research Track (Parallel, Non-Blocking)
- `gemini-worker`: `research/reverse_engineer_multibaggers.py` (RS-06)
  - Walk-forward on $U_t$ at *t*; CPCV embargo ≥ 12mo
  - LASSO + RF → symbolic rule extraction (`sklearn.tree.export_text` + pruning)
  - Max 40% empirical weight in production hybrid

---

**Dispatch Commands (Ready When Specs Approved):**
```bash
# 1. Dynamic PIT Universe + Price Cache
python -m ingestion.universe_builder --rebuild --top-1500 --start 2018-01-01 --end 2024-12-31

# 2. Validate PIT Timestamp Enforcement
python -m ingestion.sec_rpo_parser --validate-pit --filing-date-only
python -m ingestion.sec_mda_parser --validate-pit --filing-date-only

# 3. Test Price Cache (2-day forward fill max)
python -m ingestion.price_cache --test --max-forward-fill 2

# 4. Re-register Gates 3 & 4 (Sector-Relative, Dual-Track)
python -m ingestion.harness.gate_engine --re-register-gates 3,4 --sector-quantile --dual-track

# 5. Gate Calibration on New Gates (10,000 null portfolios)
python -m validation.gate_calibration --run --n 10000 --gates 3,4

# 6. Full Backtest Re-Run (Primary Window)
python -m ingestion.harness.three_arm_runner --start 2018-01-01 --end 2024-12-31 --window primary

# 7. Extended Window (Qual Coverage Validation)
python -m ingestion.harness.three_arm_runner --start 2020-01-01 --end 2026-12-31 --window extended
```

---

## 12. Backtest Execution Results (2026-09-10)

### 12.1 Validation Suite Outcomes

| Validation | Status | Key Metrics | Implication |
|------------|--------|-------------|-------------|
| **RPO Imputation OOS** | **FAIL** | OOS R² = 0.157 (need >0.80), Shapiro p = 0.000, DW = 1.196, β-stability unstable | 2014-2017 RPO signal **NON-VALIDATED**; secondary window (2018-2024) clean |
| **Glassdoor ↔ ATS Granger** | **FAIL** | ATS data only 2023 (42-row panel) → insufficient lags | Glassdoor **gated OUT** (Tier 3 experimental) |
| **Gate Calibration (n=10,000)** | **MIXED** | Gates 2-4 calibrated vs α=0.0071; Gate 1 (χ²) under-fires (FPR 0.0012) | Statistical gates conservative; performance gates informational |
| **Provenance Audit** | **PASS 11/11** | All factor files valid SHA-256 + PIT compliance | Data integrity confirmed |

### 12.2 Dual-Window 3-Arm Backtest: FAIL (Both Windows)

#### Primary Window (2014–2024): 2/10 Gates Pass

| Gate | Result | Value vs Threshold |
|------|--------|-------------------|
| Chi-Square Contingency | **FAIL** | p=0.034 > 0.0071 |
| ANOVA 3-Arm | **FAIL** | p=0.905 |
| Welch C vs A | **FAIL** | p=0.500 (Arm C ≡ Arm A) |
| Welch C vs B | **FAIL** | p=0.668 |
| OOS Sharpe > 1.0 | **FAIL** | 0.861 |
| Max DD < -20% | **FAIL** | -53.4% |
| Information Ratio > 0.3 | **FAIL** | 0.168 |
| Turnover Drag < 30% | **PASS** | 2.8% |
| Probabilistic Sharpe > 0.95 | **PASS** | 0.983 |
| Calmar Ratio > 2.0 | **FAIL** | 0.515 |

#### Secondary Window (2018–2024): 1/10 Gates Pass
(Only Turnover Drag passes; Probabilistic Sharpe fails at 0.925)

### 12.3 Root Causes (Binding for Next Cycle)

1. **Universe Coverage Collapse** — 27 SEC names → **13 tradable**
   - 9 excluded: **no local price data** (AMZN, COST, CSCO, JPM, MA, NFLX, PYPL, TSLA, WMT)
   - 5 excluded: **no complete fundamental rows** (ACN, JNJ, META, PFE, V — zero shares_outstanding, sparse EBIT)

2. **Arm C ≡ Arm A Degeneracy** (Data Finding, Not Bug)
   - Pre-registered screen (ROIC>10%, ICR>3, FCF Yield>5%) → **singleton qualified set in 71/72 months**
   - Mean qualified size = 1.014, max = 2 → Tier-2 qual tilt trivially equals Arm A

3. **Qualitative Factors Not Contributing**
   - GH/NHTSA/CFPB/ATS/Patents: 2023 sample cohorts → tickers not in tradable 13
   - MDA tone/App Store: 2026 data → **out of 2014-2024 window**
   - Velocity z=0 on months without observations → no tilt signal

4. **SPY Benchmark Bug Fixed Mid-Run**
   - `spy` never wired to GateEngine → Gates 1 & 7 returned `missing_spy_benchmark`
   - Fixed before certification; χ² and IR now compute genuine values

### 12.4 Required CEO Ruling (Per Risk Register §9, Decision Log §10) — **RESOLVED 2026-09-11**

| Option | Action | Gate Impact | Ruling |
|--------|--------|-------------|--------|
| **(A) Fix Coverage** | Acquire price + fundamental data for 14 excluded names; densify universe | Enables real screen diversification, Arm C ≠ Arm A | **EXECUTED via dynamic PIT top-1500** |
| **(B) Revisit Screen** | Deliberately relax ROIC/ICR/FCF thresholds (logged as design change) | Increases qualified set size; must re-register gates | **EXECUTED via sector-relative dual-track** |
| **(C) Add In-Window OOS** | Split primary window for Gate 5 (OOS Sharpe) per pre-registration intent | Addresses `scored_oos=false` flag; statistical rigor | **DEFERRED — Extended window serves this purpose** |

**New Rulings (2026-09-11):**
- Primary window = 2018-2024 (RPO-clean); 2014-2017 dropped
- Extended window = 2020-2026 (full qual coverage) — separate report
- Universe = ~1,500 PIT names (top-1000 + 500 mid/micro) monthly reconstitution
- Screen = Sector-relative dual-track (Mainstream top-25% ∪ Achievers)
- Gates 3 & 4 re-registered for sector-relative hypothesis
- Qualitative PIT: missing → neutral; strict filing-date for fundamentals
- Entity mapping = Heuristic with confidence flags
- ML track = Walk-forward CPCV → symbolic rules; max 40% weight
- Arm C = Full universe qual z-score with neutral fill, zero weight outside quant gate

**Artifacts:** `.agents/project/org/backtests/20260910-phase4-3arm-{primary,secondary}.{md,gates.json}`  
**Validation Reports:** `data/validation/rpo_imputation_report.json`, `glassdoor_ats_granger_report.json`, `gate_calibration_report.json`, `provenance_audit_report.json`

---

**Document Control:** This strategy binds all downstream implementation. Any deviation requires CEO ruling update.

---

## 13. Data Scientist Sanity Check — Executive Overhaul B-20260910-001 (2026-09-10)

### 13.1 Evaluation Summary

| Directive | Verdict | Primary Technical Risk | Required Remedial Condition |
| :--- | :--- | :--- | :--- |
| **Directive 1: Universe Expansion (Top 1,000)** | **CONDITIONAL** | Survivorship bias in backward lookback (2014–2024). | Must construct dynamic PIT universe lists ($U_t$) as top 1,000 market cap on each historical rebalance date $t$, including delisted/bankrupt firms. Forward-fill limited to max 2 trading days; drop illiquid symbols. |
| **Directive 2: Adaptive Moat Filter & Sector Gate** | **CONDITIONAL** | Target leakage in fundamental filings & qualitative timestamps; arbitrary sector quotas. | Enforce strictly SEC filing publication timestamps (`SEC_EDGAR_FILING_DATE`) for ROIC/WACC. Qualitative $z$-scores must use point-in-time point-of-collection timestamps. |
| **Directive 3: Multi-Bagger ML Track (CPCV)** | **CONDITIONAL** | Selection bias (selecting top 10% 10-year winners upfront) & cross-validation leakage. | Do NOT select top 10% post-hoc across 10 years. Machine learning models must train strictly walk-forward on $U_t$ at time $t$. CPCV must enforce an embargo period $\ge$ forward target horizon length (e.g., 12-month embargo for 12-month forward returns). |

### 13.2 Detailed Question Evaluation

1. **Survivorship Bias** — **FAIL (if static top 1,000 today)** / **PASS (if PIT historical top 1,000)**. Current top 1,000 omits fallen giants (e.g., Enron, Sears, SVB). Re-index $U_t$ dynamically per month $t$.

2. **Look-Ahead / Target Leakage** — **CONDITIONAL**. Financial ratios must lag 45 days post-quarter end or use exact SEC Form 10-Q/K filing timestamps. Qualitative velocity metrics must be stamped with scraper log dates.

3. **Sector Quantile Gate** — **PASS with caution**. Top 30% per sector guarantees minimum breadth ($N \ge 20$), resolving the singleton qualified set bug logged at §12.3. Does not invalidate Bonferroni gates, but shifts testing baseline.

4. **CPCV for Reverse Engineering** — **CONDITIONAL**. CPCV handles serial correlation if embargo length $\ge$ prediction horizon. However, pre-filtering the dataset for "top 10% outperformers overall" creates severe selection leakage that CPCV cannot fix. Models must be fit on unselected historical universes.

5. **Data Quality Floor** — **CONDITIONAL**. 5-day forward fill on small-cap Yahoo prices creates zero-volatility artifacts, artificially inflating Sharpe ratios. Restrict forward fill to max 2 days; flag quotes older than 48 hours as un-tradable.

6. **Statistical Power** — **PASS**. Minimum detectable $\Delta\text{Sharpe} \approx 0.075$ at $\alpha=0.0071$, power $= 0.80$, $T=120$ months. Evaluated in §3.

7. **Hybrid Weight Cap (40/60)** — **PASS**. 40% cap bounds empirical ML over-adaptation relative to the 60% structural fundamental moat anchor.

8. **Gate Re-Registration** — **ACTION REQUIRED**. Re-register Gates 3 & 4 in `ingestion/harness/gate_engine.py` to reflect sector-relative thresholds before executing benchmark Arm C evaluations.

### 13.3 Binding Remedial Actions (Pre-Build Gates) — **UPDATED 2026-09-11**

| ID | Action | File Target | Deadline | Status |
|----|--------|-------------|----------|--------|
| RS-01 | Build `universe_builder.py` dynamic PIT top-1500 monthly reconstitution (include delisted) | `ingestion/universe_builder.py` | Before Phase 1 dispatch | **Pending** |
| RS-02 | Update `sec_rpo_parser.py` / `sec_mda_parser.py` to use `FILING_DATE` strictly (not `period_end`) | `ingestion/sec_rpo_parser.py`, `ingestion/sec_mda_parser.py` | Before Phase 1 dispatch | **Pending** |
| RS-03 | Qualitative velocity loaders must add `retrieval_timestamp` + `mapping_confidence` provenance to each row | All Phase 2/3 loaders | Before Phase 2 dispatch | **Pending** |
| RS-04 | Reduce Yahoo price forward-fill to max 2 trading days; add `price_stale_flag` column | `ingestion/price_cache.py` (new) | Before Phase 1 dispatch | **Pending** |
| RS-05 | Re-register Gates 3, 4 (Welch C vs A, C vs B) with sector-relative dual-track hypothesis | `ingestion/harness/gate_engine.py` | Before backtest re-run | **Pending** |
| RS-06 | Implement `research/reverse_engineer_multibaggers.py` with walk-forward on $U_t$, CPCV embargo $\ge$ horizon, symbolic rule extraction | New research module | Parallel research track | **Pending** |
| RS-07 | **NEW** Qualitative coverage audit matrix: (ticker, month, source) → has_data | `validation/qual_coverage_audit.py` | Before backtest re-run | **Pending** |
| RS-08 | **NEW** Arm C conviction sizing: full universe qual z-score with neutral fill | `ingestion/harness/three_arm_runner.py` | Before backtest re-run | **Pending** |

---

## 14. Rebuild Plan Summary (2026-09-11)

### Architectural Decisions Locked
| Decision | Resolution | Document Reference |
|----------|------------|-------------------|
| Universe Scope | ~1,500 PIT names (Top-1000 + 500 mid/micro), monthly reconstitution | §1, §5 |
| Primary Window | 2018-2024 (RPO-clean, 7yr) | §3, §10 |
| Extended Window | 2020-2026 (full qual coverage, separate report) | §3, §10 |
| Screen Design | Sector-relative dual-track: Mainstream (top-25% sector) ∪ Achievers (FCF Margin > sector median 3yr + ROIC-WACC > 2%) | §6, §10 |
| Gate Integrity | Gates 3 & 4 re-registered sector-relative; frozen pre-run; 10k null calibration | §3, §10 |
| Qualitative PIT | Strict: missing → neutral (0.0); 2014-2024 available-only; 2020-2026 full | §4, §6 |
| Entity Mapping | Heuristic with `mapping_confidence ∈ {high, medium, low}`; `low` excluded from core | §5, §8 |
| ML Track | Walk-forward expanding window on $U_t$; embargo ≥ horizon; LASSO+RF → symbolic rules; max 40% | §6, §10 |
| Arm C Design | Full universe qual z-score with neutral fill; zero weight outside quant gate | §6 |
| Delisted Prices | Flag with `price_stale_flag`; exclude from tradable; document survivorship bias | §5, §9 |

### Phase Gates (Must Pass Before Next Phase)
| Phase | Gate | Criteria |
|-------|------|----------|
| 0 → 1 | Specs Approved | You sign off on `sector_screen_v2.md`, `gate_reregistration_v2.md` |
| 1 → 2 | Universe + Core Valid | PIT universe builds; price cache 2-day max; factor store writes; PIT validator rejects lookahead |
| 2 → 3 | Qual Pipeline Loads | All Tier 1-2 sources load with `retrieval_timestamp` + `mapping_confidence`; coverage audit runs |
| 3 → 4 | Gates Calibrated | New Gates 3/4 pass 10k null calibration at α=0.0071; provenance audit 100% |
| 4 → 5 | Backtest Runs | Primary (2018-2024) + Extended (2020-2026) complete; all 10 gates computed |

### Success Criteria for Rebuild
- **Universe:** ≥1,200 tradable names median across 2018-2024 (vs 13 previously)
- **Qualified Set:** ≥20 names median per month in Arm A (vs 1.014 previously)
- **Gate 3/4:** Welch C vs A and C vs B p < 0.0071 (sector-relative thresholds)
- **OOS Sharpe (Arm C):** > 1.0 in primary window
- **Max DD (Arm C):** < -20%
- **PSR:** > 0.95
- **Calmar:** > 2.0
- **Provenance:** 100% Parquet files pass SHA-256 + PIT compliance + mapping_confidence audit

---

**Document Control:** This strategy binds all downstream implementation. Any deviation requires CEO ruling update.