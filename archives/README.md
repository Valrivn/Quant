# `archives/` — Phase Snapshots & Decision Records

## Roadmap Position
Cross-cutting (history): immutable phase snapshots — debates, audits, sprint
continuations. Read-only; content is preserved as-is with sha256 sidecars where
it matters.

## Folder Summary
Date-stamped phase archives: `phase8_sprint_continuation_20260925/` (data
scientist/educator/quant_research docs), `phase9_debate_20260926/` (position
A/B + disagreement map + synthesis + brief), `phase9_audit_20260926/`,
`phase9_extended_qual_20260930/`, `phase9_gate_calibration_20260930/`
(INDEX.md + sha256 + provenance manifest), `phase9_provenance_audit_v2_20260930/`,
`phase9_regime_v6_20260928/` (CESI ARI diagnostic + session checkpoint),
`phase9_v7_transition_20260929/` (calibrated params, prereg v7, prereg.py,
provenance_audit.py).

## Skeleton
```
archives/
  phase9_debate_20260926/ phase9_gate_calibration_20260930/ ...
  phase9_v7_transition_20260929/   # calibrated params + prereg v7 (sha256'd)
```

## Files (key)

| File | Purpose |
|---|---|
| `phase9_debate_20260926/phase9_position_a.md` / `position_b.md` | adversarial positions |
| `phase9_debate_20260926/phase9_disagreement_map.md` | disagreement map |
| `phase9_gate_calibration_20260930/INDEX.md(.sha256)` | gate calibration snapshot index |
| `phase9_v7_transition_20260929/pre_registration_v7.yaml` | v7 transition snapshot |

## Entrypoints
None (read-only).

## Dependencies
None.

## Efficient Pathways
- `gate_audit`: consult `phase9_gate_calibration_20260930/` before re-quoting
  calibration numbers; current calibration is `config/gate_calibration_phase9_v7.json`.