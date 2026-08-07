"""Concept-vs-ticker separation (SEC 3.5).

Non-ticker trends (e.g. "double-descent") go to a RESEARCH BACKLOG and can never
enter candidate/allocation lists. A concept may not become a candidate unless it
later names a ticker that independently passes the full pipeline. This module
enforces that in code: passing a concept to the candidate path raises.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config_loader import load_discovery_config

# Deterministic ticker heuristic: 1-5 uppercase letters (optionally with a dot
# for BRK.B-style). Anything else is treated as a concept.
_TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


class ConceptToCandidateError(RuntimeError):
    """Raised when a concept is passed to the candidate path (SEC 3.5)."""


@dataclass
class Concept:
    """A research-backlog concept (SEC 3.5)."""

    concept_name: str
    topic: str
    first_seen: int
    sources: List[str] = field(default_factory=list)
    linked_tickers: List[str] = field(default_factory=list)  # informational only
    hypothesis: Optional[str] = None
    status: str = "research_backlog"


def is_ticker(entity: str) -> bool:
    """Deterministic ticker-vs-concept classification."""
    return bool(_TICKER_RE.match(entity or ""))


class ConceptBacklog:
    """Research backlog for concepts. Concepts never enter candidate lists."""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or load_discovery_config()
        self.default_status = self.config["concepts"]["default_status"]
        self._concepts: Dict[str, Concept] = {}

    def add(self, concept: Concept) -> Concept:
        """Add a concept to the backlog (dedup by concept_name)."""
        if not concept.status:
            concept.status = self.default_status
        self._concepts[concept.concept_name] = concept
        return concept

    def get(self, concept_name: str) -> Optional[Concept]:
        return self._concepts.get(concept_name)

    def all(self) -> List[Concept]:
        return list(self._concepts.values())

    def to_candidate(self, concept_name: str) -> None:
        """REFUSE: a concept can never enter the candidate path (SEC 3.5)."""
        raise ConceptToCandidateError(
            f"concept '{concept_name}' cannot become a candidate: concepts are "
            "research-backlog only and never enter candidate/allocation lists"
        )

    def classify(self, entity: str) -> str:
        """Return 'ticker' or 'concept' deterministically."""
        return "ticker" if is_ticker(entity) else "concept"