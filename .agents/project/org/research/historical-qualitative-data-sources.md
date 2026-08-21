# Historical Qualitative Data Sources — Backtestable Catalog

**Date:** 2026-08-20 | **Context:** D-20260820-001 Track 1 — sources that let
qualitative signals be quantized with point-in-time timestamps, so they can
enter backtests as rules (house invariant: backtests trade numbers, never
arguments).

## Tier A — PIT-clean, free, actionable now

| Source | Window | What it gives | House fit |
|---|---|---|---|
| **Wikipedia pageviews** (wikimedia REST pageviews API) | 2015-07→ (pagecounts: 2007→) | Daily per-article attention = PIT attention/interest series per company. Timestamped by construction — the missing "date" the Wikidata graph lacks | G3 altdata historical leg; dates discovery-style signals retroactively |
| **SEC EDGAR full-text + filings** (efts.sec.gov, 2001→) | 2001→ | MD&A tone, risk-factor additions, customer/supplier mentions — filed_date anchored, already house-native | Extends frontier edges historically; qualitative language → quant features |
| **GDELT Project** (gdeltproject.org) | 1979→, 15-min updates | Global news events + tone/salience per entity, 100% free | News-tone factor across full regime history incl. 2020/2022 bears |

## Tier B — Earnings-call transcripts (qualitative engine fuel)

| Source | Window | Notes |
|---|---|---|
| **Strux Transcripts Dataset** (struxdata.github.io) | 2017–2024, 11,950 calls, S&P500/NASDAQ500 | Free; ships ground-truth labels from 30-day post-earnings performance — ready-made for gate validation |
| **HF `idleengine/sp500_earnings_transcripts`** | **2005–2025**, S&P500 + large caps | Deepest free archive found; ~20y spans multiple regimes |
| FMP transcripts API | 10+ y, 200k+ calls | Freemium; bulk S3 delivery on paid tier |
| earningscalls.dev | 2020→ | Free to read; cheap API |

Transcript tone/guidance language quantizes exactly like Glassdoor tilt
(D-20260802-FINAL): score at call date, trade T+1, no hindsight.

## Tier C — Social/historical (extend existing lanes backward)

- **Pushshift Reddit academic dumps** (Academic Torrents, 2005–2023): could
  backfill reddit_quant.db history far beyond current depth.
- **Kaggle "Daily News for Stock Market Prediction"**: Reddit worldnews
  headlines 2008–2016, headline-sentiment factor.
- **Glassdoor 2018 research dump (~1.6M reviews)**: exists but licensing is
  gray — needs a rights review before any use; live Glassdoor lane already
  covers the going-forward need.

## Discipline notes

1. Every source above is timestamped at creation/filing — that is what makes
   it backtestable where Wikidata edges are not.
2. Survivorship still applies (delisted companies' pages/transcripts vanish or
   stagnate); PIT universe discipline (db retention of delisted names)
   required when joining to prices.
3. Licensing: Tier A/B items are free/open; verify redistribution terms before
   committing any dataset into `data/`.
4. Recommended first integration: Wikipedia pageviews as historical G3 leg +
   Strux/HF transcripts for qualitative-gate calibration — both zero-cost,
   both PIT-clean, both directly consumable by existing gates.
