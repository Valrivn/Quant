import asyncio
import time
from typing import Dict, Optional
from dataclasses import dataclass, field
from collections import deque


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""
    requests_per_window: int
    window_seconds: int
    burst_allowance: int = 0


class TokenBucket:
    """Token bucket for rate limiting."""
    
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.refill_rate = refill_rate  # tokens per second
        self.tokens = float(capacity)
        self.last_refill = time.time()

    def consume(self, tokens: int = 1) -> bool:
        """Try to consume tokens. Returns True if successful."""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def _refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def time_until_ready(self, tokens: int = 1) -> float:
        """Seconds until enough tokens are available."""
        self._refill()
        if self.tokens >= tokens:
            return 0.0
        return (tokens - self.tokens) / self.refill_rate


class TokenBucketRateLimiter:
    """Manages rate limits per API source."""

    DEFAULT_LIMITS = {
        "stocktwits": {"capacity": 200, "refill_rate": 1.0},    # 200 req, 1/sec refill
        "apewisdom": {"capacity": 100, "refill_rate": 0.5},     # 100 req, 0.5/sec refill
        "reddit": {"capacity": 60, "refill_rate": 1.0},         # 60 req/min
    }

    def __init__(self, configs: Dict[str, RateLimitConfig] = None):
        self.buckets: Dict[str, TokenBucket] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        
        if configs:
            for source, config in configs.items():
                capacity = config.requests_per_window + config.burst_allowance
                refill_rate = config.requests_per_window / config.window_seconds
                self.buckets[source] = TokenBucket(capacity, refill_rate)
                self._locks[source] = asyncio.Lock()
        else:
            for source, config in self.DEFAULT_LIMITS.items():
                self.buckets[source] = TokenBucket(**config)
                self._locks[source] = asyncio.Lock()

    async def acquire(self, source: str, tokens: int = 1) -> float:
        """Acquire permission to make a request. Returns wait time."""
        if source not in self.buckets:
            config = self.DEFAULT_LIMITS.get(source, {"capacity": 100, "refill_rate": 1.0})
            self.buckets[source] = TokenBucket(**config)
            self._locks[source] = asyncio.Lock()

        async with self._locks[source]:
            bucket = self.buckets[source]
            wait_time = bucket.time_until_ready(tokens)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            bucket.consume(tokens)
            return wait_time

    def get_remaining(self, source: str) -> int:
        """Get approximate remaining tokens for a source."""
        if source in self.buckets:
            self.buckets[source]._refill()
            return int(self.buckets[source].tokens)
        return 0


# Backward compatibility
RateLimiter = TokenBucketRateLimiter