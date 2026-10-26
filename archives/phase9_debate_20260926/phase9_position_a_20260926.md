# Position A — bigpickle-worker (Conservative: schema / validation / persistence)

**Thesis: Fix the HMM. Then recalibrate parameters. Restructure gates last.**

ARI=-0.08 with 94% SHOCK occupancy is a **labeling** failure, not a threshold failure. Tuning `entry_exp`/`ke_var` against a broken regime optimizes noise. Path A is disqualified outright; Path C is right on scope, wrong on order.

**3.1 HMM — FIRST (`Quantitative/regime_shield_v3.py`)**
- 3 states, but SHOCK must be **capable of being rare**: hard occupancy invariants (CRISIS ≤20%, no state >60%). 94% occupancy is detector failure, not market truth.
- Monotonicity ke = **hard rejection**, not a Dirichlet prior. Soft priors are unauditable; the ke inversion is direct evidence.
- VIX labels: rolling **PIT** quantiles only. Full-sample = lookahead = invalid OOS.
- λ=0.05 Dirichlet **plus min-sojourn 5d**; flapping is the 94% mechanism.
- Persist posteriors + SHA256 to `data/regime/hmm_v3`. **Stop: ARI ≥0.70 on ≥3/4 folds.**

**3.2/3.3 ML + clusters — ONLY AFTER HMM PASSES**
- Sequential **D→C→B→A**. Never joint 13-param LHS. Cluster A contains `hmm_n_states`; it must be last and frozen.
- Purged/embargoed CV, TCA in loop, synthetic-fallback guard hard-blocked.
- Trial ledger: DSR N counts HMM grid **and** param evals. N=500 LHS is not auditable.

**3.4 Gates — LAST**
- Do **not** collapse Gates 1-4. Preserves diagnostics; apply Holm across a **pre-declared** family instead.
- Gates become a DAG: `11(ARI) → param freeze → 12,13 → 5,6,7,9,10 → 1-4 → 14,15,16`. Sequential dependency, not parallel.
- Sign `config/gates_v3.yaml` + `pre_registration_v4.yaml` **before** the run.
- `ke_var` floor derived from data (77bps), never hardcoded 50bps.

**Execution order: 9B → 9A → 9C → 9D. Reverse it and every downstream number is noise.**
