"""Consensus gate config loader + strict validation (D-20260816-001, P1).

Reads ``config/weights_consensus.yaml`` (the ONLY new config file for the
anti-bias consensus gate) and validates it fail-closed: unknown keys, NaN,
non-normalized positive weights, or a missing required key => load error (never
silently defaulted). Mirrors ``discovery/config_loader.py``. The frozen weight
sheet + all flag/usability thresholds come from this file — nothing is
hard-coded in the engine.
"""

import math
import os
from typing import Any, Dict

import yaml

_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "weights_consensus.yaml"
)

_ALLOWED_TOP = {"consensus", "weights", "usability", "flags", "modeled", "sector", "talent"}

_REQUIRED = {
    "consensus": {"enabled"},
    "weights": {"blocks", "factors"},
    "usability": {
        "insufficient_threshold", "directional_threshold",
        "distributional_threshold", "usable_threshold", "set_aside_threshold",
    },
    "flags": {"bribe_attack", "punishing_attack", "polarized"},
    "modeled": {"min_monthly_visits"},
    "sector": {"min_companies_for_two_extreme"},
    "talent": {"enabled", "surfaces", "min_senior_mentions"},
}

_ALLOWED_NESTED = {
    "weights": {"blocks", "factors"},
    "blocks": {"quantifiable", "expression", "subjective"},
    "bribe_attack": {
        "min_same_star_burst", "burst_window_hours", "suspicious_profile_ratio",
    },
    "punishing_attack": {
        "weekly_volume_spike_ratio", "novice_profile_ratio", "coordinated_language",
    },
    "polarized": {
        "skewness_abs_floor", "bimodal_star_gap", "bimodal_min_cluster",
        "min_converging_sources",
    },
    "talent": {"enabled", "surfaces", "min_senior_mentions"},
}


class ConsensusConfigError(ValueError):
    """Raised when the consensus config fails validation (fail closed)."""


def _check_contains_keys(cfg: Dict, allowed: set, where: str) -> None:
    unknown = set(cfg.keys()) - allowed
    if unknown:
        raise ConsensusConfigError(f"unknown key(s) in {where}: {sorted(unknown)}")


def _check_required(cfg: Dict, required: set, where: str) -> None:
    missing = required - set(cfg.keys())
    if missing:
        raise ConsensusConfigError(f"missing required key(s) in {where}: {sorted(missing)}")


def _check_nan(value: Any, where: str) -> None:
    if isinstance(value, float) and math.isnan(value):
        raise ConsensusConfigError(f"NaN value at {where}")


def _check_positive_num(value: Any, where: str) -> None:
    _check_nan(value, where)
    if not isinstance(value, (int, float)) or value <= 0:
        raise ConsensusConfigError(f"{where} must be a positive number, got {value!r}")


def _check_fraction(value: Any, where: str) -> None:
    _check_nan(value, where)
    if not isinstance(value, (int, float)) or not (0.0 <= value <= 1.0):
        raise ConsensusConfigError(f"{where} must be in [0,1], got {value!r}")


def _validate_weights(weights: Dict) -> Dict[str, Dict[str, float]]:
    _check_required(weights, _REQUIRED["weights"], "weights")
    _check_contains_keys(weights, _ALLOWED_NESTED["weights"], "weights")

    blocks = weights["blocks"]
    _check_contains_keys(blocks, _ALLOWED_NESTED["blocks"], "weights.blocks")
    block_sum = 0.0
    for k in ("quantifiable", "expression", "subjective"):
        _check_fraction(blocks[k], f"weights.blocks.{k}")
        block_sum += blocks[k]
    if abs(block_sum - 1.0) > 1e-6:
        raise ConsensusConfigError(
            f"weights.blocks must sum to 1.0, got {block_sum}"
        )

    factors = weights["factors"]
    expected = {
        "quantifiable": {"transaction_volume", "sec_attrition_velocity",
                         "hiring_velocity", "review_volume"},
        "expression": {"talent_capture", "review_skewness", "sentiment_breadth"},
        "subjective": {"exec_approval", "culture_values", "comp_equity", "work_life"},
    }
    for block, keys in expected.items():
        f = factors.get(block)
        if not isinstance(f, dict):
            raise ConsensusConfigError(f"weights.factors.{block} must be a dict")
        _check_contains_keys(f, keys, f"weights.factors.{block}")
        _check_required(f, keys, f"weights.factors.{block}")
        total = 0.0
        for k in keys:
            _check_fraction(f[k], f"weights.factors.{block}.{k}")
            total += f[k]
        if abs(total - 1.0) > 1e-6:
            raise ConsensusConfigError(
                f"weights.factors.{block} must sum to 1.0, got {total}"
            )
    return factors


def _validate_usability(us: Dict) -> None:
    _check_required(us, _REQUIRED["usability"], "usability")
    _check_contains_keys(us, _REQUIRED["usability"], "usability")
    for k in _REQUIRED["usability"]:
        _check_positive_num(us[k], f"usability.{k}")
    if not (us["insufficient_threshold"] <= us["directional_threshold"] <=
            us["distributional_threshold"] <= us["usable_threshold"]):
        raise ConsensusConfigError(
            "usability thresholds must be non-decreasing "
            "(insufficient <= directional <= distributional <= usable)"
        )


def _validate_flags(flags: Dict) -> None:
    _check_required(flags, _REQUIRED["flags"], "flags")
    for group in ("bribe_attack", "punishing_attack", "polarized"):
        _check_contains_keys(flags[group], _ALLOWED_NESTED[group], f"flags.{group}")

    ba = flags["bribe_attack"]
    _check_positive_num(ba["min_same_star_burst"], "flags.bribe_attack.min_same_star_burst")
    _check_positive_num(ba["burst_window_hours"], "flags.bribe_attack.burst_window_hours")
    _check_fraction(ba["suspicious_profile_ratio"], "flags.bribe_attack.suspicious_profile_ratio")

    pa = flags["punishing_attack"]
    _check_positive_num(pa["weekly_volume_spike_ratio"], "flags.punishing_attack.weekly_volume_spike_ratio")
    _check_fraction(pa["novice_profile_ratio"], "flags.punishing_attack.novice_profile_ratio")
    if not isinstance(pa["coordinated_language"], bool):
        raise ConsensusConfigError("flags.punishing_attack.coordinated_language must be bool")

    pol = flags["polarized"]
    _check_positive_num(pol["skewness_abs_floor"], "flags.polarized.skewness_abs_floor")
    _check_positive_num(pol["bimodal_star_gap"], "flags.polarized.bimodal_star_gap")
    _check_positive_num(pol["bimodal_min_cluster"], "flags.polarized.bimodal_min_cluster")
    _check_positive_num(pol["min_converging_sources"], "flags.polarized.min_converging_sources")


def _validate_modeled_sector_talent(cfg: Dict) -> None:
    _check_positive_num(cfg["modeled"]["min_monthly_visits"], "modeled.min_monthly_visits")
    _check_positive_num(cfg["sector"]["min_companies_for_two_extreme"],
                        "sector.min_companies_for_two_extreme")
    t = cfg["talent"]
    _check_contains_keys(t, _ALLOWED_NESTED["talent"], "talent")
    if not isinstance(t["enabled"], bool):
        raise ConsensusConfigError("talent.enabled must be bool")
    if not (isinstance(t["surfaces"], list) and t["surfaces"]):
        raise ConsensusConfigError("talent.surfaces must be a non-empty list")
    _check_positive_num(t["min_senior_mentions"], "talent.min_senior_mentions")


def load_consensus_config(path: str = None) -> Dict[str, Any]:
    """Load and strictly validate the consensus config. Raises on any problem."""
    cfg_path = path or _CONFIG_PATH
    if not os.path.exists(cfg_path):
        raise ConsensusConfigError(f"config file not found: {cfg_path}")
    with open(cfg_path, "r") as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise ConsensusConfigError("config root must be a mapping")
    _check_contains_keys(cfg, _ALLOWED_TOP, "config root")
    _check_required(cfg, set(_REQUIRED.keys()), "config root")

    if not isinstance(cfg["consensus"]["enabled"], bool):
        raise ConsensusConfigError("consensus.enabled must be bool")

    _validate_weights(cfg["weights"])
    _validate_usability(cfg["usability"])
    _validate_flags(cfg["flags"])
    _validate_modeled_sector_talent(cfg)
    return cfg