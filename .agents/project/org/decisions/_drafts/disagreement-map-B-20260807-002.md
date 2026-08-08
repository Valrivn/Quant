# Disagreement Map — B-20260807-002 (big-pickle, 200-token cap)

1. **D2 fail-closed vs honest-neutral.** B: keep "avoid" for missing data.
   A: "avoid" is a wiring defect (empty moat composite → blended 0.0, against
   the code's own "hold" docstring) — mislabels missing data as an avoid
   signal. "hold" cannot pass, so no data-poor advantage exists.
2. **D1 cache purity vs provenance.** B: no cached data in the z-frame (mixes
   vintages → invalid cross-section). A: hybrid is fine when every row carries
   `source` provenance and the report prints the vintage mix; rejecting cache
   makes the lane hostage to network blips and blocks a result today.
3. **D3** — AGREED: percentile-normalize mahalanobis to 0–1 within batch.
4. **Acceptable-outcome bar.** B: strictness ⇒ possible 0/0 vacuous comparison
   is acceptable. A: hybrid at least yields a non-empty traditional pass
   cohort, so D-20260807-001 gets an answer this run.
