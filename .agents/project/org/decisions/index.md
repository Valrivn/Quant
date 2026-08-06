# Executive Decision Ledger — Index

Every CEO ruling becomes a decision file in this folder. This index is the
spine of the leadership record (and the college portfolio).

## Ledger

| Decision-ID | Date | Tier | Task | Ruling | Models in loop | Cost row? | Status |
|-------------|------|------|------|--------|----------------|-----------|--------|
| D-20260801-001 | 2026-08-01 | T2 | Commit stabilization + org setup | APPROVE | big-pickle, gemini-planner | D-20260801-001 | DONE |
| D-20260801-002 | 2026-08-01 | T3 | Run full agent-pipeline audit | APPROVE | big-pickle, gemini-planner, hermes-bridge | D-20260801-002 | DONE |
| D-20260801-003 | 2026-08-01 | T3 | Stochastic risk dashboard tab implementation plan | APPROVE | big-pickle, gemini-planner, hermes-bridge | D-20260801-003 | DONE |
| D-20260801-004 | 2026-08-01 | T3 | Relative-Alpha Value Evaluation Architecture | APPROVE | big-pickle, gemini-planner, hermes-bridge | D-20260801-004 | COMPLETE |
| D-20260802 | 2026-08-02 | T3 | Backtest deep review: megacap-vs-not, alpha logic, data validity | APPROVE | big-pickle, gemini-planner, hermes-bridge, deepseek-worker | D-20260802 | APPROVED (FINAL) — CEO ruled APPROVE with hermes hybrid: regime-dependent exit band, Glassdoor continuous tilt, falsification-first sequencing. |
| D-20260802-002 | 2026-08-02 | T3 | Pilot validation redirect: reinvestment-rate + moat discovery | MODIFY | CEO direct ruling + big-pickle (evidence); hermes not re-invoked | D-20260802-002 | MODIFY — reinvestment-rate + qualitative-moat discovery re-thesis; 3-5y horizon; moat-compromise-only exit; head-to-head test case (profitable vs high-reinvestment). |
| D-20260802-002 | 2026-08-02 | T3 | Reinvestment-rate + moat thesis implementation | APPROVE | big-pickle (pilot+evidence) + CEO ruling | D-20260802-002 | APPROVE — reinvestment-rate + moat thesis with profit-agnostic tilt and qualitative moat discovery |
| D-20260803-001 | 2026-08-03 | T3 | Selective-Small-Cap Thesis: Bounded Falsification-First Build | APPROVE (hybrid — falsification-first build) | big-pickle (Position A), big-pickle (Position B under degradation-ladder), hermes-bridge (synthesis) | D-20260803-001 | APPROVE — bounded falsification-first build; test OCF/reinvestment/margin-trend on 1002-name universe; fund insider-buying datastore + gross-margin PIT only if gates pass; watchlist is monitor not investment rule; modifies D-20260802-002's profit-agnostic stance to include profitability as selectivity gate |
| D-20260803-002 | 2026-08-03 | T3 | two-sleeve portfolio + cost-aware dynamic allocation + $10k fee simulation | MODIFY | big-pickle, gemini-planner, big-pickle (map), hermes-bridge, logger | D-20260803-002 | MODIFY — hybrid two-sleeve portfolio with transaction-cost flooring and opportunistic liquidation; $10k implementation-today simulation required; college agent records adaptation data. |
| D-20260803-003 | 2026-08-03 | T3 | multi-asset sleeves + macro-state rotation | APPROVE (hybrid phased) | big-pickle (A) + gemini-planner (B) + big-pickle (map) + hermes-bridge (synthesis) + logger | D-20260803-003 | APPROVE |
| D-20260803-004 | 2026-08-03 | T3 | Phase 2: opportunistic + dividend engines | APPROVE (hybrid + floor) | big-pickle (A), gemini-planner (B), big-pickle (map), hermes-bridge (synthesis), logger | D-20260803-004 | APPROVE — Phase-2 greenlit; stable-dividend audit + minimum-candidates floor/bills fallback + opportunistic engine (reused swap + bear-regime gate) + DIVIDEND variant in fee_sim3; decision file + archived drafts exist |
| D-20260803-005 | 2026-08-03 | T3 | risk-constrained ML allocator: static + adaptive | MODIFY | big-pickle (posA + map) + gemini-planner (posB) + hermes-bridge (synthesis) + CEO ruling | D-20260803-005 | MODIFY — three-strategy risk-constrained ML allocator: STATIC-40/20/20/20 + STATIC-after-ML + ADAPTIVE (Sharpe-max objective, risky-weight penalty, hard <=30% maxDD at every point); all params pre-registered in config/weights_diversification.yaml; hybrid base adopted |
| D-20260804-001 | 2026-08-04 | DISCOVERY | Return-max discovery pivot | (pending CEO decision) | big-pickle (primary-session execution) | D-20260804-001 | DISCOVERY — return-max pivot shows directional improvement (+171% vs SPY +217%) with 60% lower DD, but fails all three success bars; modules built (markov_momentum.py, return_max.py, fee_sim3 run_sim_discovery, 23 tests); recommendation that any follow-up be a Tier-3 MODIFY brief |
| D-20260804-002 | 2026-08-04 | T3 | 7-phase PIT data-layer rebuild (B-20260804-002) gated on <=1hr API probe; per-source DEGRADED fallback; Discovery re-run with % deltas; success S1-S6 pre-registered | APPROVE (hybrid) | big-pickle (Position A) + gemini-planner (Position B) + big-pickle (map) + hermes-bridge (synthesis) + logger | D-20260804-002 | APPROVE — 7-phase PIT data-layer rebuild gated on <=1hr API probe; per-source DEGRADED fallback; Discovery re-run with % deltas; success S1-S6 pre-registered |
| D-20260806-001 | 2026-08-06 | T3 | Modify discovery pipeline with deterministic trend-ranked feed (SEC/Reddit/StockTwits immediate scope; IG/TikTok gated on sandbox evidence; no epsilon-greedy RNG; relative-vs-baseline no-regression bar) | MODIFY | big-pickle (Position A), gemini-planner (Position B), big-pickle (map), hermes-bridge (synthesis), logger | D-20260806-001 | MODIFY — deterministic trend-ranked discovery feed with SEC/Reddit/StockTwits immediate scope; IG/TikTok gated on sandbox evidence of ≥1 qualitative-gate pass; no epsilon-greedy RNG; pre-registered bar amended to relative-vs-baseline |

## Conventions

- Filename: `D-YYYYMMDD-NNN.md` (matches `DECISION-ID`).
- Draft artifacts (briefs/positions/synthesis) live in `_drafts/` until a
  ruling, then are archived inside the decision file or under `_drafts/archive/`.
- Every entry has a matching cost-ledger row and a tendencies note.
- logger appends rows; data-scientist reads them for pattern reports.