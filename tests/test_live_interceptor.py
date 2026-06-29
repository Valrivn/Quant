import os
import sys
import tempfile
from pathlib import Path
import pytest

# Insert workspace root to sys.path
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE_ROOT))

from opencode_scripts.preflight_gate import check_imports_and_interfaces, repair_from_trunk
from opencode_scripts.live_patcher import repair_worktree, patch_indeed_scraper_alias, patch_async_context_manager

def test_preflight_gate_on_workspace():
    venv_python = str(WORKSPACE_ROOT / ".venv" / "bin" / "python")
    if not os.path.exists(venv_python):
        venv_python = sys.executable
    ok, err = check_imports_and_interfaces(venv_python, str(WORKSPACE_ROOT))
    assert ok, f"Preflight gate failed on trunk workspace: {err}"

def test_live_patcher_indeed_alias():
    with tempfile.TemporaryDirectory() as tmpdir:
        scrapers_dir = Path(tmpdir) / "psychological" / "scrapers"
        scrapers_dir.mkdir(parents=True)
        corp_audit = scrapers_dir / "corp_audit.py"
        corp_audit.write_text("class GlassdoorScraper:\n    pass\n")

        patched = repair_worktree(tmpdir, "ImportError: cannot import name 'IndeedScraper' from 'psychological.scrapers.corp_audit'")
        assert patched
        assert "IndeedScraper" in corp_audit.read_text()

def test_live_patcher_async_context():
    with tempfile.TemporaryDirectory() as tmpdir:
        scrapers_dir = Path(tmpdir) / "psychological" / "scrapers"
        scrapers_dir.mkdir(parents=True)
        corp_anon = scrapers_dir / "corp_anonymous.py"
        corp_anon.write_text("class CorpAnonymousScraper:\n    def __init__(self):\n        pass\n")

        patched = repair_worktree(tmpdir, "TypeError: object is not an async context manager")
        assert patched
        assert "__aenter__" in corp_anon.read_text()
