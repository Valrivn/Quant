# .agents/general — Portable Org Layer

Everything here applies to ANY project opencode runs. Copy this folder (or
these files) into another repo to bring the same governance with you.

```
general/
├── README.md              <- this index
└── org/
    ├── runbook.md         <- tiers, escalation, debate protocol, roles
    ├── contracts.md       <- exact document contracts (brief/position/synthesis/ruling)
    ├── token-budget.md    <- token caps, efficiency rules, cost ledger format
    └── fallback.md        <- opencode-only fallback: model swap map + degradation ladder
```

## What is portable

| Artifact | Portable? |
|----------|-----------|
| Org chart + routing rules | yes (AGENTS.md at repo root or `.agents/AGENTS.md`) |
| Debate protocol + document contracts | yes |
| Token budgets + cost ledger format | yes |
| OpenCode-only fallback model map | yes |
| Agent definitions in `.opencode/agent/` | yes, tag as `scope: general` |
| Quality gate CONCEPT (≥90%) | yes — the concrete metric is project-local |
| Blueprint | NO — project-local (`.agents/project/org/blueprint.md`) |
| Decision ledger / tendencies / portfolio | NO — project-local (`.agents/project/org/`) |

## The one rule

General agents never hard-code project specifics. They reference
`.agents/project/` paths for parameters. If an agent needs a project fact, it
reads the project blueprint, never its own baked-in copy.
