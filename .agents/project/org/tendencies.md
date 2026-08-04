# Tendencies — Decision Pattern & Alt-Data Intel

**Maintained by:** data-scientist. Updated after every ruling or monthly
(whichever is sooner). Reads: decisions/, cost-ledger.md, and the alt-data the
repo already collects.

## Model alignment ("who wins")

| Window | Big-pickle sided | Gemini/planner sided | Neutral/hybrid | CEO reversal rate |
|--------|------------------|----------------------|----------------|-------------------|
| 2026-08 (inception) | N/A — no T3 debates yet | N/A — no T3 debates yet | N/A | 0 / 2 (0%) |

**Insight:** First recorded run. No Tier-3 debates have occurred; both initial decisions (D-001 commit approval, D-002 pipeline audit approval) were routine T1/T2 approvals without manager disagreement. Model alignment signal will emerge once a T3 debate is ruled on.

## Escalation & risk appetite

- Escalations (T2→T3) this month: 0 — N/A
- Discovery briefs raised: 0 — acted on: 0
- Risk posture trend: **Moderate / governance-first** — CEO approved a structural commit (D-001) and a full pipeline audit (D-002) as opening moves. This signals preference for institutional rigor over speed; no aggressive allocation bets or strategy pivots yet.

## Token spend trend

- Paid-tier spend by month: **$0.00** (cost ledger seeded but empty; all invocations on free `opencode/*` models per fallback.md)
- Debate tier ROI: N/A — no paid debate has occurred. Free-model T1/T2 throughput is the baseline.

## Recurring patterns

- **Governance-first bias:** First two decisions were structural (commit hygiene, audit gate) not alpha-seeking.
- **Zero reversals so far:** Both rulings stood as approved; no 7-day reversals observed.
- **No escalations triggered:** All work remained at T1/T2; escalation triggers (multi-subsystem touch, >10% test failures, blueprint invariant risk) not yet hit.
- **Philosophy shift pattern:** CEO moving from profitability gate to reinvestment-rate + moat thesis; from quantitative hard gates to qualitative tilts; from pure alpha-seeking to hybrid approach combining quantitative reinvestment screens with qualitative moat discovery.
- **Bounded evidence-gated builds:** CEO chooses bounded falsification-first builds over full builds; selectivity-after-pilot-failure pattern; planner-lane outage noted (gemini-planner subagent returned 3 empty runs under degradation-ladder Step 2)
- **Dynamic-but-fee-bounded allocation:** CEO adopts dynamic-but-fee-bounded allocation; prefers opportunistic liquidation over churn; asks for dollarized (10k) implementation-today simulations; uses college recordings as adaptive evidence.
- **Phased scope discipline:** CEO accepted a phased ruling over one-shot scope — evidence of budget-aware scope discipline (he APPROVED splitting five engines across two rulings rather than forcing a single unbuildable one); his earlier fee-aversion pattern extended: he required the dividend-fee-coverage hypothesis to be MEASURED inside the $10k sim, not assumed.

## Recommendations to CEO

1. **Seed a T3 debate deliberately** — pick a blueprint invariant (e.g., conviction threshold for "Strong Buy" ≥ 9/10) to force a first manager disagreement and calibrate model alignment.
2. **Activate paid-model tier for synthesis** — run `opencode auth login` and switch hermes-bridge to `google/antigravity-*` per fallback.md; the 300-token synthesis cap keeps cost trivial while unlocking Claude quality.
3. **Schedule monthly alt-data discovery review** — the scrapers are live (see below); a standing 20k-token discovery slot prevents signal backlog.

## Alternative-data watch

- **Conviction dispersion is wide:** AAPL/GOOGL/META/MSFT = 10/10 Strong Buy; NVDA 8/10; AVGO 9/10; AMZN 6/10 Buy; AMD/TSLA 3/10 Reduce; INTC 0/10 Don't Consider (source: `center/lane_summary.md`, Lane Delta verified).
- **Competitive displacement signals:** GOOGL/META (DR 0.242) and AAPL/TSLA (DR 0.197) show measurable challenger pressure — not yet in pipeline as explicit regime features.
- **Live ingestion confirmed across 7 alt-sources:** SEC XBRL, GitHub, Glassdoor, Indeed, Product Intel, Fintech (Reddit/StockTwits/ApeWisdom), Adzuna — 1,700–2,600 rows/ticker over 5 years (`lane_results/Lanes Summary.MD`).
- **Psychological regime engine configured but unvalidated:** Panic/euphoria/apathy thresholds + asymmetric employee/GitHub velocity sigmas defined in `config/hybrid_config.yaml`; no backtest IC reported yet (Lane Delta Spearman ρ = 0.0000).
- **Scenario B (Fintech Fallback) weights unoptimized:** ApeWisdom 0.60 / StockTwits 0.40 are defaults; Bayesian optimization enabled but `optimize_scenario_b: true` has no IC history yet.
- **Hiring velocity fracture detector armed:** Operational fracture thresholds (Δ ≥ 0.9, z ≥ 3.0) in config; no alerts fired — worth a discovery brief if/when INTC/AMD hiring cliffs appear.

---

## 2026-08-01 — Inception Tendency Report (this run)

*First monthly append. Ledger has 2 decisions (D-001, D-002), cost ledger empty, no T3 history. Baseline established above.*

## 2026-08-03 — D-20260803-002 fee-sim adaptive-evidence note

The CEO's fee-aversion was empirically validated as adaptive, not conservative:
after fearing dynamic-allocation interaction fees, he required a dollarized $10k
comparison before approving dynamic allocation. The simulation proved his
specified opportunistic-liquidation rule dominates both flat monthly churn
(43.8% of gains in fees vs 0.44%) and fee-gating (same gain, ~40x fewer fees),
and that the approved 60/40 two-sleeve plan is nearly fee-free when run
cost-aware ($110 vs $9,055). The CEO's instinct was directionally right and he
converted it into a measurable design rule — a self-correcting, evidence-first
risk-appetite data point.
