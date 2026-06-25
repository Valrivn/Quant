from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Dict, Optional, Any
from datetime import datetime


@dataclass
class FintechMessage:
    """Unified message schema across all fintech sources."""
    source: str
    source_id: str
    ticker: str
    text: str
    sentiment_score: Optional[float]
    author: str
    created_at: datetime
    engagement: Dict[str, int]
    url: str
    metadata: Dict[str, Any]


@dataclass
class FintechHealth:
    """Health status for circuit breaker."""
    source: str
    is_healthy: bool
    last_success: Optional[datetime]
    consecutive_failures: int
    rate_limit_remaining: Optional[int]
    rate_limit_reset: Optional[datetime]
    error_message: Optional[str]


class BaseFintechClient(ABC):
    """Abstract base for all fintech API clients."""

    def __init__(self, api_key: str, rate_limiter: "RateLimiter"):
        self.api_key = api_key
        self.rate_limiter = rate_limiter
        self.source_name = self.__class__.__name__.lower().replace("client", "")

    @abstractmethod
    async def fetch_messages(
        self,
        tickers: List[str],
        since: Optional[datetime] = None,
        limit: int = 100
    ) -> List[FintechMessage]:
        """Fetch messages for given tickers."""
        pass

    @abstractmethod
    async def fetch_trending(self, limit: int = 50) -> List[FintechMessage]:
        """Fetch trending tickers/messages."""
        pass

    @abstractmethod
    async def health_check(self) -> FintechHealth:
        """Check API health and rate limit status."""
        pass

    @abstractmethod
    def normalize_message(self, raw: Dict[str, Any], ticker: str = None) -> FintechMessage:
        """Convert source-specific JSON to unified FintechMessage."""
        pass

    async def _rate_limited_request(self, func, *args, **kwargs):
        """Execute request with rate limiting."""
        await self.rate_limiter.acquire(self.source_name)
        return await func(*args, **kwargs)