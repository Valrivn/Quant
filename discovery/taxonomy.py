"""Discovery trend-feed taxonomy (D-20260806-001 P1).

Fixed 8-topic taxonomy (SEC 3.2) and the source taxonomy (SEC 3.1). This module
holds deterministic constants only — no RNG, no stochastic draws, no mutable
global state. Changing the topic set is a config change (and a T3 if it expands
candidate-relevant topics).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Tuple


class SourceClass(str, Enum):
    """Source taxonomy classes (SEC 3.1)."""

    STRUCTURED = "structured"              # immediate scope, full gates P1->P5
    VIDEO = "video"                        # sandbox only until P1 evidence
    AI_MODEL_SURVEY = "ai_model_survey"    # concept-only, never allocation
    COMPANY_EXPLORATION = "company_exploration"  # concept + ticker priming


class Gating(str, Enum):
    """Gating policy for a source."""

    FULL = "full"          # full gates P1->P5
    SANDBOX = "sandbox"    # sandbox only until P1 evidence


# Fixed 8-topic taxonomy (SEC 3.2). Order is significant: it is the tie-break
# priority for the deterministic ranker (SEC 3.4).
TOPICS: Tuple[str, ...] = (
    "LLM",
    "Statistics",
    "Opensource AI",
    "AI",
    "Stocks",
    "Applied Math",
    "Quant trading",
    "Datascience",
)


@dataclass(frozen=True)
class Source:
    """A source in the discovery taxonomy."""

    source_id: str
    name: str
    source_class: SourceClass
    gating: Gating


# Source taxonomy (SEC 3.1). Structured sources are immediate scope; video is
# gated behind P1 sandbox evidence.
SOURCE_TAXONOMY: Tuple[Source, ...] = (
    Source("sec_edgar_new_filers", "SEC EDGAR new-filers", SourceClass.STRUCTURED, Gating.FULL),
    Source("reddit", "Reddit", SourceClass.STRUCTURED, Gating.FULL),
    Source("stocktwits", "StockTwits", SourceClass.STRUCTURED, Gating.FULL),
    Source("apewisdom", "ApeWisdom", SourceClass.STRUCTURED, Gating.FULL),
    Source("instagram", "Instagram", SourceClass.VIDEO, Gating.SANDBOX),
    Source("tiktok", "TikTok", SourceClass.VIDEO, Gating.SANDBOX),
    Source("ai_model_survey", "AI-model trend survey", SourceClass.AI_MODEL_SURVEY, Gating.FULL),
    Source("company_exploration", "Company-exploration", SourceClass.COMPANY_EXPLORATION, Gating.FULL),
)


def get_topics() -> Tuple[str, ...]:
    """Return the fixed 8-topic taxonomy (deterministic)."""
    return TOPICS


def get_source(source_id: str) -> Source:
    """Return the Source for ``source_id`` or raise KeyError."""
    for s in SOURCE_TAXONOMY:
        if s.source_id == source_id:
            return s
    raise KeyError(f"unknown discovery source: {source_id}")


def structured_source_ids() -> Tuple[str, ...]:
    """Source ids in the STRUCTURED class (immediate scope)."""
    return tuple(s.source_id for s in SOURCE_TAXONOMY if s.source_class == SourceClass.STRUCTURED)


def video_source_ids() -> Tuple[str, ...]:
    """Source ids in the VIDEO class (sandbox-gated)."""
    return tuple(s.source_id for s in SOURCE_TAXONOMY if s.source_class == SourceClass.VIDEO)