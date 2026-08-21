# Track 1 Prospective Validation — WIKI-T1-20260820

**Rule:** D-20260820-001 | **Registered:** 2026-08-20 | **Status:** CLOCK RUNNING

This is the falsification instrument for the Wikidata discovery lane in place
of an impossible backtest (PIT edge coverage 1.89% << 50% bar). The cohort
below was discovered on 2026-08-20 and frozen BEFORE any forward data exists.
Every future observation is genuinely out-of-sample.

## Frozen artifacts

| Item | Value |
|---|---|
| Freeze file | `data/wiki_track1_freeze_20260820.json` |
| SHA-256 | `83e9659d49d1850d60ab548e37597d5986e4baff35e210d88de5d8ca2d755dd5` |
| Crawl run | wiki_runs id (p1_probe_wave), finished 2026-08-20 |
| Seeds (excluded from scoring) | NVDA AMD AVGO MSFT GOOGL META AMZN AAPL TSM ASML |
| Cohort ALL (17) | AMD AMZN ASML ATY AVGO BLK BRCM GOOGL META MLNX MMI NVDA STT TIT TSM VMW WFM |
| **Cohort NON-SEED (9, scored)** | ATY BLK BRCM MLNX MMI STT TIT VMW WFM |

Known-stale informational flags (from alpha-worker audit): ATY BRCM MLNX VMW
WFM TIT. Tradability is NOT hand-curated at evaluation — it is re-derived
mechanically (rule M2).

## Pre-registered evaluation bars (set 2026-08-20, before forward data)

**M — Mechanical rules**
- M1: Returns use dividend/split-adjusted closes; SPY buy-and-hold over the identical window is the benchmark.
- M2: A name is TRADEABLE at an evaluation date iff it has ≥200 price observations in the trailing 12m at that date. Stale names drop out mechanically; staleness rate is reported, never hand-filtered.
- M3: Non-seed names only. Seeds are anchors, not discoveries.
- M4: No cohort edits after this registration. Later crawls form NEW cohorts with their own freezes.

**Bars at each evaluation date**
- P1 PRIMARY (+12m): median total return of tradeable NON-SEED names − SPY return > 0.
- P2 SECONDARY (+12m): ≥50% of tradeable NON-SEED names beat SPY.
- Y1 YIELD (each quarterly re-crawl): ≥5 NEW CIK-valid non-seed names per crawl (breadth growth; current yield = 9 total).
- G1 GUARD: staleness rate of newly surfaced names < 40% (current baseline).
- INSUFFICIENT rule: if tradeable non-seed count < 5 at any evaluation → verdict INSUFFICIENT, abstain, clock extends (mirrors house usability ladder).

## Evaluation dates

| Checkpoint | Date | Bars scored |
|---|---|---|
| E1 | 2026-11-20 | Y1, G1 |
| E2 | 2027-02-20 | Y1, G1 |
| E3 | 2027-05-20 | Y1, G1 |
| E4 FINAL | 2027-08-20 | P1, P2, Y1, G1 |

Monitoring (price checks) is permitted at any time; RULINGS only at these
dates. Verdicts: PASS → lane graduates toward wiring brief; FAIL → lane dies
per pruning-policy.md; INSUFFICIENT → extend.

## Context recorded at registration

First-cohort ex-post (alpha-worker, descriptive): non-seed tradeable n=3,
median 12m 7.46% vs SPY 21.53% — no edge claimed. Structural facts: typed-edge
graph shallow (315 nodes reachable from 11 seeds); PIT coverage 1.89%; topic
DFS functional (1 trigger / 1 descent); structural firewall PASS (sim-guardian).
