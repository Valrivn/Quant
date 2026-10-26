# Tier-3 Debate Brief: Phase 9 Architectural Redesign
**Date:** 2026-09-26 | **Status:** READY FOR DEBATE PROTOCOL | **Classification:** EXECUTIVE OVERHAUL CANDIDATE

---

## 1. Problem Statement (The "Why")

The Phase 8 backtest **did not test alpha**. Root cause chain (both auditors agree):

```
Broken HMM (ARI=-0.08, 94% SHOCK occupancy)
        ↓
Wrong regime signal → CRISIS/SHOCK lock
        ↓
Entry thresholds (0.85×IV, 0.95×IV) block ALL entries
        ↓
Fold 1: 0 trades | Folds 2-4: 30-58 trades (starvation)
        ↓
Gates 1-4 SKIP (no Arm B/C data) + Gates 5,7,9,10,11,14,15 FAIL
        ↓
Synthetic fallback triggers → statistical test invalidated
        ↓
Result: NEGATIVE OOS alpha (-14.05%, -2.45%, -4.73%)
```

**Three interlocking failures:**
1. **HMM structural failure**: 3-state t-HMM unidentifiable on 2 observables; economically inverted ke
2. **Parameter strictness**: 50bps ke_var (impossible floor 77bps), probation band contracted by α/2, entry gates block expansion regime
3. **Gate architecture compromise**: Gates 1-4 (Harvey core test) SKIPPED; 16 gates treated as independent but effective tests ≈ 5-7

---

## 2. Decision Space (The "What")

| Path | Scope | Fixes | Execution Risk |
|------|-------|-------|----------------|
| **A: Parameter Recalibration Only** | Gates 12, 13, 14, 5 thresholds | ke_var→85bps, probation→[0.35,0.65], entry_exp→0.95×IV, swap_C→250bps | HMM still broken → regime wrong → alpha fails |
| **B: HMM Redesign Only** | Factor-Model HMM (PCA→3-state, monotonic ke) | Gate 11, regime signal | Entry gates still starve; swap barrier math inconsistent; probation AND logic persists |
| **C: Full Architectural Redesign** | **All of above + Harvey gate restructuring (16→8 effective) + Barrier v3 + Probation v2 weighted-OR + WACC v3 option-implied ERP** | **All 16 gates** | 2-3 weeks; requires debate protocol; highest confidence |
| **D: Executive Overhaul** | Reframe at archetype level (new strategy architecture) | Systemic | 4+ weeks; nuclear option |

---

## 3. Specific Technical Questions for Debate

### 3.1 HMM Parameter Relationships & Redesign
- **Current**: 3-state Student-t HMM (ν=6, κ=50) on 2 raw observables → ARI=0.12
- **Proposed**: Factor-Model HMM on 3 PCs from 8 macro observables (BAA10Y, 10Y2Y, CP-TBill, VIX/VVIX, RealizedVol21d, HY_OAS, CESI, SkewIndex)
- **Questions**:
  - Should regime states be EXP/SHOCK/CRISIS (3) or EXP/CRISIS (2)?
  - Monotonicity constraint: enforce μ(ke|EXP) < μ(ke|SHOCK) < μ(ke|CRISIS) via Dirichlet prior?
  - VIX label locking: rolling PIT quantiles vs full-sample (lookahead risk)?
  - Transition smoothing: κ=50 (current) → λ=0.05 Dirichlet?

### 3.2 Buy Signal Parameter Optimization (ML with Purged/Embargoed CV)
**Parameters to optimize jointly:**
| Parameter | Current | Range | Constraint |
|-----------|---------|-------|------------|
| `entry_exp` | 0.85×IV | [0.80, 0.85, 0.90, 0.95]×IV | Regime-conditional |
| `entry_shock` | 0.95×IV | [0.85, 0.90, 0.95, 1.00]×IV | Regime-conditional |
| `entry_crisis` | 0.75×IV | [0.70, 0.75, 0.80]×IV | Black Swan only |
| `trailing_k` | sector-static | per-sector nested CV | Harvey cluster B |
| `trailing_tau` | fixed | [5, 10, 20] days | DSR T-adjustment |

**CV Protocol (López de Prado 2018 compliant):**
- **Purged**: Remove training labels whose event horizons overlap test set (holding period = trailing_tau × 5)
- **Embargo**: 12-month gap post-test before next training window
- **Walk-forward**: 4 folds (2018-2024), expanding window
- **Inner CV**: Nested time-series within each training fold
- **Selection metric**: Paired Sharpe vs SPY (cluster bootstrap p < 0.0071)

### 3.3 Parameter Correlation Clusters (Harvey/Bonferroni)
| Cluster | Parameters | Correlation | Correction | Effective α |
|---------|------------|-------------|------------|-------------|
| **A: WACC/Regime** | ke_var, hmm_n_states, omega_stress | ρ > 0.7 | Bonferroni | 0.0167 |
| **B: Swap/Exit** | swap_gamma, swap_C, trailing_k, trailing_tau | ρ > 0.6 | Bonferroni | 0.0125 |
| **C: Entry Thresholds** | entry_exp, entry_shock, entry_crisis | ρ ~ 0.4 | Holm-Bonferroni | step-down |
| **D: Probation/Qual** | probation_band, qual_weight, mapping_conf | ρ < 0.3 | Bonferroni | 0.0167 |

**Question**: Should optimization search all 4 clusters jointly (13 params, N≈500 LHS) or sequentially (A→B→C→D)?

### 3.4 Gate Restructuring (Harvey Compliant)
- **Current**: 16 gates, Holm-Bonferroni α=0.003125, Gates 1-4 SKIP
- **Proposed**: 8 effective gates with proper FWER control
  - Statistical: ANOVA 3-arm (Gate 2), Welch C vs A (3), Welch C vs B (4)
  - Performance: OOS Sharpe (5), Max DD (6), IR vs SPY (7), PSR/DSR (9), Calmar (10)
  - Structural: HMM ARI (11), WACC Coverage (12), Probation (13), Swap Churn (14), TCA (15), Provenance (16)
- **Question**: Collapse Gates 1+2+3+4 into single "Arm Superiority" gate with FWER α/8?

---

## 4. ML Optimization Framework Requirements (Non-Negotiable)

Per López de Prado (2018), any ML parameter optimization **MUST** include:
1. **Purged K-Fold**: Training labels with horizons overlapping test fold removed
2. **Embargo Gap**: Minimum 12 months between test end and next train start
3. **No Random Shuffle**: Chronological splits only (TimeSeriesSplit)
4. **Nested CV**: Inner loop for parameter selection, outer for performance estimation
5. **Trial Accounting**: Track true N (every parameter set evaluated) for DSR
6. **Feature Horizon Alignment**: Rolling features must not bleed across purge/embargo boundaries
7. **Transaction Cost Integration**: TCA simulator in the loop (not post-hoc)

**Edge Cases to Handle:**
- Short test folds (Fold 1: 0 trades) → minimum trade count threshold for gate evaluation
- Regime-dependent parameters → separate CV per regime or joint with regime as feature?
- Cluster-correlated parameters → grouped shrinkage or joint optimization?
- Synthetic fallback guard → MUST remain active; optimize on REAL Arm B/C data only

---

## 5. Success Criteria (Pre-Registered)

**Phase 9 PASSES iff on F4 (2024 test fold):**
1. ✅ DSR Probabilistic Sharpe > 0.95 (Bailey et al., N=13, T=4yr)
2. ✅ OOS Sharpe (Arm C) > 1.0
3. ✅ Max DD < -20%
4. ✅ Information Ratio vs SPY > 0.30
5. ✅ HMM ARI ≥ 0.70 on ≥2/4 folds
6. ✅ Swap barrier churn < 5/yr
7. ✅ TCA ≤ 150 bps
8. ✅ Probation pass rate ∈ [35%, 65%]
9. ✅ WACC coverage = 100%
10. ✅ Provenance chain integrity = 100%
11. ✅ Arm C adds ≥ 50 bps alpha vs Arm A (qual tilt efficacy)
12. ✅ Gates 1-4 NOT SKIP (genuine 3-arm data)

---

## 6. Debate Protocol Execution Order

```
1. THIS BRIEF → .agents/project/org/decisions/_drafts/phase9_debate_brief_20260926.md
2. POSITION A (bigpickle-worker) — Conservative: "Fix HMM first, then parameters, gate restructuring last"
3. POSITION B (gemini-planner) — Aggressive: "Joint ML optimization with purged CV, full gate restructure now"
4. DISAGREEMENT MAP (bigpickle-worker) — Names substantive splits
5. SYNTHESIS (hermes-bridge / Central Commander) — ≤300 tokens, one recommendation
6. CEO RULING — APPROVE / REJECT / MODIFY / EXECUTIVE OVERHAUL
7. BLUEPRINT UPDATE (bigpickle-worker) — Ruling block + invariant changes
8. CONDUCTOR GATE — ≥90% pytest before merge
```

---

## 7. Educator Role Requirement (Post-Debate)

**After CEO ruling, the `educator` agent MUST produce a Feynman-method explainer covering:**

| Concept | What to Explain | Level |
|---------|-----------------|-------|
| **Purged & Embargoed CV** | Why standard K-fold leaks in finance; how purge/embargo fixes it | L2-L3 |
| **Deflated Sharpe Ratio** | How DSR adjusts for trial count N, track record T, skew/kurt | L3 |
| **Harvey/Bonferroni/Holm** | Multiple testing correction; why 16 gates ≠ 16 independent tests | L2-L3 |
| **HMM Identifiability** | Why 3-state t-HMM on 2 observables fails; factor-model PCA solution | L3-L4 |
| **Swap Barrier Math** | Exponential decay mechanics; γ, C, t_target interaction; moat penalty | L2 |
| **Probation Band Contraction** | How α/2 tails shrink effective band; weighted-OR vs 3-gate AND | L2 |
| **DSR with ML Optimization** | How to honestly count N when ML searches parameter space | L3 |
| **Edge Cases** | Short folds, regime-dependent params, synthetic guard, cluster correlation | L3-L4 |

**Format**: 6-section Feynman template (Original Context → Key Terms → Prerequisites → Feynman Concepts → Translation → Check-In)

---

## 8. Dispatch Triggers (Post-Ruling)

```bash
# If Path C (Full Redesign) approved:
# Phase 9A: ML Grid Search with Purged/Embargoed CV
python -m research.ml_parameter_optimization --lhs-samples 500 --clusters A,B,C,D \
  --purged-cv --embargo-months 12 --walkforward-folds 4 \
  --dsr-n 13 --dsr-t 1008 --output data/parameter_search/phase9a

# Phase 9B: HMM Factor-Model Build
python -m Quantitative.regime_shield_v3 --factor-model --pca-components 3 \
  --macro-indicators 8 --monotonicity-ke --dirichlet-prior 0.05 \
  --rolling-vix-quantiles --output data/regime/hmm_v3

# Phase 9C: Gate Restructure
python -m ingestion.harness.gate_engine --restructure --effective-gates 8 \
  --harvey-compliant --fwер-control --output config/gates_v3.yaml

# Phase 9D: Genuine 3-Arm + Full Audit
python -m ingestion.harness.three_arm_runner --start 2018-01-01 --end 2024-12-31 \
  --window primary --arms A,B,C --persist --no-synthetic-fallback
python -m ingestion.harness.gate_engine --run --data-root data/backtests --all-gates
python -m validation.provenance_audit --run --data-root data/
```

---

**Brief Status**: READY FOR POSITION A (bigpickle-worker) AND POSITION B (gemini-planner) INDEPENDENT WRITING

**Next Action**: Central Commander dispatches both positions simultaneously per debate-protocol skill.