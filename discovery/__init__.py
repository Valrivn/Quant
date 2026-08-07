"""Deterministic trend-ranked discovery feed (D-20260806-001, P1 sandbox).

This module is a LEAF: it may import public interfaces from the existing
qualitative engine, quant baseline screens, and the ``db/`` layer, but no
production module may import ``discovery/`` (asserted in a later phase). It is
additive-only and never touches the frozen cores (``config/weights*.yaml``,
``diversification/``, ``portfolio/``, ``Quantitative/stochastic/``,
``backtesting/``).

P1 scope: taxonomy, source taxonomy, DEGRADED registry, provenance ledger,
structured-source wrappers, and the sandbox-gated video stub.
"""

from .taxonomy import (
    TOPICS,
    Source,
    SourceClass,
    Gating,
    SOURCE_TAXONOMY,
    get_topics,
    get_source,
    structured_source_ids,
    video_source_ids,
)
from .deg_registry import DegradedRegistry, SourceStatus, DegradedLedgerEntry
from .ledger import (
    Mention,
    Candidate,
    GatePass,
    IntegrationDecision,
    ProvenanceLedger,
)
from .video_sources import VideoSourceStub, VideoSourceLockedError

__all__ = [
    "TOPICS",
    "Source",
    "SourceClass",
    "Gating",
    "SOURCE_TAXONOMY",
    "get_topics",
    "get_source",
    "structured_source_ids",
    "video_source_ids",
    "DegradedRegistry",
    "SourceStatus",
    "DegradedLedgerEntry",
    "Mention",
    "Candidate",
    "GatePass",
    "IntegrationDecision",
    "ProvenanceLedger",
    "VideoSourceStub",
    "VideoSourceLockedError",
]