"""Lane Alpha math-engine entry point.

Re-exports the canonical implementations from the psychological pillar so the
Lane Alpha audit suite can import them from ``opencode_scripts.qualitative_scoring``.
"""
import sys
from pathlib import Path

_QUAL_DIR = Path(__file__).resolve().parents[1] / "Qualitative"
if str(_QUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_QUAL_DIR))

from psychological.qualitative_scoring import (  # noqa: E402
    EMAFilter,
    DoubleStandardizer,
    SubSectorConfig,
    tanh_clamp,
)

__all__ = ["EMAFilter", "DoubleStandardizer", "SubSectorConfig", "tanh_clamp"]
