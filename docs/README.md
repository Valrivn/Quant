# `docs/` — Documentation & Support Markdown

## Roadmap Position
Cross-cutting: architecture context, decision context, and Workstream-B
reports. Non-executable; referenced by agents and onboarding.

## Folder Summary
Collated markdown documentation: `ARCHITECTURAL_CONTEXT.md`,
`DECISION_CONTEXT.md`, `WORKSTREAM_B_REORGANIZATION_REPORT.md` (this sweep's report),
`Math_Problem.md`, `STRATEGY_VIABILITY_BRIEFING.md`, `COMPLIANCE.md`, audit and
session docs, plus `data/` (XBRL sector concept chains as
json/md/yaml + p1_sp500_sources.md) and `_misc/` (stray notes).

## Skeleton
```
docs/
  ARCHITECTURAL_CONTEXT.md  DECISION_CONTEXT.md  WORKSTREAM_B_REORGANIZATION_REPORT.md
  STRATEGY_VIABILITY_BRIEFING.md  COMPLIANCE.md Math_Problem.md ...
  data/      # xbrl_sector_concept_chains.{json,md,yaml} p1_sp500_sources.md
  _misc/     # stray text notes (test.txt)
```

## Files (key)

| File | Purpose |
|---|---|
| `ARCHITECTURAL_CONTEXT.md` | system architecture context |
| `DECISION_CONTEXT.md` | decision provenance |
| `WORKSTREAM_B_REORGANIZATION_REPORT.md` | Workstream B sweep report (this effort) |
| `WORKSTREAM_B_REPORT.md` | project's 3-Arm backtest P0-fix report (unrelated, UTF-16 — left as-is) |
| `STRATEGY_VIABILITY_BRIEFING.md` | strategy viability briefing |
| `data/xbrl_sector_concept_chains.*` | XBRL concept chain deliverables (3 formats) |

## Entrypoints
None (documentation only).

## Dependencies
None (accepts content from every other folder).

## Efficient Pathways
- `full_pipeline`: read `ARCHITECTURAL_CONTEXT.md` first, then the per-folder
  DEEP_DIVEs before touching code.