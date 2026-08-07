"""Tandem/ecosystem extraction (SEC 4.2, CEO example: ASML).

Scores whether content explains HOW companies work in a chain (supplier/customer
+ monopoly/dependency). Deep calls into the existing qualitative engine
(``Quantitative/company_classifier``) require fundamental metrics that are not
available for raw co-mentions, so this module provides:

  * a deterministic co-mention graph + a chain/monopoly heuristic, and
  * a pluggable ``classifier_hook`` callable that, if provided, is invoked
    READ-ONLY to enrich the heuristic (documented below).

Fully deterministic: no stochastic draws of any kind.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .config_loader import load_discovery_config


@dataclass
class EcosystemResult:
    """Ecosystem/chain analysis for one mention."""

    entity: str
    co_mentioned: List[str]
    chain_score: float          # [0, 1] deterministic
    monopoly_dependency: bool   # entity is a monopoly with dependents
    reason_codes: List[str] = field(default_factory=list)


class EcosystemAnalyzer:
    """Deterministic co-mention graph + pluggable classifier hook.

    ``classifier_hook``, if provided, is a callable
    ``(entity, co_mentioned) -> dict`` returning e.g.
    ``{"monopoly_dependency": bool, "chain_score": float}``. It is invoked
    READ-ONLY; when absent, a deterministic heuristic is used.
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        classifier_hook: Optional[Callable[[str, List[str]], dict]] = None,
    ):
        self.config = config or load_discovery_config()
        self.classifier_hook = classifier_hook

    def analyze(self, entity: str, co_mentioned: List[str]) -> EcosystemResult:
        co = list(dict.fromkeys(co_mentioned))  # dedupe, preserve order
        if self.classifier_hook is not None:
            try:
                out = self.classifier_hook(entity, co) or {}
            except Exception:  # noqa: BLE001 - fail closed to heuristic
                out = {}
            chain_score = float(out.get("chain_score", 0.0))
            monopoly = bool(out.get("monopoly_dependency", False))
            reasons = [f"hook:{k}" for k in out]
            return EcosystemResult(
                entity=entity, co_mentioned=co,
                chain_score=max(0.0, min(1.0, chain_score)),
                monopoly_dependency=monopoly, reason_codes=reasons,
            )

        # Deterministic heuristic: a monopoly/dependency pattern is when the
        # entity is co-mentioned with >=2 others that do NOT co-mention each
        # other (dependents), i.e. a hub-and-spoke chain.
        n = len(co)
        monopoly = n >= 2
        # chain_score scales with the number of co-mentioned dependents, capped.
        chain_score = min(1.0, n / 5.0) if n > 0 else 0.0
        reasons = []
        if monopoly:
            reasons.append(f"co_mention_hub:{n}")
        if chain_score > 0:
            reasons.append("chain_content")
        return EcosystemResult(
            entity=entity,
            co_mentioned=co,
            chain_score=chain_score,
            monopoly_dependency=monopoly,
            reason_codes=reasons,
        )


# --- ASML fixture (CEO example): AI-enabling monopoly, dependents = chipmakers.
ASML_FIXTURE = {
    "entity": "ASML",
    "co_mentioned": ["NVDA", "TSM", "INTC", "AMD"],
    "expected_monopoly_dependency": True,
    "expected_chain_score": 0.8,  # 4 dependents / 5 cap
}