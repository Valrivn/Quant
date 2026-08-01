# Fallback — OpenCode-Only Mode (no paid tokens)

As of the initial build, `opencode auth list` reports **0 credentials** and only
free `opencode/*` models exist. Therefore opencode-only mode is the SHIPPED
DEFAULT. The paid tier is an upgrade path, not a requirement. This file is the
recovery path whenever Antigravity/Claude/Gemini tokens run dry.

## Model map (default = free; paid = the swap you may make)

| Agent | Paid target (upgrade, via Antigravity plugin) | Free default (fallback) |
|-------|------------------------------------------------|--------------------------|
| hermes-bridge | `google/antigravity-claude-sonnet-4-6` (Claude Sonnet 4.6, Antigravity quota) | `opencode/nemotron-3-ultra-free` |
| gemini-planner | `google/antigravity-gemini-3.1-pro` (Gemini 3.1 Pro, Antigravity quota) | `opencode/nemotron-3-ultra-free` |
| gemini-flash-worker | `google/antigravity-gemini-3-flash` (Gemini 3.x Flash, Antigravity quota) | `opencode/deepseek-v4-flash-free` |
| deepseek-worker | — (already free) | `opencode/deepseek-v4-flash-free` |
| big-pickle | — | `opencode/big-pickle` |
| bug-fixer / optimizer / conductor | — | `opencode/deepseek-v4-flash-free` |
| logger | — | `opencode/north-mini-code-free` |
| data-scientist | — | `opencode/nemotron-3-ultra-free` |
| discovery-altdata | — | `opencode/nemotron-3-ultra-free` |

NOTE: the provider prefix is `google` and the model IDs use the
`antigravity-` prefix (from the `opencode-antigravity-auth` plugin). Alternative
flash name seen in the wild: `gemini-3.6-flash` (Antigravity agent default) —
map to whatever `opencode models` shows after auth.

## How to switch modes

Paid mode: edit the `model:` line in the target agent file(s)
(`.opencode/agent/<name>.md`) to the paid ID, ensure the provider is
authenticated (`opencode auth login`), and restart opencode.

OpenCode-only (recovery): revert those same lines to the free IDs.

## Degradation ladder (if tokens run dry mid-session)

1. **Step 0 — normal paid mode.** Full debate, two independent positions.
2. **Step 1 — free but still diverse:** hermes-bridge and gemini-planner run on
   free models. Debate keeps its two-model structure (big-pickle vs nemotron-3)
   — different training families still reduce correlated error.
3. **Step 2 — single-model debate:** if only one family is usable, run
   "position + adversarial self-critique": the one model writes a position,
   then a fresh subagent (same model, different context) attacks it. Costs
   ~50% of a full debate.
4. **Step 3 — triage collapse:** T2 collapses to a single manager review; T3
   collapses to solo position + self-critique + NO synthesis (CEO reads the two
   artifacts directly). T1 unchanged. Budgets: T2 ≤ 18k, T3 ≤ 25k.

## Token-dollar reality check

Free opencode models carry no dollar cost (rate-limited). The paid tier is the
only spend. The org is engineered so 100% of daily work runs on free models and
paid tokens are spent ONLY on: the two-position debate (T3), the synthesis
(Claude), and fast-lane builds. If you want to be ruthless about cost, run Step
1 permanently — you keep diversity at zero marginal dollar cost.

## Antigravity wiring (for the paid tier) — the ONLY step needed

Your Antigravity subscription already includes Claude + Gemini models. The one
missing piece was a way for opencode to *reach* them. That now exists: the
`opencode-antigravity-auth` plugin (OAuth gateway into Google's Antigravity API).
It is already added to `opencode.json` (`"plugin":
["opencode-antigravity-auth@latest"]`).

To activate the paid tier:

1. `opencode auth login` → choose **Google** → OAuth with your Google account
   (the one tied to your Antigravity subscription).
2. When prompted, choose **"Configure models in opencode.json"** — the plugin
   auto-registers `google/antigravity-*` models (claude, gemini 3.x pro/flash).
3. Verify with `opencode models` — the antigravity models should now be listed.
4. Swap the three paid agents to their paid models per the model map above
   (edit the `model:` line in `.opencode/agent/hermes-bridge.md`,
   `gemini-planner.md`, `gemini-flash-worker.md`).
5. Restart opencode.

Fallback (recovery): swap the same three lines back to the free IDs and restart.
The `/fallback` command does this for you if the lines point at paid models.

No Anthropic console key and no separate Gemini API key are required when using
this plugin — everything flows through your existing Google/Antigravity
subscription. (Alternative gateways exist — `frieser/antigravity-proxy`,
`CHN-beta/antigravity-route` — but the plugin is the supported opencode path.)
