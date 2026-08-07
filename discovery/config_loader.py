"""Discovery config loader + strict validation (D-20260806-001, invariant 4).

Reads ``config/weights_discovery.yaml`` (the ONLY new config file) and validates
it fail-closed: unknown keys, NaN, non-normalized positive weights, or a missing
required key => load error (never silently defaulted). All ranker weights,
sanitizer thresholds, and caps come from this file — nothing is hard-coded.
"""

import math
import os
from typing import Any, Dict

import yaml

_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "weights_discovery.yaml"
)

# Top-level allowed keys.
_ALLOWED_TOP = {"discovery", "ranker", "sanitizers", "caps", "concepts"}

# Required keys per section (fail closed if missing).
_REQUIRED = {
    "discovery": {"enabled"},
    "ranker": {
        "source_weights", "w_vel", "w_cross", "w_topic",
        "penalty_weights", "norm_rank", "velocity_window_days",
        "velocity_trailing_days", "topic_priority", "sanitizer_apply",
    },
    "sanitizers": {"clout_chaser", "niche", "ad"},
    "caps": {"max_candidates_per_cycle", "max_position_share_per_cycle", "top_k"},
    "concepts": {"default_status"},
}

# Allowed keys per section (unknown key => load error).
_ALLOWED_NESTED = {
    "ranker": {
        "source_weights", "w_vel", "w_cross", "w_topic", "penalty_weights",
        "norm_rank", "velocity_window_days", "velocity_trailing_days",
        "topic_priority", "sanitizer_apply",
    },
    "penalty_weights": {"w_ad", "w_clout"},
    "sanitizer_apply": {"ad", "clout"},
    "clout_chaser": {
        "runup_ratio_floor", "mention_velocity_floor", "explosion_lag_min_days",
    },
    "niche": {"views_ceiling", "comment_to_view_band", "view_to_follower_band"},
    "ad": {
        "hashtags", "affiliate_patterns", "brand_account_signals",
        "engagement_anomaly_bands",
    },
    "engagement_anomaly_bands": {"comment_to_view", "view_to_follower"},
    "caps": {"max_candidates_per_cycle", "max_position_share_per_cycle", "top_k"},
    "concepts": {"default_status"},
    "discovery": {"enabled"},
}


class DiscoveryConfigError(ValueError):
    """Raised when the discovery config fails validation (fail closed)."""


def _check_contains_keys(cfg: Dict, allowed: set, where: str) -> None:
    unknown = set(cfg.keys()) - allowed
    if unknown:
        raise DiscoveryConfigError(
            f"unknown key(s) in {where}: {sorted(unknown)}"
        )


def _check_required(cfg: Dict, required: set, where: str) -> None:
    missing = required - set(cfg.keys())
    if missing:
        raise DiscoveryConfigError(
            f"missing required key(s) in {where}: {sorted(missing)}"
        )


def _check_nan(value: Any, where: str) -> None:
    if isinstance(value, float) and math.isnan(value):
        raise DiscoveryConfigError(f"NaN value at {where}")


def _validate_ranker(ranker: Dict) -> None:
    _check_required(ranker, _REQUIRED["ranker"], "ranker")
    _check_contains_keys(ranker, _ALLOWED_NESTED["ranker"], "ranker")

    sw = ranker["source_weights"]
    if not isinstance(sw, dict) or not sw:
        raise DiscoveryConfigError("ranker.source_weights must be a non-empty dict")
    for k, v in sw.items():
        _check_nan(v, f"ranker.source_weights.{k}")
        if not isinstance(v, (int, float)) or v < 0:
            raise DiscoveryConfigError(f"ranker.source_weights.{k} must be >= 0")

    for key in ("w_vel", "w_cross", "w_topic"):
        v = ranker[key]
        _check_nan(v, f"ranker.{key}")
        if not isinstance(v, (int, float)) or v < 0:
            raise DiscoveryConfigError(f"ranker.{key} must be >= 0")

    # Positive weights must sum to 1.0 (normalized).
    positive_sum = sum(sw.values()) + ranker["w_vel"] + ranker["w_cross"] + ranker["w_topic"]
    if abs(positive_sum - 1.0) > 1e-6:
        raise DiscoveryConfigError(
            f"ranker positive weights must sum to 1.0, got {positive_sum}"
        )

    pw = ranker["penalty_weights"]
    _check_contains_keys(pw, _ALLOWED_NESTED["penalty_weights"], "ranker.penalty_weights")
    for k in ("w_ad", "w_clout"):
        v = pw[k]
        _check_nan(v, f"ranker.penalty_weights.{k}")
        if not isinstance(v, (int, float)) or v < 0:
            raise DiscoveryConfigError(f"ranker.penalty_weights.{k} must be >= 0")

    sa = ranker["sanitizer_apply"]
    _check_contains_keys(sa, _ALLOWED_NESTED["sanitizer_apply"], "ranker.sanitizer_apply")
    for k, mode in sa.items():
        if mode not in ("exclude", "penalty"):
            raise DiscoveryConfigError(f"ranker.sanitizer_apply.{k} must be 'exclude' or 'penalty'")

    tp = ranker["topic_priority"]
    if not isinstance(tp, list) or len(tp) != 8 or len(set(tp)) != 8:
        raise DiscoveryConfigError("ranker.topic_priority must be 8 unique topics")


def _check_sanitizers(san: Dict) -> None:
    _check_required(san, _REQUIRED["sanitizers"], "sanitizers")
    _check_contains_keys(san, {"clout_chaser", "niche", "ad"}, "sanitizers")

    cc = san["clout_chaser"]
    _check_contains_keys(cc, _ALLOWED_NESTED["clout_chaser"], "sanitizers.clout_chaser")
    for k in ("runup_ratio_floor", "mention_velocity_floor", "explosion_lag_min_days"):
        v = cc[k]
        _check_nan(v, f"sanitizers.clout_chaser.{k}")
        if not isinstance(v, (int, float)):
            raise DiscoveryConfigError(f"sanitizers.clout_chaser.{k} must be numeric")

    niche = san["niche"]
    _check_contains_keys(niche, _ALLOWED_NESTED["niche"], "sanitizers.niche")
    _check_nan(niche["views_ceiling"], "sanitizers.niche.views_ceiling")
    for band in ("comment_to_view_band", "view_to_follower_band"):
        b = niche[band]
        if not (isinstance(b, list) and len(b) == 2 and b[0] <= b[1]):
            raise DiscoveryConfigError(f"sanitizers.niche.{band} must be [lo, hi]")

    ad = san["ad"]
    _check_contains_keys(ad, _ALLOWED_NESTED["ad"], "sanitizers.ad")
    for k in ("hashtags", "affiliate_patterns", "brand_account_signals"):
        if not isinstance(ad[k], list) or not ad[k]:
            raise DiscoveryConfigError(f"sanitizers.ad.{k} must be a non-empty list")
    eab = ad["engagement_anomaly_bands"]
    _check_contains_keys(eab, _ALLOWED_NESTED["engagement_anomaly_bands"], "sanitizers.ad.engagement_anomaly_bands")
    for band in ("comment_to_view", "view_to_follower"):
        b = eab[band]
        if not (isinstance(b, list) and len(b) == 2 and b[0] <= b[1]):
            raise DiscoveryConfigError(f"sanitizers.ad.engagement_anomaly_bands.{band} must be [lo, hi]")


def _check_caps(caps: Dict) -> None:
    _check_required(caps, _REQUIRED["caps"], "caps")
    _check_contains_keys(caps, _ALLOWED_NESTED["caps"], "caps")
    for k in ("max_candidates_per_cycle", "max_position_share_per_cycle", "top_k"):
        v = caps[k]
        _check_nan(v, f"caps.{k}")
        if not isinstance(v, (int, float)) or v <= 0:
            raise DiscoveryConfigError(f"caps.{k} must be > 0")


def load_discovery_config(path: str = None) -> Dict[str, Any]:
    """Load and strictly validate the discovery config. Raises on any problem."""
    cfg_path = path or _CONFIG_PATH
    if not os.path.exists(cfg_path):
        raise DiscoveryConfigError(f"config file not found: {cfg_path}")
    with open(cfg_path, "r") as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise DiscoveryConfigError("config root must be a mapping")
    _check_contains_keys(cfg, _ALLOWED_TOP, "config root")
    _check_required(cfg, _REQUIRED.keys(), "config root")

    _check_nan(cfg["discovery"]["enabled"], "discovery.enabled")
    _validate_ranker(cfg["ranker"])
    _check_sanitizers(cfg["sanitizers"])
    _check_caps(cfg["caps"])
    return cfg