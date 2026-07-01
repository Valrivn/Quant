# Lane Gamma Results — Free Live Data Ingestion

**Timestamp:** 2026-06-28T23:33:27Z
**Elapsed:** 205.7s
**Script:** `opencode_scripts/lanes/lane_gamma/ingest_all.py`

## Execution Summary

| Task | Result | Details |
|------|--------|---------|
| 1. SEC EDGAR XBRL Backfill | ✅ | 62 unique facts across 9/10 tickers (AVGO 404 — CIK 1730167 has no `RevenueFromContractWithCustomerExcludingAssessedTax` fact) |
| 2. GitHub Org REST API | ✅ | 450 repos across 9/10 orgs (Amazon org API returns 404 — `amazon` GitHub org may require authenticated endpoints or different org handle) |
| 3. Reddit Daily Aggregations Verify | ✅ | 34,340 records, range 2021-01-01 to 2026-06-26, all tickers except INTC have data |
| 4. Glassdoor Current Snapshot | ⛔ | All 10 tickers returned HTTP 403 (Cloudflare WAF) despite UA rotation and 5-12s delays |
| 5. DB Schema | ✅ | `sec_xbrl_facts` and `github_org_metrics` tables created in schema.py and reddit_quant.db |
| 6. Data Completeness Audit | ✅ | Written to `lane_results/data_completeness_audit.md` |

## Gaps Identified

| Gap | Ticker(s) | Impact | Remediation |
|-----|-----------|--------|-------------|
| No SEC XBRL revenue fact | AVGO | Missing financial fundamental signal | Use CIK 1730167 submissions API instead; XBRL taxonomy may use different fact name (e.g., `RevenueFromContractWithCustomer`) |
| No GitHub org repos | AMZN | Missing developer metrics | Try `amzn` or other org handles; or use search API instead of org endpoint |
| No INTC Reddit aggregations | INTC | Intel missing from sentiment pipeline | Check subreddit category filters or mention resolution; may need entity resolution debug |
| Glassdoor 403 all tickers | All | No current employer rating | Upgrade to nodriver/browserless fallback chain with CDP stealth in corp_audit.py |

## Data Volume

| Table | Records | Source |
|-------|---------|--------|
| `sec_xbrl_facts` | 62 | SEC EDGAR XBRL API |
| `github_org_metrics` | 450 | GitHub REST `/orgs/{org}/repos` |
| `glassdoor_snapshots` | 2,389 (12 new today) | Current snapshot (403) + historical |
| `daily_aggregations` | 34,340 | Pre-computed Reddit pipeline |
