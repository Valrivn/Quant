# Master Blueprint — Quant-Py

**Custodian:** big-pickle. Updated after every T3 ruling and every structural
change. Agents read this file for project facts — do not bake facts into
prompts.

## Mission

Three-pillar quantitative investment research: Reddit/social alternative-data
NLP + stochastic risk modeling (Monte Carlo) + conviction-scored portfolio
allocations, surfaced through a Streamlit dashboard.

## Pillars

1. **Qualitative** — Reddit/social sentiment NLP, alternative-data scrapers
   (Glassdoor, G2, GitHub, SEC EDGAR, StockTwits, ApeWisdom), psychological
   regime state machine, multi-source Bayesian fusion.
2. **Quantitative** — ETF screening (bonds, gold, equities), stochastic models
   (Bernoulli shock filter, Markov lifecycle, Poisson black swan), sensitivity
   analysis, tactical allocation.
3. **Dashboard** — Streamlit app: portfolio overview, sentiment & risk, conviction.

## Module map (top-level)

| Path | Responsibility |
|------|----------------|
| `scraper/` | sentiment engine, reddit client, risk detector, hybrid orchestrator, SEC EDGAR, product intel, fintech clients |
| `Qualitative/psychological/` | monte_carlo, four_lane_pipeline, qualitative_scoring, bayesian_calibration, nlp_engine, velocity_tracker, state_machine, behavioral_feature_store, signal_matrix, dcf_floor, data_fusion |
| `Quantitative/` | stochastic/, bonds/, gold_etf/, dividends/, fragility/, funds/, allocation/, sensitivity/, audit/, shared/, company_classifier |
| `optimization/` | optuna_search, ab_testing (champion/challenger) |
| `backtesting/` | monthly rebalancing sim (IC, Sharpe, Hit Rate), drift detection |
| `valuation_alpha/` | L1 equity sleeve: 50-name universe (bias/beta tags), SEC XBRL 10y datastore, peer-relative ratios, FF5 residual alpha, candidate portfolio ranking, stats battery |
| `diversification/` | L2 sleeve: replay bond/gold/fund selectors over 10y, bond/rate factors |
| `portfolio/` | L3: LLM-guided gradient-descent allocator (alpha+Sharpe), whole-portfolio backtest, risk |
| `dashboard/` | stream_quant.py (+ tab_sentiment_risk, tab_stochastic_risk, stochastic_risk_service) |
| `db/` | SQLite WAL layer: connection, schema (+ fintech), feature_store, jobs |
| `config/` | YAML configs, constants, weights, credentials (git-ignored) |
| `scripts/` | scheduler (APScheduler), migrate_db, seed_historical |
| `tests/` | ~43 test files — the quality gate's raw material |
| `center/` | audit reports + conviction scores |
| `lane_results/` | legacy parallel-lane outputs (superseded) |

## Architecture invariants (T3-grade; never violated without a ruling)

1. **DB-first, WAL mode.** All persistence through `db/` layer; threads use
   thread-local connections. Never open raw connections ad hoc.
2. **Provenance everywhere.** Every fused score carries source provenance;
   audit module can trace any number to its inputs.
3. **Credentials only in `config/*_credentials.yaml`, git-ignored.** Never in
   code, comments, or committed files.
4. **Weight changes go through `config/weights*.yaml`**, not hard-coded constants.
5. **Anything decision-critical ships with tests.** New modules require a test
   file; pass rate target ≥90% (quality-gate.md).
6. **No secrets in logs.** Structured logging via `config/logging_config.py`.

## Stochastic core (non-negotiable math)

- **Bernoulli shock filter:** Damodaran ICR→rating→default-probability (14
  tiers) + balance-sheet resilience modifier M_health + sector shock prob.
- **Markov lifecycle:** 6 states (FAST_GROWER→…→ASSET_PLAY), dynamic matrices.
- **Poisson black swan:** systemic event counts, regime-aware λ from credit spreads.
- Sector shock probs: Bayesian-shrunk from yfinance EBIT (Beta(2,98) prior).

## Data sources

Reddit, StockTwits, ApeWisdom, Glassdoor, G2, Capterra, App Store, GitHub,
SEC EDGAR, FRED, yfinance. Credentials: Reddit, StockTwits, ApeWisdom.
Discovery trend feed (SEC EDGAR new-filers, Reddit, StockTwits, ApeWisdom
structured; IG/TikTok video gated) — PENDING (falsification-first,
D-20260806-001): research-only, NOT wired into the pipeline pending P1–P5 gates.

## Open questions / contested areas (escalate to T3)

- Lane fusion weights drifting after regime shifts — revisit via
  bayesian_calibration + drift_detection.
- Whether psychological state_machine should gate allocation or only flag.
- Dashboard load patterns (Streamlit) vs compute cost of MC simulations.

## Decision history

See `.agents/project/org/decisions/index.md` for every CEO ruling. Append a
ruler block here for rulings that change structure or invariants.

```text
## Ruling D-20260801-### (T3)
SUMMARY: <what changed>  |  BY: CEO  |  EFFECT: <blueprint delta>
```

## Ruling D-20260802 (T3)
SUMMARY: MODIFY — deep review of D-004 architecture found zero OOS residual
alpha (only momentum/beta) and no exit logic. CEO rules the engine is missing a
SELL algorithm and a DISCOVERY algorithm. SELL: exit a position that rises
15-20% above its highest range, justified by cashflow + macrotrends (new
valuation_alpha/exit.py overlay). DISCOVERY: expand universe to S&P MidCap 400
($1.8B-$35.5B) + S&P SmallCap 600 ($0.7B-$3.2B), quant baseline gate + Glassdoor
qualitative screen (non-tech names). Prerequisite: SEC CIK resolver fixing the
40/50 fundamentals gap. Plan B-20260803 drafted; falsification test in parallel.
  |  BY: CEO  |  EFFECT: blueprint gains sell/exit overlay pillar + discovery
universe pillar + CIK resolver; L1 selection relabeled momentum/beta sleeve
until falsification is rerun; blueprint custodian restored this ruler block
after an accidental deletion during write-up.

## Ruling D-20260802-FINAL (T3) — plan B-20260803 APPROVED
SUMMARY: CEO approves the hermes hybrid on the sell + discovery plan.
(1) Exit band is REGIME-DEPENDENT (widen 25-30%/2.5x ATR stop in trending,
tighten 10-12% + both gates in choppy) validated as an ablation; keep PIT
cashflow (filed_date<=decision_date), re-entry cooldown 60d + macro gate,
whipsaw metric, slippage 1%/2%. (2) Glassdoor is a CONTINUOUS TILT (+0.1 z per
0.1 above median), not a hard floor. (3) FALSIFICATION FIRST — expanding-window
purge-and-embargo OOS test gates any SP400/SP600 pilot; CIK resolver stands
alone as P1.  |  BY: CEO  |  EFFECT: B-20260803 status APPROVED; phases gated
P1 (CIK resolver) → P2 (falsification → stratified survivorship-corrected pilot)
→ P3 (exit overlay + ablation) → P4 (validation) → P5 (integration); sell band,
tilt weight, and phases live in config/weights*.yaml.

## Ruling D-20260802-002 (T3) — DISCOVERY RE-THESIS (reinvestment-rate + moat)
SUMMARY: P3 pilot failed its pre-registered gates (Sharpe fell, maxDD flat,
excess turned negative, FF5 alpha t≈0.4-0.8; discovery excess ≤+1.00%, t≤0.75).
CEO MODIFY — small caps must be held 3-5 years (not 1-3); the gate is
REINVESTMENT RATE (profit-agnostic; small caps are legitimately unprofitable
while reinvesting), NOT profitability; sell ONLY on qualitative-moat compromise
(supersedes the regime price-band); moat = product uniqueness vs competitors +
a "mindset" signal from external sources (Reddit, Amazon); test case = profitable
cohort vs high-reinvestment cohort; study notes required on small-cap → S&P 500
graduates.  |  BY: CEO  |  EFFECT: discovery thesis rebuilt around reinvestment
rate + qualitative moat; L1/discovery horizon 3-5y; exit rule changed from price
band to moat-compromise-only; new artifact
.agents/project/org/research/small-cap-graduates.md; new head-to-head test case
replaces the SP400/SP600 pilot as primary validation.

## Ruling D-20260801-004 (T3)
SUMMARY: APPROVE hybrid Relative-Alpha Value Evaluation Architecture — three
layers: L1 equity sleeve (valuation_alpha/), L2 diversification sleeve
(diversification/), L3 whole-portfolio (portfolio/). 50-name universe
(30 tech + 10 same-beta control + 10 megacaps bias=ON); bias ablation runs
all-50 vs non-megacap-40; algorithm ranks portfolios; CEO pre-registers
strategy/execution; FF5 residual alpha primary + SPY excess; 3y primary/1y
secondary; L3 = LLM-guided gradient-descent allocator (max residual alpha AND
Sharpe, walk-forward frozen OOS); stats battery = block bootstrap, deflated
Sharpe, CIs, Reality-Check 100k MC; gate = OOS alpha CI lower bound > 0
(2015-2023). College: logger continuous transcription into portfolio-reflections.md.
BY: CEO  |  EFFECT: three new pillars in module map; bias-ablation protocol
becomes part of L1 selection; dynamic allocation replaces static weights.

## Ruling D-20260801-003 (T3)
SUMMARY: APPROVE stochastic risk dashboard tab — dashboard gains 5th tab
(tab_stochastic_risk.py + stochastic_risk_service.py) wired into
stream_quant.py st.tabs (line 549); st.cache_data on MC;
tests/test_stochastic_dashboard.py; perf compute_ms evidence; merge gate >=90%
+ zero new failures. Defers: CI-on-Windows, D-004 paid-model wiring, real Optuna
backend.  |  BY: CEO  |  EFFECT: module map dashboard row updated; open question
"Dashboard load patterns vs MC compute cost" addressed via st.cache_data + perf
evidence.

## Ruling D-20260803-001 (T3) — SELECTIVE-SMALL-CAP THESIS (falsification-first)
SUMMARY: CEO APPROVES hybrid on brief B-20260803-001. Discovery gains a
SELECTIVITY stage: pre-registered falsification of OCF level/trend +
reinvestment + operating-margin trend on the existing 1002-name XBRL+prices
universe (reuse cohort infrastructure) BEFORE any new datastore funding.
Insider-buying datastore + gross-margin PIT work are CONTINGENT on falsification
results. Near-miss WATCHLIST is a monitor artifact, not an investment rule.
MODIFIES D-20260802-002's profit-agnostic stance: profitability factors return
as a selectivity gate, not a hard profitability requirement. gemini-planner
position was written under single-model fallback (subagent down).  |  BY: CEO
|  EFFECT: new selectivity/falsification phase gates discovery; datastore
funding decisions are now evidence-gated; watchlist deliverable added.

## Ruling D-20260803-002 (T3) — TWO-SLEEVE PORTFOLIO (cost-aware allocation)
SUMMARY: CEO MODIFY on brief B-20260803-002. APPROVE hybrid: deploy BOTH
sleeves at once — megacap DCF+moat AND S&P600 relative-moat — with small-cap
capital FLOORED at pre-registered 10-15% until D-20260803-001 falsification
passes, then scales up. Allocation = L3 LLM dynamic with BOUNDED ranges
(30-70% per sleeve); static 60/40 is the fee-baseline reference only, not an
invariant. Moat-creation = continuous PIT-bounded score (rising OCF +
profitable + high reinvestment = dedicated-capital tilt), NO binary no-moat ->
profitability fallback. CEO MODIFICATION: dynamic allocation is
transaction-cost-aware (weights change only when expected gain clears trading
friction); LIQUIDATE ONLY on absolute buying opportunities (no rebalancing
churn; sell only to redeploy or de-risk); a $10k implementation-today
simulation must sum all backtests (total gain, fees lost to constant trading,
Sharpe, maxDD); backtest registry adopted (universe/params/gates/exit/metrics/
dates/verdict); college agent records ruling + simulation results as adaptive
evidence.  |  BY: CEO  |  EFFECT: two-sleeve discovery + cost-floor dynamic
allocator + opportunistic-liquidation rule + backtest registry; new $10k fee
simulation deliverable; small-cap sleeve stays floored pending falsification.

## Ruling D-20260803-003 (T3) — MULTI-ASSET SLEEVES + MACRO-STATE ROTATION (phased)
SUMMARY: CEO APPROVE hybrid (phased) on brief B-20260803-003. PHASE 1 (this
ruling): (1) bonds + gold as additional sleeves; (2) gradient-descent-like risk
minimizer across ALL assets (equity sleeves + bonds + gold), friction-bounded;
(3) macro-state allocator — bull -> buy cheaper bonds/alternatives, bear -> buy
cheap stocks — opportunistic gains run through the regime overlay; (4) $10k sim
vs baseline + one theoretically-better strategy, with dividend-fee-coverage
measured inside the sim (stable-dividend basket yield vs rotation fee drag);
(5) auditor gate: NO hardcoded weights/picks/params in the backtest, OOS
validation mandatory; (6) multi-source data (FRED/SEC EDGAR etc., not only
yfinance). PHASE 2 (deferred, separate brief): opportunistic stock engine +
dividend engine + stable-dividend audit — greenlit only after Phase 1 conductor
pass + dividend-fee-coverage recorded. D-20260803-002 fee-aware liquidate-only
rule preserved and applies to new sleeves; small-cap sleeve stays floored until
D-20260803-001 falsification passes.  |  BY: CEO  |  EFFECT: portfolio becomes
multi-asset (equity + bonds + gold) governed by macro-state allocator + risk
minimizer; phased build; auditor-gated $10k sim deliverable.

## Ruling D-20260803-004 (T3) — PHASE 2 GREENLIT: OPPORTUNISTIC + DIVIDEND ENGINES
SUMMARY: CEO APPROVE hybrid + floor on brief B-20260803-004 (Phase 2 of
D-20260803-003; both greenlight conditions met — Phase 1 conductor pass
857/18/0 + dividend-fee-coverage recorded 53-71x). Scope: (1) STABLE-DIVIDEND
AUDIT module — SEC XBRL cash dividends cross-checked vs Nasdaq/yfinance price
yields, 5y unbroken-payout window (arithmetically feasible from 2018; 10y
rejected), >=3% yield floor, special dividends excluded, REIT/BDC/MLP excluded,
yields used for fee-coverage measurement only (never selection); (2) MINIMUM-
CANDIDATES FLOOR with bills fallback in pre-registered config so the sim cannot
starve in 2018-2022; (3) OPPORTUNISTIC engine — reuses the D-20260802-002
absolute-buying-opportunity swap rule AS-IS with a pre-registered bear-regime
gate appended, equity sleeve only, small-cap floor preserved; (4) DIVIDEND
strategy variant in fee_sim3 (BASELINE/MACRO/MINVAR untouched) for comparability.
Sequencing: audit module -> opportunistic engine -> DIVIDEND variant -> $10k sim
re-run -> auditor (no hardcoding + OOS) -> coverage re-measure + registry ->
conductor -> CEO report.  |  BY: CEO  |  EFFECT: blueprint gains Phase-2 engine
scope; fee_sim3 gains a fourth strategy; multi-source dividend audit (SEC XBRL
+ price yields) enters the data layer.

## Ruling D-20260803-005 (T3) — ML-OPTIMIZED ALLOCATOR: STATIC + ADAPTIVE (MODIFY)
SUMMARY: CEO MODIFY on brief B-20260803-005 (hermes hybrid adopted as build
base, then restructured). THREE test strategies: (1) STATIC-40/20/20/20 (CEO
version, fixed weights); (2) STATIC-after-ML — gradient-descent finds optimal
weights once (in-sample fit, pre-registered train/OOS split), then held
statically; (3) ADAPTIVE — weights re-optimized dynamically through the window
with REQUIRED risk optimization. Risk optimization: objective = MAXIMIZE SHARPE;
weights PUNISHED when too risky; HARD constraint that the portfolio is down at
most 30% at any point (max drawdown <= 30% at every point). Comparison set:
SPY baseline, STATIC-40/20/20/20, STATIC-after-ML, ADAPTIVE, opportunistic-only
baseline. Build order: static 40/20/20/20 -> dynamically find optimal ratio ->
run it statically (STATIC-after-ML) -> adaptive re-optimizing version.
Unchanged from hybrid: all weights/thresholds/train-OOS-split/risk-penalty/30%
DD bound pre-registered in config/weights_diversification.yaml before any run
(invariant 4); profit-change OR-gate flag with fee-churn attribution; small/mid
via liquid ETF proxies (MDY/IWM) under 10-15% floor; cash measured not
maximized; auditor OOS/no-hardcoding discipline; cash-shortfall relocation
reported as an ablation.  |  BY: CEO  |  EFFECT: blueprint gains a risk-
constrained ML allocator delivering three strategies (static CEO, static-after-
ML, adaptive); config/weights_diversification.yaml becomes the required
pre-registration artifact before any optimization run; risk objective is Sharpe
with a hard 30% max-drawdown bound and risky-weight penalty.

## Ruling D-20260804-002 (T3) — BACKTEST DATA-ACCURACY REBUILD (APPROVE hybrid)
SUMMARY: CEO APPROVES hybrid on brief B-20260804-002 after a 3-way council
debate (Position A: full 7-phase PIT rebuild; Position B: minimal 3-phase patch
on #1/#4/#3; hermes synthesis: hybrid). Scope: 7-phase data-layer-only rebuild
(P1 multi-provider prices yfinance+Tiingo/Stooq+Nasdaq with dual raw/adjusted
columns + factor series; P2 real corporate-action dividend/split events replacing
the static DIVIDEND_YIELDS map; P3 macro PIT via fredapi + ALFRED vintages with
source-tagged states; P4 EDGAR PIT via edgartools keyed to filing dates; P5
survivor-free PIT universe incl. delisted retention so the buy-more basket is
populated from 2018; P6 fill-at-next-open + look-ahead/tripwire tests; P7
Discovery re-run with % deltas vs degraded + success bars re-evaluated).
GOVERNANCE: gated on a <=1-hour API probe (FRED/EDGAR/Tiingo keys + quotas)
before P2; per-source DEGRADED-ledger fallback if a key is unavailable — never a
full stop for one missing API; zero strategy-logic changes (data layer only);
invariant-4 pre-registration untouched; success S1-S6 pre-registered; auditor
OOS/no-hardcoding discipline; data-status ledger on every run (no silent
fallbacks — Provenance invariant). Plan + full debate record:
.agents/project/org/research/backtest-accuracy-plan.md.  |  BY: CEO  |  EFFECT:
blueprint gains a data-integrity rebuild contract (7 phases, probe-gated,
DEGRADED-tagged fallbacks); all future backtests must carry a data-status
ledger; the return-max Discovery result is pending re-run on accurate data
before any T3 promotion call.

## Ruling D-20260806-001 (T3) — DISCOVERY TREND FEED (MODIFY; falsification-first)
SUMMARY: CEO MODIFIES on brief B-20260806-001 (synthesis S-20260806-001). Build a
deterministic trend-ranked discovery feed — NO epsilon-greedy RNG. Immediate scope:
structured sources (SEC EDGAR new-filers, Reddit, StockTwits); IG/TikTok video gated
on P1 sandbox evidence (≥1 qualitative-gate pass). IG/TikTok hygiene: clout-chaser
filter, niche/minimal-popularity + tandem/ecosystem preference (monopoly+dependents,
ASML example), ad/sponsored exclusion. Every mentioned ticker MUST run the FULL
pipeline (qualitative engine + quant baseline) before candidate/allocation. Core
baseline RM-FINAL and ML weights in config/weights*.yaml must NOT change; baseline
rerun bit-identical (hash-asserted). Pipeline stays offgrid/off-main on
feature/b-20260806-001. No-regression bar is RELATIVE-vs-baseline (metrics ≥
baseline), not absolute floors. Plan:
.agents/project/org/decisions/_drafts/impl-plan-B-20260806-001.md (gates P1 sandbox
census → P2 deterministic ranker → P3 integration ablation → P4 OOS purge-and-embargo
→ P5 go/no-go; every phase fails closed, research-only on fail). | BY: CEO | EFFECT:
blueprint gains a PENDING discovery trend-feed pillar (research-only, NOT wired);
new additive top-level discovery/ module planned; existing config/weights*.yaml,
diversification/, portfolio/, Quantitative/stochastic/, backtesting/ cores frozen
with SHA-256 manifest + auditor bit-identical check; no integration until P1–P5 pass
and a separate APPROVE ruling.
