# Fallback — Model Ladder (worker lane 50/50 zen-Antigravity; all others opencode zen primary)

The WORKER lane is split half/half between opencode zen (free) primaries and
Antigravity Gemini Flash (`google/antigravity-gemini-3-flash`, paid) primaries —
this spreads congestion off the free gate and de-correlates worker error
families. Every other lane (managers, hermes-bridge, conductor, logger,
data-scientist, discovery-altdata) keeps an opencode zen (free) primary, with
Antigravity as the escalation path only — EXCEPT gemini-planner, whose Gemini
3.1 Pro model always comes from Antigravity. This file is the single source of
truth for both the per-agent primary map and the retry ladder.

## Model map (workers 50/50; gemini-planner = Gemini via Antigravity; other lanes = zen)

| Agent | Primary | Notes |
|-------|------------------------|--------------------------------------|
| deepseek-worker | `opencode/mimo-v2.5-free` | zen free lane |
| gemini-flash-worker | `google/antigravity-gemini-3-flash` | paid fast lane |
| bug-fixer | `google/antigravity-gemini-3-flash` | paid lane |
| optimizer | `opencode/mimo-v2.5-free` | zen |
| edgar-worker | `opencode/mimo-v2.5-free` | zen |
| screen-worker | `google/antigravity-gemini-3-flash` | paid fast lane |
| ig-worker | `opencode/mimo-v2.5-free` | zen |
| alpha-worker | `google/antigravity-gemini-3-flash` | paid fast lane |
| stoch-worker | `google/antigravity-gemini-3-flash` | paid fast lane |
| audit-worker | `opencode/mimo-v2.5-free` | zen |
| datasource-worker | `google/antigravity-gemini-3-flash` | paid fast lane |
| datasource-gatherer | `google/antigravity-gemini-3-flash` | paid lane |
| rapid-agent | `opencode/mimo-v2.5-free` | zen |
| pipeline-supervisor | `opencode/mimo-v2.5-free` | zen; watchdog lane |
| big-pickle | `opencode/big-pickle` | zen; escalation target at 5-6 |
| gemini-planner | `google/antigravity-gemini-3.1-pro` | Gemini via Antigravity; ≥7 → `google/antigravity-claude-opus-4-6-thinking` |
| hermes-bridge | `opencode/nemotron-3-ultra-free` | zen; ≥7 → `google/antigravity-claude-sonnet-4-6` |
| conductor | `opencode/mimo-v2.5-free` | zen |
| logger | `opencode/mimo-v2.5-free` | zen |
| data-scientist | `opencode/nemotron-3-ultra-free` | zen |
| discovery-altdata | `opencode/nemotron-3-ultra-free` | zen |

## Retry / escalation ladder (any lane, any provider)

| Errors | Action |
|--------|--------|
| 0-4 | Retry with your own primary. |
| 5-6 | Stop, hand the task to big-pickle (`opencode/big-pickle`, zen, unchanged). |
| ≥7 | Escalate to the agent's top escalation model below — ladder cap. logger records the ≥7 outcome and the task is re-briefed. |

## Escalation tops (≥7 errors)

| Agent | Escalation model |
|-------|------------------|
| Antigravity-flash lane (gemini-flash-worker, bug-fixer, screen, alpha, stoch, datasource) | `google/antigravity-gemini-3.1-pro` (paid) |
| gemini-planner | `google/antigravity-claude-opus-4-6-thinking` (paid) |
| hermes-bridge | `google/antigravity-claude-sonnet-4-6` (paid) |
| everything else (zen primaries) | `google/antigravity-gemini-3.1-pro` (paid) |

## Core ban

- **NVDA is banned.** The `nvidia/*` model family and the NVDA API key are
  heavily rate-limited and must never be used. Only `opencode/*` (zen) and
  `google/antigravity-*` (Antigravity) providers are allowed.
