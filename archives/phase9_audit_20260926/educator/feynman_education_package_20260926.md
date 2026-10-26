# Phase 9 Feynman Education Package — Executive Decision Briefing
**Date:** 2026-09-26 | **Status:** CEO APPROVED — Educational Translation Required | **Role:** `educator` (Feynman Method)

---

## 1. ORIGINAL CONTEXT

> **CEO Ruling**: APPROVED — Phase 9 Full Architectural Redesign with Atomic Pipeline + HMM Internal Gate
> 
> **Synthesis Decision**: Single orchestrator (`research/phase9_full_pipeline.py`) that:
> 1. Builds Factor-Model HMM → **HARD GATE**: ARI ≥0.70 on ≥3/4 folds, occupancy caps (no state >60%, CRISIS ≤20%), hard monotonicity ke rejection
> 2. Runs Joint ML parameter search (500 LHS, purged/embargoed CV, TCA in loop, honest DSR trial tracking N=13+500)
> 3. Executes 8-gate engine (Arm Superiority gate collapsing Gates 1-4, FWER α/8)
> 4. Genuine 3-arm backtest (synthetic fallback disabled)
> 5. Full audit (provenance, gate re-check, pre-registration compliance)
> 
> **Pre-registration freeze**: `config/pre_registration_v4.yaml` with SHA-256 before execution.

**Purpose of This Document**: Translate every technical decision above into plain English with definitions, examples, why it benefits us, pros/cons, and edge cases — so leadership can govern without being statistical practitioners.

---

## 2. KEY TERMS (Glossary)

| Term | Plain Definition | Level |
|------|------------------|-------|
| **Walk-Forward Backtest** | Testing a strategy on future data it hasn't seen, rolling forward in time (like real trading) | L1 |
| **Purged & Embargoed CV** | Cross-validation that removes data leakage by cutting out overlapping time periods | L2 |
| **Deflated Sharpe Ratio (DSR)** | A "luck-adjusted" Sharpe ratio that penalizes you for how many strategies you tested | L3 |
| **Family-Wise Error Rate (FWER)** | The probability of at least one false positive when running multiple statistical tests | L2 |
| **Holm-Bonferroni** | A step-down method to control FWER that's less conservative than standard Bonferroni | L3 |
| **Hidden Markov Model (HMM)** | A statistical model that infers hidden "regimes" (bull/bear/crisis) from observable market data | L3 |
| **Factor-Model HMM** | HMM that uses Principal Components of macro indicators instead of raw prices | L3 |
| **Monotonicity Constraint** | Forcing the model to obey economic logic (e.g., cost of equity must rise in crisis) | L2 |
| **Swap Barrier** | A dynamic threshold that decides when to replace a holding with a better opportunity | L2 |
| **Probation Gate** | A quality filter that flags degraded signals before they enter the portfolio | L2 |
| **Synthetic Fallback** | Fake random data inserted when real data is missing — caught and blocked by our guard | L2 |
| **Trial Ledger / N-count** | Honest accounting of every parameter combination tested for DSR calculation | L3 |

---

## 3. PREREQUISITE CHECK (Concept Ladder)

```
L1: Basic Trading Logic
    │
    ├─→ "We buy low, sell high. Backtest = simulate past. Live = real money."
    │
    ▼
L2: Statistics for Finance
    │
    ├─→ "Sharpe = return / risk. p-value = probability result is luck. 
    │     Multiple tests = more false positives. Embargo = time gap to prevent leakage."
    │
    ▼
L3: Advanced Econometrics
    │
    ├─→ "DSR adjusts Sharpe for trial count N, track record T, skew, kurtosis.
    │     HMM infers hidden states from observables. PCA compresses correlated signals.
    │     Purge/embargo removes lookahead bias in rolling features."
    │
    ▼
L4: Stochastic Calculus / ML Systems
    │
    └─→ "Nested CV with grouped Lasso. Dirichlet priors on transition matrices. 
         Sobol sequences for quasi-random parameter search. SHA-256 provenance chains."
```

**You are here**: L2-L3 boundary. This document bridges to L3-L4.

---

## 4. FEYNMAN CONCEPTS (Plain Explanations + Analogies)

---

### CONCEPT 1: Purged & Embargoed Cross-Validation

**Plain Explanation**: 
Imagine you're studying for a history exam. Standard K-fold would be: rip the textbook into 5 pieces, study 4 pieces, test on the 5th piece. But history has *chronology* — Chapter 5 (WWII) depends on Chapter 4 (Great Depression). If you test on Chapter 5 but studied Chapter 4, you didn't really predict WWII — you just memorized the timeline.

**Purged & Embargoed CV** fixes this:
- **Purge**: If your test is on WWII (1939-1945), you *remove* the Great Depression (1929-1939) from your training set because its effects "bleed into" WWII.
- **Embargo**: After testing on WWII, you wait a "cooling off" period (e.g., 1945-1950) before training on the Cold War, so WWII's aftermath doesn't leak into Cold War predictions.

**Analogy**: 
> You're teaching a kid to ride a bike. Standard CV: show them a video of riding, then immediately test them on a different bike. Purged/Embargoed: teach them on Monday, *wait a week* (embargo) so they forget the specific wobbles, then test on a *different* bike on a *different* day (purge overlapping skills).

**Why It Benefits Us**:
- Prevents the #1 cause of backtest-overfit: **lookahead bias** from rolling features (22-day vol, 60-month beta)
- López de Prado (2018) proved standard CV produces "flawless backtests that collapse live"
- Our Phase 8 failure: HMM learned noise because CV leaked regime information across folds

**Pros**:
- ✅ Mathematically correct for time series
- ✅ Matches live trading reality (you can't use future data)
- ✅ Required for any credible academic/quant review

**Cons**:
- ❌ Reduces training data (purge + embargo remove ~18 months per fold)
- ❌ More complex to implement correctly
- ❌ Fewer parameter combinations can be tested (smaller N for DSR)

**Edge Cases**:
| Edge Case | Handling |
|-----------|----------|
| Short test fold (Fold 1: 0 trades) | Minimum trade count threshold (e.g., ≥20 trades) before gate evaluation |
| Regime-dependent parameters | Separate CV per regime OR joint with regime as feature (Position B: joint) |
| High turnover strategies | Purge window = holding_period × 5 (our spec: trailing_tau × 5 days) |
| Structural breaks (2020 COVID) | Embargo must span the break (12 months covers it) |

---

### CONCEPT 2: Deflated Sharpe Ratio (DSR) — Bailey et al. (2014)

**Plain Explanation**:
You try 500 different moving-average combinations. One gives Sharpe 2.0. Traditional finance says "Genius!" DSR says: "If you try 500 random strategies on pure noise, the *best* one will average Sharpe ~3.7 just by luck. Your 2.0 is *worse* than random luck. Rejected."

**Formula Intuition**:
```
Expected max Sharpe from N random trials ≈ √(2 × ln(N))
Your observed Sharpe = 1.4
DSR asks: "What's the probability 1.4 beats the noise benchmark?"
```

**Analogy**:
> Lottery with 500 tickets. One wins. Traditional: "Winner has psychic powers!" DSR: "With 500 tickets, someone wins by chance. Show me they win *repeatedly* with a *new* ticket."

**Why It Benefits Us**:
- Phase 8: We tested ~13 effective parameter clusters + 500 LHS = N ≈ 513
- Observed OOS Sharpe = 0.6665 → DSR = **40.3%** (FAILS >95% threshold)
- Without DSR, we'd falsely believe 0.6665 Sharpe is "good"
- DSR is the **primary falsification criterion** in our pre-registration

**Pros**:
- ✅ Honest accounting for multiple testing (the "file drawer problem")
- ✅ Incorporates skew/kurtosis (tail risk)
- ✅ Track record length T adjustment (short records = less confidence)

**Cons**:
- ❌ Requires honest N (hard to track in ML loops)
- ❌ Assumes trial independence (our clusters are correlated → conservative)
- ❌ Can be overly harsh for correlated parameter sweeps

**Edge Cases**:
| Edge Case | Handling |
|-----------|----------|
| Correlated parameters (our clusters) | Use effective N (13 clusters) not raw count (500 LHS) — both tracked |
| Non-normal returns (skew/kurt) | DSR formula includes γ₁, γ₂ adjustments — we compute from data |
| Very short track record (T < 2yr) | DSR → 0 automatically; our T=4yr (1008 days) is minimum viable |
| ML hyperparameter search | TrialTracker class logs EVERY param set evaluated for honest N |

---

### CONCEPT 3: Harvey / Bonferroni / Holm — Multiple Testing Correction

**Plain Explanation**:
You test 30 technical indicators. One has p=0.028 (t=2.2). Old rule: "Significant!" Harvey: "You tested 30 things. By chance, 1.5 will hit p<0.05. Your threshold must be p<0.05/30 = 0.00167 (t≥3.2). Your indicator fails."

**Bonferroni**: Divide α by number of tests (α/30). Very conservative.
**Holm-Bonferroni**: Step-down — sort p-values, test smallest at α/30, next at α/29, etc. Less conservative, same guarantee.

**Analogy**:
> Courtroom with 30 witnesses. Each has 5% chance of lying. Old rule: "Any witness with p<0.05 is credible." Harvey: "With 30 witnesses, expect 1.5 liars by chance. Raise the bar so expected liars < 0.05."

**Why It Benefits Us**:
- Phase 8: 16 gates but only ~5-7 effective independent tests (clusters)
- Gates 1-4 (Arm comparisons) SKIPPED → Harvey test **removed entirely**
- Our synthesis: Collapse to 8 effective gates, FWER α/8 = 0.00625 per gate
- Pre-registration: Success criteria fixed BEFORE run (no p-hacking)

**Pros**:
- ✅ Controls false discovery rate rigorously
- ✅ Holm more powerful than Bonferroni
- ✅ Required for publication-grade research

**Cons**:
- ❌ Assumes test independence (our gates are correlated)
- ❌ Can be too conservative (Type II errors — miss real edges)
- ❌ Requires pre-specifying the "family" of tests

**Edge Cases**:
| Edge Case | Handling |
|-----------|----------|
| Highly correlated gates (our clusters) | Cluster-aware correction: Cluster A (3 params) α/3, Cluster B (4 params) α/4 |
| Gates 1-4 SKIP (no Arm B/C data) | Synthetic fallback guard blocks SKIP; genuine 3-arm run mandatory |
| Pre-registration vs discovery | All gates pre-registered in `config/pre_registration_v4.yaml` SHA-256 frozen |

---

### CONCEPT 4: HMM Identifiability & Factor-Model Redesign

**Plain Explanation**:
An HMM tries to infer hidden "weather" (regimes) from "thermometer readings" (market data). 

**Phase 8 Failure**: 3-state t-HMM on 2 raw observables → **94% SHOCK occupancy**. Like a weather model that says "HURRICANE" 94% of days because the thermometer has too many settings (ν=6, κ=50) and only 2 readings.

**Factor-Model Fix**: Instead of 2 noisy thermometers, use **3 Principal Components** from **8 macro indicators** (credit spreads, yield curve, vol, sentiment). PCA extracts the "true signal" — like using barometer + humidity + wind + satellite instead of just a thermometer.

**Monotonicity Constraint**: Force the model to obey economics: Cost of Equity (ke) MUST be: EXPANSION < SHOCK < CRISIS. Phase 8 violated this (ke_CRISIS < ke_SHOCK) — the model was economically nonsense.

**Analogy**:
> Teaching a child "Hot > Warm > Cold". Phase 8 model learned "Cold > Hot > Warm". Factor-model + monotonicity = show them a proper thermometer (PCA) AND force the ordering rule.

**Why It Benefits Us**:
- ARI (Adjusted Rand Index) measures regime labeling consistency: Phase 8 = 0.12 (random), Target = 0.70
- Without interpretable regimes, entry/exit signals are random → trade starvation
- Hard occupancy caps (no state >60%, CRISIS ≤20%) prevent the 94% SHOCK trap

**Pros**:
- ✅ Macroeconomic interpretability (regimes map to real economics)
- ✅ Dimensionality reduction (8→3) improves statistical power
- ✅ Hard constraints prevent economically impossible states

**Cons**:
- ❌ PCA components are linear combinations — harder to explain to PMs
- ❌ Rolling PIT quantiles for VIX labels add engineering complexity
- ❌ Factor selection bias (which 8 indicators?) must be pre-registered

**Edge Cases**:
| Edge Case | Handling |
|-----------|----------|
| VIX label lookahead | Rolling 252-day PIT quantiles ONLY (never full-sample) |
| State flapping (rapid switching) | Min-sojourn 5 days + Dirichlet prior λ=0.05 |
| ARI < 0.70 on ≥3/4 folds | **HARD GATE** — abort entire pipeline (orchestrator raises RuntimeError) |
| 2-state vs 3-state | Frozen at 3 for optimization; 2-state ablation as post-hoc validation |

---

### CONCEPT 5: Swap Barrier Math — Exponential Decay with Moat Penalty

**Plain Explanation**:
You own a stock. When do you swap it for a better one? The **swap barrier** is a dynamic hurdle: "Only swap if the new opportunity beats current holding by X%, and X% *decays over time* so you don't churn on noise."

**Math**: `barrier(t) = C × exp(-γ × t / t_target) + moat_penalty`
- `C` = initial barrier (250 bps = 2.5% outperformance required at t=0)
- `γ` = decay speed (0.25 = slow decay, holds ~12 months)
- `t_target` = target hold period (24-36 months)
- `moat_penalty` = extra hurdle for low-moat stocks (OMR penalty)

**Phase 8 Bug**: Two different formulas used in different modules:
- Phase 6: `exp(γ × t_target / t_held)` → **cliff effect** (freezes ~12 months)
- Phase 9 spec: `exp(-γ × t / t_target)` → **almost flat** (barely decays)
- **No justification for the change** → 14.32 swaps/yr vs <5 target

**Analogy**:
> You're dating. Barrier = "How much better must the new person be?" 
> - C=2.5%: "They must be 2.5% better initially"
> - γ=0.25: "My standards drop slowly over 2 years"
> - Moat penalty: "If they're flaky (low moat), standards go UP"

**Why It Benefits Us**:
- Controls **churn** (Gate 14: <5 swaps/yr) → controls TCA (Gate 15: ≤150 bps)
- Moat penalty protects quality names from being swapped for momentum
- Consistent math across modules eliminates the Phase 8 inconsistency

**Pros**:
- ✅ Principled decay (exponential) matches holding period targets
- ✅ Moat penalty aligns with quality thesis
- ✅ Unit testable (verified exponential decay in `test_swap_barrier_v3.py`)

**Cons**:
- ❌ γ and C interact non-linearly — hard to calibrate intuitively
- ❌ Requires accurate moat metric (OMR) which has estimation error
- ❌ Regime-dependent barriers add complexity (our spec: regime-conditional)

**Edge Cases**:
| Edge Case | Handling |
|-----------|----------|
| Concurrent exits (many positions hit barrier) | Correlation clustering in exit_scheduler (5% ADV, 3-day TWAP) |
| Black Swan (CRISIS regime) | Barrier drops to 0.75×IV (opportunistic buying) — separate logic |
| Illiquid names | Illiquid flag → barrier × 2 (harder to exit) |
| New position (t=0) | Full barrier C applies — prevents immediate flip-flops |

---

### CONCEPT 6: Probation Band Contraction & Weighted-OR Logic

**Plain Explanation**:
**Probation Gate**: "Is this signal degraded?" You test 3 sub-conditions. Old logic (Phase 8): **ALL 3 must pass** (3-gate AND). Result: 11.13% pass rate vs 40-60% target — **89% rejection rate**.

**Why it failed**: Statistical interiority. Each sub-test at α=0.05 has 2.5% tail on EACH side. Three independent tests → effective pass band shrinks from [0.40, 0.60] to **[0.45, 0.55]**. A perfectly healthy signal fails 36% of the time!

**Weighted-OR Fix**: 
```
pass_score = qual_weight × mapping_conf + (1-qual_weight) × entry_score
PASS if pass_score ∈ [0.35, 0.65] (widened band)
```
One strong signal can compensate for one weak signal — like a diversified portfolio.

**Analogy**:
> Job interview with 3 interviewers. Old rule: "ALL 3 must say YES." Result: Great candidates rejected because one interviewer had a bad day. New rule: "Weighted average of scores > threshold." One strong "YES" can outweigh a weak "MAYBE."

**Why It Benefits Us**:
- Phase 8: Probation pass rate 11.13% → Gate 13 FAIL
- Widened written band [0.35, 0.65] → effective band ≈ [0.40, 0.60] (target)
- Weighted-OR matches qualitative thesis: multiple independent signals

**Pros**:
- ✅ Fixes the statistical contraction mathematically
- ✅ Aligns with "multiple independent signals" philosophy
- ✅ Tunable weights (qual_weight, mapping_conf) for regime adaptation

**Cons**:
- ❌ More complex than simple AND/OR
- ❌ Weight calibration adds parameters (but in Cluster D, low correlation)
- ❌ Requires mapping_confidence from qualitative entity mapping

**Edge Cases**:
| Edge Case | Handling |
|-----------|----------|
| Low mapping confidence (qual data noisy) | mapping_conf downweights qual signal automatically |
| All sub-signals degraded | Weighted-OR still fails — protection maintained |
| Regime-dependent weights | Cluster D optimization includes qual_weight as parameter |

---

### CONCEPT 7: DSR with ML Optimization — Honest Trial Accounting

**Plain Explanation**:
ML searches 500 parameter combinations. Standard practice: "Best one has Sharpe 1.8!" DSR says: "You tried 500. The noise benchmark for 500 trials is Sharpe ~2.8. Your 1.8 is noise. Rejected."

**Our Solution**: `TrialTracker` class logs **every single parameter set evaluated** in the ML loop. DSR uses N = 13 (base clusters) + 500 (LHS) = 513. This is **honest accounting** — the ML search IS the multiple testing.

**Analogy**:
> Scientist runs 500 experiments, publishes only the 1 that worked. DSR = peer reviewer who demands the lab notebook with ALL 500 experiments.

**Why It Benefits Us**:
- Phase 8: DSR=40% because N was undercounted (we didn't track all tuning runs)
- Pre-registration: N=13 base + 500 LHS tracked → DSR threshold honest
- Prevents "ML found alpha" when ML just found luck

**Pros**:
- ✅ Eliminates the #1 ML backtest fraud (hidden multiple testing)
- ✅ Integrates with purged/embargoed CV (trials only on training folds)
- ✅ Audit trail: `TrialTracker` → SHA-256 provenance

**Cons**:
- ❌ 500 LHS inflates N → DSR threshold rises (conservative)
- ❌ Cluster correlation means effective N < 513 (but we use 513 for safety)
- ❌ Adds computational overhead (logging every trial)

**Edge Cases**:
| Edge Case | Handling |
|-----------|----------|
| Early stopping in ML | TrialTracker logs stopped trials too (they consumed search budget) |
| Warm starts / transfer learning | Only count NEW parameter evaluations |
| Ensemble methods | Each ensemble member = 1 trial (conservative) |

---

### CONCEPT 8: Edge Cases Synthesis — Will It Work?

| Edge Case | Risk | Mitigation in Design |
|-----------|------|---------------------|
| **HMM gate fails (ARI < 0.70)** | Pipeline aborts — no parameter search run | Pre-test HMM on synthetic regimes; 3-state frozen; 2-state ablation ready |
| **Fold 1 still 0 trades** | Arm Superiority gate fails | Minimum trade count threshold; regime-conditional entry (0.95×IV in EXP) |
| **Qualitative data sparse (2018-2020)** | Arm B/C weak → Arm Superiority fails | Neutral fill (0.0 z-score) + mapping_confidence weighting; extended window 2020+ |
| **ML overfits training folds** | Nested CV + embargo + DSR penalty | PurgedEmbargoCV class; cluster bootstrap p<0.0071; honest N=513 |
| **Swap barrier v3 still churns** | Gate 14/15 fail | 60-day min lock; moat penalty; correlation-clustered TWAP exits |
| **Pre-registration violated** | Audit failure | SHA-256 frozen configs; TrialTracker immutable log; provenance chain |
| **Regime change 2025-2026 (S8)** | Extended window validation separate | Dual-window design: primary (2018-2024) for gates, extended (2020-2026) for qual coverage |

---

## 5. FEYNMAN TRANSLATION & TECHNICAL EXECUTION MAP

### Decision → Implementation Mapping

| CEO Decision | Technical Implementation | File/Module | Validation |
|--------------|--------------------------|-------------|------------|
| **Atomic pipeline + HMM gate** | `phase9_full_pipeline.py` orchestrator with `hmm.validate()` hard gate | `research/phase9_full_pipeline.py` (NEW) | RuntimeError if ARI<0.70 |
| **Joint 500 LHS search** | `PurgedEmbargoCV` + `GroupLasso` + `TrialTracker` | `research/ml_parameter_optimization.py` | Unit tests + integration test |
| **8 effective gates** | `GateEngine.run_arm_superiority()` + 7 gate DAG | `ingestion/harness/gate_engine.py` | Gate audit on null portfolios (n=10k) |
| **HMM hard occupancy/monotonicity** | `build_factor_hmm(occupancy_cap=0.6, crisis_cap=0.2, monotonicity='hard')` | `Quantitative/regime_shield_v3.py` | `validate()` method returns bool |
| **Swap barrier v3** | `exponential_decay(C=0.025, γ=0.25, t_target=30, moat_penalty)` | `Quantitative/swap_barrier_v3.py` | `test_swap_barrier_v3.py` unit tests |
| **Probation v2 weighted-OR** | `pass_score = w_qual×conf + (1-w_qual)×entry; band=[0.35,0.65]` | `ingestion/quality_thresholds.py` | Calibration on 10k null portfolios |
| **Honest DSR N=513** | `TrialTracker.log(param_set)` called in ML loop | `research/ml_parameter_optimization.py` | SHA-256 provenance on trial ledger |
| **Pre-registration freeze** | All configs in `pre_registration_v4.yaml` → SHA-256 | `config/pre_registration_v4.yaml` | `validation/provenance_audit.py` |

---

### Architectural Change Necessity Assessment

| Change | Architectural? | Why Required |
|--------|----------------|--------------|
| Factor-Model HMM (8→3 PCA) | **YES** — replaces core regime engine | 3-state t-HMM on 2 observables fundamentally unidentifiable (ARI=-0.08) |
| Hard monotonicity ke rejection | **YES** — changes HMM inference | Soft Dirichlet prior failed; ke inversion observed |
| Purged/Embargoed CV class | **YES** — new CV infrastructure | Standard CV leaks; López de Prado mandate |
| TrialTracker for DSR | **YES** — new audit infrastructure | Phase 8 N undercounted → DSR=40% false confidence |
| Gate restructure (16→8) | **YES** — changes validation topology | Gates 1-4 SKIP removes Harvey test; FWER control broken |
| Swap barrier v3 (consistent math) | **YES** — fixes execution engine | Two incompatible formulas in Phase 6 vs 9 spec |
| Probation v2 weighted-OR | **YES** — changes quality filter logic | 3-gate AND statistically contraction → 89% rejection |
| Atomic orchestrator | **YES** — replaces sequential phases | Sequential handoffs caused Phase 8 failure mode |

**Non-Architectural (Parameter Only)**:
- ke_var_threshold 50→85 bps (data-derived floor)
- Entry thresholds (0.85→0.95×IV etc.)
- Band widening [0.40,0.60]→[0.35,0.65]
- Swap C 150→250 bps, γ 0.5→0.25

---

## 6. CHECK-IN

**This document covers all 8 mandated concepts with:**
- ✅ Definition (plain English)
- ✅ Analogy/Example
- ✅ Why it benefits us (Phase 8 failure link)
- ✅ Pros & Cons
- ✅ Edge Cases with mitigations
- ✅ Architectural necessity assessment

**Ready for**: 
- Leadership review
- Team onboarding
- Audit trail documentation
- Pre-registration freeze (`config/pre_registration_v4.yaml`)

---

**Next Step**: Dispatch `research/phase9_full_pipeline.py` build (gemini-worker + bigpickle-worker parallel) with frozen pre-registration.