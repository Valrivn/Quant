from typing import TypedDict, Dict, List, Optional
from datetime import datetime


class PsychologicalFeatureVector(TypedDict):
    ticker: str
    timestamp: int
    source_provenance: str
    raw_text: Optional[str]
    compound_vader: Optional[float]
    bull_bear_ratio: Optional[float]
    bullish_count: int
    bearish_count: int
    mention_velocity: Optional[float]
    comment_volume_sigma: Optional[float]
    acceleration: Optional[float]
    employee_sentiment_proxy: Optional[float]
    dev_fork_acceleration: Optional[float]
    metadata_json: Optional[str]


class VelocitySnapshot(TypedDict):
    ticker: str
    window_start: int
    window_end: int
    window_type: str
    mention_count: int
    comment_volume: int
    unique_authors: int


class AdzunaJobSnapshot(TypedDict):
    ticker: str
    company_name: str
    date: str
    job_count: int
    job_count_7d_ago: int
    job_count_30d_ago: int
    delta_7d_pct: float
    delta_30d_pct: float


class FusedPsychologicalState(TypedDict):
    ticker: str
    active_behavioral_regime: str
    contrarian_buy_authorized: bool
    system_confidence_score: float


class RegimeOutput(TypedDict):
    regime: str
    contrarian_buy_authorized: bool
    confidence: float


class NLPMetrics(TypedDict):
    compound_vader: float
    bull_bear_ratio: float
    bullish_count: int
    bearish_count: int


class VelocityMetrics(TypedDict):
    mention_velocity: float
    comment_volume_sigma: float
    acceleration: float


class CorporateAffinity(TypedDict):
    employee_sentiment_proxy: Optional[float]
    dev_fork_acceleration: Optional[float]
    product_sentiment_proxy: Optional[float]


class RedditCommentPayload(TypedDict):
    ticker: str
    text: str
    subreddit: str
    created_utc: int
    score: int


class GitHubMetrics(TypedDict):
    star_velocity: float
    fork_velocity: float
    commit_frequency: float
    contributor_growth: float
    dev_fork_acceleration: float


__all__ = [
    "PsychologicalFeatureVector",
    "VelocitySnapshot",
    "AdzunaJobSnapshot",
    "FusedPsychologicalState",
    "RegimeOutput",
    "NLPMetrics",
    "VelocityMetrics",
    "CorporateAffinity",
    "RedditCommentPayload",
    "GitHubMetrics",
]