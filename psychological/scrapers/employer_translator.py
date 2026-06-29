import asyncio
import logging
import statistics
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import load_hybrid_config
from psychological.scrapers.company_resolver import CompanyResolver
from psychological.scrapers.corp_audit import (
    GlassdoorScraper,
    IndeedScraper,
    ComparablyScraper,
)


logger = logging.getLogger(__name__)


@dataclass
class EmployerSentimentResult:
    ticker: str
    glassdoor_normalized: Optional[float]
    indeed_normalized: Optional[float]
    comparably_normalized: Optional[float]
    weighted_score: float
    agreement_score: float
    source_count: int
    timestamp: str
    raw_scores: Dict[str, Optional[float]] = field(default_factory=dict)


class EmployerTranslator:
    def __init__(self, config_dict: Optional[dict] = None):
        self._config = config_dict or load_hybrid_config()
        self._resolver = CompanyResolver(self._config)
        self._glassdoor = GlassdoorScraper(self._config)
        self._indeed = IndeedScraper(self._config)
        self._comparably = ComparablyScraper(self._config)

    async def initialize(self) -> None:
        await asyncio.gather(
            self._glassdoor.initialize(),
            self._indeed.initialize(),
            self._comparably.initialize(),
        )

    async def close(self) -> None:
        await asyncio.gather(
            self._glassdoor.close(),
            self._indeed.close(),
            self._comparably.close(),
        )

    async def __aenter__(self) -> "EmployerTranslator":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    @staticmethod
    def _normalize_score(raw: Optional[float], scale: float = 5.0) -> Optional[float]:
        if raw is None:
            return None
        return max(0.0, min(1.0, raw / scale))

    @staticmethod
    def _compute_agreement(scores: List[float]) -> float:
        valid = [s for s in scores if s is not None]
        if len(valid) < 2:
            return 1.0
        return max(0.0, 1.0 - (max(valid) - min(valid)))

    @staticmethod
    def _compute_weighted(scores: Dict[str, Optional[float]]) -> float:
        valid = [(k, v) for k, v in scores.items() if v is not None]
        if not valid:
            return 0.0
        weights = {"glassdoor": 0.40, "indeed": 0.30, "comparably": 0.30}
        total_weight = 0.0
        weighted_sum = 0.0
        for source, val in valid:
            w = weights.get(source, 1.0 / len(valid))
            weighted_sum += val * w
            total_weight += w
        if total_weight == 0.0:
            return statistics.mean([v for _, v in valid])
        return weighted_sum / total_weight

    async def translate(self, ticker: str) -> EmployerSentimentResult:
        entity = self._resolver.resolve(ticker)
        if entity is None:
            logger.warning("No company entity found for %s", ticker)
            return EmployerSentimentResult(
                ticker=ticker,
                glassdoor_normalized=None,
                indeed_normalized=None,
                comparably_normalized=None,
                weighted_score=0.0,
                agreement_score=0.0,
                source_count=0,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        gd_task = self._glassdoor.scrape_company(ticker)
        ind_task = self._indeed.scrape_company(ticker)
        comp_task = self._comparably.scrape_company(ticker)

        gd_result, ind_result, comp_result = await asyncio.gather(
            gd_task, ind_task, comp_task,
        )

        gd_norm = self._normalize_score(gd_result.raw_score) if hasattr(gd_result, "raw_score") else None
        ind_norm = self._normalize_score(ind_result.overall_rating) if hasattr(ind_result, "overall_rating") else None
        comp_norm = self._normalize_score(comp_result.overall_rating) if hasattr(comp_result, "overall_rating") else None

        scores_dict = {
            "glassdoor": gd_norm,
            "indeed": ind_norm,
            "comparably": comp_norm,
        }
        valid_scores = [v for v in scores_dict.values() if v is not None]
        agreement = self._compute_agreement(valid_scores)
        weighted = self._compute_weighted(scores_dict)

        return EmployerSentimentResult(
            ticker=ticker,
            glassdoor_normalized=gd_norm,
            indeed_normalized=ind_norm,
            comparably_normalized=comp_norm,
            weighted_score=weighted,
            agreement_score=agreement,
            source_count=len(valid_scores),
            timestamp=datetime.now(timezone.utc).isoformat(),
            raw_scores={
                "glassdoor_raw": gd_result.raw_score if hasattr(gd_result, "raw_score") else None,
                "indeed_raw": ind_result.overall_rating if hasattr(ind_result, "overall_rating") else None,
                "comparably_raw": comp_result.overall_rating if hasattr(comp_result, "overall_rating") else None,
            },
        )

    async def translate_all(self, tickers: List[str]) -> Dict[str, EmployerSentimentResult]:
        results = {}
        for ticker in tickers:
            results[ticker] = await self.translate(ticker)
        return results
