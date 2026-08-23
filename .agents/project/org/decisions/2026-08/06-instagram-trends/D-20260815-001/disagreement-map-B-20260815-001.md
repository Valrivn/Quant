DISAGREE-ON:
1. Hallucination mitigation depth — A calls for source-URL grounding + human-readable audit trail per score; B calls for strict typing + structural penalties. The specific enforcement mechanism differs.
2. Scope guardrails — A explicitly hard-prohibits gate threshold changes in the same PR; B does not raise this constraint.
3. Token/cost budget — B flags weekly LLM synthesis token costs as a primary risk; A does not assess pipeline budget impact.
4. Entity-resolution — B flags small-cap ticker collision risk; A does not address it.

CONSENSUS-ON:
1. Build the IG_LLM crawler/synthesis pipeline (both APPROVE).
2. Proxy scores flow INTO the existing qual gate — gate is never bypassed or weakened.
3. IG_LLM_ ticker prefix for isolated provenance; DB-first persistence.
4. RFF256 (IC 0.054) is the target signal to unlock.
5. LLM hallucination passing the qual gate is the top shared risk.

RESOLUTION-PATH:
1. Hallucination controls — implementation brief must specify both: structured output schema (B) AND source-URL + audit trail (A); not either/or.
2. Gate guardrail — brief must hard-prohibit threshold changes; CEO confirms scope boundary.
3. Budget — datasource-worker to estimate weekly token cost before build starts.
4. Entity resolution — brief must mandate ticker-collision validation step in crawler.
