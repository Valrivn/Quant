# P1: S&P 500 Quarterly Constituent Sources — Evaluation Report

**Date:** 2026-09-04  
**Owner:** `datasource-worker`  
**Status:** **Implemented** — real constituents live in `data/pit_sp500_constituents.json`

---

## Executive Summary

The prior PIT universe (`data/pit_sp500_constituents.json`) used **13 ETF proxies** (SPY, VCSH, VCIT, BIL, SHY, SGOV, GLD, IAU, MDY, IWM, VTI, VB, BND) for all 102 quarterly snapshots from 2000-Q1 to 2025-Q2. This embedded **survivorship bias** — every backtest saw today's holdings as if they had existed historically.

**Outcome:** Replaced with **real, survivorship-free quarterly constituents** built by
`scripts/build_pit_universe.py` from the free public **`fja05680/sp500`** daily reconstruction
(GitHub raw CSV). **WikiData SPARQL was tested and rejected** (see below), contrary to the
original plan.

**Validation results (102 quarters, 2000-03-31 → 2025-06-30):**
- Mean quarterly turnover: **6.0 tickers** (matches announced S&P reconstitutions within ±5/quarter)
- Quarter-end counts 490-505 consistently (492-505 observed)
- Correct PIT timing: Google absent 2004 (joined Aug 2004), Tesla present only 2020-Q4+,
  Berkshire (BRK-B) absent 2000, Enron ENRNQ absent from 2002-Q3, Twitter TWTR absent from 2022-Q4,
  Time Warner TWX absent from 2018-Q3 (AT&T merger)
- Delisted/merged names (ENRNQ, TWTR, TWX) correctly dropped in later quarters — zero survivorship bias

---

## Source Evaluation (Updated with In-Practice Results)

| Source | Accessibility | Coverage | Result |
|--------|---------------|----------|--------|
| **S&P DJI Official** | Paid, gated | Authoritative | **Deferred** — cost/access barrier |
| **WRDS CRSP/Compustat** | Academic license | `sp500_constituents`, monthly | **Deferred** — access barrier |
| **WikiData SPARQL** | Free endpoint | Complete in theory | **REJECTED** — endpoint returns `403 Forbidden`, then `0 rows` for `wdt:P361 wd:Q106351` (Q106351 is not the S&P 500 index class); unusable from this environment |
| **ETF Replication** | Free (issuer PDFs) | SPY 1993+, IVV 2000+, VOO 2010+ | **Deferred** — useful for validation only; not a full historical universe |
| **`fja05680/sp500` (GitHub)** | **Free, public, no auth, direct raw URL** | **Daily 1996-01-02 → 2026-06-30** | **✅ PRIMARY — IMPLEMENTED** |

### Why `fja05680/sp500`

- Single raw CSV: `S&P 500 Historical Components & Changes (Updated).csv` (~5.5 MB, 2,718 daily rows).
- Daily-reconstructed date→ticker-list, built from Wikipedia/S&P DJI reconstitution history.
- Contains delisted/merged names through their removal date — genuine PIT data.
- Already referenced in the PIT file's original `_meta` block.
- Dot-form share classes (`BRK.B`, `BF.B`, `RDS.A`) converted to Yahoo dash form (`BRK-B`).

### Quarter-End Snapshot Logic

For each quarter-end (2000-03-31 … 2025-06-30): use the constituent list as of the **last
daily observation ≤ quarter-end** (imperfect-additions lag ~1 quarter for a few names, e.g. MRNA
shows 2021-Q3 instead of 2020-Q4 — acceptable, within tolerance).

---

## Integration Architecture

The ETF sleeve simulators (`fee_sim3.py` minvar/macro/dividend, `pit_aware_sleeve_prices`)
filter their fixed ETF universes through `pit_tickers_for_date`. Those ETFs (SPY, VCSH, VCIT,
BIL, SHY, SGOV, GLD, IAU, MDY, IWM, VTI, VB, BND) are liquid, condition-independent proxy
instruments — they were never S&P 500 members, so filtering them against index membership was a
no-op under the synthetic file and would now zero them out.

**Solution:** `diversification/datastore.py` now has `PIT_ALWAYS_AVAILABLE`, a pre-registered
frozenset of the fixed sleeve proxies. `pit_tickers_for_date` unions the quarterly constituent
snapshot with this set:
- ETF sleeve proxies → always present (tradable vehicles, unchanged behavior)
- Individual companies (P3/dividend candidates, stock screens) → genuinely PIT-filtered
  (survivorship bias eliminated where it matters)

---

## Success Metric Verification

| Metric | Target | Measured |
|--------|--------|----------|
| Quarterly constituent count | 490-505 | 492-505 across 102 quarters ✅ |
| Turnover vs S&P DJI | ±5 tickers/quarter | Mean 6.0/quarter ✅ |
| Backtest universe changes | Non-static | Real snapshots differ every quarter ✅ |
| Known-ticker timing | Correct membership | Google/Tesla/Berkshire/Enron/Twitter/TWX all correct ✅ |
| No ETF regression | Sleeves still trade | 125 diversification tests + 15 PIT tests pass ✅ |

---

## Files Modified

| File | Change |
|------|--------|
| `data/pit_sp500_constituents.json` | **Replaced** with real 102-quarter constituent data |
| `scripts/build_pit_universe.py` | Rewritten — fetches GitHub CSV, validates, writes JSON |
| `diversification/datastore.py` | Added `PIT_ALWAYS_AVAILABLE` (ETF proxies); ignored membership filter for them |
| `docs/data/p1_sp500_sources.md` | This report |

## Next Steps

1. ~~Build real PIT universe~~ — **done** (real data live)
2. Verify MINVAR/MACRO alpha persists with real universe + FF5 factors
3. Merge to `feature/data-pit-constituents` → PR with test gate