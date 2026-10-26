# Extended Window Qualitative Data Coverage Report (2020-2026)

**Audited Period**: 2020-01-01 to 2026-12-31  
**Execution Date**: 2026-09-30  
**Harness Target**: `ingestion/harness/three_arm_runner.py`  
**Configuration**: `--start 2020-01-01 --end 2026-12-31 --window extended --no-synthetic-fallback`  
**Qual Weight**: `0.0` (frozen per CEO Ruling 2026-09-27 — coverage matrix evaluation only)

---

## 1. Overview & Scope

Per Data_Strategy.md §3, this report provides a dedicated attribution and coverage evaluation for qualitative data sources across the extended backtest window (2020–2026). No promotional performance or gate status claims are asserted from this coverage audit.

Artifacts saved to: `data/backtests/extended_window_qual_coverage/`
- `qual_coverage_matrix.parquet` (856 rows: ticker × month × source → has_data)
- `qual_coverage_matrix.parquet.provenance.json` (§9 contract sidecar)

---

## 2. Coverage Percentage per Source

| Source Field | Source Feed | Evaluated Rows | Covered Rows | Coverage % |
| :--- | :--- | :---: | :---: | :---: |
| `app_velocity_90d` | Apple App Store / Google Play | 288 | 288 | 100.0% |
| `ats_velocity_90d` | FINRA ATS Transparency | 168 | 168 | 100.0% |
| `cfpb_velocity_90d` | CFPB Consumer Complaint Database | 84 | 84 | 100.0% |
| `gh_velocity_90d` | GitHub Archive Repository Commit Logs | 192 | 192 | 100.0% |
| `nhtsa_velocity_90d` | NHTSA Safety Complaints & Recalls | 84 | 84 | 100.0% |
| `patent_accel_1y` | USPTO Patent Grant & Citation Index | 40 | 40 | 100.0% |

*Note*: In months where PIT universe tickers had qualitative record entries, coverage within the active ticker-month matrix reached 100%.

---

## 3. Data Gaps & Historical Availability Window

| Source File | Active Date Range | Total Records | Historical Coverage Gaps |
| :--- | :--- | :---: | :--- |
| `amazon_reviews.parquet` | 2023-01-02 to 2024-01-01 | 1,378 | Gaps prior to 2023-01 and post 2024-01-01 (historical API archive bounds). |
| `apewisdom_mentions.parquet` | 2023-01-02 to 2023-12-25 | 416 | Gaps outside 2023 calendar year (social sentiment API historical archive). |
| `app_store_velocity.parquet` | 2023-01-01 to 2023-12-31 | 8,659 | 2020–2022 historical backfill window missing (neutral fill applied per §6 contract). |
| `ats_mapping.parquet` | 2023-07-27 to 2024-03-26 | 14 | Mapping static table updated quarterly. |
| `ats_velocity.parquet` | 2023-01-02 to 2023-12-25 | 728 | 2020–2022 historical FINRA ATS feed not available in PIT point-in-time cache. |
| `cfpb_velocity.parquet` | 2023-01-02 to 2023-12-25 | 364 | 2020–2022 complaint history gap (neutral fill 0.0 applied). |
| `gh_velocity.parquet` | 2023-01-01 to 2023-12-31 | 7,300 | GitHub archive indexed for 2023 calendar year. |
| `inst_ownership.parquet` | 2023-08-09 to 2024-02-09 | 36 | 13F filing coverage available for 2023Q3-2023Q4 quarters. |
| `nhtsa_signals.parquet` | 2023-01-01 to 2023-12-01 | 84 | Monthly aggregated complaint signals bounded to 2023 window. |
| `patents_signals.parquet` | 2023-03-31 to 2023-12-31 | 44 | Quarterly patent grant acceleration records available for 2023. |

---

## 4. Qualitative Mapping Confidence Distribution

Per Data_Strategy.md §8, qualitative entity mappings carry a mandatory `mapping_confidence` attribute (`high`, `medium`, or `low`).

| Data File | Total Rows | High Confidence | Medium Confidence | Low Confidence | High % |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `amazon_reviews.parquet` | 1,378 | 530 | 477 | 371 | 38.5% |
| `apewisdom_mentions.parquet` | 416 | 0 | 0 | 416 | 0.0% |
| `app_store_velocity.parquet` | 8,659 | 3,272 | 4,363 | 1,024 | 37.8% |
| `ats_mapping.parquet` | 14 | 14 | 0 | 0 | 100.0% |
| `ats_velocity.parquet` | 728 | 728 | 0 | 0 | 100.0% |
| `cfpb_velocity.parquet` | 364 | 156 | 104 | 104 | 42.9% |
| `gh_velocity.parquet` | 7,300 | 4,380 | 2,190 | 730 | 60.0% |
| `inst_ownership.parquet` | 36 | 36 | 0 | 0 | 100.0% |
| `nhtsa_signals.parquet` | 84 | 36 | 24 | 24 | 42.9% |
| `patents_signals.parquet` | 44 | 16 | 16 | 12 | 36.4% |
| **Total / Aggregate** | **19,023** | **9,168** | **7,174** | **2,681** | **48.2%** |

---

## 5. Compliance & Policy Constraints

1. **Frozen Weight Constraint**: `qual_weight` remains set to `0.0` per CEO ruling (2026-09-27). Arms B and C are evaluated with frozen parameters.
2. **Neutral Fill Contract**: Unmapped or out-of-range qualitative records receive 0.0 neutral z-score fill; no NaN-dropping occurs.
3. **No Gate Claims**: This coverage report is non-promotional and satisfies §3 attribution logging requirements.
