# Antigravity Multi-Agent Context Tree & Self-Healing Instructions

When executing the autonomous `/goal` self-healing loop to monitor `stream_guard.log` and resolve failures:

## 1. Asymmetric Model Hierarchy (Division of Labor)
To maintain a strict **75% maximum token ceiling** (leaving a 25% safety margin), route tasks through this structure:
- **Gemini Pro (The Context Funnel)**: Ingests the workspace context + recent thoughts retrieved from `opencode.db` SQLite in read-only mode, and slashes the context down to an isolated `<15k` token block containing only the broken function/file and the error. When reading `stream_guard.log` to extract context and tracebacks, the script/agent must compress consecutive repeating spinner or helix characters/lines and replace them with a single placeholder line: `[code helix deleted]`.
- **Claude 3.5 Sonnet (The Surgical Code Architect)**: Receives the `<15k` token block. Its sole job is to design the fallback code (e.g. `curl_cffi` or socket fallback) or repair deep exceptions.
- **GPT / Local OSS (The Coder)**: Writes the code to disk and executes `pytest` validation.
- **Gemini Flash (The Triage Logger)**: Logs execution metrics and clean diff summaries to [changes.md](file:///Users/hayden/Desktop/quant-py/changes.md) upon every successful repair cycle.
- **Nemotron 3 Ultra**: The primary planner. If the cascade fails to resolve a test block 3 times consecutively, pause the cascade and let Nemotron take over macro diagnosis.

## 2. Reading Opencode SQLite DB
Always connect to `~/.local/share/opencode/opencode.db` using read-only mode to prevent database locks:
```python
conn = sqlite3.connect("file:~/.local/share/opencode/opencode.db?mode=ro", uri=True, timeout=15.0)
```
Extract the latest thoughts (`type = 'reasoning'`) and messages to understand the agent's context.

## 3. Lock Verification
Verify that `.guard_lock` exists before applying changes, and delete `.guard_lock` only after the changes are written, validated, and logged to `changes.md`.

## 4. Background Lifecycle & Token-Saving Scheduling
To prevent constant active polling and excessive token usage when running the `/goal` daemon:
- When starting or executing a `/goal` autonomous task, the agent must immediately register a background cron check (e.g. using `schedule` with a 5-minute interval `*/5 * * * *` or similar duration timer) and then end its turn immediately.
- On each scheduled wakeup, the agent must first read the file system for `.guard_lock`.
  - If `.guard_lock` does NOT exist, the agent must immediately end its turn without running any other commands or calling other subagents. This maintains an idle state and preserves your token budget.
  - If `.guard_lock` exists, the agent must proceed to execute the self-healing cascade to analyze the log, write the patch, and validate the code. Only after the validation successfully passes and `.guard_lock` is deleted should the agent return to its idle scheduling state.
