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
| `Qualitative/psychological/` | monte_carlo, four_lane_pipeline, qualitative_scoring, bayesian_calibration, nlp_engine, velocity_tracker, state_machine, behavioral_feature_store, signal_matrix, dcf_floor, data_fusion, scrapers/ (instagram_primary, nodriver_scraper) |
| `Quantitative/` | stochastic/, bonds/, gold_etf/, dividends/, fragility/, funds/, allocation/, sensitivity/, audit/, shared/, company_classifier |
| `optimization/` | optuna_search, ab_testing (champion/challenger) |
| `backtesting/` | monthly rebalancing sim (IC, Sharpe, Hit Rate), drift detection, chi-square gate + v2 metric bundle (`chi_square.run_standard_backtest` over fee_sim3 ENGINE_MAP) |
| `discovery/` | RESEARCH-ONLY lanes — leaf module, kill-switch `discovery.enabled: false`: deterministic trend ranker, supply-chain frontier engine (frontier.py, ai_cons.py, etf_weights.py), Wikidata lane (wikidata/wiki_frontier/wiki_census/wiki_sec_diff), alt-data consensus gate + review collectors. NEVER wired into run-all without a separate APPROVE ruling |
| `valuation_alpha/` | L1 equity sleeve: 50-name universe (bias/beta tags), SEC XBRL 10y datastore, peer-relative ratios, FF5 residual alpha, candidate portfolio ranking, stats battery |
| `diversification/` | L2 sleeve: replay bond/gold/fund selectors over 10y, bond/rate factors, sigmoid macro allocator + sub-period diagnostics (backtest_diagnostics.py), grade-12 execution calendar (trading_calendar.py) |
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

Core: Reddit, StockTwits, ApeWisdom, Glassdoor, G2, Capterra, App Store,
GitHub, SEC EDGAR, FRED, yfinance. Credentials: Reddit, StockTwits, ApeWisdom.

**FREE-ONLY mandate (D-20260811-001):** no paid sources (Sharadar/Norgate/
Kaiko/Cboe DataShop out of scope). Backtest scope = the 10-year AI window
2016–2026 (not dot-com); two-track plan = Track A historical proxies +
Track B live collection merged via overlap transferability.

**Instagram/TikTok lane (D-20260807-* → D-20260809-001):** independent
experimental discovery channel (NOT gated on structured sources). Cookie-import
auth → `config/instagram_cookies.json` / pool `config/instagram_sessions/*.json`
(git-ignored); nodriver CDP-stealth primary + curl_cffi+cookies+proxy fallback;
proxies from `config/proxies.txt`; distributed async architecture targeting
100k+ Reels (100+ sessions, residential rotation, 5–15s irregular delays);
ffmpeg + Whisper audio transcription (decoupled GPU queue) since key tickers
live in speech. Attention tracking: Kalman + S-curve inflection on
`ig_historical_telemetry`; reverse-heatmap low-mention scan; hybrid LLM
(`opencode run`) semantic transcript analysis with static-keyword fallback.
Demoted to off-market-hours hygiene fallback behind `--with-ig` per
D-20260819-001 (noise floor: 315 junk "tickers" in 10 days).

**Review-site consensus (G2/Glassdoor/Indeed/Capterra):** gate built
(D-20260816-001), kill-switch OFF after D-20260818-001 F1 reject; live passes
run supervised-only via `--live-consensus` with `logs/consensus_status.json`;
flipping requires a further ruling.

**Wikidata×SEC lane (D-20260820-001):** WDQS SPARQL live-screener behind a hard
structural firewall from backtest-agent; PIT replay DEAD at current coverage
(2.02% dated edges << 50% bar); USPTO/USASpending catalogued as complementary
future sources.

**Storm-warning catalog (logged, not built):** EDGAR 8-K/SC 13D/Form 4/144,
USPTO trademarks/patents, CourtListener dockets, USASpending contracts,
openFDA, FINRA short interest + Cboe put/call.

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

## Ruling D-20260801-002 (T3 demo/audit)
SUMMARY: APPROVE full agent-pipeline demo producing an audit of current work.
| BY: CEO  |  EFFECT: none (audit only; quality gate re-passed at 95%).

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

## Ruling D-20260807-001 (T3) — IG INDEPENDENT DISCOVERY EXPERIMENT (MODIFY)
SUMMARY: CEO MODIFY — the StockTwits/ApeWisdom credential pipeline is NOT the
census path (Reddit already runs via browser driver on SUBREDDIT_TAXONOMY, no
API keys). Instagram/TikTok becomes an INDEPENDENT experimental discovery
channel, not gated on Reddit; structured-source credential work deprioritized.
Every IG candidate MUST pass the STANDARD screen (qualitative engine + quant
baseline); the experiment's test = whether IG surfaces companies existing
scrapers miss. Isolation contract stands: discovery/ leaf, discovery.enabled=
false, research-only, RM-FINAL untouched, no merge without a gate pass.
| BY: CEO  |  EFFECT: blueprint gains the independent-channel IG experiment;
full-screen qual+quant gate applies to every IG mention.

## Ruling D-20260807-002 (DISCOVERY) — INSTAGRAM ANTI-BOT SCRAPER BUILD (APPROVE)
SUMMARY: CEO approves in-session build of the real IG feed: cookie import from
the CEO's logged-in browser → config/instagram_cookies.json (git-ignored, no
credentials in repo); nodriver CDP-stealth primary + curl_cffi+cookies+proxy
fallback; additive instagram source weight in config/weights_discovery.yaml.
Built as Qualitative/psychological/scrapers/instagram_primary.py + discovery
wiring; 42 offline tests pass. Live runs require a fresh session cookie from
the CEO.  |  BY: CEO  |  EFFECT: IG independent channel has a real feed;
discovery/ remains a leaf behind the kill-switch.

## Ruling D-20260808-001 (T3) — REVERSE-HEATMAP TRANSITION SCRAPER + WHISPER (APPROVE hybrid)
SUMMARY: Invert the heatmap — scan LOW-mention names for transition-into-
stardom signals: MAD robust Z-scores, Spiegelhalter funnels, ≥3-mentions floor,
positive 7-day attention-velocity inflection; heuristic <$10B size filter +
discrete rolling Markov-Bayes. Plus ffmpeg audio extraction from Reels +
Whisper transcription — critical tickers live in creators' speech, not captions.
| BY: CEO  |  EFFECT: pending reverse-heatmap + audio-transcription architecture
added to the discovery/scraping pillar (research-only).

## Ruling D-20260808-002 (T3) — S-CURVE + KALMAN ATTENTION TRACKER (APPROVE hybrid)
SUMMARY: Dual-tracker attention model: pre-computed tech-keyword CIK mapping
(no runtime graph queries), linear Kalman filter for sparse-tracking stability,
closed-form logistic S-curve inflection check once counts suffice (Goldilocks
transition zone). All telemetry persists to ig_historical_telemetry for
walk-forward backtests.  |  BY: CEO  |  EFFECT: ig_historical_telemetry schema
+ Kalman/S-curve tracking in the discovery/gate_data engine.

## Ruling D-20260808-003 (T3) — LLM SEMANTIC TRANSCRIPT ANALYZER (APPROVE hybrid)
SUMMARY: Hybrid transcript understanding: `opencode run` LLM semantic analysis
extracts breakthrough/AI concepts from spoken Reels and maps them to subsystem
supplier tickers; static keyword dictionaries remain the failure fallback.
llm_analyze_transcript_buzzwords added and extract_tickers integrates it.
| BY: CEO  |  EFFECT: semantic supplier linkage beyond rigid keywords with
zero-regression static fallback (82 tests pass).

## Ruling D-20260808-004 (T3) — HIGH-SCALE PROXY & SESSION ROTATION (APPROVE)
SUMMARY: Scale safely toward 100k–1M Reels: proxies read dynamically from
config/proxies.txt; cookie sessions loaded from a pool under
config/instagram_sessions/*.json and rotated on rate limits; silent fallback to
single session file keeps compatibility. InstagramConfig gains proxy/session
fields (_get_random_proxy, _resolve_session_path).  |  BY: CEO  |  EFFECT:
ban-safe large-crawl configuration (82 tests pass).

## Ruling D-20260808-005 (T3) — MULTI-ASSET OPTIMIZATION RESULTS ADOPTED (APPROVE)
SUMMARY: Adopt optimized multi-asset results (stocks+bonds+gold, 40% equity
floor, projected gradient descent maximizing Sharpe under a ≤30% drawdown
constraint): IG/niche-tech hybrid 185.37% vs Traditional 99.92% vs SPY 68.54%.
The low-coverage qualitative-gate bypass + expanded AI-supplier keywords (CIK
supply-chain mapping; VRT/CLS/FN captured during the AI buildout) become the
production discovery-screening standard.  |  BY: CEO  |  EFFECT: bypass +
supplier mapping formally adopted; multi-asset sleeve structure validated.

## Ruling D-20260808-006 (T3) — WALK-FORWARD VERIFICATION, NO LOOKAHEAD (APPROVE)
SUMMARY: Dynamic walk-forward backtest (trailing 30-day selection windows,
monthly rebalance, point-in-time data only) confirms alpha is structural, not
lookahead luck: 186.67% vs Traditional 162.31% vs SPY 68.42%. Walk-forward
validation + PIT boundaries become the standard backtest-registry requirement.
| BY: CEO  |  EFFECT: no-leakage discipline formalized for all future backtests.

## Ruling D-20260808-007 (T3) — MACRO-REGIME SIGMOID ALLOCATOR (APPROVE)
SUMMARY: Replace binary macro switching with continuous logistic sigmoid
transition weights driven by first/second derivatives of credit spreads /
market stress; rotate early into short T-bills/gold as stress accelerates
(mitigated 2022-style drawdowns −45% → −9.4%). Chronological sub-period
reporting (Bear 2022 vs Bull 2024–26) + component-level returns join core
reporting. New diversification/backtest_diagnostics.py.  |  BY: CEO  |
EFFECT: smooth friction-reducing regime rotation replaces whipsaw-prone
binary switches.

## Ruling D-20260808-008 (T3) — GRADE-12 CALENDAR + 70/30 IG/REDDIT SLEEVE (APPROVE)
SUMMARY: Rebalancing dates bounded by the CEO's real school calendar (Wednesday
afternoons, orientation days, half days, breaks) to remove execution-window
bias; dynamic equity sleeve split 70% Instagram / 30% Reddit. New
diversification/trading_calendar.py; backtest_diagnostics filters rebalances
through it.  |  BY: CEO  |  EFFECT: simulations match real-world execution
constraints; IG weighted as primary alpha signal, Reddit support sleeve.

## Ruling D-20260809-001 (T3) — SCALE REELS SCRAPER TO 100K+ VIDEOS (APPROVE)
SUMMARY: Distributed asynchronous scraping architecture targeting the
personalized Reels feed: pool of 100+ account sessions, residential proxy
rotation, irregular 5–15s delays; Whisper transcription DECOUPLED onto GPU
task-queue workers so crawling is never throttled by transcription. Follow-ups:
proxy rotation registry, S3-compatible storage, programmatic cookie manager.
| BY: CEO  |  EFFECT: discovery pipeline spec updated to distributed async +
proxy pools + decoupled GPU Whisper workers.

## Ruling D-20260809-002 (T3) — HOUSE BACKTEST v2: CHI-SQUARE GATE + ARCHETYPE COUNCIL
SUMMARY: CEO APPROVES the v2 backtest build (brief B-20260809-002). (1) New gate:
`backtesting/chi_square.py` — regime×win/loss chi-square (chi2, fisher fallback on
sparse cells) emits SYSTEMATIC vs CHANCE on the FULL window; `run_standard_backtest`
validates methods against ENGINE_MAP (spy, macro, minvar, dividend, opportunistic,
static-ml, adaptive, rm-final — existing fee_sim3 engines only) and appends rows
with the full metric bundle (Sharpe, Sortino, Calmar, win-rate, FF5 alpha + 95% CI,
excess vs SPY, info ratio, maxDD, fees $/% of gains, trades). (2) Fixed windows:
FULL 2018-01-31..2026-07-31 and RECENT 2025-01-01..2026-07-31; regimes bull 2023-24,
bear 2020 crash + 2022. (3) AUDIT-STATUS line: only `AUDITED CLEAN` may claim "100%
fully audited"; DEGRADED-DATA blocks; env-degraded tags report but don't block.
(4) `fee_sim3` engines gain a `vpath` return so gains are expressed as paths, not
just end values. (5) New backtest-agent (Gemini 3.1 Pro via Antigravity, CEO
mandate) executes `/backtest {method}` and `/backtest history`
(.opencode/command/backtest.md), writing artifacts to
.agents/project/org/backtests/ and append-only rows to backtest-registry.md (v2
schema). (6) Archetype council created (sim-guardian, risk-automator,
execution-strategist, alpha-integrator, devil-advocate, open-minded — advisory,
read-only, voices must appear in T3 synthesis). (7) Skills installed: philosopher
(via skills.sh), in-house debate-protocol + planning; pruning-policy.md governs
strategy retirement (CHANCE verdict, OOS decay, overfit, maxDD breach, fee > alpha,
strict-dominance successor). | BY: CEO | EFFECT: backtesting/ gains the gate +
metric bundle; backtest claims become falsifiable and audit-tagged; registry rows
are decisions, not dashboard entries; 9 new archetype agents in .opencode/agent/;
v2 engine files stay untouched by discovery/finance lanes (frozen cores discipline
applies).

## Ruling D-20260811-001 (DISCOVERY) — FREE DATA-SOURCE STRATEGY + AI-ERA WINDOW
SUMMARY: CEO direction recorded. FREE-ONLY mandate — paid sources out of scope.
58-source catalog narrowed to a tiered free set (trend: HN Algolia, GitHub,
Google Trends, GDELT, PyPI/npm, HF downloads, Reddit/StockTwits dumps; prices:
Tiingo, Stooq, Polygon; training/macro: PhraseBank, FiQA, EDGAR, FRED, Cboe
VIX). Dot-com era rejected as era-mismatch for a tech strategy — correct scope
is the 10-YEAR AI WINDOW 2016–2026 (Bull1 2016-18, Bear1 Q4-2018, Bull2
2019-21, Bear2 2022 NVDA −66%, Bull3 2023-24 ChatGPT supercycle, Bear3 2025
DeepSeek shock, live 2026 walk-forward). Two-track architecture: Track A
historical proxies now + Track B live dynamic collection, merged via overlap
validation (2023–26) producing a per-metric transferability coefficient. Alpha
hypothesis under test: IG hysteria → algorithm discovers NEW companies
(perception axis distinct from Reddit=retail, GitHub=developers,
Glassdoor=employees); IG is the LIVE experimental overlay, not a backtest
carrier; the 2016–26 weight rides on historical qualitative proxies + the
cashflow pillar. Storm-warning layer logged: EDGAR 8-K/SC 13D/Form 4/144,
USPTO, CourtListener, USASpending, openFDA, FINRA/Cboe positioning data.
| BY: CEO  |  EFFECT: free-only constraint + AI-window framing govern all
future backtests; no immediate blueprint delta pending a discovery brief.

## Ruling D-20260815-001 (T3) — IG_LLM SENTINEL VALIDATION (APPROVE hybrid)
SUMMARY: CEO APPROVES implementation of IG_LLM Sentinel Validation (brief B-20260815-001, synthesis S-20260815-001) under four constraints. (1) Combined Hallucination Controls: mandate both structured output schema and source-URL grounding + per-score audit trail for LLM-synthesized qualitative proxies. (2) Gate-Threshold Freeze: hard-prohibit edits to qualitative gate thresholds or exclusions in this PR. (3) Pre-Build Cost Estimate: analyze token usage before writing main code. (4) Ticker-Collision Validation: implement defensive ticker lookup to avoid overlaps. EFFECT: Unlocks RFF256 discovery candidate stream with programmatic proxy scoring (IG_LLM_ prefix) inside existing qualitative gate.

## Ruling D-20260816-001 (T3) — ANTI-BIAS ALT-DATA CONSENSUS GATE (APPROVE rev3)
SUMMARY: CEO APPROVES the anti-bias alt-data consensus gate (brief B-20260816-001, synthesis S-20260816-001, impl-plan rev 3) as a research-only build. (1) Hardcoded frozen weight sheet: quantifiable anchor 50% / expression-voice 30% / subjective ratings 20% cap; per-factor weights fixed pre-ingestion, only CEO-ruling mutable on a pre-registered sufficient-data condition. (2) Review usability ladder: <10 = INSUFFICIENT abstain (never neutral 0.5); 10-49 = directional; 50-99 = distributional; >=100 = usable evidence. (3) SET-ASIDE rule: <50 total reviews across platforms -> company left aside + marked, review block contributes 0 (still scores on Type-A/B). (4) Attack flags: BRIBE-ATTACK (>=5 same-star in-window burst + suspicious profiles), COMPANY-PUNISHING ATTACK (>3x weekly volume spike / coordinated 1-star barrage) — flagged evidence quarantined, cannot cross pass line. (5) POLARIZED / NO-CONVERGENCE flag: |skew|>1.5, bimodal clusters, or 2-of-3 convergence failure at usable threshold. (6) LinkedIn talent scout (Type-B, 10% weight): talked-about people joining the company across LinkedIn/JobSpy/IG/Reddit/TikTok hiring mentions = talent capture signal, directional-only. (7) Anti-bot layer for all collectors, NodeDriver strategy for Glassdoor/Cloudflare. (8) Backtest gate: P1 outputs sorted per-company rows -> pre-registered house backtest vs rm-final baseline (relative-vs-baseline bar per D-20260806-001) BEFORE implementation. Constraints held: D-20260815-001 gate-threshold freeze intact; frozen cores untouched; Provenance invariant (data-status ledger, no silent fallbacks); research-only until P1-P5 + separate APPROVE ruling. EFFECT: Blueprint gains the alt-data consensus-gate pillar (research-only, NOT wired); discovery/ gains consensus+collector modules; all flags machine-checkable and audit-trailed.

## Ruling D-20260816-002 (T3) — ANTI-BOT ENGINE FINGERPRINT AUDIT GATE (APPROVE)
SUMMARY: Empirical gate before any engine commitment: a fingerprint audit of
nodriver (UA-pinned) vs Playwright-stealth against Glassdoor's live
Cloudflare/Turnstile wall decides patch-nodriver (A) vs re-platform to
Playwright MCP (C); manual CDP browser driving is disqualified for unattended
batch pipelines. cf_clearance TTL logging in the data-status ledger required
before any engine is declared production-ready (silent ~30d cookie expiry =
broken batch runs). UA pin already implemented in
Qualitative/psychological/scrapers/nodriver_scraper.py.  |  BY: CEO  |  EFFECT:
none yet (pre-build testing phase); engine swap would force provenance
re-validation across all five scrapers.

## Ruling D-20260818-001 (T2) — SUPERVISED LIVE CONSENSUS: F1 REJECT → PARTIAL RECOVERY
SUMMARY: APPROVE enabling supervised live consensus: consensus.enabled
false→true, --live-consensus on the active pipeline, supervisor watcher with
logs/consensus_status.json + zero-evidence notification, bars S1–S4 and failure
plan F1–F4 pre-registered. First supervised run FAILED S1 (zero evidence:
port-9222 attach down, Indeed extraction mismatch, G2 403 ×2, Browserless
localhost:3000 down, DNS fail) → F1 triggered → REJECT live-in-active-pass;
kill-switch reverted. Fail-closed design validated end-to-end (no guest-jar, no
fake rows, supervisor alerted). Follow-up option B executed: Brave launched
debuggable on 127.0.0.1:9222 (master profile) → next loop MET S1 (NVDA score
0.203, usable=2, reviews=4289, no SET-ASIDE). Remaining blockers: Browserless
down (Capterra), Adzuna API 401, JobSpy not installed, Indeed extraction.
consensus.enabled REMAINS false — live passes only via supervised
--live-consensus; flipping requires a further ruling. Re-plan brief pending for
residual site failures.  |  BY: CEO  |  EFFECT: supervisor gains -LiveConsensus
health visibility; consensus gate stays research-only.

## Ruling D-20260820-001 (T3) — Wiki×SEC DISCOVERY LANE (ADOPT hybrid + PIT amendment)
SUMMARY: CEO ADOPTS the hybrid on brief B-20260820-001 (synthesis S-20260820-001).
New research-only Wikidata discovery lane: SPARQL bulk pull of P249 tickers +
P355/P127/P749 typed edges WITH P580/P582 validity qualifiers; grade-prioritized
BFS over company→company edges; topic-triggered DFS descent GATED behind a
pre-registered ≥10-descent falsification experiment vs IG's 315-junk noise
floor; hard STRUCTURAL firewall wiki↔backtest-agent from day one; hub defenses
(class filter, industry excluded, out-degree>50 zeroed, blocklist); parallel
wiki×SEC diff harness (PASS = both buckets non-trivial). CEO PIT amendment:
dated edges form reconstructable historical graph states; revision-history
reconstruction prototype on NVDA/AMD at T−3y/T−5y; ≥50% seed-graph edge-dating
bar unlocks walk-forward discovery replay (dated edges only), else lane stays
live-screener. USPTO/USASpending catalogued as complementary future source.
Flip condition: experiment false-positive rate ≥ IG baseline → abandon DFS,
pivot USPTO primary.  |  BY: CEO  |  EFFECT: discovery/ gains wikidata.py +
wiki_frontier.py + wiki_census.py + wiki_sec_diff.py (research-only, own
tables wikidata_companies/wiki_edges/wiki_runs with valid_from/valid_to from
day one); config/sentinel.yaml gains lanes.wikipedia block; frozen cores and
ecosystem_graph_* untouched; separate APPROVE ruling required before any
wiring into run-all.

## Ruling D-20260823-001 (T3) — STACK A PIT SANDBOX: PIPELINE SKILL FALSIFICATION (APPROVE hybrid, condition met, Phase 0 executed)
SUMMARY: CEO conditional APPROVE of hybrid synthesis S-20260823-001 (brief B-20260823-001): Position A's per-row
`available_as_of` storage schema + quarantine partitions as the enforcement layer, Position B's staged metric
sequencing (NLP corpus skill proven on verified human-labeled corpora BEFORE any return linkage), sim-guardian's
frozen-hash regression replay as the mandatory audit instrument, centralized clock-stepped read layer over the views
(sim-guardian fix of B's static-clock hole). Condition "sources must be sortable by real dates" VERIFIED: transcripts
dataset carries populated `date` field (2005–2025) + MIT license; XBRL/FSDS filed-dated; Damodaran year-labeled;
Chen-Zimmermann vintaged; GDELT event-dated; PhraseBank/SemEval undated → atemporal oracle use only. Council
counter-tests implemented as executable gates: DA-1 timestamp coverage ≥80%/source; DA-2/B-2 oracle transfer
(>15pp F1 drop on held-out REAL inputs = miscalibrated); B-1 instrument provenance (rule-based or training cutoff ≤
2009-01-01); SG-1 frozen-hash replay bit-identity incl. negative control. Pre-registered bars frozen in
config/weights_sentinel_bars.yaml BEFORE any data run. Tuning locked until all stage verdicts recorded.  |  BY: CEO
 |  EFFECT: db/schema_pit.py + db/pit_reader.py (pit_transcripts/pit_scores/pit_market_labels + _excluded
quarantine twins + pit_audit_log; pit_query read-only via v_* temp views, raw-table access blocked);
scripts/pit_phase0_audit.py; tests/test_pit_sandbox.py (9 gates); config/weights_sentinel_bars.yaml. Frozen cores,
existing config/weights*.yaml untouched. Next phases gated: ingest idleengine corpus w/ coverage audit → Stage-1
corpus scoring → Stage-2 transfer check → Stage-3 Strux return linkage → Stage-4 ablation matrix → verdicts → then
tuning. Separate APPROVE required before any wiring into run-all.

## Ruling D-20260819-001 (T3) — VIRUS-FRONTIER: OVERLAP-GRADED FRONTIER ENGINE (APPROVE hybrid, executed)
SUMMARY: CEO APPROVES the index-anchored overlap frontier (brief B-20260819-001, synthesis S-20260819-001). Discovery feed now walks the SUPPLY-CHAIN web, not social noise: seed = NVDA + competitors (AMD/INTC/AVGO) + major set (MSFT/GOOGL/META/AMZN/AAPL); grade(S) = Σ relevance(customer) over shared customers across the MAJOR SET (overlap-graded, not seed-only); upstream inheritance ("the spot before the shovel") — tier-2 suppliers graded by the tier-1 customers they feed; max 3 hops, ≤200 nodes/seed, ≤50 edges/node, edges CIK-validated + point-in-time (filed_date) + ≥2 sources; QQQ ETF weights SUPPLIED as supplemental cache (quarterly) only — competitors-primary; AI-cons vector: 10 pre-registered cons ("companies solving AI's problems") by sector + quant gate, narrative never required, hidden-gem steal filter; IG DEMOTED to off-market-hours hygiene fallback behind --with-ig (noise floor: 315 "tickers" in 10 days); slow binge-block pacing (block 10, gaps 120-300s, max 3 active hours) for ban safety; quant gate is the SOLE value filter ("a steal doesn't need attention"). Co-mention/13F vectors SUPPRESSED at launch (fail ≥2-source gate). | BY: CEO | EFFECT: discovery/frontier.py + ai_cons.py + etf_weights.py; ecosystem_graph_nodes/edges + etf_holdings schema (point-in-time); frontier sentinel lane + frontier-expand CLI; run-all demotes IG; config/frontier_edges.yaml seed; 51 new tests; full suite 1236 passed/18 skipped/0 failed; commit 61147ea on feature/frontier-overlap-engine. Live SEC >10% customer extraction is Phase-2.
