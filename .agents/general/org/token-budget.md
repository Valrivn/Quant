# Token Budget & Efficiency Rules

The org exists to trade a little extra token spend for much higher decision
quality. That trade only works if the spend is *controlled*. These numbers are
the control.

## Per-tier budget caps (total input+output across all models in the flow)

| Tier | Cap | Notes |
|------|-----|-------|
| T1 Routine | 15k | one worker + one review + conductor |
| T2 Standard | 35k | worker + two independent reviews |
| T3 Architecture | 60k | full debate + synthesis + ruling + blueprint |
| Discovery | 20k | scan + discovery brief |
| hermes-bridge (Claude) | 1.5k TOTAL | 0.3k output, reads only artifacts |

## Efficiency rules

1. **hermes-bridge is the frugal apex.** It consumes less context than anyone
   and is invoked exactly once per T3. If a chain ever needs Claude twice, the
   chain is misdesigned — stop and re-read this file.
2. **No agent loads the full repo.** Every agent starts with a minimal context:
   the one artifact it needs + a blueprint excerpt. Managers read diffs, not
   source trees.
3. **Artifacts are files.** Never regenerate a brief/position in a conversation;
   read the file.
4. **Compaction stays on.** Long sessions compact rather than grow context.
5. **Cheapest-capable model wins.** Workers default to free models; paid models
   are used only for the specific role they exist for.
6. **Every decision logs spend.** Cost ledger rows per decision-id, per model:
   input tokens, output tokens, est cost. If a row is missing, logger flags it.

## Cost ledger format (`.agents/project/org/cost-ledger.md`)

```text
| date | decision-id | tier | models | in-tok | out-tok | est-cost | ruling | notes |
```

One row per model invocation within a decision (so the debate's 5 calls show
as 5 rows).

## When to renegotiate

- After every 5 T3 rulings, review the ledger. If the debate tier is not
  improving ruling quality (data-scientist can compare), tighten triggers.
- If monthly spend on the paid tier exceeds your budget, run `/fallback` and
  move to opencode-only mode (see `fallback.md`).
