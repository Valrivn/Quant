# Fix gate_engine.py for CEO rulings:
# 1. Update band comment [0.35, 0.65] -> [0.38, 0.62]
# 2. Add FWER scope clarification: Gate 1 only for statistical FWER
# 3. Update structural component comment

with open('ingestion/harness/gate_engine.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Fix the band comment on line ~3645
content = content.replace(
    '"wacc_coverage: == 1.0", "probation_pass_rate: in [0.35, 0.65]",',
    '"wacc_coverage: == 1.0", "probation_pass_rate: in [0.38, 0.62]",'
)

# 2. Add FWER scope clarification in Phase 9 section (after line ~3600)
# Find the Phase 9 section header and add clarification
old_phase9_comment = """#
# Every threshold is READ FROM THE FROZEN CONFIG at module load (and
# re-verified by SHA-256) — none is a literal in a gate body.  The metric maths
# is delegated to the already-audited Phase-0 implementations above, and
PHASE9_THRESHOLD_PARITY records that the legacy literal thresholds still agree
with the frozen config."""

new_phase9_comment = """#
# Every threshold is READ FROM THE FROZEN CONFIG at module load (and
# re-verified by SHA-256) — none is a literal in a gate body.  The metric maths
# is delegated to the already-audited Phase-0 implementations above, and
PHASE9_THRESHOLD_PARITY records that the legacy literal thresholds still agree
with the frozen config.
#
# FWER SCOPE (CEO Ruling 4A): Gate 1 (Arm Superiority) is the SOLE statistical
# hypothesis test under family-wise error rate control (Holm at alpha = 0.05/8).
# Gates 2-8 are strictly enforced as ENGINEERING TOLERANCE CEILINGS with
# no statistical FWER claim — they are pass/fail thresholds, not p-value gates."""

content = content.replace(old_phase9_comment, new_phase9_comment)

with open('ingestion/harness/gate_engine.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('gate_engine.py updated:')
print('  - Band comment fixed: [0.35, 0.65] -> [0.38, 0.62]')
print('  - FWER scope clarification added (Gate 1 only for statistical FWER)')