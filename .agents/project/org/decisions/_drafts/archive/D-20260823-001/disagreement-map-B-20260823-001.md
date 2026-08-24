DISAGREE-ON:
1. PIT enforcement mechanism: A = per-row `available_as_of` column + quarantine
   partitions inside the db/ layer; B = read-only "simulated-clock" wrapper over
   SQLite requiring every query to carry a clock parameter (no schema change).
2. Primary skill metric & sequence: A = F1/Spearman against LM-dict baseline
   with three pre-registered bars INCLUDING ablation-lift, market labels in
   Phase 3; B = cross-entropy vs verified human corpora FIRST, defer all
   return-linked simulation until NLP skill is proven.
3. First-run scope: A builds all five ablation columns up front; B stages
   NLP-only before any market simulation.
CONSENSUS-ON: human-labeled oracle replaces manual grading entirely; frozen
hash-locked grading scripts; per-source isolation matrix; pre-registered bars
in config/weights*.yaml before any run; conductor gate per phase; tuning
unlocked only after verdicts recorded.
RESOLUTION-PATH: mechanism choice → sim-guardian audit of which design
provably blocks look-ahead at least complexity; metric staging → does corpus
accuracy without return-linkage risk a false-positive verdict (B's own RISK 2)?
