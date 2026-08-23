# Executive Decision Ledger — Index

Every CEO ruling is a `D-YYYYMMDD-NNN.md` file. Decisions live in
`<month>/<project>/` folders so they can be reviewed chronologically OR by
initiative. This index is the spine of the leadership record (and the college
portfolio).

**Layout**

```
decisions/
  index.md            ← you are here (two views below)
  TEMPLATE.md         ← logger copies this for each new ruling
  _drafts/            ← pre-ruling artifacts; archive under _drafts/archive/
  2026-08/            ← one folder per month
    01-org-platform/          05-data-pipeline/
    02-value-alpha/           06-instagram-trends/
    03-portfolio-allocation/  07-scraping-altdata/
    04-discovery-feed/        08-frontier-ecosystem/
```

---

## View 1 — By project

| Project | Scope | Decisions | Span |
|---------|-------|-----------|------|
| [01-org-platform](2026-08/01-org-platform/) | Org setup, pipeline audits, risk dashboard tooling, House Backtest v2 (`/backtest`) | 4 | Aug 1 → Aug 9 |
| [02-value-alpha](2026-08/02-value-alpha/) | Relative-Alpha architecture, backtest deep review, reinvestment/moat thesis, selective small-cap gate | 4 | Aug 1 → Aug 3 |
| [03-portfolio-allocation](2026-08/03-portfolio-allocation/) | Sleeves, dividend/opportunistic engines, ML allocator, multi-asset optimization, walk-forward verification, macro-regime allocator | 8 | Aug 3 → Aug 9 |
| [04-discovery-feed](2026-08/04-discovery-feed/) | Discovery-feed design: return-max pivot, deterministic trend-ranked feed | 2 | Aug 4 → Aug 6 |
| [05-data-pipeline](2026-08/05-data-pipeline/) | Data reliability: PIT data-layer rebuild, free data-source strategy | 2 | Aug 4 → Aug 11 |
| [06-instagram-trends](2026-08/06-instagram-trends/) | IG/TikTok trend engine: scraper builds, attention tracking, transcript analysis, proxies, scaling, Sentinel validation | 8 | Aug 7 → Aug 15 |
| [07-scraping-altdata](2026-08/07-scraping-altdata/) | Cross-source scraping infrastructure: anti-bot engine gate, live consensus (G2/Glassdoor/Indeed) | 2 | Aug 16 → Aug 18 |
| [08-frontier-ecosystem](2026-08/08-frontier-ecosystem/) | Frontier discovery lanes: Virus-Frontier supply chain, Wiki×SEC (Wikidata SPARQL BFS/DFS) | 2 | Aug 19 → Aug 20 |

---

## View 2 — Chronological timeline (newest first)

Times = file creation (local). Overnight sessions spill past midnight; where a
ruling's ID-date and creation date differ, both are shown.

### Fri Aug 21 – Thu Aug 20

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 7:40 PM | [D-20260820-001](2026-08/08-frontier-ecosystem/D-20260820-001.md) | frontier-ecosystem | T3 | Wiki×SEC discovery lane (Wikidata SPARQL BFS + gated DFS + PIT probe) | ADOPT hybrid + PIT amendment — PIT replay dead (1.89%); live forward falsification registered (E4 final 2027-08-20); TRACK-1 FROZEN |

### Tue Aug 19

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 10:33 PM | [D-20260819-001](2026-08/08-frontier-ecosystem/D-20260819-001.md) | frontier-ecosystem | T3 | Virus-Frontier: overlap-graded supply-chain frontier engine + AI-cons vector | APPROVE (hybrid, CEO-modified) — built + tested (1236 passed), commit 61147ea |

### Tue Aug 18

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 8:17 PM | [D-20260818-001](2026-08/07-scraping-altdata/D-20260818-001.md) | scraping-altdata | T2 | Enable supervised live consensus (G2/Glassdoor/Indeed) | APPROVE — live run S1 FAILED → F1 REJECT, kill-switch reverted; re-plan pending |

### Sun Aug 16

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 4:15 PM | [D-20260816-002](2026-08/07-scraping-altdata/D-20260816-002.md) | scraping-altdata | T3 | Scraping anti-bot strategy engine selection gate | APPROVE — fingerprint audit gate before engine selection |

### Sat Aug 15

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 11:15 PM | [D-20260815-001](2026-08/06-instagram-trends/D-20260815-001.md) · [artifacts](2026-08/06-instagram-trends/D-20260815-001/) | instagram-trends | T3 | IG_LLM Sentinel Validation (hybrid A+B) for qualitative gating bottleneck | APPROVE — structured schema, URL grounding, audit trail, threshold freeze |

### Tue Aug 11

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 9:18 PM | [D-20260811-001](2026-08/05-data-pipeline/D-20260811-001.md) | data-pipeline | DISCOVERY | Free data-source strategy: AI-era backtest window + IG discovery-alpha question | direction recorded — 58-source catalog narrowed; 10-yr AI window adopted; two-track plan |

### Sun Aug 9

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 4:02 PM | [D-20260809-002](2026-08/01-org-platform/D-20260809-002.md) | org-platform | T3 | House Backtest v2: chi-square gate, metric bundle, archetype council, `/backtest` | APPROVE — falsifiable registry rows; pruning policy |
| 2:45 PM | [D-20260809-001](2026-08/06-instagram-trends/D-20260809-001.md) | instagram-trends | T3 | Scale Instagram Reels scraper to 100k+ videos with anti-bot measures | APPROVE |

### Sat Aug 8 → Sun Aug 9 (overnight session)

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 1:36 AM ⏾ | [D-20260808-008](2026-08/03-portfolio-allocation/D-20260808-008.md) | portfolio-allocation | T3 | Grade 12 schedule integration + 70/30 IG/Reddit sleeve | APPROVE |
| 1:05 AM ⏾ | [D-20260808-007](2026-08/03-portfolio-allocation/D-20260808-007.md) | portfolio-allocation | T3 | Calculus-based macro-regime sigmoid allocator | APPROVE — bear/bull split reporting |
| 12:29 AM ⏾ | [D-20260808-006](2026-08/03-portfolio-allocation/D-20260808-006.md) | portfolio-allocation | T3 | Walk-forward backtest verification (no lookahead) | APPROVE — 186.67% return PIT |
| 12:21 AM ⏾ | [D-20260808-005](2026-08/03-portfolio-allocation/D-20260808-005.md) | portfolio-allocation | T3 | Multi-asset portfolio optimization & backtest (2024–2026) | APPROVE — 40% stock floor, 185.37% return |
| 11:37 PM | [D-20260808-004](2026-08/06-instagram-trends/D-20260808-004.md) | instagram-trends | T3 | High-scale proxy & session rotation for IG Reels | APPROVE |
| 11:31 PM | [D-20260808-003](2026-08/06-instagram-trends/D-20260808-003.md) | instagram-trends | T3 | LLM semantic transcript analyzer integration | APPROVE |
| 6:15 PM | [D-20260808-002](2026-08/06-instagram-trends/D-20260808-002.md) | instagram-trends | T3 | S-Curve + Kalman filter attention tracker | APPROVE |
| 5:34 PM | [D-20260808-001](2026-08/06-instagram-trends/D-20260808-001.md) | instagram-trends | T3 | Reverse-heatmap transition scraper + Whisper audio transcription | APPROVE |

⏾ created after midnight (Aug 9) during the Aug 8 evening session.

### Fri Aug 7

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 8:19 PM | [D-20260807-002](2026-08/06-instagram-trends/D-20260807-002.md) | instagram-trends | DISCOVERY | Instagram anti-bot scraper build (real IG feed) | APPROVE — cookie-import auth, nodriver CDP stealth; 42 tests pass |
| 6:32 PM | [D-20260807-001](2026-08/06-instagram-trends/D-20260807-001.md) | instagram-trends | T3 | Instagram independent discovery experiment (alpha-gate test) | MODIFY — independent channel, standard screen, isolation contract holds |

### Thu Aug 6

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 7:38 PM | [D-20260806-001](2026-08/04-discovery-feed/D-20260806-001.md) | discovery-feed | T3 | Deterministic trend-ranked discovery feed (SEC/Reddit/StockTwits; IG gated) | MODIFY — no RNG; relative-vs-baseline bar; P1–P5 gates fail closed |

### Tue Aug 4

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 8:26 PM | [D-20260804-002](2026-08/05-data-pipeline/D-20260804-002.md) | data-pipeline | T3 | 7-phase PIT data-layer rebuild, API-probe-gated | APPROVE (hybrid) — DEGRADED fallbacks; success S1–S6 pre-registered |
| 7:54 PM | [D-20260804-001](2026-08/04-discovery-feed/D-20260804-001.md) | discovery-feed | DISCOVERY | Return-max discovery pivot | pending CEO decision — fails all three success bars; follow-up = T3 MODIFY brief |

### Mon Aug 3

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 9:44 PM | [D-20260803-005](2026-08/03-portfolio-allocation/D-20260803-005.md) | portfolio-allocation | T3 | Risk-constrained ML allocator: static + adaptive | MODIFY — Sharpe-max objective, hard ≤30% maxDD bound |
| 8:48 PM | [D-20260803-004](2026-08/03-portfolio-allocation/D-20260803-004.md) | portfolio-allocation | T3 | Phase 2: opportunistic + dividend engines | APPROVE (hybrid + floor) — stable-dividend audit + minimum-candidates floor |
| 7:58 PM | [D-20260803-003](2026-08/03-portfolio-allocation/D-20260803-003.md) | portfolio-allocation | T3 | Multi-asset sleeves + macro-state rotation (phased) | APPROVE (hybrid phased) — Phase 1 bonds/gold + risk minimizer |
| 6:26 PM | [D-20260803-002](2026-08/03-portfolio-allocation/D-20260803-002.md) | portfolio-allocation | T3 | Two-sleeve portfolio + cost-aware allocation + $10k fee sim | MODIFY — transaction-cost flooring; opportunistic liquidation only |

### Sun Aug 2

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 9:11 PM ⏾* | [D-20260803-001](2026-08/02-value-alpha/D-20260803-001.md) · [artifacts](2026-08/02-value-alpha/D-20260803-001/) | value-alpha | T3 | Selective-small-cap thesis: bounded falsification-first build | APPROVE (hybrid) — OCF/reinvestment/margin falsification gates datastore funding |
| 5:36 PM | [D-20260802-002](2026-08/02-value-alpha/D-20260802-002.md) | value-alpha | T3 | Reinvestment-rate + moat discovery re-thesis | MODIFY/APPROVE — CLOSED; superseded in part by D-20260803-001 selectivity gate |
| 1:02 PM | [D-20260802](2026-08/02-value-alpha/D-20260802.md) | value-alpha | T3 | Backtest deep review: alpha logic, data validity | APPROVE (FINAL) — regime-dependent exit band, Glassdoor tilt, falsification-first |

\* file created Aug 2, ruled/dated Aug 3.

### Sat Aug 1

| Time | ID | Project | Tier | Task | Ruling / Status |
|------|----|---------|------|------|-----------------|
| 11:36 PM | [D-20260801-004](2026-08/02-value-alpha/D-20260801-004.md) | value-alpha | T3 | Relative-Alpha Value Evaluation Architecture | APPROVE — COMPLETE, three-layer sleeve architecture |
| 8:19 PM | [D-20260801-003](2026-08/01-org-platform/D-20260801-003.md) | org-platform | T3 | Stochastic risk dashboard tab implementation plan | APPROVE — DONE |
| 7:50 PM | [D-20260801-002](2026-08/01-org-platform/D-20260801-002.md) | org-platform | T3 | Full agent-pipeline audit | APPROVE — DONE |
| 7:49 PM | [D-20260801-001](2026-08/01-org-platform/D-20260801-001.md) | org-platform | T2 | Commit stabilization + org setup | APPROVE — DONE |

---

## Conventions

- **ID/filename unchanged:** `D-YYYYMMDD-NNN.md` always matches its DECISION-ID,
  wherever it lives.
- **Location:** decisions are filed under `<YYYY-MM>/<NN-project>/`. New months
  start a fresh month folder; new initiatives get the next number with a short
  kebab-case slug. logger files new rulings via TEMPLATE.md into the right
  project folder (ask the CEO if the project is new).
- **Debate artifacts:** accumulate in `_drafts/` until ruling, then archive to
  `_drafts/archive/<DECISION-ID>/`; full T3 artifact sets may also sit beside
  their decision as `<DECISION-ID>/` inside the project folder (see
  D-20260803-001, D-20260815-001).
- Every entry has a matching cost-ledger row and a tendencies note.
- logger appends rows to BOTH views above; data-scientist reads them for
  pattern reports.
