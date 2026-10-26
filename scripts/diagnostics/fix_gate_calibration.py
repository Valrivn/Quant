# Fix gate_calibration.py: change PHASE9_REPORT to calibration_report_phase9.json

with open('validation/gate_calibration.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix PHASE9_REPORT path
content = content.replace(
    'PHASE9_REPORT = Path("data/validation/calibration_report.json")',
    'PHASE9_REPORT = Path("data/validation/calibration_report_phase9.json")'
)

# Fix the error message on line ~686
content = content.replace(
    '"data/validation/calibration_report.json, "',
    '"data/validation/calibration_report_phase9.json, "'
)

# Fix the docstring on line 7
content = content.replace(
    'report: data/validation/gate_calibration_report.json',
    'report: data/validation/calibration_report_phase9.json (Phase 9)'
)

with open('validation/gate_calibration.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('gate_calibration.py updated:')
print('  PHASE9_REPORT -> data/validation/calibration_report_phase9.json')