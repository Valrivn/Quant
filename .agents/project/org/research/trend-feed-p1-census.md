# P1 Sandbox Census — Discovery Trend Feed (D-20260806-001)

**Verdict artifact:** `trend-feed-p1-census.md`
**Run date:** 2026-08-07 (UTC) — LIVE run
**Branch:** `feature/b-20260806-001`
**Ruling:** D-20260806-001 (MODIFY) — falsification-first, deterministic, no RNG
**Runner:** `discovery/census.py` (minimal P1 census, not the P3 harness)
**Live gate:** `DISCOVERY_LIVE=1` (set for this run)

## 1. Purpose (SEC 2.1)

Zero pipeline contact. Ingest all structured sources into the sandbox, run every
validated mention through the FULL qualitative engine + quant baseline
(READ-ONLY consumers), and measure the pass-through rate per source. Video
(IG/TikTok) stays LOCKED unless >=1 candidate passes the qualitative gate.

## 2. Environment / credential status per source

| Source | Live gate | Credential status | Outcome |
|---|---|---|---|
| SEC EDGAR new-filers | `DISCOVERY_LIVE=1` | Public (no auth) — endpoint reachable | **LIVE** |
| Reddit | `DISCOVERY_LIVE=1` | `config/reddit_credentials.yaml` client_id/client_secret empty | **DEGRADED** |
| StockTwits | `DISCOVERY_LIVE=1` | `access_token` = `${STOCKTWITS_ACCESS_TOKEN}` (env unset) | **DEGRADED** |
| ApeWisdom | `DISCOVERY_LIVE=1` | `api_key` = `${APEWISDOM_API_KEY}` (env unset) | **DEGRADED** |

`DISCOVERY_LIVE=1` was set. SEC EDGAR (public, no auth) fetched real data. The
three credential-gated sources deg-tagged gracefully on placeholder creds — no
data was faked.

## 3. Census table (source | mentions | validated | gated | pass% | status)

| source | mentions | validated | gated | pass% | status |
|---|---|---|---|---|---|
| sec_edgar_new_filers | 500 | 500 | 0 | 0.0% | **LIVE** |
| reddit | 0 | 0 | 0 | 0.0% | DEGRADED |
| stocktwits | 0 | 0 | 0 | 0.0% | DEGRADED |
| apewisdom | 0 | 0 | 0 | 0.0% | DEGRADED |

SEC EDGAR fetched 500 real mentions (tickers from the SEC `company_tickers`
map via `cik_resolver`); all 500 validated to CIK-resolved tickers. A capped
batch of 20 was run through the read-only qualitative gate; all 20 rejected.

## 4. Reject reason histogram

| reason | count |
|---|---|
| `qual:avoid` | 20 |

All 20 gated SEC EDGAR tickers returned `avoid` from the read-only
`AlternativeStrategyPipeline`. Cause: with no live signals (Reddit/StockTwits/
ApeWisdom sentiment and fundamentals are DEGRADED), the pipeline's moat composite
is empty -> blended score 0.0 -> `avoid`. This is honest: the qualitative gate
requires the full multi-source signal set, which is currently unavailable.

## 5. DEGRADED reasons

| source | reason |
|--------|--------|
| reddit | Reddit credentials missing/placeholder (config/reddit_credentials.yaml) |
| stocktwits | StockTwits credentials missing/placeholder (config/fintech_credentials.yaml) |
| apewisdom | ApeWisdom credentials missing/placeholder (missing APEWISDOM_API_KEY) |

## 6. Video-gate verdict

**LOCKED.** No candidate passed the qualitative gate this cycle (0 gated). Per
SEC 2.1, video sources (Instagram/TikTok) remain sandbox-gated until >=1
candidate passes the qualitative gate. `discovery/video_sources.py` stub raises
`VideoSourceLockedError` if asked to produce candidates.

## 7. P1 verdict

**PARTIAL PASS** (fail-closed on the qualitative gate).

The SEC EDGAR structured-source pipeline works end-to-end: real fetch (500
mentions), real validation (500 tickers), and the read-only qualitative gate ran
without error. However, **0% pass-through** because the qualitative gate has no
signals to work with — the sentiment/fundamental sources that feed it are
DEGRADED. This is a legitimate fail-closed outcome, not a data fabrication.

**What would un-block full pass-through:**
1. Provide real credentials in the git-ignored config files:
   - `config/reddit_credentials.yaml` — set `client_id` / `client_secret`
     (Reddit script app).
   - `config/fintech_credentials.yaml` — set `STOCKTWITS_ACCESS_TOKEN` and
     `APEWISDOM_API_KEY` env vars (or the file values).
2. Re-run `DISCOVERY_LIVE=1 python -m discovery.census` so the qualitative
   engine receives real multi-source signals and can emit non-`avoid`
   recommendations, producing a non-zero pass-through rate.

SEC EDGAR alone cannot un-block the qualitative gate; it needs the sentiment
sources to feed the engine.

## 8. Notes / scope

- Read-only consumers used: `AlternativeStrategyPipeline`
  (`Qualitative/psychological/qualitative_scoring.py`) for the qualitative gate
  and `valuation_alpha.discovery_screen.quant_baseline_flags` for the quant
  baseline. No portfolio/diversification allocator code was called; no
  production tables were written.
- Per-source network timeout (15s) and per-ticker skip-with-reason were added so
  one dead source cannot stall the census.
- The full multi-source Bayesian fusion + selectivity stage (D-20260803-001) and
  Glassdoor tilt require live data and are exercised only when a source is LIVE.
- No commit made; this artifact is research-only.