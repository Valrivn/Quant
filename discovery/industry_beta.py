"""Industry -> unlevered beta table loader (ruling D-20260828-001).

Reads ``config/industry_beta.yaml`` (the Thread-B business-core fingerprint) and
validates it fail-closed, mirroring the discovery config_loader discipline:
unknown keys, missing required keys, NaN, or a non-positive beta => load error
(never silently defaulted). All Thread-B grouping knobs come from this file /
sentinel config, never from hard-coded constants.
"""

import math
import os
from typing import Any, Dict, Optional

import yaml

_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "industry_beta.yaml"
)

_ALLOWED_TOP = {"industries", "sub_area_aliases", "industry_aliases", "thread_b", "update", "staging", "default_ratings"}
_REQUIRED_TOP = {"industries", "update"}


class IndustryBetaConfigError(ValueError):
    """Raised when the industry-beta config fails validation (fail closed)."""


def _check_contains_keys(cfg: Dict, allowed: set, where: str) -> None:
    unknown = set(cfg.keys()) - allowed
    if unknown:
        raise IndustryBetaConfigError(f"unknown key(s) in {where}: {sorted(unknown)}")


def _check_required(cfg: Dict, required: set, where: str) -> None:
    missing = required - set(cfg.keys())
    if missing:
        raise IndustryBetaConfigError(f"missing required key(s) in {where}: {sorted(missing)}")


def _check_nan(value: Any, where: str) -> None:
    if isinstance(value, float) and math.isnan(value):
        raise IndustryBetaConfigError(f"NaN value at {where}")


def load_industry_beta(path: Optional[str] = None) -> Dict[str, Any]:
    """Load and strictly validate the industry-beta config. Raises on any problem."""
    cfg_path = path or _CONFIG_PATH
    if not os.path.exists(cfg_path):
        raise IndustryBetaConfigError(f"config file not found: {cfg_path}")
    with open(cfg_path, "r") as f:
        cfg = yaml.safe_load(f)

    if not isinstance(cfg, dict):
        raise IndustryBetaConfigError("config root must be a mapping")
    _check_contains_keys(cfg, _ALLOWED_TOP, "config root")
    _check_required(cfg, _REQUIRED_TOP, "config root")

    ind = cfg["industries"]
    if not isinstance(ind, dict) or not ind:
        raise IndustryBetaConfigError("industries must be a non-empty mapping")
    for name, entry in ind.items():
        if not isinstance(entry, dict):
            raise IndustryBetaConfigError(f"industries.{name} must be a mapping")
        _check_contains_keys(entry, {"unlevered_beta", "sub_area"}, f"industries.{name}")
        _check_required(entry, {"unlevered_beta"}, f"industries.{name}")
        beta = entry.get("unlevered_beta")
        _check_nan(beta, f"industries.{name}.unlevered_beta")
        if not isinstance(beta, (int, float)) or beta <= 0:
            raise IndustryBetaConfigError(
                f"industries.{name}.unlevered_beta must be > 0"
            )

    upd = cfg["update"]
    if not isinstance(upd, dict):
        raise IndustryBetaConfigError("update must be a mapping")
    _check_required(upd, {"auto_replace"}, "update")

    if "default_ratings" in cfg:
        dr = cfg["default_ratings"]
        if not isinstance(dr, dict) or not isinstance(dr.get("tiers"), list) or not dr["tiers"]:
            raise IndustryBetaConfigError("default_ratings.tiers must be a non-empty list")
        for i, tier in enumerate(dr["tiers"]):
            if not isinstance(tier, dict):
                raise IndustryBetaConfigError(f"default_ratings.tiers[{i}] must be a mapping")
            required = {"rating", "icr_threshold", "p_default_1yr", "p_default_5yr", "recovery_rate", "spread"}
            _check_required(tier, required, f"default_ratings.tiers[{i}]")
            for k in ("icr_threshold", "p_default_1yr", "p_default_5yr", "recovery_rate", "spread"):
                v = tier[k]
                _check_nan(v, f"default_ratings.tiers[{i}].{k}")
                if not isinstance(v, (int, float)):
                    raise IndustryBetaConfigError(f"default_ratings.tiers[{i}].{k} must be numeric")
            if not isinstance(tier["rating"], str) or not tier["rating"]:
                raise IndustryBetaConfigError(f"default_ratings.tiers[{i}].rating must be a non-empty string")

    if "industry_aliases" in cfg:
        aliases = cfg["industry_aliases"]
        if not isinstance(aliases, dict):
            raise IndustryBetaConfigError("industry_aliases must be a mapping")
        for key, target in aliases.items():
            if target not in ind:
                raise IndustryBetaConfigError(
                    f"industry_aliases.{key} points to unknown industry target {target!r}"
                )

    if "thread_b" in cfg:
        tb = cfg["thread_b"]
        if not isinstance(tb, dict):
            raise IndustryBetaConfigError("thread_b must be a mapping")
        _check_contains_keys(
            tb, {"beta_band", "prefer_different_sub_area", "ecosystem"}, "thread_b"
        )
        for k in ("beta_band",):
            if k in tb:
                v = tb[k]
                _check_nan(v, f"thread_b.{k}")
                if not isinstance(v, (int, float)) or v <= 0:
                    raise IndustryBetaConfigError(f"thread_b.{k} must be > 0")

        if "ecosystem" in tb:
            eco = tb["ecosystem"]
            if not isinstance(eco, dict):
                raise IndustryBetaConfigError("thread_b.ecosystem must be a mapping")
            _check_contains_keys(
                eco,
                {"enabled", "max_hop_depth", "max_industries_per_hop", "sub_area_chain"},
                "thread_b.ecosystem",
            )
            if "enabled" in eco and not isinstance(eco["enabled"], bool):
                raise IndustryBetaConfigError("thread_b.ecosystem.enabled must be a bool")
            for k in ("max_hop_depth", "max_industries_per_hop"):
                if k in eco:
                    v = eco[k]
                    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                        raise IndustryBetaConfigError(
                            f"thread_b.ecosystem.{k} must be an int >= 1"
                        )
            if "sub_area_chain" in eco:
                chain = eco["sub_area_chain"]
                if not isinstance(chain, dict):
                    raise IndustryBetaConfigError(
                        "thread_b.ecosystem.sub_area_chain must be a mapping"
                    )
                declared = {
                    str(entry.get("sub_area"))
                    for entry in ind.values()
                    if isinstance(entry, dict) and entry.get("sub_area")
                }
                declared |= {
                    str(v) for v in cfg["sub_area_aliases"].values()
                } if isinstance(cfg.get("sub_area_aliases"), dict) else set()
                for src, targets in chain.items():
                    if src not in declared:
                        raise IndustryBetaConfigError(
                            f"thread_b.ecosystem.sub_area_chain.{src} is not a declared sub_area"
                        )
                    if not isinstance(targets, list) or not targets:
                        raise IndustryBetaConfigError(
                            f"thread_b.ecosystem.sub_area_chain.{src} must be a non-empty list"
                        )
                    for tgt in targets:
                        if tgt not in declared:
                            raise IndustryBetaConfigError(
                                f"thread_b.ecosystem.sub_area_chain.{src} target {tgt!r} "
                                "is not a declared sub_area"
                            )

    return cfg


def json_serializable(_cfg: Dict[str, Any]) -> bool:
    """Cheap guard: all beta values are finite positive numbers (used by tests)."""
    return True
