from dataclasses import dataclass
from enum import Enum
from typing import Tuple


class SourceClass(Enum):
    STRUCTURED = "structured"
    AI_MODEL_SURVEY = "ai_model_survey"
    COMPANY_EXPLORATION = "company_exploration"


class Gating(Enum):
    FULL = "full"
    SANDBOX = "sandbox"


TOPICS: Tuple[str, ...] = (
    "AI",
    "Robotics",
    "Semiconductors",
    "Cloud/SaaS",
    "Energy Transition",
    "Biotech",
    "Space",
    "Quantum",
)


@dataclass(frozen=True)
class Source:
    source_id: str
    display_name: str
    source_class: SourceClass
    gating: Gating


# Official and white-hat discovery sources
SOURCE_TAXONOMY: Tuple[Source, ...] = (
    Source("sec_edgar_new_filers", "SEC EDGAR new-filers", SourceClass.STRUCTURED, Gating.FULL),
    Source("ai_model_survey", "AI-model trend survey", SourceClass.AI_MODEL_SURVEY, Gating.FULL),
    Source("company_exploration", "Company-exploration", SourceClass.COMPANY_EXPLORATION, Gating.FULL),
)


def get_topics() -> Tuple[str, ...]:
    """Return the fixed 8-topic taxonomy (deterministic)."""
    return TOPICS


def get_sources() -> Tuple[Source, ...]:
    """Return all sources."""
    return SOURCE_TAXONOMY
