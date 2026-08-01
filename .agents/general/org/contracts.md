# Document Contracts — exact shapes, hard caps

Every artifact the org produces follows these templates. Caps are token limits
on the file content, not soft guidance. hermes-bridge depends on these being
strict — any producer that exceeds a cap breaks the chain's efficiency.

## Brief (≤400 tokens) — by any manager/requester

```text
BRIEF-ID: B-YYYYMMDD-###
TIER: 1 | 2 | 3 | DISCOVERY
TASK: <one line>
CONTEXT: <≤3 bullets>
STAKE: <high | medium | low — and why>
CONSTRAINT: <blueprint rules that bind this task>
BLUEPRINT-EXCERPT: <≤100 tokens pulled verbatim from blueprint.md>
SUCCESS: <definition of done>
```

## Position (≤400 tokens) — big-pickle or gemini-planner

```text
POSITION-BY: big-pickle | gemini-planner
BRIEF-ID: <of the brief>
POSITION: <one-sentence recommendation>
REASONS: <top 3, one line each>
RISKS: <top 2>
BLIND-SPOTS: <what you did not verify>
```

## Disagreement map (≤200 tokens) — big-pickle

```text
DISAGREE-ON: <itemized, where the two positions actually differ>
CONSENSUS-ON: <itemized>
RESOLUTION-PATH: <what would settle each disagreement>
```

## Synthesis (≤300 tokens) — hermes-bridge ONLY

```text
SYNTHESIS-ID: S-YYYYMMDD-###
RECOMMENDATION: <approve A | approve B | hybrid — describe>
DISAGREEMENTS: <which are substantive vs stylistic>
RATIONALE: <2–3 sentences>
RISK: <1 line>
DECISION-REQUESTED: <the exact question for the CEO>
```

## Ruling (no cap) — CEO, primary session

```text
DECISION-ID: D-YYYYMMDD-###
RULING: APPROVE | REJECT | MODIFY
MODIFICATION: <instruction, if MODIFY>
RATIONALE: <1–3 lines>
```

## Conductor report

```text
CONDUCTOR-PASS: PASS | FAIL
PASS-RATE: <nn.n%>
NEW-FAILURES: <0 | list>
BLOCKING-ISSUES: <list or none>
```

## Discovery brief (≤300 tokens) — discovery-altdata → CEO

```text
DISCOVERY-ID: D-YYYYMMDD-###
SIGNAL: <what was found, one line>
SOURCE: <data source>
IMPLICATION: <why it matters, 1–2 lines>
RECOMMENDED-NEXT: <fund a T2/T3? who should investigate?>
```

## Filing convention

- Briefs/positions/synthesis live in `.agents/project/org/decisions/_drafts/`
  until a ruling is made, then move under the ruling's `DECISION-ID`.
- Cost rows go straight into `.agents/project/org/cost-ledger.md`.
