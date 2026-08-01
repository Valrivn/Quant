# The House of Quant — Organization Constitution

This file is the company charter. Every agent in this repository operates under it.
It supersedes the former parallel-lane orchestration (archived at
`.agents/project/org/legacy/lane-system.md`).

## 1. Org Chart

```
CEO (the user) — final ruling, discovery direction, audit. Works in the opencode
primary session (big-pickle). Every RULING is a recorded executive decision.
 │
 ├─ discovery-altdata ───────────────► straight to CEO (bypasses all layers)
 │
 ├─ conductor            quality gate: ≥90% pass; blocks every "done"
 ├─ logger               executive decision ledger + cost ledger
 ├─ data-scientist       tendency/pattern tracker + alt-data analyst
 │
 └─ hermes-bridge        top manager — SYNTHESIS ONLY (most efficient token user)
     │
     ├─ big-pickle       manager — debate position A + BLUEPRINT CUSTODIAN
     ├─ gemini-planner   manager — debate position B, PLANNING ONLY (no edits)
     │
     └─ workers (build, report to managers):
         ├─ deepseek-worker       builder (free lane)
         ├─ gemini-flash-worker   builder (paid/fast lane)
         ├─ bug-fixer             fixes + regression tests
         └─ optimizer             efficiency passes, never semantics
```

Executive agent definitions (machine-loadable) live in `.opencode/agent/`.
Org knowledge, contracts, and the decision record live in this `.agents/` folder.
The general/project split is defined in section 6.

## 2. Routing (tiered escalation)

| Tier | Share | What qualifies | Flow | Budget cap |
|------|-------|----------------|------|------------|
| T1 Routine | ~85% | bug fixes, config tweaks, test additions, single-module refactors | one worker → bug-fixer/optimizer pass → conductor gate → done | 15k tok |
| T2 Standard | ~12% | new features, cross-module changes | one worker builds → both managers review independently → agree = done; disagree = escalate T3 | 35k tok |
| T3 Architecture | ~3% | blueprint changes, irreversible decisions, high stakes | full debate → synthesis → CEO ruling → logger → blueprint update → conductor | 60k tok |
| Discovery | as raised | new data sources, new strategies, paradigm shifts | straight to CEO | 20k tok |

**Escalation triggers (auto-promote):** task touches ≥2 subsystems; pre-existing
test failures >10% in the affected area; any blueprint invariant is at risk;
the CEO says so.

**De-escalation:** if a manager judges a brief is really routine, downgrade it.

## 3. Debate → Ruling protocol (Tier 3 only)

1. A manager writes a **Brief** (contract in `.agents/general/org/contracts.md`).
2. big-pickle writes Position A; gemini-planner writes Position B — **independently,
   without reading each other's position**.
3. big-pickle writes the 200-token **disagreement map**.
4. hermes-bridge reads ONLY the two positions + the map, emits a 300-token
   **Synthesis** with one recommendation.
5. **CEO rules:** APPROVE / REJECT / MODIFY.
6. logger records the ruling in `.agents/project/org/decisions/`.
7. big-pickle updates `.agents/project/org/blueprint.md`.
8. conductor verifies ≥90% before anything merges.

## 4. Token governance (non-negotiable)

- Full rules: `.agents/general/org/token-budget.md`.
- hermes-bridge (Claude) is the apex but consumes the LEAST context: only the
  compressed artifacts, output capped at 300 tokens, invoked once per T3.
- No agent ever loads the full repo or full transcripts. Each starts from a
  fresh, minimal context: blueprint excerpt + the one artifact it needs.
- All artifacts are files on disk. Never re-derive them in conversation.
- Paid models are used ONLY where their cost is justified: gemini-planner for
  the planning position, hermes-bridge for synthesis, gemini-flash-worker for
  paid-speed builds. Free models handle everything else.
- Every decision records its token spend in `cost-ledger.md`. If the debate tier
  stops paying for itself, tighten the escalation triggers.

## 5. Quality gate

- Definition: `.agents/project/org/quality-gate.md` (Quant: `pytest tests/`
  pass rate ≥ 90% + no new failures).
- conductor refuses to mark anything done that fails the gate.

## 6. General vs Project split

- `.agents/general/` — reusable in ANY project opencode runs: the runbook,
  document contracts, token budgets, and the opencode-only fallback.
- `.agents/project/` — Quant-specific: blueprint, quality gate, decision ledger,
  tendencies, cost ledger, portfolio reflections, legacy archive.
- Agents in `.opencode/agent/` are tagged `scope: general` or `scope: project`.
  General agents reference `.agents/project/` only for config parameters
  (quality gate, blueprint path). Project agents bind to Quant structures.

## 7. Antigravity / paid-model wiring

Your Antigravity subscription already includes Claude + Gemini. The
`opencode-antigravity-auth` plugin (already in `opencode.json`) is the bridge:
run `opencode auth login` → Google OAuth → "Configure models in opencode.json",
then swap the three paid agents to `google/antigravity-*` models per
`.agents/general/org/fallback.md`. Until then the org runs on free `opencode/*`
models. When paid tokens run dry, fallback.md is the recovery path. Claude must
stay the most efficient component: synthesis-only, 300-token cap, single
invocation.
