# SUPERSEDED — Legacy Parallel-Lane Orchestration (ARCHIVE)

> **STATUS: SUPERSEDED by `.agents/AGENTS.md` (The House of Quant org).** This
> archive exists for reference only. The parallel-lane / self-healing-daemon
> governance has been replaced by the tiered org: lanes → workers + managers;
> the daemon's monitoring → the conductor's quality gate. Scripts referenced
> below (e.g. `opencode_scripts/*.py`, worktree lanes) are legacy and not part
> of the active org. Do NOT revive this orchestration without a CEO ruling.

## Former content (verbatim archive)

### Antigravity Multi-Agent Context Tree & Self-Healing Instructions

When executing the autonomous `/goal` self-healing loop to monitor
`stream_guard.log` and resolve failures:

**1. Asymmetric Model Hierarchy (Division of Labor).** Maintain a strict 75%
maximum token ceiling (25% safety margin). Route tasks: Gemini Pro (Context
Funnel) → Claude 3.5 Sonnet (Surgical Code Architect) → GPT/Local OSS (Coder) →
Gemini Flash (Triage Logger) → Nemotron 3 Ultra (primary planner, takes over
after 3 consecutive cascade failures).

**2. Reading Opencode SQLite DB.** `sqlite3.connect("file:~/.local/share/opencode/opencode.db?mode=ro", uri=True, timeout=15.0)` to extract `type='reasoning'` thoughts.

**3. Lock Verification.** `.guard_lock` must exist before changes; delete only
after validation passes and `changes.md` is updated.

**4. Background Lifecycle.** Register a cron (e.g. `*/5 * * * *`), end turn
immediately; on wake, if `.guard_lock` absent, idle; else run the healing
cascade.

### Phase 2.5: Central Oversight & Policing Protocol

**1. Architectural Separation of Concerns.** Hub = Claude 3.5 Sonnet / Gemini
Pro (Day) or Nemotron 3 Ultra (Night), sub-5-second structural validation. Lanes
= headless workers in isolated Git worktrees.

**2. Invariants & Guardrails.** Git worktree isolation (never act in the trunk
repo directly); shift rotation (DAY = rapid cycles, NIGHT = exhaustive deep
loops with cross-lane tip injection).

## What replaced what

| Legacy concept | Org replacement |
|----------------|-----------------|
| 75% token ceiling | tiered budgets in `.agents/general/org/token-budget.md` |
| Asymmetric model hierarchy | org roles in `.opencode/agent/` |
| Self-healing daemon / `.guard_lock` | conductor quality gate + T1/T2 flows |
| Parallel lanes (alpha→omega, worktrees) | deepseek-worker / gemini-flash-worker under managers |
| `/goal` loop | tiered routing + `/brief` `/debate` `/rule` `/conduct` commands |
