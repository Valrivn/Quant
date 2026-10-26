# Fix test_phase9_core.py to use tmp dir

with open('tests/test_phase9_core.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Add tempfile import
content = content.replace(
    'import numpy as np',
    'import tempfile\nimport numpy as np'
)

# Fix test_phase9_orchestrator_dry_run to use tmp dir
old_test = '''def test_phase9_orchestrator_dry_run():
    orchestrator = Phase9Orchestrator(output_dir="data/phase9_full")
    summary = orchestrator.run_pipeline(n_samples=10, dry_run=True)
    assert summary["status"] == "SUCCESS"
    assert summary["gates_summary"]["all_gates_pass"] is True'''

new_test = '''def test_phase9_orchestrator_dry_run():
    with tempfile.TemporaryDirectory() as tmpdir:
        orchestrator = Phase9Orchestrator(output_dir=tmpdir)
        summary = orchestrator.run_pipeline(n_samples=10, dry_run=True)
        assert summary["status"] == "SUCCESS"
        assert summary["gates_summary"]["all_gates_pass"] is True'''

content = content.replace(old_test, new_test)

with open('tests/test_phase9_core.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('test_phase9_core.py fixed - uses tmp dir')