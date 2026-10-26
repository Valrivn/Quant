# Phase 9 Mathematical Framework & Backtest Audit
**Date:** 2026-09-26 | **Agent:** Quant Research | **Classification:** Structural Flaw Analysis
**Source Documents:** Phase 8 decisions, Data_Strategy.md (§5,6,7,8,12), Tommorow.md (16-gate), Phase 9 Design Spec

---

## EXECUTIVE SUMMARY (3 BULLETS)

- **The strategy has no sustainable edge in growth regimes (S8)** — 4-stage tuned backtest: S7 +63.96%/2.15 Sharpe (BEATS SPY) but S8 -3.89%/-0.09 Sharpe (loses to SPY by 36%). Regime-conditional entry thresholds are a necessary but **insufficient** fix; the qualitative tilt (Stage 2 grassroots) **never activated** due to coverage gaps and Arm C ≡ Arm A degeneracy (singleton qualified set in 71/72 months).

- **Three structural flaws are mathematically irreparable by parameter tuning alone**: (1) WACC nowcaster produces flat `ke` (coverage 37.7%, irreducible floor 77bps > 50bps threshold); (2) HMM regime detection has ARI = -0.08 (states not reproducible, economically inverted); (3) Probation 3-gate AND logic rejects 89% of entries (11.1% pass vs 40-60% target). These are **architectural**, not parametric.

- **Harvey/Bonferroni gate architecture is compromised**: Gates 1-4 (core statistical comparison) SKIPPED due to synthetic arm artifact guard — this **removes the Harvey test entirely**. Gate 13 (Probation) uses 3-gate AND logic contradicting CEO ruling for weighted-OR. Multiple testing across 13+ effective parameters lacks proper FWER control; cluster assignments are heuristic, not empirically validated.

---

## MATHEMATICAL FINDINGS (NUMBERED WITH EQUATIONS/REFERENCES)

### 1. Alpha Test Validity — Strategy Failure, Not Regime Mismatch

**Finding 1.1: Fold 1 had 0 trades — entry gates completely blocked signal**
- Phase 7B walk-forward (Tommorow.md §18.3.1): Fold 1 (test 2022-07 to 2023-06) OOS Sharpe = 1.26 but **win rate 43.8%** with no trade count reported. Phase 8 Phase 3-4 (Data_Strategy.md §19): "Fold 1: 0 trades — entry gates completely blocked signal." The asymmetric entry threshold (fixed 0.90×IV_P25) combined with probation 3-gate AND logic created a **complete entry blockade** in the first test fold.

**Finding 1.2: Folds 2-4 negative returns — strategy failure confirmed**
| Fold | Period | Total Return | Ann Return | Sharpe | Max DD | Trades |
|------|--------|--------------|------------|--------|--------|--------|
| 2 | 2022 | -14.05% | -17.96% | -2.25 | -13.52% | 30 |
| 3 | 2023 | -2.45% | -3.29% | -0.98 | -2.36% | 33 |
| 4 | 2024 | -4.73% | -5.42% | -0.41 | -7.36% | 58 |

**Finding 1.3: Arm C ≡ Arm A degeneracy — qualitative tilt never activated**
- Data_Strategy.md §12.3 (line 801): "Pre-registered screen (ROIC>10%, ICR>3, FCF Yield>5%) → **singleton qualified set in 71/72 months**. Mean qualified size = 1.014, max = 2 → Tier-2 qual tilt trivially equals Arm A."
- The sector-relative dual-track screen (Track 1: top-25% per sector; Track 2: Achievers) was designed to fix this (Data_Strategy.md §7, Decision Log 2026-09-11), but **Phase 8 Phase 3-4 backtest still produced singleton qualified sets** — the screen relaxation to top-30% when <20 names did not trigger because the absolute thresholds (ROIC>10%, ICR>3x, FCF Yield>5%) are mutually contradictory in practice.

**Finding 1.4: Stage 2 Grassroots signal (C_t lead-lag) never fired**
- Data_Strategy.md §12.3 (line 806-808): "GH/NHTSA/CFPB/ATS/Patents: 2023 sample cohorts → tickers not in tradable 13. MDA tone/App Store: 2026 data → **out of 2014-2024 window**. Velocity z=0 on months without observations → no tilt signal."
- Qualitative coverage audit matrix (1,128 cells) shows near-zero overlap between qualitative signal tickers and tradable universe in primary window. The **neutral fill (z=0)** for missing data means Stage 2 adds zero marginal signal.

**Finding 1.5: S8 regime adaptation failure is structural**
- 4-stage tuned: S7 +63.96%/2.15 Sharpe (BEATS SPY) vs S8 -3.89%/-0.09 Sharpe (loses by 36%).
- Phase 9 Design Spec §1: "Fixed entry discount (0.85×IV) missed momentum compounders in growth regimes (S8)" — but the regime-conditional entry thresholds (EXP 0.85×IV, SHOCK 0.95×IV, CRISIS 0.75×IV) require **correct regime identification**, which the HMM fails to provide (ARI = -0.08).

### 2. Harvey/Bonferroni Gate Architecture — Compromised Validity

**Finding 2.1: Gates 1-4 (Harvey core) SKIPPED — statistical comparison removed**
- Tommorow.md §2 (16-Gate Audit): Gates 1-4 all `SKIP` with `insufficient_arm_data` reason.
- Data_Strategy.md §12.5B.2 (line 939-940): Synthetic artifact in `three_arm_runner.py:1071-1078` falls back to `np.random.normal(base_mean, 0.035, n)` — **all three arms share identical σ (0.0337/0.0336/0.0354)**.
- Gate engine hardened with `_verify_arm_source()` producer-marker + statistical-fingerprint checks. Gates 1-4 now **return SKIP with reason per arm** — but this **removes the Harvey test entirely**. The core hypothesis "Arm C ≠ Arm A" is untested.

**Finding 2.2: Holm-Bonferroni α=0.003125 correctly specified but misapplied**
- α_family = 0.05, n_tests = 16 → α_per_test = 0.05/16 = 0.003125 (correct).
- Parameter Optimization Plan §1.1: "Global FWER: 13 effective tests → Bonferroni α = 0.0038 (aligns with existing α=0.003125)."
- **Problem**: The 16 gates are not independent. Gates 5,6,7,9,10 are performance gates on the **same return stream** (perfectly correlated). Gates 11,14,15 are structural gates on **same HMM/barrier/TCA systems**. Effective independent tests ≈ 5-7, not 16.

**Finding 2.3: Gate 13 Probation — 3-gate AND logic violates CEO ruling**
- CEO Ruling (Data_Strategy.md Decision Log 2026-09-20): "Probation v2 (weighted OR logic + capital ramps)."
- Phase 9 Design Spec §1.3: CEO Decision 2 mandates weighted-OR: `S_i = 0.40×I(ROIC≥WACC+2%) + 0.35×I(Qual≥0.50) + 0.25×I(Risk≤0.40)`, pass if `S_i ≥ 0.60`.
- **Current implementation (Phase 7B)**: `probation_gates.py` still uses 3-gate AND — 60/539 triggers convert (11.1% vs 40-60% target). **Weighted-OR fix NOT implemented** in validation gates.

**Finding 2.4: Multiple testing across 13+ parameters lacks FWER control**
- Parameter Optimization Plan §1.1 lists 13 core parameters with 4 correlation clusters.
- Cluster A (WACC/Regime): 3 params, ρ>0.7 → Bonferroni α=0.0167
- Cluster B (Swap/Exit): 4 params, ρ>0.6 → Bonferroni α=0.0125
- Cluster C (Entry): 3 params, ρ~0.4 → Holm-Bonferroni step-down
- Cluster D (Probation): 3 params, ρ<0.3 → Bonferroni α=0.0167
- **Critique**: Correlation estimates (ρ) are **assumed, not empirically measured** from Phase 8 data. No evidence provided that these clusters reflect true parameter dependence. The Phase 9 Plan §8.1 tasks Data Scientist to "Validate cluster correlation estimates (ρ) from Phase 8 data" — this has not been done.

### 3. HMM Structural Issues — Regime Model Fundamentally Broken

**Finding 3.1: 3-state t-HMM has ARI = -0.08 (negative stability)**
- Tommorow.md §18.3.3: "min ARI = -0.080 across folds; state sequences not reproducible."
- CEO Ruling §19.2: "Regime Detection Architecture is Insufficient: 3-state HMM + dwell filter stabilizes transitions but states are not reproducible (ARI = -0.08)."
- Phase 9 Design Spec §1.1: Factor-Model HMM redesign mandated — "ARI > 0.70 target."

**Finding 3.2: State occupancy degenerate — 94.1% SHOCK**
- Sprint Continuation (Data_Strategy.md §12.5B.2 line 947): "Regime Label Degeneracy: 94.1% SHOCK; CRISIS ke (5.87%) < SHOCK ke (6.74%) — labels not economically ordered."
- Phase 9 Design Spec §1.1: "3-state raw HMM collapsed into a single dominant state (94.1% SHOCK, ARI = 0.123 vs >0.70 target, economically inverted ke)."

**Finding 3.3: Factor-model HMM redesign — specification incomplete**
- Phase 9 Design Spec §1.1 defines 8 observables: `[Term Spread, Credit Spread, VIX, SPY_1m_vol, CESI, DXY, Oil_1m_vol, Steepness]`
- PCA retaining m components explaining ≥80% variance → factor scores `f_t = V_m^T Z_t`
- Sticky Student-t HMM with κ=50, ν=6
- **Missing**: What is the **economic identification strategy**? PCA components are rotationally invariant — how are states mapped to EXP/SHOCK/CRISIS?
- Phase 9 Spec §1.1.C: "Economic Monotonicity & Ordering Constraint: μ_VIX,1 < μ_VIX,2 < μ_VIX,3 AND μ_ke,1 < μ_ke,2 < μ_ke,3. If unconstrained HMM yields non-monotonic means, states are re-indexed post-fit by ascending order of mean VIX factor score."
- **Critique**: Post-hoc re-indexing by VIX mean **guarantees** monotonicity but destroys the HMM's probabilistic interpretation. The states become **sorted bins**, not discovered regimes. ARI measures label agreement — if you re-index to force ordering, ARI becomes a tautology.

**Finding 3.4: Ω_stress multiplier — arbitrary calibration**
- Phase 8 Decision Log 2026-09-20: "Ω_stress multiplier" added to WACC engine.
- Parameter Optimization Plan §1.1: `hmm_n_states` ↔ `omega_stress_multiplier` in Cluster A (ρ>0.7).
- **No mathematical specification found** for how Ω_stress scales WACC components. Is it a multiplier on `ke`? On credit spread? On the entire WACC? Without specification, this is an uncalibrated free parameter.

**Finding 3.5: Transition matrix stickiness — κ=50 vs κ=100 discrepancy**
- Phase 6 config (Data_Strategy.md §12.7.4): `sticky_kappa: 50`
- Phase 7 config (Data_Strategy.md §17.3): `sticky_kappa: 100`
- Phase 9 Design Spec §1.1: `sticky_prior_alpha: 50`
- **Inconsistency**: Which value is correct? The dwell filter (Phase 7) uses Mahalanobis-scaled dwell days: `τ_dwell = clamp(10 × (1 + 2.0/(D_M + ε)), 3, 15)`. This is a **post-hoc filter**, not part of the HMM transition matrix. The HMM itself needs sticky transitions to prevent whipsaw.

### 4. Swap Barrier & Exit Mathematics — Churn & Hold Period Mismatch

**Finding 4.1: Exponential barrier parameters imply mean hold ~25 days, not 24-36 months**
- Phase 6 config (Data_Strategy.md §12.7.2): `C=150bps, γ=0.5, t_target=24 months`
- Barrier formula: `barrier = C * exp(γ * t_target / (t_held + ε))`
- At t_held = 1 month: barrier = 150 * exp(0.5 * 24 / 1) = 150 * exp(12) ≈ **24.5 million bps** (impossible to clear)
- At t_held = 12 months: barrier = 150 * exp(0.5 * 24 / 12) = 150 * exp(1) ≈ **408 bps**
- At t_held = 24 months: barrier = 150 * exp(0.5) ≈ **247 bps**
- **Behavior table** (Data_Strategy.md §12.7.2) confirms: Month 1 "huge", Month 3 "54.6×", Month 6 "7.39×", Month 12 "2.72×".
- **Result**: Positions are **frozen for ~12 months** (barrier > 400 bps), then suddenly become swappable. This creates a **cliff effect**, not smooth decay.
- Phase 7B results: Swaps/yr = 3.25 (PASS) but Fold 3: 14.3 swaps/yr, turnover 80% — **barrier fails to control churn in regime transitions**.

**Finding 4.2: Phase 9 Design Spec changes parameters but not structure**
- Phase 9 Spec §1.5: `C=250bps` (up from 150), `γ=0.30` (down from 0.50), `t_target=36 months` (up from 24)
- New barrier: `B_i(t) = 250 * exp(-0.30 * t / 36) + 200 * I(Moat Breached)`
- **Wait**: Formula changed from `exp(γ * t_target / t_held)` to `exp(-γ * t / t_target)` — **sign flipped, variables swapped**.
- Phase 6: `exp(γ * t_target / (t_held + ε))` — barrier **decreases** as t_held increases (denominator grows)
- Phase 9: `exp(-γ * t / t_target)` — barrier **decreases** as t increases (numerator grows negative)
- **These are mathematically different functions**. Phase 9 version: at t=0, barrier=250+penalty; at t=36, barrier=250*exp(-0.3)=185+penalty. **Almost flat** — no meaningful time decay.
- **Critical error**: The Phase 9 barrier equation is **not a recalibration** — it's a **different function** with no justification.

**Finding 4.3: Staggered TWAP exits — correlation clustering unvalidated**
- Phase 6 config: `max_daily_portfolio_pct: 0.05, twap_days: 3, correlation_clustering: true, cluster_threshold: 0.7`
- Phase 9 Spec §1.5: "Correlation Clustering Execution: Assets with pairwise return correlation ρ_ij > 0.75 undergoing simultaneous exit are queued with staggered start days (t_j = t_i + 1 day)."
- **No validation** that this prevents market impact clustering. The TCA simulator (Phase 6) reported 74.1 bps base cost, but **concurrent stress test not run** (Phase 9 Spec §5 R-05: "TWAP Market Impact in Low ADV... Expand TWAP window to 8 days for stocks with ADV <$5M").

**Finding 4.4: TCA budget 150 bps — LOB simulator at 74.1 bps base, but concurrent stress test missing**
- Phase 6 validation: "TCA Budget: 74.1 bps round-trip (vs 150 bps cap) — PASS"
- Phase 7B honest recomputation: "TCA cost: 151.6 bps (FAIL)" — **2× discrepancy** between reported and honest.
- Data_Strategy.md §18.3.3: "Flat TCA Artifacts: Walk-forward summary reported 75.8 bps; honest recomputation = 151.6 bps."
- Phase 9 Plan §7.1: "Concurrent exit stress test (Gate 15)" — **not yet implemented**.
- Almgren-Chriss model assumes independent executions; **correlated concurrent exits** (sector rotation, regime shift) will multiply impact.

### 5. Parameter vs Architecture Decision — Evidence Points to Structural Failure

**Finding 5.1: Gate failures split cleanly into parameter vs structural**

| Gate | Type | Phase 7B Result | Root Cause |
|------|------|-----------------|------------|
| 5: OOS Sharpe >1.0 | Performance | PASS (1.15 aggregate) | **But Fold 3=0.28, Fold 4=0.03** — not robust |
| 6: Max DD <-20% | Performance | PASS (-16.52%) | |
| 7: IR >0.30 | Performance | Not explicitly tested | |
| 9: PSR >0.95 | Performance | Not tested | |
| 10: Calmar >2.0 | Performance | Not tested | |
| **11: HMM ARI >0.70** | **Structural** | **FAIL (ARI=-0.08)** | **HMM broken** |
| **13: Probation 40-60%** | **Structural** | **FAIL (11.1%)** | **3-gate AND logic** |
| **14: Swaps <5/yr** | **Structural** | **FAIL (Fold 3: 14.3/yr)** | **Barrier cliff effect** |
| **15: TCA ≤150 bps** | **Structural** | **FAIL (151.6 bps)** | **Concurrent impact unmodeled** |

**Finding 5.2: Performance gates failing due to structural gates failing**
- Low Sharpe (Folds 3-4) → HMM misidentifies regime → wrong entry threshold → bad trades
- High churn (Fold 3) → Barrier cliff effect → wholesale portfolio turnover in regime transition
- High TCA → Concurrent exits during regime shifts → impact model breakdown
- **Causal chain**: HMM failure → wrong regime → wrong entry/exit → churn → TCA breach → negative alpha

**Finding 5.3: Parameter thresholds are not the primary problem**
- `ke_var_threshold` 50bps vs 77bps floor: **Real but secondary** — even at 80bps, WACC engine produces flat `ke` (coverage 37.7%)
- Probation band [0.40,0.60] vs effective [0.45,0.55]: **Real but secondary** — even with weighted-OR, if qualitative signals have zero coverage, pass rate stays near 0%
- Entry thresholds: **Dependent on HMM** — regime-conditional thresholds useless if regime identification is random (ARI=-0.08)

**Finding 5.4: Strategy has no edge in S8 (growth regime)**
- Core DCF baseline: S8 -1.16%/-0.11 Sharpe (Data_Strategy.md §12.5)
- 4-stage tuned: S8 -3.89%/-0.09 Sharpe (worse than baseline!)
- Hybrid Arm C: S8 -7.14%/-0.24 Sharpe
- **The strategy architecture is fundamentally misaligned with growth-led regimes**. No parameter tuning fixes missing momentum/quality factors in the screen.

---

## STRUCTURAL DIAGNOSIS

**The main problem is the HMM regime detection is mathematically broken (ARI = -0.08, states not reproducible, economically inverted), which cascades into every downstream component: wrong entry thresholds, wrong exit targets, wrong position sizing, wrong swap barriers, wrong TCA regime multipliers, and wrong probation gating. This is compounded by a WACC nowcaster that produces flat cost-of-equity (37.7% coverage, 77bps irreducible floor), and a probation engine with 3-gate AND logic that rejects 89% of entries. Parameter recalibration cannot fix these — they require architectural redesign of the regime model, discount rate engine, and entry/exit logic.**

---

## SPECIFIC RECOMMENDATIONS

### A. HMM Redesign Specification (Priority 1 — Blocks Everything)

**Replace PCA-based factor HMM with economically identified regime model:**

1. **Observable set**: Use **structural factors with economic identification**, not PCA:
   - `y1` = Term spread (10Y-2Y) — **growth/inflation expectations**
   - `y2` = Credit spread (BAA-AAA) — **credit stress**
   - `y3` = VIX level — **equity fear**
   - `y4` = Realized vol (SPY 1m) — **vol regime**
   - `y5` = DXY — **dollar/liquidity**
   - `y6` = Quality spread (HML or ROIC factor) — **quality regime**

2. **Identification**: **Sign restrictions** on impact matrix (not post-hoc re-indexing):
   - EXPANSION: term spread ↑, credit spread ↓, VIX ↓, quality spread ↑
   - SHOCK: term spread ↓, credit spread ↑, VIX ↑, quality spread ↓
   - CRISIS: term spread ↓↓, credit spread ↑↑, VIX ↑↑, quality spread ↓↓

3. **Model**: Bayesian VAR with Minnesota prior + regime-switching intercepts (not HMM on PCA scores)
   - `y_t = μ(S_t) + Φ y_{t-1} + ε_t, ε_t ~ N(0, Σ(S_t))`
   - Sticky transition prior on `μ(S_t)` with κ=100
   - Identify regimes by **sign pattern of μ_k**, not by sorting VIX means

4. **Validation**: ARI > 0.70 on **out-of-sample state sequences** (not in-sample fit). Require ≥3/4 folds.

### B. WACC Nowcaster v3 (Priority 2 — Enables Valuation)

1. **Cost of Equity**: Option-implied ERP from ATM IV term structure:
   ```
   ERP_opt,t = IV_30d,ATM,t / √12 * (1 + 0.5 * (IV_60d/IV_30d - 1)) - rf_t
   ERP_t = 0.3 * ERP_opt,t + 0.7 * ERP_Damodaran  [bounds: 5%-8%]
   ```

2. **Beta**: James-Stein shrinkage to sector mean:
   ```
   β_JS = w_i * β_OLS + (1-w_i) * β_sector
   w_i = 1 - (N_g - 3) * SE(β_i)^2 / Σ(β_j - β_sector)^2
   ```

3. **Dynamic dispersion threshold**: Regime-conditional (Phase 9 Spec §1.2.C):
   - EXPANSION: 80 bps
   - SHOCK: 100 bps  
   - CRISIS: 120 bps

4. **Coverage target**: 100% PIT — requires daily option chain data (CBOE free) + sector beta panel.

### C. Probation Engine v2 (Priority 3 — Enables Entry)

1. **Weighted-OR scoring** (Phase 9 Spec §1.3, CEO Decision 2):
   ```
   S_i = 0.40 * I(ROIC_cap_rd ≥ WACC + 2%) + 0.35 * I(Qual_Composite ≥ 0.50) + 0.25 * I(Risk_Score ≤ 0.40)
   Pass if S_i ≥ 0.60
   ```

2. **Capital ramps**: Tier 1 (1 pass) = 33%, Tier 2 (2 passes) = 66%, Tier 3 (3+ passes) = 100%

3. **Written band**: [0.35, 0.65] → effective [0.40, 0.60] under α=0.05 interiority

4. **Qualitative signal coverage**: Must fix mapping/coverage before this works. Extended window (2020-2026) has full qual coverage — **use it for probation calibration**.

### D. Swap Barrier v3 (Priority 4 — Controls Churn)

1. **Fix the formula**: Use consistent time-decay specification:
   ```
   B(t_held) = C * exp(-γ * t_held / t_target) + Penalty_moat
   ```
   - C = 150 bps (transaction cost floor)
   - γ = 0.5 (decay rate, calibrated to achieve <5 swaps/yr)
   - t_target = 24 months (target hold)
   - t_held = months since entry
   - Penalty_moat = 200 bps if ROIC>WACC+2% AND OMR>0.5

2. **Add minimum hold constraint**: Hard lock for first 6 months (prevents cliff effect)

3. **Churn budget**: Explicit annual swap budget (5/yr) enforced at portfolio level, not per-position

### E. Harvey-Compliant Gate Restructuring (Priority 5 — Statistical Validity)

1. **Re-run genuine 3-arm backtests** (Arm A, B, C on real PIT data) — **unblocks Gates 1-4**

2. **Reduce gate count to effective independent tests**:
   - Gate 1-4: 3-arm statistical comparison (1 effective test family, Holm α=0.05/4=0.0125)
   - Gate 5,6,7,9,10: Performance on same return stream (1 effective test, α=0.05)
   - Gate 11: HMM stability (1 test, α=0.05)
   - Gate 12: WACC coverage (1 test, α=0.05)
   - Gate 13: Probation pass rate (1 test, α=0.05)
   - Gate 14: Swap churn (1 test, α=0.05)
   - Gate 15: TCA budget (1 test, α=0.05)
   - Gate 16: Provenance (deterministic, no α)
   - **Total: 8 effective independent tests → Holm-Bonferroni step-down on 8 tests**

3. **Pre-register gate logic fixes**:
   - Gate 5 (Swap churn): `passed = (swaps_per_year < 5)` not `>`
   - Gate 12 (Swaps/yr): same
   - Gate 2 (Crisis precision): `exclude_folds_with_zero_crisis_months: true`

4. **DSR calculation**: Use Bailey et al. (2014) with N=8 effective tests, T=1750 trading days, skew/kurt from empirical return distribution.

---

## DECISION FRAMEWORK FOR CEO

### Option Matrix

| Decision Path | Scope | Timeline | Probability of Success | Risk |
|---------------|-------|----------|------------------------|------|
| **A. Parameter Recalibration Only** | Adjust thresholds (ke_var, probation band, entry discounts, barrier γ/C) | 1-2 weeks | **<10%** — Structural flaws (HMM ARI=-0.08, flat ke, 3-gate AND) are parametric | Wastes compute; produces false confidence |
| **B. HMM Redesign Only** | Factor-model HMM with sign restrictions, sticky transitions, ARI>0.70 target | 3-4 weeks | **~40%** — Fixes regime identification but WACC flat, probation 89% reject, S8 edge missing remain | Partial fix; performance gates still fail |
| **C. Full Architectural Redesign (Recommended)** | HMM v2 + WACC v3 + Probation v2 + Barrier v3 + TCA v3 + Gate restructuring + 3-arm re-run | 6-8 weeks | **~70%** — Addresses all three structural flaws simultaneously; aligns with Phase 9 Design Spec | Requires parallel dispatch; highest resource cost |
| **D. Strategy Pivot** | Abandon 4-stage hybrid; adopt pure quant (Arm A) + momentum overlay + regime hedge | 4-6 weeks | **~50%** — Simpler; Arm A sector-relative screen has edge in S7; S8 needs separate sleeve | Loses qualitative thesis; Arm C ≡ Arm A persists |

### Recommended Path: **C (Full Architectural Redesign) with Phase 9 Design Spec as Blueprint**

**Rationale:**
1. Phase 9 Design Spec already specifies the mathematical fixes for all three structural flaws
2. CEO Decisions 1 & 2 (ke_var_threshold 85bps, probation band [0.35,0.65]) are pre-registered
3. 3-arm genuine re-run (Decision 3) unblocks Gates 1-4 and validates Arm C ≠ Arm A
4. Extended window (2020-2026) provides full qualitative coverage for probation/qual tilt calibration
5. Harvey-compliant gate restructuring reduces effective tests from 16→8, increasing power

**Pre-conditions before dispatch:**
1. ✅ CEO rules on Decisions 1, 2, 3 (ke_var, probation band, 3-arm re-run)
2. ⬜ Factor-model HMM sign restrictions specified (not PCA + post-hoc sort)
3. ⬜ Option-implied ERP data pipeline confirmed (CBOE free tier sufficient?)
4. ⬜ Qualitative mapping coverage audit on extended window (2020-2026)
5. ⬜ TCA concurrent stress test designed (LOB simulator with correlated exits)

**Go/No-Go Gate for Phase 9 Execution:**
- **Week 2 checkpoint**: HMM v2 achieves ARI > 0.70 on Fold 1-2 (in-sample) → proceed
- **Week 4 checkpoint**: WACC v3 ke variation > 200bps across regimes + coverage > 90% → proceed
- **Week 6 checkpoint**: Probation v2 pass rate 40-60% on extended window → proceed
- **Week 8 checkpoint**: Full 16-gate (restructured to 8 effective) Holm-Bonferroni PASS on F4 (2024 test fold) → **PRODUCTION**

---

## APPENDIX: KEY EQUATIONS REFERENCE

### HMM ARI (Adjusted Rand Index)
```
ARI = (RI - E[RI]) / (max(RI) - E[RI])
RI = (a + d) / (a + b + c + d)
where a = pairs in same state both times, d = pairs in different states both times
```
Phase 7B: ARI = -0.08 → **worse than random labeling**

### Deflated Sharpe Ratio (Bailey et al. 2014)
```
EMSR = √(2 ln N) * [1 + (skew/6)√(2 ln N) + (kurt-3)/24 * (2 ln N) - (skew²/36)(2 ln N)]
Var(SR) = (1 + 0.5 SR²)/T * [1 + skew*SR/3 + (kurt-3)SR²/24]
PSR = Φ((SR_obs - EMSR)/√Var(SR))
```
Parameter Optimization Plan §6: N=13, T=1750, target PSR > 0.95

### Exponential Barrier (Corrected)
```
B(t_held) = C * exp(-γ * t_held / t_target) + Penalty_moat * I(moat_intact)
```
Phase 6: C=150, γ=0.5, t_target=24 → Month 12: 408 bps, Month 24: 247 bps
Phase 9: C=250, γ=0.30, t_target=36 → Month 12: 225 bps, Month 24: 202 bps, Month 36: 185 bps

### Weighted-OR Probation Score
```
S_i = 0.40 * I(ROIC_cap_rd ≥ WACC_i + 0.02) + 0.35 * I(Qual_i ≥ 0.50) + 0.25 * I(Risk_i ≤ 0.40)
Pass if S_i ≥ 0.60
Capital ramp: Tier 1 (33%), Tier 2 (66%), Tier 3 (100%)
```

### James-Stein Beta Shrinkage
```
β_JS = w_i * β_OLS + (1-w_i) * β_sector
w_i = max(0, 1 - (N_g - 3) * SE(β_i)² / Σ(β_j - β_sector)²)
```

---

**Audit Complete.** This document should be read in conjunction with Phase 9 Design Spec (`phase9_design_spec_20260925.md`) and Parameter Optimization Plan (`parameter_optimization_plan_20260926.md`) for implementation details.