# Disagreement Map — Phase 9 Architectural Redesign (2026-09-26)

Split 1: Sequencing — A: HMM first with hard ARI stop, then 9A→9C→9D; B: atomic single pipeline, no handoffs.
Split 2: Search method — A: sequential D→C→B→A, never joint 13-param LHS; B: joint 13-param 500-Sobol LHS + grouped Lasso (ρ>0.6 coupling).
Split 3: Gate architecture — A: keep Gates 1-4, Holm across pre-declared family; B: collapse 1-4 → single Arm Superiority, FWER α/8, 8 effective gates.
Split 4: Monotonic ke — A: hard rejection; B: Dirichlet prior λ=0.05.
Split 5: DSR trial N — A: must count HMM grid + param evals, 500 LHS unauditable; B: N=13 tracked honestly, 500 LHS accepted.
Split 6: HMM state occupancy — A: hard caps (CRISIS ≤20%, no state >60%); B: no occupancy cap, PCA factor-model 3-state.
Split 7: ARI pass bar — A: ≥0.70 on ≥3/4 folds; B: ≥0.70 on ≥2/4 folds.
Split 8: `hmm_n_states` — A: last and frozen outside the loop; B: inside the joint loop.
