"""Sentinel config loader — fail-closed, pre-registered thresholds.

Mirrors ``discovery/config_loader.py``: unknown top-level keys and missing
required keys raise, so the funnel never silently defaults a gate to pass.
"""

import math
import os
from typing import Any, Dict

import yaml

_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "sentinel.yaml"
)

_ALLOWED_TOP = {"sentinel"}
_REQUIRED_TOP = {"sentinel"}
_REQUIRED_SENTINEL = {
    "enabled", "db_path", "queue", "gates", "lanes", "governor", "frontier",
}

_FRONTIER_REQUIRED = {
    "seed_tickers", "competitor_set", "max_depth", "max_nodes_per_seed",
    "max_edges_per_node", "block_size", "inter_block_gap_seconds",
    "max_active_hours",
}


class SentinelConfigError(ValueError):
    """Raised on invalid sentinel config (fail closed)."""


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise SentinelConfigError(msg)


def _require(cfg: Dict, keys, where: str) -> None:
    missing = set(keys) - set(cfg.keys())
    _check(not missing, f"missing required key(s) in {where}: {sorted(missing)}")


def _check_nan(value: Any, where: str) -> None:
    if isinstance(value, float) and math.isnan(value):
        raise SentinelConfigError(f"NaN value at {where}")


def _validate_gates(gates: Dict) -> None:
    _require(gates, {"g1_survival", "g2_fundamentals", "g3_altdata", "g4_enrich"}, "gates")
    g1 = gates["g1_survival"]
    _require(g1, {"altman_z_floor", "cash_runway_quarters_floor", "min_quarters_data"}, "gates.g1_survival")
    _check(g1["cash_runway_quarters_floor"] >= 1, "g1.cash_runway_quarters_floor must be >= 1")
    g2 = gates["g2_fundamentals"]
    _require(g2, {"ocf_positive_quarters", "gross_margin_floor", "capex_tracked_quarters", "min_quarters_trend"}, "gates.g2_fundamentals")
    _check(0.0 <= g2["gross_margin_floor"] <= 1.0, "g2.gross_margin_floor must be in [0,1]")
    g3 = gates["g3_altdata"]
    _require(g3, {"reddit_z_floor", "reddit_window_days", "github_star_growth_floor", "github_lookback_days", "min_coverage_sources"}, "gates.g3_altdata")
    _check(g3["min_coverage_sources"] >= 1, "g3.min_coverage_sources must be >= 1")
    g4 = gates["g4_enrich"]
    _require(g4, {"jina_base_url", "max_text_chars"}, "gates.g4_enrich")


def _validate_lanes(lanes: Dict) -> None:
    _require(lanes, {"sec", "altdata", "enrich", "ig", "github"}, "lanes")
    for name in ("sec", "altdata", "enrich", "ig", "github"):
        lane = lanes[name]
        _check_nan(lane.get("rate_per_second", 0.0), f"lanes.{name}.rate_per_second")
        _check(lane.get("rate_per_second", 0.0) > 0, f"lanes.{name}.rate_per_second must be > 0")


def _validate_governor(gov: Dict) -> None:
    _require(gov, {"circuit_failure_threshold", "circuit_success_threshold", "circuit_timeout_seconds"}, "governor")
    _check(gov["circuit_failure_threshold"] >= 1, "governor.circuit_failure_threshold must be >= 1")


def _validate_frontier(fr: Dict) -> None:
    _require(fr, _FRONTIER_REQUIRED, "frontier")
    seeds = fr["seed_tickers"]
    _check(isinstance(seeds, list) and len(seeds) >= 1, "frontier.seed_tickers must be a non-empty list")
    _check(all(isinstance(t, str) for t in seeds), "frontier.seed_tickers must be strings")
    comp = fr["competitor_set"]
    _check(isinstance(comp, list) and len(comp) >= 1, "frontier.competitor_set must be a non-empty list")
    for k in ("max_depth", "max_nodes_per_seed", "max_edges_per_node", "block_size"):
        _check(isinstance(fr[k], int) and fr[k] >= 1, f"frontier.{k} must be an int >= 1")
    _check(isinstance(fr["max_active_hours"], (int, float)) and fr["max_active_hours"] > 0,
           "frontier.max_active_hours must be > 0")
    gap = fr["inter_block_gap_seconds"]
    _check(isinstance(gap, list) and len(gap) == 2 and gap[0] >= 0 and gap[1] >= gap[0],
           "frontier.inter_block_gap_seconds must be [lo, hi] with 0 <= lo <= hi")


def load_sentinel_config(path: str = None) -> Dict[str, Any]:
    """Load and strictly validate sentinel.yaml. Raises on any problem."""
    cfg_path = path or _CONFIG_PATH
    if not os.path.exists(cfg_path):
        raise SentinelConfigError(f"config file not found: {cfg_path}")
    with open(cfg_path, "r") as f:
        cfg = yaml.safe_load(f)
    _check(isinstance(cfg, dict), "config root must be a mapping")
    _require(cfg, _REQUIRED_TOP, "config root")
    _check(set(cfg.keys()) == _ALLOWED_TOP, f"unknown top-level keys: {sorted(set(cfg.keys()) - _ALLOWED_TOP)}")

    s = cfg["sentinel"]
    _require(s, _REQUIRED_SENTINEL, "sentinel")
    _check(isinstance(s["enabled"], bool), "sentinel.enabled must be a bool")
    _require(s["queue"], {"batch_size", "max_attempts"}, "sentinel.queue")
    _check(s["queue"]["batch_size"] >= 1, "sentinel.queue.batch_size must be >= 1")
    _validate_gates(s["gates"])
    _validate_lanes(s["lanes"])
    _validate_governor(s["governor"])
    _validate_frontier(s["frontier"])
    return cfg


def get_sentinel_config(path: str = None) -> Dict[str, Any]:
    """Memoized loader used by callers that do not want to re-validate."""
    return load_sentinel_config(path)
