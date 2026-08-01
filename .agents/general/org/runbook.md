# Runbook — Tiers, Escalation, Debate, Roles

This is the operational manual for the org. Reads by all agents. Concise by
design: agents should consult this file rather than carry the whole system in
context.

## Roles and hard rules

| Agent | Hard rule |
|-------|-----------|
| big-pickle | Must not peek at gemini-planner's position before writing its own. Owns blueprint updates (custodian). |
| gemini-planner | PLANNING ONLY. `edit: deny`. Never touches code. |
| hermes-bridge | Synthesis only. Reads exactly: 2 positions + disagreement map. Output ≤ 300 tokens. Never reads transcripts, code, or briefs. |
| deepseek-worker / gemini-flash-worker | Build only from the brief. Never invent scope beyond the brief. Never edit the blueprint. |
| bug-fixer | Fixes + adds regression tests. Never refactors semantics. |
| optimizer | Efficiency passes only. Never changes behavior. Reports any behavior it must change. |
| conductor | Runs the quality gate; refuses "done" on failure. |
| logger | Writes decision files + cost ledger. Never makes decisions. |
| data-scientist | Reads everything, writes only tendency reports + portfolio reflections. |
| discovery-altdata | Gathers alternative data / strategy signals. Hands a discovery brief DIRECTLY to the CEO. Never builds. |

## T1 routine flow

1. Requester (usually the primary session) writes a Brief (contracts.md).
2. Select worker = lowest-cost available model that is not mid-budget-exhausted.
3. Worker builds from brief only.
4. bug-fixer passes if bugs/regressions possible; optimizer optional.
5. conductor gate ≥90% (spot-check 1 in 5 for T1, always for T2+).
6. Log line in cost-ledger (cheap: just the worker + conductor rows).

## T2 standard flow

1. Brief → worker builds.
2. big-pickle and gemini-planner each review the diff INDEPENDENTLY (fresh
   contexts, no shared transcript).
3. If positions agree → conductor gate → done.
4. If positions disagree → promote to T3 (this is the only auto-promotion).

## T3 architecture flow (the argument)

1. Brief (must state STAKE and reference blueprint invariants).
2. Position A by big-pickle; Position B by gemini-planner (independent).
3. Disagreement map by big-pickle (≤200 tokens).
4. Synthesis by hermes-bridge (≤300 tokens).
5. CEO ruling (in the primary session). logger records. big-pickle updates
   blueprint. conductor gates.
6. Cost-ledger row for the full debate.

## Discovery flow

1. discovery-altdata scans alternative data sources / strategy signals.
2. Writes a discovery brief addressed to the CEO.
3. Stops there. No build, no routing through managers.
4. CEO decides whether to fund a T2/T3 investigation and who runs it.

## Cross-cutting rules

- **Independence:** positions in a debate are written before seeing each other.
- **Artifacts over memory:** everything is a file in `.agents/`; never re-derive.
- **Escalation triggers:** ≥2 subsystems touched, >10% pre-existing failures in
  the area, blueprint invariant at risk, or CEO flags.
- **De-escalation:** a manager may downgrade a routine-looking brief.
- **Conflict of two managers disagreeing but CEO absent:** default to the more
  conservative option (lowest risk, fewest lines changed) and record a pending
  ruling in the ledger.
