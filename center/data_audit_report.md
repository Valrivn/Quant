# DATA UNIVERSE COVERAGE & QUALITY AUDIT REPORT

**Date**: 2026-09-15  
**Auditor**: Data Scientist (`datasource-worker`)  
**Scope**: Comprehensive Audit of Data Infrastructure, Fundamental Universe, Point-In-Time Integrity, Survivorship Bias, and Backtest Readiness  

---

## Executive Summary & Readiness Verdict

| Metric | Current State | Target Requirement | Status |
| :--- | :--- | :--- | :--- |
| **Master Price Universe** | 2,000 Tickers | 1,500+ Tickers | ✅ PASS |
| **PIT Monthly Reconstitution Universe** | 1,902 Tickers (84 months: 2018–2024) | 1,500+ Tickers | ✅ PASS |
| **Tickers with 3+ Years Fundamental Data** | **100 CIKs / 145 Tickers** | **1,500 Minimum** | ❌ **FAIL (-1,355 Short)** |
| **Statement Completeness (Ingested Tickers)** | 100% across 22 XBRL concepts | 95%+ | ✅ PASS |
| **Point-In-Time (PIT) Publication Timestamps** | 100% filing date keyed (`filed`) | Zero Lookahead | ✅ PASS |
| **Survivorship Bias Coverage** | 107 delisting events (4 CIKs) | Full Historical Index | ⚠️ DEGRADED |

### ⛔ **READINESS VERDICT: NOT READY FOR PRODUCTION BACKTEST**
The fundamental data pipeline currently holds **only 100 CIKs (145 ticker symbols)** with continuous 3+ years of SEC EDGAR XBRL fundamentals in `data/sec_facts/facts/facts.parquet`. While the pricing engine and universe reconstitution tables track 1,902 tickers, **1,757 tickers lack fundamental filings in the data lake**. Proceeding with a multi-factor or valuation backtest under the current database state will result in severe selection bias and sample starvation.

---

## 1. Universe Count & Breakdown

### Master Price & Reconstitution Universe (1,902 Tickers)
The monthly reconstitution dataset (`data/universe/monthly_reconstitution.parquet`) and PIT universe (`data/universe/pit_universe_2018_2024.parquet`) cover **1,902 unique tickers** rebalanced monthly from **2018-01-31 to 2024-12-31** (84 rebalance periods).

#### Sector Breakdown (Unique Tickers)
```
Industrial   :  297 tickers (15.6%)
Software     :  292 tickers (15.4%)
Consumer     :  272 tickers (14.3%)
Hardware     :  263 tickers (13.8%)
Energy       :  264 tickers (13.9%)
Financial    :  260 tickers (13.7%)
Healthcare   :  254 tickers (13.4%)
Total        : 1,902 tickers (100.0%)
```

#### Market Cap Bucket Coverage (Ever Active)
- **Large Cap** ($10B+): 1,892 tickers
- **Mega Cap** ($100B+): 1,393 tickers
- **Mid Cap** ($2B - $10B): 1,127 tickers

---

## 2. Data Source Lineage & Coverage

| Source Name | System Location / File | Entity Count | Coverage Date Range | Ingestion Mechanism | Update Freq |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **SEC EDGAR XBRL** | `data/sec_facts/facts/facts.parquet` | 100 CIKs (145 tickers) | 2017-01-01 to 2024-12-31 | SEC `companyfacts` REST API | Quarterly / Annual |
| **SEC CIK Mapping** | `reddit_quant.db` (`sec_cik_map`) | 10,692 Ticker-CIK pairs | 1999 to 2026 | SEC `company_tickers.json` | Daily |
| **Master Price Engine** | `data/prices/price_cache.parquet` & `data/master_prices/*.parquet` | 2,000 Tickers | 2018-01-01 to 2026-09-13 | Yahoo Finance / Polygon API | Daily |
| **PIT Universe Recon** | `data/universe/monthly_reconstitution.parquet` | 1,902 Tickers | 2018-01-31 to 2024-12-31 | Dynamic market cap top-1500 screen | Monthly |
| **Delisting Tracker** | `data/universe/delisting_events.parquet` | 4 CIKs (107 records) | 2018-01-01 to 2026-09-13 | SEC Form 8-K (Item 3.01) / 25 / 15 | Event-driven |
| **Glassdoor / Comparably**| `reddit_quant.db` (`glassdoor_snapshots`) | 26 Tickers | 2023-01-01 to 2024-03-31 | Distributed SERP API / Scraper | Weekly |
| **GitHub Org Metrics** | `reddit_quant.db` (`github_org_metrics`) | 16 Tickers | 2023-01-01 to 2023-12-31 | GitHub REST API | Daily |
| **App Store Velocity** | `data/qual/app_store_velocity.parquet` | 24 Tickers | 2023-01-01 to 2023-12-31 | App Store RSS Feed | Daily |
| **ApeWisdom Alt-Data** | `reddit_quant.db` (`apewisdom_history`) | 10 Tickers | 2023-01-01 to 2023-12-31 | ApeWisdom Scraper | Daily |
| **FRED / ALFRED** | `data_lake/fred` & `alfred` | Macro Series | 1999-01-01 to 2026-09-01 | FRED API / Vintage Collector | Monthly |

---

## 3. Fundamental Data Quality & Gap Analysis

### Completeness % Per Statement Type
For the **100 CIKs** currently ingested into `data/sec_facts/facts/facts.parquet`, statement completeness across 22 standardized US-GAAP concepts is **100%**:

- **Income Statement (5/5 Key Concepts - 100%)**:
  - `us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax`
  - `us-gaap:OperatingIncomeLoss`
  - `us-gaap:EarningsBeforeInterestAndTaxes`
  - `us-gaap:InterestExpense`
  - `us-gaap:IncomeTaxExpenseBenefit`
- **Balance Sheet (6/6 Key Concepts - 100%)**:
  - `us-gaap:Assets`
  - `us-gaap:Liabilities`
  - `us-gaap:StockholdersEquity`
  - `us-gaap:CashAndCashEquivalentsAtCarryingValue`
  - `us-gaap:LongTermDebtNoncurrent`
  - `us-gaap:DebtAndCapitalLeaseObligations`
- **Cash Flow Statement (2/2 Key Concepts - 100%)**:
  - `us-gaap:NetCashProvidedByUsedInOperatingActivities`
  - `us-gaap:PaymentsToAcquirePropertyPlantAndEquipment` (CapEx)

### Point-In-Time Availability (Lookahead Defense)
- All records in `facts.parquet` store the exact SEC EDGAR acceptance date in the `filed` column (e.g. FY2017 filed on `2018-01-15`).
- Fundamental feature joins enforce `as_of_date >= filing_date`, guaranteeing zero lookahead bias.

### XBRL Concept Coverage for Core Metrics
- **ROIC**: $EBIT \times (1 - t) / (\text{Debt} + \text{Equity} - \text{Cash})$ — **100% Covered** for ingested CIKs.
- **FCF**: $\text{Operating Cash Flow} - \text{CapEx}$ — **100% Covered** for ingested CIKs.
- **WACC Components**: Cost of Debt ($Interest / Total Debt$) and Capital Structure weights — **100% Covered** for ingested CIKs.

### Key Data Gaps
1. **Universe Ingestion Gap (-1,757 Tickers)**: Only 100 CIKs have been parsed into `facts.parquet`. 1,752 of the 1,902 tickers in the reconstitution universe are completely missing from the fundamental lake.
2. **Frequency Gap (Annual vs. Quarterly)**: `facts.parquet` currently contains 8 annual 10-K cycles per CIK (2017–2024), but lacks 10-Q quarterly filing steps for intra-year factor updating.

---

## 4. Survivorship Bias & Database State Audit

### Database State (`reddit_quant.db`)
- `reddit_quant.db` contains 49 SQLite tables, primarily supporting alt-data, sentiment vectors, Glassdoor audit, hiring velocity, and regime tracking.
- Fundamental tables inside SQLite are sparse samples:
  - `sec_xbrl_facts`: 698 rows across 10 tickers.
  - `quantitative_dcf_floor`: 2,745 rows across 11 mega-cap tickers.
  - `historical_5y_slice`: 50 rows across 10 tickers.

### Survivorship Bias Verification
- `delisting_events.parquet` currently records **107 delisting events** across **4 CIKs**.
- Because the fundamental ingestion was restricted to 100 surviving mega/large-cap tickers, historical delisted equities (e.g., companies that went bankrupt or were acquired between 2018 and 2024) are absent from `facts.parquet`.

---

## 5. Specific Remediation Steps (Path to 1,500+ Tickers)

To reach full readiness for a production-grade 1,500-ticker backtest:

### Phase 1: Full SEC EDGAR Batch Ingestion (ETA: 5 Minutes)
1. Run `discovery/sec_pit_client.py` using the 10,692 CIK mapping from `sec_cik_map` to download `companyfacts.json` for all **1,902 tickers** in `monthly_reconstitution.parquet`.
2. At the SEC rate limit of 9 req/s, ingesting `companyfacts` for 1,900 tickers will take **~210 seconds (~3.5 minutes)**.

```bash
python -m discovery.sec_pit_client --tickers-from-db --rate-limit 9
```

### Phase 2: Expanded XBRL Fact Extraction & Parquet Storage
1. Update `discovery/xbrl_parser.py` to extract both **10-K and 10-Q filings** (annual + 3 quarterly filings per year) across the 22 core US-GAAP concepts for all 1,902 CIKs back to 2018.
2. Re-build `data/sec_facts/facts/facts.parquet` and store in `data/pit_fundamentals/`.

### Phase 3: Delisting & Historical CIK Backfill
1. Query SEC EDGAR `master.idx` historical filing archives from 2018–2024 to identify all delisted CIKs filing 10-K/10-Q forms during the sample period.
2. Append historical CIK facts and Form 25/15 delisting records to `delisting_events.parquet`.

### Phase 4: Gate Re-Verification
1. Run `python scripts/pit_phase0_audit.py` to verify that **>= 1,500 tickers** possess 3+ continuous years of PIT fundamental data with 0 lookahead bias.
2. Re-issue backtest clearance once gate clears 1,500 tickers.
