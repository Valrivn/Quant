# System Audit — errors.md

**Audited:** 2026-08-30 · **Auditor:** big-pickle (primary session, opencode)
**Scope:** full pipeline audit — qualitative → quant → backtest, discovery, data layer.
**Summary of the day's runs:**
- `python -m pytest tests/ -q` → **1312 passed, 18 skipped, 0 failed** (test suite is green).
- `python run_bt.py` (spy) → **AUDITED CLEAN**, complete.
- `python run_ig_llm.py` (ig-llm) → **BACKTEST_SUCCESS** but **labelled "AUDITED CLEAN (env-degraded)"** with multiple degraded inputs.
- `python run_backtest_write.py` → Success (rewrites registry row dated 20260821).
- `python scripts/wiki_p1_probe.py` → **DEGRADED: DISCOVERY_LIVE!=1** (fail-closed, expected).
- `python scripts/consensus_pipeline.py --seed` → OK.
- `python scripts/test_gdelt_probe.py` → OK (200, 2499 articles).
- `python scripts/pit_phase0_audit.py` → phase0 helper self-check ok.

**Headline:** The test suite passes, but **the qualitative→quantitative leg of the backtest is operating on failed/fallback data presented as a real signal**. The `ig-llm` backtest trades **zero** IG-LLM candidates yet reports a "SYSTEMATIC (p<alpha)" verdict — it is a relabeled baseline, not a qualitative-signal backtest.

---

## ⭐ Master priority ranking (fix THIS order)

| # | Severity | ID | Error |
|---|----------|----|-------|
| 1 | P0 | P0-1 | IG-LLM proxies are 100% LLM-fallback garbage, read as real signals (false "SYSTEMATIC" backtest) |
| 2 | P0 | P0-3 | No point-in-time (PIT) history on qualitative proxies → look-ahead (D1) |
| 3 | P0 | P0-2 | Proxy table polluted with 63+ fake/non-listed tickers |
| 4 | P0 | P0-4 | Qualitative gate is buy-only; rejected names can never re-enter (D2) |
| 5 | P0 | P0-5 | Sentinel flag: `lookahead_bias_leakage_detected` (external, unresolved) |
| 6 | P1 | P1-1 | yfinance price failures on 25 symbols incl. SPY (stale/partial fallback undetected) |
| 7 | P1 | P1-5 | SPY baseline: positive excess vs SP500 but negative FF5 alpha ✅ (return-basis + window + coverage fixed) |
| 8 | P1 | P1-3 | FRED unreachable → macro DEGRADED (price-proxy fallback) |
| 9 | P1 | P1-4 | FF5 factors unavailable → alpha_ff5 n/a on ig-llm |
| 10 | P1 | P1-2 | Dividend/price CSVs written into repo working tree ✅ (cache → AppData; untracked) |
| 11 | P2 | P2-1 | Silent-failure error swallowing with no DEGRADED flag (D3) |
| 12 | P2 | P2-3 | Hardcoded `spearman_ic = 0.27` in Lane 4 ✅ (now None — no fake IC) |
| 13 | P2 | P2-2 | Missing libs: redis, pandas_datareader, undetected_chromedriver, etc. ✅ (installed) |
| 14 | P2 | P2-4 | Qualitative scrapers degraded — GitHub 404s fixed via D-20260830-001 (ticker-aware applicability); Adzuna 401/browserless/nodriver infra outstanding |
| 15 | P2 | P2-5 | Build/branch hygiene: dirty repo, feature branch not main |
| 16 | P3 | P3-1 | Namespace-package fragility (`Qualitative` no `__init__.py`) |
| 17 | P3 | P3-2 | `run_backtest_write.py` writes stale 20260821 registry row on re-run |
| 18 | P3 | P3-3 | Test/deprecation warnings (asyncio marks, utcnow) |
| 19 | P3 | P3-4 | `supervisor.log` is raw terminal-escape bytes (noise) |

---

## P0 — Blocking (integrity / wrong results)

### P0-1. IG-LLM qualitative proxies are 100% LLM-fallback garbage, read as real signals
- **Evidence:** `SELECT audit_trail, COUNT(*) FROM instagram_qual_proxies` →
  - `218 / 219` rows = `"Default fallback due to LLM timeout/error."`
  - `1 / 219` row = an explicit *rejection* ("All 36 database mentions are false positives of the common English word 'now'").
- **Where the garbage is written:** `scripts/ig_llm_synthesis.py::_fallback_proxy()` writes hardcoded `product_adoption=1.0, competitive_disruption=0, sentiment_score=0.0` with that audit_trail — every LLM timeout/empty write produces this.
- **Why it's silently treated as real:** `discovery/gate_data.py::qualitative_signals()` (lines 267-323) reads the rows and stamps provenance `IG_LLM_proxy:...` **without checking the audit_trail for fallback/degraded status**. Its outer `except Exception: pass` (lines 321-322) and inner `except:` (286-287) swallow DB errors with no DEGRADED flag → D3 confirmed at its worst.
- **Net effect verified live:** `_ig_llm_passed_candidates()` returned **`[]` (0 buy-class)** for all 219 tickers. Every ticker (AAPL, NVDA, EAT, GOLD, SPY, ...) evaluates to `reduce`. The qualitative gate therefore **injects zero IG_LLM_<T> candidates**.
- **Consequence (verified):** `run_ig_llm.py` still prints `BACKTEST_SUCCESS`, 51 trades, `chi2` `SYSTEMATIC (p<alpha)`, and `alpha` — but the `RM-IG-LLM` vpath is literally `RM-FINAL` renamed (`run_sim_discovery_ig_llm`, fee_sim3.py:1131-1139). **The IG-LLM backtest reports an edge it did not trade.** A user opening `run_ig_llm` / `/backtest ig-llm` sees a false positive.
- **Fix direction (per CEO D-20260829-001 MODIFY):**
  1. `qualitative_signals` must inspect `audit_trail`; any `fallback/timeout/error` row must NOT be emitted as `IG_LLM_proxy` provenance — it must set a **DEGRADED** flag (and ideally not feed the gate at all).
  2. `_ig_llm_passed_candidates` / `run_sim_discovery_ig_llm` must record and surface "IG-LLM candidates = 0 (LLM unreachable)" instead of silently returning baseline results under the IG-LLM label.
  3. `run_standard_backtest` should raise or mark `DEGRADED-DATA` (not `AUDITED CLEAN`) when `_ig_llm_passed_candidates` yields 0 or when proxies are fallback.

### P0-2. Instagram proxy table is polluted with fake / invalid tickers
- **Evidence:** `instagram_qual_proxies` holds **219 distinct tickers**; only **2** (`MO`, `SPY`) are within the backtest's known universe. **217** would need a price fetch. Heuristic scan flags **63** as likely garbage English-word / non-ticker `ANY, BAR, BIT, BULL, CAR, CARD, CASH, EAT, FUN, FUND, GAME, GOLD, HELP, HIT, KEY, LAND, LINK, LIVE, LOW, MOVE, NET, NOW, PAY, PLAY, POST, PURE, REAL, RUN, SAFE, SELF, SON, STEP, TALK, VIA, WAVE, WWW, ZONE, ...` plus crypto `BTC, ETH, XRP`.
- **Where it comes from:** Instagram captions are tokenized to tickers in the scraper; English words are matched as if they were tickers.
- **Consequence:** even when the LLM succeeds, the candidate set is dominated by invalid symbols; the qualitative gate (and any future injection) is reasoning over noise.
- **Fix direction:** validate tickers against a real ticker master / exchange listing before inserting into `instagram_qual_proxies`.

### P0-3. No point-in-time (PIT) history on qualitative proxies (look-ahead — matches D1)
- **Evidence:** `scripts/migrate_db.py:115-126` — table has `UNIQUE(ticker)` and a single `created_at`; `upsert_qual_proxy` (`ig_llm_synthesis.py:314-337`) uses `ON CONFLICT(ticker) DO UPDATE ... created_at=excluded.created_at`. Rows are **overwritten every weekly run**; only the *latest* synthesis exists.
- **DB state confirms this:** all 219 rows were written in a single ~6-minute window `2026-08-15 23:16 → 23:22` (18 distinct timestamps). There is **no history** to backtest a 1-year+ qualitative signal out-of-sample.
- **Consequence:** `_ig_llm_passed_candidates` (`fee_sim3.py:1076-1078`) selects *today's* proxies and injects them into historic prices — **look-ahead / buy-only survivorship** (D1).
- **Fix direction:** migrate to a PIT table keyed `(ticker, valid_from)` (append, not overwrite) + a clock-stepped reader (align with the existing `db/pit_reader.py` / D-20260823-001 PIT sandbox).

### P0-4. Qualitative gate is buy-only; rejected names can never re-enter (matches D2)
- **Evidence:** `fee_sim3.py:1103-1104` — only `recommendation in ("strong_buy", "buy")` are collected; all else dropped. There is no sell-side / re-admission path. The backtest never lets a company that "pops later" be traded after an earlier rejection.
- **Fix direction:** model a full decision state (hold/tilt/reduce/avoid → tradeable universe), track entry/exit, allow later re-admission.

### P0-5. Sentinel flag: look-ahead bias leakage detected
- **Evidence:** `.relaunch_requested` contains `lookahead_bias_leakage_detected`. No Python source sets this flag (repo-wide grep found nothing), so it is an external/manual sentinel. It corroborates P0-1/P0-3 above.
- **Action:** resolve P0-1/P0-3/P0-4, then clear the flag; keep it as the regression tripwire.

---

## P1 — Data / environment degradation (backtest correctness margins)

### P1-1. yfinance price download failures on core benchmark + full sleeve
- **Evidence (`run_ig_llm`):** `25 "possibly delisted; no price data found (1d 2015-01-01 -> 2026-07-31)"` for `IAU, PEP, MDY, VZ, KMB, JNJ, VCSH, KO, SPY, MO, SHY, HD, IWM, GIS, O, HYG, SGOV, VCIT, BIL, TGT, LQD, PG, GLD, MCD, CL` — **including SPY itself**. Seen twice (two fetches).
- **Where:** `diversification/fee_sim3.py::fetch_sleeve_prices` / `fetch_dividend_history` hitting yfinance.
- **Consequence:** if the price/benchmark can't download, the run silently proceeds on locally cached CSVs (see P1-2) or partial series. `audit_status` does **not** flag a yfinance download failure (it only checks FF5/FRED/div). A baseline with missing data still prints numbers — risk of stale/partial results.
- **Fix direction:** fail or DEGRADE when SPY/benchmark prices are unavailable or when `fetch_all()` returns a partial/incomplete price frame; a stale local cache must be marked, not quietly trusted.

### P1-2. Local dividend/price CSVs written into the repo working tree ✅ RESOLVED (2026-08-30)
- **Evidence:** `git status` shows ~30 *untracked* `data/dividends/*.csv` (AAPL, ACT, AG, AGO, AMAT, APHL, ASML, ASX, AU, AUGO, BABA, BAC, BAM, ...) plus many *modified* tracked ones (BIL, CL, GIS, HD, IWM, JNJ, ...), plus `data/sentinel.db`, `reddit_quant.db`, `logs/quant_pipeline.log` all dirty.
- **Provenance:** `fetch_dividend_history` writes per-ticker dividend CSVs under `data/dividends/`, including bogus tickers (see P0-2).
- **Consequence:** repo pollution, gigabytes of churn, and the "possibly delisted" downloads fall back to these CSVs.
- **Fix (done):** `diversification/datastore.py` `CACHE_DIR` now resolves to `%LOCALAPPDATA%\HouseOfQuant\cache` (out of repo), so `fetch_dividend_history`/`fetch_nasdaq` no longer write into `data/`. `.gitignore` adds `data/dividends/`, `data/nasdaq/`, `data/edgar_cache/`, `data/phrasebank_cache/`, `data/sentiment_training_cache/`, `data/sec_datasets/`. CEO-approved `git rm -r --cached data/dividends data/nasdaq` (2026-08-30) untracked the 21+8 stale CSVs from the index (files remain on disk, now ignored).

### P1-3. FRED unreachable → macro DEGRADED
- **Evidence (`run_ig_llm`):** `FRED CSV download failed for BAA10Y` (read timeout), HTML scrape failed, `pandas_datareader not installed => final fallback unavailable`, `All 3 FRED retrieval methods failed for BAA10Y`.
- **Result:** audit line correctly shows `FRED unreachable -> HYG/LQD price-proxy macro (DEGRADED tag)`. Correctly tagged — but note the macro regime signal rests on a price-proxy fallback.

### P1-4. FF5 factors unavailable → alpha_ff5 n/a
- **Evidence (`run_ig_llm`):** `alpha_annualized: null`, `alpha_ci_lower: null`, `alpha_ci_upper: null`; audit dominated by `FF5 factors unavailable -> alpha_ff5 n/a` and "env-degraded". `load_factors()` swallows the failure and returns `pd.DataFrame()`.
- **Consequence:** the flagship `ig-llm` backtest reports **no alpha and no alpha CI** — the "edge" rests on excess-vs-SPY only.

### P1-5. SPY baseline anomaly: positive excess but negative alpha ✅ RESOLVED (2026-08-30)
- **Original evidence (`run_bt` spy):** `excess_sp500: 0.0114` (positive) but `alpha_annualized: -0.08699` (negative) on the FULL window. A SPY baseline should have ~0 alpha and ~0 excess vs SP500.
- **Root cause 1 (return basis, CEO ruling):** `prices["SPY"]` is downloaded with `auto_adjust=True` (adjusted/total-return close — dividends embedded, no ex-date drop), but the `Portfolio` sim ALSO separately accrued dividends as unreinvested cash → double-counted dividends (inflated FULL-window excess/alpha) and left an idle ~17% cash buffer that dragged RECENT-window excess below benchmark. **Fix:** `Portfolio.run` now reports dividend income but does not add it to cash; mark-to-market alone is the total-return path. RECENT excess is now ~0 (1e-15) and FULL ~0.
- **Root cause 2 (window):** `metric_bundle` had `horizon_days` defaulting to 252 so FULL-window alpha used only the trailing ~year. Fixed to `horizon_days=len(r)` (same window as the row); `alpha_n_obs` surfaced.
- **Root cause 3 (coverage false alarm):** `_benchmark_coverage` compared SPY against `pd.bdate_range` (Mon–Fri), so midweek market holidays registered as missing → false ~96% coverage. Reworked to detect true truncation (head/tail/none or interior gap > 20d); holidays tolerated. audit = AUDITED CLEAN.
- **Residual (legit, not a bug):** FF5 alpha is still mildly negative because SPY's factor tilts (negative SMB — large-cap only; RMW tilt) underperformed the factor premia in the sample; excess-vs-SPY (the user's concern) is now clean.

---

## P2 — Silent-failure / observability defects

### P2-1. Errors swallowed without any DEGRADED flag (matches D3; compounds P0-1) ⚠️ PARTIAL (2026-08-30)
- `diversification/fee_sim3.py`:
  - `:1080-1081` `except Exception: return []` (DB read of proxies)
  - `:1105-1106` `except Exception: continue` (per-ticker pipeline)
  - `:1108-1109` `except Exception: return []` (whole gate)
- `discovery/gate_data.py::qualitative_signals` `:286-287`, `:321-322` `except Exception: pass`.
- `backtesting/chi_square.py::load_factors` `except Exception: return pd.DataFrame()`.
- **These convert failures into neutral defaults silently, changing the backtest universe without warning.**
- **Note:** `tests/test_diversification_discovery.py:266-274` *asserts* `_ig_llm_passed_candidates() == []` "on db error" — the current tests lock in the silent-failure behavior. Any fix must update these tests.
- **Fix direction:** raise a custom `DegradedDataError` / emit a DEGRADED audit flag rather than returning `[]`/neutral.
- **Status:** 
  - `fee_sim3` swallow points are now inside the RETIRED+LOCKED `_ig_llm_passed_candidates` (raises `RuntimeError` at entry, so the swallows are unreachable dead code) — no action needed.
  - `gate_data.qualitative_signals` now LOGS the failure (module logger) instead of `pass`; neutral fallback retained.
  - `chi_square.load_factors` now LOGS and is already surfaced by `audit_status` as env-degraded ("FF5 factors unavailable").
  - Remaining: full `DegradedDataError`/DEGRADED-flag propagation for backtest-affecting data paths is a broader change (T2/T3 candidate).

### P2-2. Library gaps vs. what the code expects (environment) ✅ RESOLVED (2026-08-30)
- **Missing (verified via importlib):** `redis` + `redis.asyncio` (→ "Redis caching disabled", "metrics will use local storage only" on every run), `pandas_datareader` (→ FRED final fallback unavailable), `undetected_chromedriver` (UC tier of the browserless fallback chain unavailable), `duckdb`, `sqlalchemy`, `polars`.
- **Present:** torch, transformers, scipy, requests, bs4, curl_cffi, nodriver, yfinance, sklearn.
- **Impact:** UC fallback tier and Redis cache are dead unless installed; FRED recovery is limited.
- **Fix (done, CEO-approved install):** installed `redis-8.1.0`, `pandas_datareader-0.11.1`, `undetected-chromedriver-3.5.5`, `duckdb-1.5.5`, `sqlalchemy-2.0.52`, `polars-1.44.1` (ged: pip). All import OK incl. `redis.asyncio`; FRED tier-4 (pandas_datareader) fallback now reached and executes; backtest-path FRED fetch (BAA10Y/DGS10) confirmed working. Note: a direct FREDScraper probe hit a transient DNS blip during verification (network, not library); `fetch_fred_series` retried OK.

### P2-3. Hardcoded `spearman_ic = 0.27` in Lane 4 ✅ RESOLVED (2026-08-30)
- **Evidence:** `Qualitative/psychological/four_lane_pipeline.py:698` hardcodes `spearman_ic = 0.27` regardless of data — a constant IC, not a measured signal. Violates house invariant 4 (config, not hardcoded constants) and produces a fake validation quantity.
- **Fix:** no real Spearman IC is computed in the Lane-4 path and the value is not persisted/consumed downstream (only the pass flags are), so the honest fix is `spearman_ic = None` (typed `Optional[float]`) with a comment; a made-up "validation" number is no longer emitted.

### P2-4. Qualitative scrapers degraded (from `logs/quant_pipeline.log`) ✅ RESOLVED via ruling D-20260830-001
- GitHub API **404**s against hardcoded repo guesses (`broadcom/broadcom-mcu`, `qualcomm/open-source`, `micron/micron-ddr`, `tsmc/open-source`, `salesforce/salesforce-sdk`, ...). **Fixed:** `config/hybrid_config.yaml::github_mappings` corrected to live-verified top-starred repos (QCOM→`qualcomm/GenieX`, CRM→`salesforce/LAVIS`, ADBE→`adobe/brackets`, IBM→`IBM/sarama`); non-applicable tickers (AVGO, MU, TSM, DELL, SMCI) removed — those have no real public OSS footprint and per ruling D-20260830-001 `developer_momentum` is `not_applicable` for them (never a forced/silent 0.5). No dead 404 fetches remain.
- Qualitative GitHub signal rearchitected: moat scoring is now ticker-aware + trustworthiness-weighted (static reliability ranking + 40% single-factor cap, pre-registered in `config/weights_diversification.yaml::qualitative_moat_scoring`). Full T3 debate (brief B-20260830-001, ruling D-20260830-001). Full suite: 1317 passed / 18 skipped / 0 failed (98.6%, conductor gate PASS).
- Adzuna API **401** (no key): still outstanding — needs ADZUNA_APP_ID/ADZUNA_APP_KEY, or keep on web-UI fallback. **Browserless** down (localhost:3000), **Nodriver** CDP attach fails (127.0.0.1:9222): separate infrastructure items, tracked by the browserless/nodriver fallback chain (D-20260816-002 anti-bot engine audit). Not scored as a regression because each source degrades to a non-silent fallback.
- **Consequence:** Glassdoor/G2/Comparably/job-count fallbacks still carry weak data for proxies; GitHub now only counted where a real OSS footprint exists.

### P2-5. Build/branch hygiene
- Current branch is `feature/educator-agent` (working tree), not `main`. Untracked/churned data files (P1-2) plus `.relaunch_requested` sentinel should be cleaned before any new backtest produces trustworthy, committed results.

---

## P3 — Minor / warnings (non-blocking)

- `Qualitative` is a **namespace package** (no `__init__.py`); modules import both as `Qualitative.psychological` and (in `preflight_gate.py`, `fee_sim3.py`) as top-level `psychological.`. Both work because `Qualitative/` is inserted on `sys.path`, but the dual-path reliance is fragile. Add `Qualitative/__init__.py` and settle on one import convention.
- `preflight_gate.CORE_MODULES` lists `"config"` and `"psychological.qualitative_scoring"`; verified both import OK when `Qualitative/` is on PYTHONPATH (fine, but the `"config"` module is a lightweight `__init__`; keep it intentional).
- `run_backtest_write.py` writes a registry row hardcoded to date `20260821`; on re-runs it appends new rows but never updates the timestamp — stale-dated artifacts.
- `tests/test_fintech_clients.py`: `@pytest.mark.asyncio` on non-async functions (warnings); `datetime.utcnow()` deprecation warnings across many modules — no functional impact, housekeeping.
- `supervisor.log` contains raw terminal-escape bytes (a stray capture), not meaningful logs.

---

## Recommended fix order (to get to a trustworthy backtest)
1. **P0-1** — stop reading fallback proxies as real; emit DEGRADED; make `ig-llm` raise/skip when 0 candidates. (Update test P2-1.)
2. **P0-3** — PIT proxy table + clock-stepped reader (ties into D-20260829-001 and existing PIT sandbox).
3. **P0-2 / P1-2** — ticker-master validation; move download cache out of repo.
4. **P0-4** — full decision state (buy+sell), not buy-only.
5. **P1-1 / P1-3 / P1-4 / P1-5** — data-source availability gating + SPY/alpha reconciliation.
6. **P2-3** — remove hardcoded IC.
7. Re-run `tests/`, then `/backtest ig-llm` and `/backtest spy`; confirm the `ig-llm` label reflects real qualitative candidates and a non-degraded audit line before trusting any "SYSTEMATIC" verdict.

## Verification commands
```powershell
python -m pytest tests/ -q
python run_bt.py
python run_ig_llm.py
python scripts/consensus_pipeline.py --seed
python scripts/wiki_p1_probe.py   # expect DEGRADED: DISCOVERY_LIVE!=1 (fail-closed is correct)
```
