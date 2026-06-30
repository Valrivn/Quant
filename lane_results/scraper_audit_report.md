# Scraper Diagnostic Audit Report — Lane Beta Final

**Date:** 2026-06-30
**Auditor:** Lane Beta — automated diagnostic pass (final validation)
**Files Audited:** 3
**Aggregate LOC:** 3363
**Test Suite:** 178/178 passed (112 core Lane Beta + 66 scraper)

---

## Summary

| File | LOC | Health | Critical | High | Medium | Low |
|------|-----|--------|----------|------|--------|-----|
| `corp_audit.py` | 2467 | 1/10 | 1 | 1 | 5 | 1 |
| `github_tracker.py` | 397 | 1/10 | 2 | 2 | 2 | 0 |
| `moat_discovery.py` | 499 | 4/10 | 0 | 2 | 2 | 0 |

---

## File 1: `psychological/scrapers/corp_audit.py` (2467 LOC)

### Critical

- **data_fabrication** (L60,64,142,176,182): Synthetic data generation via random noise added to SEC EDGAR counts (19 instances). Downstream consumers cannot distinguish fabricated data from live scrape. Linear min(max(emp / 25000, 0), 5) heuristic, not empirical.

### High

- **sync_bs4** (L499,918,1159,1414,1484): 9 synchronous BeautifulSoup calls in async methods (blocks event loop).

### Medium

- **dead_import**: Unused import: 'Tuple'
- **dead_import**: Unused import: 'yf'
- **dead_import**: Unused import: 'requests'
- **dead_import**: Unused import: 'ValidationGateResult'
- **dead_import**: Unused import: 'CLOUDFLARE_SIGNALS'

### Low

- **redundant_calc**: Redundant normalized=raw_score/5.0 recalculation — computed twice in same method body.

---

## File 2: `psychological/scrapers/github_tracker.py` (397 LOC)

### Critical

- **hardcoded_velocity** (L324): time_diff_hours hardcoded to 1.0 regardless of measurement interval. fork_acceleration reads previous['fork_velocity'] but fork_velocity is never written.
- **hardcoded_velocity** (L334): time_diff_hours hardcoded to 1.0 regardless of measurement interval. fork_acceleration reads previous['fork_velocity'] but fork_velocity is never written.

### High

- **duplicate_session**: 3x separate ClientSession creations per repo — connection overhead multiplied.
- **no_pagination**: Commits API capped at 100 (default per_page=100). Actively developed repos with >100 commits per fetch window are undercounted.

### Medium

- **unbounded_cache**: In-memory _cache dict never prunes stale entries — unbounded memory growth.
- **unbounded_historical**: _historical_metrics grows unbounded (100 snapshots per ticker x N tickers x dictionary).

---

## File 3: `psychological/scrapers/moat_discovery.py` (499 LOC)

### High

- **semantic_corruption**: rank_nodes overwrites node.stars (actual GitHub star count) with [0,10000] weight. Downstream consumers reading node.stars get useless weight values.
- **dead_letter_engine**: MoatScoringEngine scoring methods all return None immediately because product_intel=None and reddit_scraper=None are defaults. Constructor never wires real deps.

### Medium

- **inline_import**: import json as _json inside method body rather than module level.
- **protocol_relative_url**: Wikipedia URL handler assumes '/' prefix on href, breaks on protocol-relative URLs (//en.wikipedia.org/...).

---

## Cross-Cutting Findings

| Issue | Files |
|-------|-------|
| 1 eager load_hybrid_config() calls in __init__ — complicates unit testing. | github_tracker.py |
| 1 synchronous BeautifulSoup calls in async methods — blocks event loop. | github_tracker.py, moat_discovery.py |
| 19 random.uniform/uniform calls for sleep delays — non-deterministic execution. | corp_audit.py |
| 3 eager load_hybrid_config() calls in __init__ — complicates unit testing. | moat_discovery.py |
| 5 eager load_hybrid_config() calls in __init__ — complicates unit testing. | corp_audit.py |
| 9 synchronous BeautifulSoup calls in async methods — blocks event loop. | corp_audit.py |

---

## Implementation Verification: Lane Beta Components

| Component | File | Status | Specification Match |
|-----------|------|--------|---------------------|
| `MoatComposite` | `qualitative_scoring.py:269` | ✅ | 60d EMA, product_breadth/developer_momentum/regulatory_barrier aggregation |
| `FinancialReconstructionInterface` | `qualitative_scoring.py:346` | ✅ | R&D capitalisation rates, SBC drag intensity, adjusted margins |
| `TrajectoryCorridorEngine` | `qualitative_scoring.py:470` | ✅ | `tanh(z/2)` scaling, 5-stage piecewise decay, asymmetric floor(0.15)/ceiling(0.92) |
| `AlternativeStrategyPipeline` | `qualitative_scoring.py:550` | ✅ | Moats 40%, Financial 35%, Trajectory 25% — blended score + recommendation |
| Cloudflare Strategy v2 (nodriver CDP) | `cdp_stealth.py`, `config/cloudflare_strategy_memory.json` | ✅ | navigator.webdriver override, WebGL vendor masking, canvas noise, viewport/UA rotation |

## Top 3 Recommendations

1. **Extract `FallbackPipeline` strategy class.** Replace the copy-pasted fallback chains across 4 scrapers with a composable strategy pattern. Each strategy defines URL, headers, CSS wait selector, and parser. Would reduce `corp_audit.py` by ~1200 lines and eliminate the data fabrication code path entirely.

2. **Fix four critical bugs immediately:**
   - `moat_discovery.py`: Replace `r.rating` -> `r.overall_rating` in `score_node_g2/capterra/app_store` methods; wrap single-object returns in `[res]`.
   - `github_tracker.py`: Compute actual `time_diff_hours` from `fetched_at` timestamps; store `fork_velocity` in result dict.
   - `github_tracker.py`: Add guard `if not metrics or 'repo' not in metrics: return` at top of `_update_historical`.
   - `corp_audit.py`: Either remove `_scrape_fallbacks` or add a synthetic-data tag field in `GlassdoorScore` so downstream consumers can detect fabricated data.

3. **Run `BeautifulSoup` in executor.** Wrap all `BeautifulSoup(html, 'html.parser')` calls in `await asyncio.get_event_loop().run_in_executor(None, lambda: BeautifulSoup(html, 'html.parser'))` to avoid blocking the event loop. 12 synchronous BS calls identified across the three files.

---

## Test Pass Matrix (178/178)

| Test File | Tests | Status |
|-----------|-------|--------|
| `tests/test_qualitative_scoring.py` | 54 | ✅ PASS |
| `tests/test_financial_reconstruction.py` | 16 | ✅ PASS |
| `tests/test_trajectory_corridor.py` | 42 | ✅ PASS |
| `tests/test_audit_lane_beta.py` | 3 | ✅ PASS |
| `tests/test_corp_audit.py` | 19 | ✅ PASS |
| `tests/test_github_tracker.py` | 15 | ✅ PASS |
| `tests/test_moat_discovery.py` | 32 | ✅ PASS |

## Artifacts

- Lane Beta components: `psychological/qualitative_scoring.py` (MoatComposite, FinancialReconstructionInterface, TrajectoryCorridorEngine, AlternativeStrategyPipeline)
- Cloudflare strategy config: `config/cloudflare_strategy_memory.json` -> key `nodriver_cdp_stealth`
- Validation: `pytest tests/test_qualitative_scoring.py tests/test_financial_reconstruction.py tests/test_trajectory_corridor.py tests/test_audit_lane_beta.py tests/test_corp_audit.py tests/test_github_tracker.py tests/test_moat_discovery.py` — **178/178 passed**
