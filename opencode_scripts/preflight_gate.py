"""Pre-flight gate run against a worktree before a lane ships.

Verifies that the given interpreter can import the core modules and that the
key public interfaces are present. ``repair_from_trunk`` is the recovery entry
point when the gate fails.
"""
import importlib
import logging
import os
import subprocess
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

CORE_MODULES = [
    "config",
    "db.connection",
    "psychological.qualitative_scoring",
]

INTERFACES: List[Tuple[str, str]] = [
    ("psychological.qualitative_scoring", "AlternativeStrategyPipeline"),
    ("db.connection", "init_db"),
]


def check_imports_and_interfaces(venv_python: str, workspace_root: str) -> Tuple[bool, str]:
    """Run an interpreter-side import + interface check against a workspace.

    Returns (ok, err). ``ok`` is True when every core module imports and every
    required interface is present.
    """
    root = str(Path(workspace_root).resolve())
    qual = str(Path(workspace_root).resolve() / "Qualitative")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (root, qual) if p not in env.get("PYTHONPATH", ""))
    imports = "\n".join(f"import {m}" for m in CORE_MODULES)
    checks = "\n".join(
        f"import {m} as _m; assert hasattr(_m, '{a}'), '{a} missing from {m}'"
        for m, a in INTERFACES
    )
    script = f"import sys\n{imports}\n{checks}\nprint('GATE_OK')\n"
    try:
        proc = subprocess.run(
            [venv_python, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=root,
            env=env,
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        return False, f"gate subprocess failed: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "unknown failure").strip()
    return "GATE_OK" in proc.stdout, (proc.stderr or "").strip()


def repair_from_trunk(workspace_root: str, error_msg: str) -> bool:
    """Best-effort repair of the workspace from trunk state.

    Returns True if a repair was attempted/applied, False when there is nothing
    to fix. The live_patcher covers the common scraper regressions.
    """
    if "IndeedScraper" in error_msg:
        from opencode_scripts.live_patcher import patch_indeed_scraper_alias

        scrapers = Path(workspace_root) / "Qualitative" / "psychological" / "scrapers"
        return patch_indeed_scraper_alias(scrapers)
    if "async context manager" in error_msg:
        from opencode_scripts.live_patcher import patch_async_context_manager

        scrapers = Path(workspace_root) / "Qualitative" / "psychological" / "scrapers"
        return patch_async_context_manager(scrapers)
    logger.warning(f"No known repair for: {error_msg}")
    return False
