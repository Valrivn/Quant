"""Central configuration package.

Two independent configuration concerns live here.  Do not conflate them:

1. Legacy weight/taxonomy config (``.weights``, ``.constants``) — unchanged.
2. Arm A refactor parameter registry (:func:`get_config` and friends) — the
   65/35 composite reweight, vol targeting, turnover controls, graded
   de-risking, tail hedge and cost model.

THE TWO REGISTRIES ARE NOT THE SAME FILE
----------------------------------------
``ARM_A_CONFIG_PATH``   the Arm A parameter registry (DRAFT, awaiting CEO
                        approval).  Default target of :func:`get_config`.
``LEGACY_PREREG_PATH``  the Phase 9 Unified Regime contract, v7.0, status
                        APPROVED, SHA-256 pinned.  Read it through
                        :func:`ingestion.harness.prereg.load_prereg`, which
                        verifies the digest on every load.  :func:`get_config`
                        will read it if you pass the path explicitly, but this
                        package deliberately does NOT make it the default: v7
                        is frozen and must keep verifying.

Why the separation matters
--------------------------
The Arm A plan was drafted against "edit config/pre_registration_v7.yaml".  v7
turned out to be an already-APPROVED, SHA-256-pinned artifact holding a
different contract, pinned in code and asserted by tests.  Writing the Arm A
parameters into it would have destroyed the approved record and broken the
provenance chain, so the parameters went to their own hash-pinned file instead.

DOCTRINE: a missing key RAISES
------------------------------
Accessors below raise :class:`ConfigKeyError` when a declared threshold is
absent.  They never fall back to a literal.  This mirrors
``ingestion.harness.prereg``, whose stated rule is that "reading a threshold
that the frozen document no longer declares must raise, not default" — a
silent default is how a pre-registered parameter quietly stops being
pre-registered.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .weights import (load_weights, SUBREDDIT_TAXONOMY, CATEGORY_WEIGHTS, OPTIMIZATION_CONFIG, 
                      load_hybrid_config, load_fintech_credentials, load_hybrid_weights,
                      HYBRID_SOURCE_WEIGHTS, HYBRID_CATEGORY_WEIGHTS, HYBRID_SUBREDDIT_WEIGHTS,
                      HYBRID_FUSION_PARAMS, HYBRID_OPTIMIZATION_CONFIG)
from .constants import TICKER_BLACKLIST, VALIDATION_KEYWORDS, FINANCIAL_LEXICON, ENTITY_RESOLUTION, RISK_KEYWORDS

# ---------------------------------------------------------------------------
# Registry locations
# ---------------------------------------------------------------------------

#: Arm A refactor parameter registry (this workstream's deliverable).
ARM_A_CONFIG_PATH = "config/arm_a_refactor_prereg.yaml"

#: Phase 9 Unified Regime contract — APPROVED, frozen, digest-pinned.
#: Read via ``ingestion.harness.prereg.load_prereg()`` so the hash is checked.
LEGACY_PREREG_PATH = "config/pre_registration_v7.yaml"

#: Digest of LEGACY_PREREG_PATH as pinned 2026-09-28.  Recorded here so a
#: caller who loads v7 through this package can still detect drift; the
#: authoritative pin lives in ``ingestion.harness.prereg.EXPECTED_SHA256_V7``.
LEGACY_PREREG_SHA256 = "c80a47bf6ae8d1c595f3b5e607c2588478bec0c47c4f0f97eb6c374b277a1685"

#: Sections every Arm A registry must declare.
REQUIRED_SECTIONS: Tuple[str, ...] = (
    "vol_targeting",
    "composite_weights",
    "turnover_controls",
    "market_event_detection",
    "tail_hedge",
    "cost_model",
)

#: Weight tolerance for the block/total consistency assertions.  The declared
#: blocks are exact decimals (0.20+0.15+0.15+0.15 == 0.65), so anything above
#: float noise is a real editing error, not a rounding artefact.
_WEIGHT_TOL = 1e-9


class ConfigError(RuntimeError):
    """Base class for Arm A configuration failures."""


class ConfigKeyError(ConfigError, KeyError):
    """A declared threshold is absent from the registry.

    Deliberately a ``KeyError`` as well as a ``ConfigError`` so existing
    ``except KeyError`` handlers around config lookups keep working.
    """

    def __init__(self, dotted: str, path: str) -> None:
        super().__init__(
            f"pre-registered key missing: {dotted!r} (in {path}). "
            f"Accessors do not default — a silent fallback would let a "
            f"pre-registered parameter stop being pre-registered without any "
            f"signal.")
        self.dotted = dotted
        self.path = path


class ConfigIntegrityError(ConfigError):
    """The registry's SHA-256 sidecar does not match the file on disk."""


# ---------------------------------------------------------------------------
# Path resolution + loading
# ---------------------------------------------------------------------------

def config_repo_root() -> Path:
    """
    Directory that owns ``config/``.

    Resolution order: an already-correct CWD, then the package's own parent
    (this file lives in ``<root>/config/__init__.py``), then a two-level walk.
    Relative config paths therefore resolve from any working directory, which
    matters because pytest, the harness and the CLI all start in different
    places.
    """
    pkg_parent = Path(__file__).resolve().parent.parent
    if (pkg_parent / "config").is_dir():
        return pkg_parent
    cwd = Path.cwd()
    if (cwd / "config").is_dir():
        return cwd
    here = Path(__file__).resolve()
    for cand in here.parents:
        if (cand / "config").is_dir():
            return cand
    return pkg_parent


def resolve_config_path(config_path: str) -> Path:
    """Resolve a registry path against CWD first, then the repo root."""
    p = Path(config_path)
    if p.is_absolute():
        return p
    cwd_cand = Path.cwd() / p
    if cwd_cand.exists():
        return cwd_cand
    root_cand = config_repo_root() / p
    if root_cand.exists():
        return root_cand
    return cwd_cand          # non-existent: let the loader raise clearly


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_config_integrity(config_path: str = ARM_A_CONFIG_PATH) -> Dict[str, Any]:
    """
    Compare the file against its bare-digest ``.sha256`` sidecar.

    The sidecar is optional — when absent the result reports
    ``status="NO_SIDECAR"`` rather than failing, because a registry without a
    pinned digest is merely unratified, whereas a registry whose digest
    disagrees is tampered.  Set ``require_sidecar=True`` for ratification
    checks.
    """
    path = resolve_config_path(config_path)
    out: Dict[str, Any] = {
        "path": str(path), "exists": path.exists(), "expected_sha256": None,
        "actual_sha256": None, "verified": None, "status": "MISSING",
    }
    if not path.exists():
        out["detail"] = f"config not found: {path}"
        return out
    actual = sha256_of_file(path)
    out["actual_sha256"] = actual
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.exists():
        out["status"] = "NO_SIDECAR"
        out["detail"] = (f"no sidecar digest at {sidecar.name}; file is unratified "
                         f"and may be edited without detection")
        return out
    expected = sidecar.read_text(encoding="utf-8").strip().lower()
    out["expected_sha256"] = expected
    out["verified"] = bool(expected == actual)
    out["status"] = "VERIFIED" if out["verified"] else "MISMATCH"
    out["detail"] = ("sha256 matches the pinned sidecar" if out["verified"] else
                     f"sha256 MISMATCH: sidecar {expected[:12]}..., "
                     f"file {actual[:12]}...")
    return out


@lru_cache(maxsize=8)
def _load_config_cached(config_path: str, verify: bool) -> Dict[str, Any]:
    path = resolve_config_path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config not found: {config_path} (resolved to {path}; repo root "
            f"is {config_repo_root()})")
    if verify:
        integ = verify_config_integrity(config_path)
        if integ["status"] == "MISMATCH":
            raise ConfigIntegrityError(
                f"{integ['detail']}\npath: {integ['path']}\nThe registry is "
                f"hash-pinned; a mismatch means it was edited after pinning and "
                f"needs a CEO re-approval, not a sidecar refresh.")
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    if not isinstance(cfg, dict):
        raise ConfigError(f"config did not parse to a mapping: {path}")
    _validate_registry(cfg, str(path))
    return cfg


def _validate_registry(cfg: Dict[str, Any], path: str) -> None:
    """
    Structural checks that must hold for ANY Arm A registry.

    Weight arithmetic is verified here rather than trusted: a typo in a weight
    is invisible at runtime (the composite still returns numbers) but silently
    changes the strategy, which is precisely the class of post-hoc edit a
    pre-registration exists to prevent.
    """
    missing = [s for s in REQUIRED_SECTIONS if s not in cfg]
    if missing:
        if len(missing) == 1:
            raise ConfigKeyError(missing[0], path)
        raise ConfigError(
            f"registry {path} is missing {len(missing)} required sections: "
            f"{missing}. Every Arm A registry must declare "
            f"{list(REQUIRED_SECTIONS)}."
            + (" Note that config/pre_registration_v7.yaml is NOT an Arm A "
               "registry — it is the Phase 9 Unified Regime contract. Read it "
               "with ingestion.harness.prereg.load_prereg()."
               if "composite_weights" in missing else ""))

    cw = cfg["composite_weights"]
    grand = 0.0
    for block in ("directional_alpha", "operational_health"):
        if block not in cw:
            raise ConfigKeyError(f"composite_weights.{block}", path)
        node = cw[block]
        if "components" not in node:
            raise ConfigKeyError(f"composite_weights.{block}.components", path)
        comp_sum = 0.0
        for name, w in node["components"].items():
            try:
                comp_sum += float(w)
            except (TypeError, ValueError):
                raise ConfigError(
                    f"composite_weights.{block}.components.{name} is not a "
                    f"number: {w!r} (in {path})") from None
        declared = node.get("total_weight")
        if declared is None:
            raise ConfigKeyError(f"composite_weights.{block}.total_weight", path)
        if abs(comp_sum - float(declared)) > 1e-6:
            raise ConfigError(
                f"composite_weights.{block}: components sum to {comp_sum:.6f} but "
                f"total_weight declares {float(declared):.6f} (in {path}). "
                f"The block weight and its components disagree.")
        grand += float(declared)
    if abs(grand - 1.0) > 1e-6:
        raise ConfigError(
            f"composite_weights total is {grand:.6f}, expected 1.0 (in {path}).")


def get_config(config_path: str = ARM_A_CONFIG_PATH,
               verify: bool = False) -> Dict[str, Any]:
    """
    Load and cache a registry.  Returns the full dict with all sections.

    Args:
        config_path: registry to read.  Defaults to :data:`ARM_A_CONFIG_PATH`.
            Pass :data:`LEGACY_PREREG_PATH` for the Phase 9 v7 Unified Regime
            contract — but prefer ``ingestion.harness.prereg.load_prereg()``
            for that one, since it enforces the digest on every read.
        verify: check the ``.sha256`` sidecar and raise on mismatch.  Off by
            default so day-to-day reads are not coupled to the pin; turn it on
            in ratification and CI provenance jobs.

    Raises:
        FileNotFoundError: the registry does not exist.
        ConfigIntegrityError: ``verify=True`` and the sidecar disagrees.
        ConfigKeyError: a required section is absent.
        ConfigError: the file is not a mapping, or its weight arithmetic fails.
    """
    return _load_config_cached(str(config_path), bool(verify))


def clear_config_cache() -> None:
    """Drop the registry cache (call after editing a registry in-process)."""
    _load_config_cached.cache_clear()


# ---------------------------------------------------------------------------
# Typed section accessors
# ---------------------------------------------------------------------------

def _require(cfg: Dict[str, Any], dotted: str, path: str) -> Any:
    """Walk a dotted path, raising :class:`ConfigKeyError` on any gap."""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigKeyError(dotted, path)
        node = node[part]
    return node


def get_vol_config(config_path: str = ARM_A_CONFIG_PATH) -> Dict[str, Any]:
    """Vol targeting block (consumed by ``portfolio/allocator.py``)."""
    path = resolve_config_path(config_path)
    return dict(_require(get_config(config_path), "vol_targeting", str(path)))


def get_composite_config(config_path: str = ARM_A_CONFIG_PATH) -> Dict[str, Any]:
    """The 65/35 composite weights block (consumed by ``gate_engine.py``)."""
    path = resolve_config_path(config_path)
    return dict(_require(get_config(config_path), "composite_weights", str(path)))


def get_turnover_config(config_path: str = ARM_A_CONFIG_PATH) -> Dict[str, Any]:
    """Turnover controls (deadband, swap gap, dwell, monthly cap)."""
    path = resolve_config_path(config_path)
    return dict(_require(get_config(config_path), "turnover_controls", str(path)))


def get_market_event_config(config_path: str = ARM_A_CONFIG_PATH) -> Dict[str, Any]:
    """
    The ``market_event_detection`` SUB-BLOCK (triggers, persistence gate,
    graded de-risking, re-entry embargo).

    READ THIS BEFORE CALLING Workstream A's regime classes.
    ``regime.MarketEventDetector``, ``regime.GradedDerisking`` and
    ``regime.ReEntryEmbargo`` each expect the WHOLE registry and reach for
    ``config["market_event_detection"]`` themselves.  Handing them the value
    returned by THIS function makes their lookup miss, and because all three
    fall back to hardcoded literals on a miss they will appear to work while
    running unparameterised defaults.  Pass them :func:`get_regime_config`.
    """
    path = resolve_config_path(config_path)
    return dict(_require(get_config(config_path), "market_event_detection", str(path)))


def get_regime_config(config_path: str = ARM_A_CONFIG_PATH) -> Dict[str, Any]:
    """
    The FULL registry, shaped for ``regime.*``.

    This is the correct argument for ``MarketEventDetector(config)``,
    ``GradedDerisking(config)`` and ``ReEntryEmbargo(config)``, each of which
    does ``config.get("market_event_detection", {})``.  It is also the
    constructor argument for anything else that wants the whole document.
    """
    return get_config(config_path)


def get_tail_hedge_config(config_path: str = ARM_A_CONFIG_PATH) -> Dict[str, Any]:
    """Tail hedge block (DD cap, vol-z de-lever trigger, de-lever speed)."""
    path = resolve_config_path(config_path)
    return dict(_require(get_config(config_path), "tail_hedge", str(path)))


def get_cost_config(config_path: str = ARM_A_CONFIG_PATH) -> Dict[str, Any]:
    """Cost model block (round-trip bps, slippage bps)."""
    path = resolve_config_path(config_path)
    return dict(_require(get_config(config_path), "cost_model", str(path)))


def get_factor_data_contracts(config_path: str = ARM_A_CONFIG_PATH) -> Dict[str, Any]:
    """
    Declared inputs + availability per factor.

    Advisory rather than enforced, but it is the audit trail for *why* a
    composite might be running on a subset of its weights.
    """
    path = resolve_config_path(config_path)
    return dict(_require(get_config(config_path), "factor_data_contracts", str(path)))


def get_composite_weight_map(config_path: str = ARM_A_CONFIG_PATH) -> Dict[str, float]:
    """
    Flatten the composite into ``{factor_name: weight}``.

    Convenience for callers that want to iterate factors without knowing the
    block structure.  Names are the canonical factor identifiers used by
    ``gate_engine.compute_composite_score``.
    """
    cw = get_composite_config(config_path)
    out: Dict[str, float] = {}
    for block in ("directional_alpha", "operational_health"):
        for name, w in cw[block]["components"].items():
            if name in out:
                raise ConfigError(
                    f"composite factor {name!r} is declared in more than one block")
            out[str(name)] = float(w)
    return out


def describe_registry(config_path: str = ARM_A_CONFIG_PATH) -> Dict[str, Any]:
    """
    Human-readable provenance summary: identity, integrity, weight layout.

    Intended for report headers so a run states which parameters it used and
    whether the registry still matches its pin.
    """
    resolved = str(resolve_config_path(config_path))
    cfg = get_config(config_path)
    cw = get_composite_config(config_path)
    return {
        "path": resolved,
        "document": cfg.get("document"),
        "version": cfg.get("version"),
        "status": cfg.get("status"),
        "date": cfg.get("date"),
        "adds_to": cfg.get("adds_to"),
        "integrity": verify_config_integrity(config_path),
        "composite": {
            "directional_alpha_total": cw["directional_alpha"]["total_weight"],
            "operational_health_total": cw["operational_health"]["total_weight"],
            "weights": get_composite_weight_map(config_path),
        },
        "legacy_prereg": {
            "path": LEGACY_PREREG_PATH,
            "sha256": LEGACY_PREREG_SHA256,
            "note": "read via ingestion.harness.prereg.load_prereg(); not modified",
        },
    }


__all__ = ["load_weights", "SUBREDDIT_TAXONOMY", "CATEGORY_WEIGHTS", "OPTIMIZATION_CONFIG",
           "TICKER_BLACKLIST", "VALIDATION_KEYWORDS", "FINANCIAL_LEXICON", "ENTITY_RESOLUTION", "RISK_KEYWORDS",
           "load_hybrid_config", "load_fintech_credentials", "load_hybrid_weights",
           "HYBRID_SOURCE_WEIGHTS", "HYBRID_CATEGORY_WEIGHTS", "HYBRID_SUBREDDIT_WEIGHTS",
           "HYBRID_FUSION_PARAMS", "HYBRID_OPTIMIZATION_CONFIG",
           # Arm A parameter registry
           "ARM_A_CONFIG_PATH", "LEGACY_PREREG_PATH", "LEGACY_PREREG_SHA256",
           "REQUIRED_SECTIONS", "ConfigError", "ConfigKeyError", "ConfigIntegrityError",
           "config_repo_root", "resolve_config_path", "sha256_of_file",
           "verify_config_integrity", "get_config", "clear_config_cache",
           "get_vol_config", "get_composite_config", "get_turnover_config",
           "get_market_event_config", "get_regime_config", "get_tail_hedge_config",
           "get_cost_config", "get_factor_data_contracts",
           "get_composite_weight_map", "describe_registry"]