# .agents/project — Quant-Specific Layer

Everything here binds the org to the Quant repository. It is NOT portable.

```
project/
├── README.md                 <- this index
└── org/
    ├── blueprint.md          <- MASTER BLUEPRINT (big-pickle custodian)
    ├── quality-gate.md       <- the 90% gate metric for THIS repo
    ├── decisions/            <- executive decision ledger
    │   ├── index.md
    │   └── TEMPLATE.md
    ├── tendencies.md         <- data-scientist pattern reports
    ├── cost-ledger.md        <- token spend per decision
    ├── portfolio-reflections.md <- college/leadership narrative
    └── legacy/
        └── lane-system.md    <- superseded parallel-lane orchestration (archive)
```

## The one rule

Project agents and config bind to Quant structures (modules, test suite,
pipeline). General agents read these files for parameters but never hard-code
Quant facts. If a general agent needs a Quant fact, it reads `blueprint.md`.

## What a new project needs to do to adopt the org

1. Copy `.agents/general/` + `.opencode/agent/` from here.
2. Write its own `.agents/project/org/blueprint.md` + `quality-gate.md`.
3. Point `quality-gate.md` at its own test command.
