# DISAGREEMENT MAP — D-20260801-004 live-run audit

POSITION-BY: big-pickle (blueprint custodian) | 200-token cap

## AGREE (both positions)
- Do NOT trade k5/k10/k15 as alpha; they are an AI/semiconductor sector-concentration bet.
- EDGE_REAL is an artifact: unmodeled sector beta + the megacap-bias ablation being the edge itself.
- Inference unadjusted: no FDR, selection on same window, i.i.d. CIs invalid (need HAC/Newey-West).
- Deflated-Sharpe 1.000 suspicious.
- FRED dead → regime gate never tested; sleeve excess NaN; decisions not persisted.

## DISAGREE
1. short_bills: A says −2.05% return is a DATA BUG (bills can't lose money). B says it is expected risk-free drag — labeling only.
2. Remedy: A → real L1 walk-forward (rank half-1/score half-2) + keep megacaps only as explicit hedged tilt. B → augment FF5 with industry/semiconductor factor + FDR q-values + nested CV + pre-2015 persistence test.
3. Deflated-Sharpe: A = "zero-margin, survived boundary". B = "placeholder/bug, must verify formula".
4. Emphasis: A prioritizes slippage-unit + rebalance-cost math; B prioritizes factor augmentation + cross-validation design.

## BOTH ASK CEO
Trade nothing yet. Fix, then re-audit.
