"""Lane Epsilon summary generation.

Compiles the Opus 4.6 architectural summary from the per-lane artifacts written
to ``center/``. The canonical report lives at ``center/lane_summary.md``.
"""
import logging
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SUMMARY_PATH = PROJECT_ROOT / "center" / "lane_summary.md"

_LANE_NAMES = [
    "Lane Alpha",
    "Lane Beta",
    "Lane Gamma",
    "Lane Delta",
    "Lane Epsilon",
]

_HEADER = "# Comprehensive Parallel Sweeps & Architectural Summary Report (Opus 4.6)\n"


def _load_lane_summary() -> str:
    if SUMMARY_PATH.exists():
        return SUMMARY_PATH.read_text(encoding="utf-8")
    logger.warning(f"lane_summary.md not found at {SUMMARY_PATH}")
    return ""


def build_opus_summary_content() -> str:
    """Return the full Opus 4.6 summary report text."""
    base = _load_lane_summary()
    if base:
        return base
    lines = [_HEADER, "", "## 1. Overview", ""]
    for lane in _LANE_NAMES:
        lines.append(f"- {lane}: pending")
    lines.append("")
    lines.append("## 2. Active Asset Conviction Ratings")
    lines.append("")
    lines.append("No conviction ratings computed yet.")
    return "\n".join(lines)


def _write_summary(content: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def main() -> int:
    """Regenerate the lane summary report into center/."""
    content = build_opus_summary_content()
    _write_summary(content, SUMMARY_PATH)
    logger.info(f"Wrote summary report to {SUMMARY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
