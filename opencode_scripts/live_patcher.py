"""Live patcher for scraper regressions in deployed worktrees.

Repairs a broken live worktree by applying a small, surgical patch to the
scraper modules. Each patch is applied only when its trigger signature is
present in the error message and the target fix is still missing.
"""
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _scrapers_dir(worktree_root: str) -> Path:
    return Path(worktree_root) / "psychological" / "scrapers"


def patch_indeed_scraper_alias(scrapers_dir) -> bool:
    """Add an IndeedScraper alias to corp_audit.py when it is missing."""
    corp_audit = Path(scrapers_dir) / "corp_audit.py"
    if not corp_audit.exists():
        return False
    text = corp_audit.read_text(encoding="utf-8")
    if "IndeedScraper" in text:
        return False
    alias = "\n\nclass IndeedScraper(GlassdoorScraper):\n    pass\n"
    corp_audit.write_text(text + alias, encoding="utf-8")
    logger.info(f"Patched IndeedScraper alias into {corp_audit}")
    return True


def patch_async_context_manager(scrapers_dir) -> bool:
    """Add __aenter__/__aexit__ to the first scraper class missing them."""
    corp_anonymous = Path(scrapers_dir) / "corp_anonymous.py"
    if not corp_anonymous.exists():
        return False
    text = corp_anonymous.read_text(encoding="utf-8")
    if "__aenter__" in text:
        return False
    lines = text.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if line.startswith("class "):
            methods = (
                "    async def __aenter__(self):\n"
                "        return self\n"
                "    async def __aexit__(self, exc_type, exc, tb):\n"
                "        return False\n"
            )
            lines.insert(idx + 1, methods)
            corp_anonymous.write_text("".join(lines), encoding="utf-8")
            logger.info(f"Patched async context manager into {corp_anonymous}")
            return True
    return False


def repair_worktree(worktree_root: str, error_msg: str) -> bool:
    """Dispatch to the patcher matching the failure signature.

    Returns True when a repair was applied, False when nothing matched.
    """
    scrapers = _scrapers_dir(worktree_root)
    if "IndeedScraper" in error_msg:
        return patch_indeed_scraper_alias(scrapers)
    if "async context manager" in error_msg:
        return patch_async_context_manager(scrapers)
    logger.warning(f"No repair rule for: {error_msg}")
    return False
