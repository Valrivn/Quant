# Position B: Joint ML Optimization with Full Harvey Gate Restructure Now

**Thesis**: Sequential fixes guarantee continued failure. The HMM, parameter clusters, and gate architecture are *coupled*—optimizing one without the others produces the same regime-starvation loop that killed Phase 8. We execute Path C end-to-end in a single purged/embargoed ML loop.

---

## 3.1 HMM Redesign: Factor-Model + Monotonic ke, Integrated Not Sequential

- **3 states (EXP/SHOCK/CRISIS)**: 2-state collapses SHOCK/CRISIS distinction needed for `entry_shock` vs `entry_crisis` differentiation. Monotonic Dirichlet prior λ=0.05 on ke ordering (μ_EXP < μ_SHOCK < μ_CRISIS) enforced in `Quantitative/regime_shield_v3.py:build_factor_hmm()`.
- **Rolling PIT VIX quantiles only**: Full-sample = lookahead. Rolling 252d window in `regime_shield_v3.py:label_regimes_pit()`.
- **8 macro observables → 3 PCs**: PCA in `research/macro_pca.py`; feed PCs + VIX-label into t-HMM. ARI target ≥0.70 on ≥2/4 folds.

---

## 3.2 ML Optimization: Joint Search All 4 Clusters, 500 LHS, Full Purge/Embargo

- **Single combinatorial search**: 13 params across Clusters A-D simultaneously via `research/ml_parameter_optimization.py:run_joint_optimization()`. Sequential A→B→C→D ignores ρ>0.6 cross-cluster coupling (swap↔exit↔trailing).
- **CV Protocol** (López de Prado 2018 strict):
  - Purged: holding_period = trailing_tau × 5 days
  - Embargo: 12-month gap
  - Walk-forward: 4 folds expanding (2018-2024)
  - Nested inner CV: TimeSeriesSplit(n_splits=3)
  - Metric: Paired Sharpe vs SPY, cluster bootstrap p<0.0071 (α/8 FWER)
- **File target**: `research/ml_parameter_optimization.py` — new `PurgedEmbargoCV` class wrapping `sklearn.model_selection.TimeSeriesSplit` with purge/embargo logic.

---

## 3.3 Parameter Clusters: Joint Optimization with Grouped Shrinkage

- **Reject sequential**: Cluster correlations (ρ>0.6 B, ρ>0.7 A) mean marginal gains from sequential are illusory. Use grouped Lasso penalty in outer loop: `GroupLasso(groups=[A,B,C,D], α_cluster=[0.0167,0.0125,step-down,0.0167])`.
- **13 params, N=500 LHS**: Sobol sequence in `research/ml_parameter_optimization.py:generate_lhs_samples()`. Track true N for DSR (audit finding: DSR=40% → N was undercounted).

---

## 3.4 Gate Restructure: 8 Effective Gates Now, Not Later

- **Collapse Gates 1-4 → single "Arm Superiority" gate**: ANOVA 3-arm (F-test) + pairwise Welch C vs A, C vs B with FWER α/8 = 0.00625. Implemented in `ingestion/harness/gate_engine.py:GateEngine.run_arm_superiority()`.
- **Effective gate map**:
  1. Arm Superiority (FWER α/8)
  2. OOS Sharpe
  3. Max DD
  4. IR vs SPY
  5. PSR/DSR (Bailey et al., N=13 tracked honestly)
  6. Calmar
  7. HMM ARI ≥0.70
  8. Structural Integrity (WACC coverage + Probation + Swap Churn + TCA + Provenance)
- **Swap barrier v3**: Fix math inconsistency (γ, C, t_target interaction) in `Quantitative/swap_barrier_v3.py:exponential_decay()`. Moat penalty = C × exp(-γ × t_target).
- **Probation v2**: Weighted-OR (qual_weight × mapping_conf + (1-qual_weight) × entry_score) replaces 3-gate AND. Band = [0.35, 0.65] (audit: α/2 contraction killed passes).

---

## Audit Findings Addressed Directly

| Finding | Fix in This Position |
|---------|---------------------|
| DSR=40% (N undercounted) | Honest trial accounting in ML loop; `TrialTracker` class logs every param set |
| Gates 1-4 SKIP | Arm Superiority gate forces genuine 3-arm data; synthetic fallback disabled |
| Arm C≡Arm A | Qual tilt efficacy gate (Arm C ≥ Arm A + 50bps) enforced in Gate 8 |
| Swap barrier math inconsistency | v3 rewrite with verified exponential decay; unit tests in `tests/test_swap_barrier_v3.py` |

---

## Execution Order (Single Pipeline)

```bash
# Single command — no handoffs, no sequential phases
python -m research.phase9_full_pipeline \
  --lhs-samples 500 --clusters A,B,C,D \
  --purged-cv --embargo-months 12 --walkforward-folds 4 \
  --dsr-n 13 --dsr-t 1008 \
  --hmm-factor-model --pca-components 3 --monotonicity-ke \
  --gate-restructure --effective-gates 8 --harvey-compliant \
  --output data/phase9_full/
```

**Module targets created/updated**: `research/ml_parameter_optimization.py`, `Quantitative/regime_shield_v3.py`, `research/macro_pca.py`, `Quantitative/swap_barrier_v3.py`, `ingestion/harness/gate_engine.py`, `research/phase9_full_pipeline.py` (new orchestrator).

---

**Verdict**: Path C executed atomically. Any sequential compromise repeats Phase 8 failure mode.