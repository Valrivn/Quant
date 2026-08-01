"""IC drift detection for the sentiment pipeline.

Compares the champion weight version's IC against recent recorded IC scores
and flags drift when the gap exceeds a decay threshold.
"""
import logging
import time
from typing import Any, Dict, List

import numpy as np

logger = logging.getLogger(__name__)


def _load_weight_versions() -> List[Dict[str, Any]]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT version_id, ic_score, sharpe_ratio, hit_rate, created_at, promoted_at
            FROM weight_versions
        """)
        return [dict(row) for row in cursor.fetchall()]
    except Exception as exc:
        logger.warning(f"Failed to read weight_versions: {exc}")
        return []


def check_ic_drift_and_reoptimize(
    decay_threshold: float = 0.20,
    recent_window_days: int = 60,
) -> Dict[str, Any]:
    """Detect whether recent IC has decayed materially below the champion IC.

    Returns a dict with at least the key 'drift_detected' (bool).
    """
    rows = _load_weight_versions()
    if not rows:
        return {"drift_detected": False, "champion_ic": None, "recent_ic": None}

    valid = [r for r in rows if r.get("ic_score") is not None]
    if not valid:
        return {"drift_detected": False, "champion_ic": None, "recent_ic": None}

    champion_ic = float(max(r["ic_score"] for r in valid))
    cutoff = time.time() - recent_window_days * 86400
    recent = [r["ic_score"] for r in valid if (r.get("created_at") or 0) >= cutoff]
    recent_ic = float(np.mean(recent)) if recent else champion_ic

    drift_detected = bool(champion_ic - recent_ic > decay_threshold)
    if drift_detected:
        logger.warning(
            f"IC drift detected: champion {champion_ic:.4f} vs recent {recent_ic:.4f} "
            f"(threshold {decay_threshold})"
        )
    return {
        "drift_detected": drift_detected,
        "champion_ic": champion_ic,
        "recent_ic": recent_ic,
    }
