# .opencode/skills

Skills installed here are shared with the org. install with the skills CLI:

```
npx skills add <owner>/<repo>
```

- opencode reads `.opencode/skills/**/SKILL.md`.
- Gemini CLI reads SKILL.md files from the repo root as well — so a skill
  installed here can serve both the opencode side (workers/managers) and the
  Gemini side (gemini-planner / gemini-flash-worker in paid mode).

Recommended additions for this org: a `debate-protocol` skill and a
`planning` skill from skills.sh (e.g. the Gemini agent-workflows topic). Each
installed skill must not contradict `.agents/general/org/runbook.md`.
