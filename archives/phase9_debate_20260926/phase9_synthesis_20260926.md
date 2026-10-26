# Synthesis — Phase 9 Architectural Redesign Debate
**Inputs**: Position A (bigpickle), Position B (gemini-planner), Disagreement Map (bigpickle)
**Constraint**: ≤300 tokens, reads ONLY the three artifacts

---

## Recommendation: **MODIFY Path C — Atomic Pipeline WITH HMM Gate First**

The disagreement map identifies **one load-bearing split (Split 1: Sequencing)** that cascades into Splits 2, 5, 8. Position A is correct that the HMM is an *upstream correctness gate* — 94% SHOCK occupancy with ARI=-0.08 means any downstream optimization learns noise. Position B is correct that sequential handoffs caused Phase 8 failure — the regime engine, parameters, and gates are coupled in execution.

**Resolution**: Single atomic pipeline **but** the HMM module has a **hard internal gate** (ARI ≥0.70 on ≥3/4 folds, occupancy invariants) that must pass before the parameter optimizer proceeds. This is not a handoff; it is a *branching condition inside the same orchestrator*.

---

## Binding Decisions

| Split | Decision | Rationale |
|-------|----------|-----------|
| **1. Sequencing** | **Atomic pipeline with HMM internal gate** | Both right: coupled in execution, HMM must validate before params |
| **2. Search Method** | **Joint 500 LHS** | ρ>0.6 cross-cluster coupling makes sequential gains illusory |
| **3. Gate Architecture** | **8 effective gates; collapse 1-4 to Arm Superiority** | Preserves FWER control; eliminates SKIP vulnerability |
| **4. Monotonic ke** | **Hard rejection in HMM** (Position A) | ke inversion was observed; soft priors failed audit |
| **5. DSR Trial N** | **N=13 tracked honestly; 500 LHS trials logged but penalized** | Both agree on honest accounting; DSR math requires it |
| **6. HMM Occupancy** | **Hard cap: no state >60%, CRISIS ≤20%** | Position A's anti-94% mechanism; Position B silent → adopt A |
| **7. ARI Bar** | **≥0.70 on ≥3/4 folds** (Position A) | Stricter than B's 2/4; Phase 8 had 0/4 |
| **8. hmm_n_states** | **Frozen at 3 for optimization; 2-state ablation as post-hoc** | Avoids N inflation in DSR; ablation validates necessity |

---

## Execution Specification (Single Orchestrator)

```python
# research/phase9_full_pipeline.py — SINGLE ENTRY POINT
def run_phase9():
    # 1. HMM BUILD (internal gate)
    hmm = build_factor_hmm(pca_components=3, monotonicity='hard_rejection', 
                           occupancy_cap=0.60, crisis_cap=0.20, min_sojourn=5)
    if not hmm.validate(ari_threshold=0.70, folds_required=3):
        raise RuntimeError("HMM GATE FAILED: Regime engine invalid — aborting parameter search")
    
    # 2. JOINT PARAMETER SEARCH (500 LHS, purged/embargoed CV)
    best_params = ml_parameter_optimization.run_joint(
        clusters='A,B,C,D', n_samples=500,
        cv=PurgedEmbargoCV(purge_days='trailing_tau*5', embargo_months=12, folds=4),
        dsr_tracker=TrialTracker(N_base=13),  # +500 LHS logged for DSR
        metric='paired_sharpe_vs_spy', alpha=0.0071
    )
    
    # 3. GATE ENGINE (8 effective gates)
    gates = GateEngine(effective_gates=8, harvey_compliant=True)
    gates.run_arm_superiority(arm_a, arm_b, arm_c, fw_er=0.00625)
    gates.run_all(best_params, arm_c)
    
    # 4. FULL 3-ARM + AUDIT
    three_arm_runner.run(arms='A,B,C', persist=True, synthetic_fallback=False)
    gate_engine.audit_all()
    provenance_audit.run()
```

---

## File Targets (Unified)

| Module | Owner | Action |
|--------|-------|--------|
| `research/phase9_full_pipeline.py` | **NEW** | Single orchestrator (above) |
| `Quantitative/regime_shield_v3.py` | B | Add `validate()` method with hard occupancy/monotonicity/ARI |
| `research/ml_parameter_optimization.py` | B | `PurgedEmbargoCV`, `TrialTracker`, `GroupLasso` clusters |
| `ingestion/harness/gate_engine.py` | A | `run_arm_superiority()`, 8-gate DAG, `gates_v3.yaml` |
| `Quantitative/swap_barrier_v3.py` | B | Verified exponential decay + moat penalty |
| `research/macro_pca.py` | B | 8 macro → 3 PCs |
| `config/pre_registration_v4.yaml` | A | Frozen pre-run (HMM spec + params + gates) |

---

## Pre-Registration Freeze (Before Execution)

All configs frozen in `config/pre_registration_v4.yaml` with SHA-256:
- HMM spec: 3-state, 3-PC factor-model, hard monotonicity, occupancy caps
- Parameter ranges: all 13 params with clusters
- Gate architecture: 8 effective gates, FWER α/8
- DSR: N=13 base + 500 LHS tracked
- CV: purged/embargoed, 4-fold walk-forward

---

**CEO Ruling Required**: APPROVE / REJECT / MODIFY / EXECUTIVE OVERHAUL