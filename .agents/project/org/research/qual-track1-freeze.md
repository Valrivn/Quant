# Track 1 Extension — QUAL-T1-20260820 (all qualitative lanes)

**Rule:** D-20260820-001 | **Registered:** 2026-08-20 | **Status:** CLOCK RUNNING
**Companion freeze:** WIKI-T1-20260820 (same checkpoints, same discipline)

## What was frozen

Manifest: `data/qual_track1_freeze_20260820.json`
(manifest SHA-256 `e241c6fe…51088e56`; per-DB SHA-256 inside the manifest).

| Lane | Table(s) | Rows frozen |
|---|---|---|
| **Glassdoor** | glassdoor_snapshots (+audit) | 2,955 (+2,288) — YES, Glassdoor has data |
| Comparably | comparably_snapshots | 2,379 |
| G2/Capterra | g2_capterra_reviews | 300 |
| App Store | app_store_feeds | 300 |
| Product intel | product_intel_reviews | 18,496 |
| Hiring (Indeed/Adzuna/JobSpy) | indeed/adzuna/jobspy/hiring_velocity | 610+610+3,173+2,403 |
| GitHub orgs | github_org_metrics + sentinel_github_snapshots | 550+67 |
| Instagram | instagram_raw_mentions + qual proxies + telemetry | 1,972+219+118 |
| Reddit sentiment | daily_aggregations | 34,975 |
| Psych regimes/velocity | psychological_regimes/vectors, velocity_snapshots | 8,015+1,001+2,001 |
| Consensus gate | consensus_company_rows + flags | 55+44 |
| Wiki lane | wikidata_companies/wiki_edges/wiki_runs | 15,591+11,155+3 |

Full row counts, timestamp ranges, distinct-ticker counts, and both database
hashes are in the manifest. The DB files themselves are additionally committed
to git at this date (second tamper-evidence layer).

## Pre-registered forward bars (set 2026-08-20)

Same mechanical rules as WIKI-T1 (adjusted closes, SPY benchmark, T+1 fill,
no cohort edits). Per-lane bar at E4 FINAL 2027-08-20:

- Q1: For each lane, the top-decile signal names as-of freeze date must beat
  SPY over the forward window on median (signal direction as computed by the
  existing engines — no re-tuning).
- Q2: A lane that fails Q1 AND shows staleness >40% (dead tickers among its
  covered set) is demoted one tier in the discovery feed.
- Q3: Lanes passing Q1 with ≥10 covered tradeable names graduate to
  historical-extension candidates using the catalog sources
  (research/historical-qualitative-data-sources.md).

Checkpoints shared with WIKI-T1: 2026-11-20 / 2027-02-20 / 2027-05-20 /
**2027-08-20 FINAL**. Rulings only at checkpoints.

## Answer recorded for the CEO's question

"Can we freeze-stamp Glassdoor?" — **Yes**: 2,955 snapshot rows existed and
are now stamped. Every qualitative lane in the house is now under a forward
clock; nothing needed to be discarded for lack of data except raw Reddit
submissions (0 rows — aggregates only), which limits per-post replay but not
aggregate-signal evaluation.
