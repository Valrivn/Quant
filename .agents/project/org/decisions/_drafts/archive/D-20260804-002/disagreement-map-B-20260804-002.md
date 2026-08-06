DISAGREE-ON:
- Scope: full 7-phase PIT rebuild (A) vs minimal 3-phase patch on #1/#4/#3 (B).
- S1 falsifiability: A says "zero silent degradations" is measurable; B calls it unfalsifiable absent defined error boundaries.
- External-dependency risk: A accepts new API surfaces (fredapi/edgartools/Tiingo/OpenBB) as mitigated-by-cache; B wants minimum dependency surface to avoid blockers.
- Survivorship/universe: A rebuilds the universe PIT incl. delisted; B's patch skips it, risking understating survivorship impact in the delta report.
- Decision-delay: B says a full rebuild delays the Discovery decision on diminishing returns; A says trustworthiness precedes the decision.
CONSENSUS-ON:
- #1 (FRED) and #4 (basket-empty) are the materially-moving degradations; #3 (cross-check wiring) must be fixed.
- Data-layer only; zero strategy-logic changes; re-run reports deltas honestly; external key availability is a shared risk.
RESOLUTION-PATH:
- Define S1 operationally (explicit source list + DEGRADED boundaries) before ruling.
- Probe FRED/EDGAR/Tiingo key availability + quotas now to decide scope feasibility.
- Test whether the 72/102-bear read and the buy-more thesis can be trusted on a 3-phase patch alone; if not, full rebuild is the floor.
