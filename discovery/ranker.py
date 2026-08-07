"""Deterministic trend ranker (D-20260806-001, SEC 3.4).

trend_score(entity) = sum_src w_src * norm_rank_src
                    + w_vel * velocity_z
                    + w_cross * agreement_count
                    + w_topic * topic_relevance
                    - w_ad * ad_flag
                    - w_clout * clout_flag

All weights come from config/weights_discovery.yaml (invariant 4). Fully
deterministic: no stochastic draws of any kind.
Ordering: score desc, tie-break by (topic priority, lexicographic ticker).
top_k per cycle from config (default 10).
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config_loader import load_discovery_config

# Deterministic rank normalization: 1/rank (rank is 1-based).
def normalize_rank(rank: int) -> float:
    """Normalize a 1-based rank within a source to [0, 1] via 1/rank."""
    if rank is None or rank <= 0:
        return 0.0
    return 1.0 / float(rank)


def velocity_z(mentions_7d: float, mentions_28d: float) -> float:
    """Deterministic velocity z-score: (m7 - m28) / (m28 + 1).

    Bounded and deterministic; no cross-entity statistics required. A positive
    value means the 7d count exceeds the trailing 28d baseline.
    """
    m7 = float(mentions_7d or 0.0)
    m28 = float(mentions_28d or 0.0)
    return (m7 - m28) / (m28 + 1.0)


def topic_relevance(topic: str, topic_priority: List[str]) -> float:
    """Deterministic topic relevance from the priority position.

    First priority topic -> 1.0, last -> 0.0; topics not in the list -> 0.0.
    """
    if topic not in topic_priority:
        return 0.0
    n = len(topic_priority)
    if n <= 1:
        return 1.0
    idx = topic_priority.index(topic)
    return 1.0 - (idx / (n - 1))


@dataclass
class RankInput:
    """Inputs for ranking one entity."""

    entity: str
    topic: str
    # source_id -> rank (1-based) within that source; only LIVE sources.
    source_ranks: Dict[str, int] = field(default_factory=dict)
    mentions_7d: float = 0.0
    mentions_28d: float = 0.0
    ad_flag: int = 0
    clout_flag: int = 0


@dataclass
class RankedEntity:
    """A ranked entity with its score and components."""

    entity: str
    topic: str
    score: float
    agreement_count: int
    ad_flag: int
    clout_flag: int
    components: Dict[str, float] = field(default_factory=dict)


class DeterministicRanker:
    """Deterministic trend ranker driven entirely by config weights."""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or load_discovery_config()
        self.source_weights: Dict[str, float] = self.config["ranker"]["source_weights"]
        self.w_vel = float(self.config["ranker"]["w_vel"])
        self.w_cross = float(self.config["ranker"]["w_cross"])
        self.w_topic = float(self.config["ranker"]["w_topic"])
        self.w_ad = float(self.config["ranker"]["penalty_weights"]["w_ad"])
        self.w_clout = float(self.config["ranker"]["penalty_weights"]["w_clout"])
        self.topic_priority: List[str] = self.config["ranker"]["topic_priority"]
        self.sanitizer_apply: Dict[str, str] = self.config["ranker"]["sanitizer_apply"]
        self.top_k = int(self.config["caps"]["top_k"])

    def agreement_count(self, source_ranks: Dict[str, int]) -> int:
        """Number of live independent sources flagging the entity."""
        return len(source_ranks)

    def trend_score(self, inp: RankInput) -> RankedEntity:
        """Compute the deterministic trend score for one entity."""
        src_term = sum(
            self.source_weights.get(sid, 0.0) * normalize_rank(rank)
            for sid, rank in inp.source_ranks.items()
        )
        vel = velocity_z(inp.mentions_7d, inp.mentions_28d)
        cross = self.agreement_count(inp.source_ranks)
        trel = topic_relevance(inp.topic, self.topic_priority)

        score = (
            src_term
            + self.w_vel * vel
            + self.w_cross * cross
            + self.w_topic * trel
            - self.w_ad * inp.ad_flag
            - self.w_clout * inp.clout_flag
        )
        return RankedEntity(
            entity=inp.entity,
            topic=inp.topic,
            score=score,
            agreement_count=cross,
            ad_flag=inp.ad_flag,
            clout_flag=inp.clout_flag,
            components={
                "source_term": src_term,
                "velocity_z": vel,
                "agreement_count": float(cross),
                "topic_relevance": trel,
                "ad_penalty": self.w_ad * inp.ad_flag,
                "clout_penalty": self.w_clout * inp.clout_flag,
            },
        )

    def _tie_key(self, ranked: RankedEntity):
        """Deterministic tie-break: (topic priority index, lexicographic ticker)."""
        try:
            topic_idx = self.topic_priority.index(ranked.topic)
        except ValueError:
            topic_idx = len(self.topic_priority)
        return (topic_idx, ranked.entity)

    def rank(self, inputs: List[RankInput]) -> List[RankedEntity]:
        """Rank entities deterministically and return the top_k.

        Sanitizer flags (ad_flag / clout_flag) are applied per config: a flag
        whose sanitizer is configured ``exclude`` is a hard exclusion; otherwise
        it is a penalty already folded into the score by ``trend_score``.
        """
        ranked: List[RankedEntity] = []
        for inp in inputs:
            r = self.trend_score(inp)
            if self.sanitizer_apply.get("ad") == "exclude" and inp.ad_flag:
                continue
            if self.sanitizer_apply.get("clout") == "exclude" and inp.clout_flag:
                continue
            ranked.append(r)

        ranked.sort(key=lambda r: (-r.score, self._tie_key(r)))
        return ranked[: self.top_k]