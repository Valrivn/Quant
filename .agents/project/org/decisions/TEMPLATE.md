# Decision File Template

Copy this file as `D-YYYYMMDD-NNN.md` for each CEO ruling. Fill every field.
logger maintains; CEO's words are quoted verbatim where possible.

File the ruling under `decisions/<YYYY-MM>/<NN-project>/` (see index.md for
the project list; if the initiative is new, create the next numbered folder
and add it to both views in index.md).

```markdown
# D-YYYYMMDD-NNN — <short title>

- **Tier:** 3 | 2 | DISCOVERY
- **Date:** YYYY-MM-DD
- **Brief:** B-YYYYMMDD-###
- **Ruling:** APPROVE | REJECT | MODIFY

## Options considered
1. <option, proposer>
2. <option, proposer>

## Positions
- **big-pickle:** <1-line position summary>
- **gemini-planner:** <1-line position summary>

## Disagreement map (if T3)
- Disagree on: <list>
- Consensus on: <list>

## Synthesis (hermes-bridge, if T3)
- Recommendation: <...>
- Disagreements: <substantive vs stylistic>
- Rationale: <...>
- Risk: <...>

## CEO ruling
> <verbatim ruling text, or 1–3 line summary>

**Rationale:** <CEO words or inferred from the MODIFICATION instruction>

## Effect
- Blueprint delta: <what changed in blueprint.md>
- Quality gate: <PASS/FAIL + pass rate>
- Cost rows: <decision-id → cost-ledger.md>

## Follow-up
- <open items, owners>
```

## Drafts

Before a ruling, briefs/positions/synthesis accumulate in `_drafts/` under the
brief-id. After a ruling, archive them at `_drafts/archive/<DECISION-ID>/`.
