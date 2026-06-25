import aiohttp
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from .base import BaseFintechClient, FintechMessage, FintechHealth
from .rate_limiter import TokenBucketRateLimiter


class ApeWisdomClient(BaseFintechClient):
    """ApeWisdom API client for WallStreetBets mentions and sentiment - no API key required for public endpoints."""

    BASE_URL = "https://apewisdom.io/api/v1.0"

    def __init__(self, api_key: str = "", rate_limiter: TokenBucketRateLimiter = None):
        super().__init__(api_key, rate_limiter)
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def fetch_messages(
        self,
        tickers: List[str],
        since: Optional[datetime] = None,
        limit: int = 100
    ) -> List[FintechMessage]:
        """Fetch mentions for tickers from WallStreetBets (public endpoint, no auth)."""
        all_messages = []
        for ticker in tickers:
            url = f"{self.BASE_URL}/filter/ticker/{ticker.upper()}"
            params = {"limit": min(limit, 100)}
            data = await self._rate_limited_request(self._get, url, params)
            mentions = data.get("results", [])
            all_messages.extend([self.normalize_message(m, ticker) for m in mentions])
        return all_messages[:limit]

    async def fetch_trending(self, limit: int = 50) -> List[FintechMessage]:
        """Fetch trending tickers from WallStreetBets (public endpoint, no auth)."""
        url = f"{self.BASE_URL}/filter/all"
        params = {"limit": limit}
        data = await self._rate_limited_request(self._get, url, params)
        top_tickers = [r["ticker"] for r in data.get("results", [])[:limit]]
        return await self.fetch_messages(top_tickers, limit=limit)

    async def health_check(self) -> FintechHealth:
        try:
            await self._rate_limited_request(self._get, f"{self.BASE_URL}/filter/all", {"limit": 1})
            return FintechHealth(source="apewisdom", is_healthy=True, last_success=datetime.now(timezone.utc),
                consecutive_failures=0, rate_limit_remaining=100, rate_limit_reset=None, error_message=None)
        except Exception as e:
            return FintechHealth(source="apewisdom", is_healthy=False, last_success=None,
                consecutive_failures=1, rate_limit_remaining=0, rate_limit_reset=None, error_message=str(e))

    def normalize_message(self, raw: Dict[str, Any], ticker: str) -> FintechMessage:
        return FintechMessage(
            source="apewisdom",
            source_id=str(raw.get("id", raw.get("mention_id", ""))),
            ticker=ticker.upper(),
            text=raw.get("title", "") + " " + raw.get("body", ""),
            sentiment_score=raw.get("sentiment"),
            author=raw.get("author", "unknown"),
            created_at=datetime.fromtimestamp(raw["created_utc"], tz=timezone.utc) if raw.get("created_utc") else datetime.now(timezone.utc),
            engagement={"upvotes": raw.get("score", 0), "comments": raw.get("num_comments", 0)},
            url=raw.get("url", ""),
            metadata={"subreddit": raw.get("subreddit", "wallstreetbets")}
        )

    async def _get(self, url: str, params: Dict = None) -> Dict:
        async with self.session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()


class ApeWisdomPublicClient:
    """Public ApeWisdom client that works without API token - uses documented public endpoints."""
    
    BASE_URL = "https://apewisdom.io/api/v1.0"
    
    def __init__(self, rate_limiter: TokenBucketRateLimiter = None):
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter(rate_per_minute=30, burst=10)
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    async def fetch_trending_tickers(self, limit: int = 50) -> List[str]:
        """Fetch trending tickers from WallStreetBets - no auth required."""
        url = f"{self.BASE_URL}/filter/all"
        params = {"limit": limit}
        await self.rate_limiter.acquire("apewisdom_public")
        async with self.session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return [r["ticker"] for r in data.get("results", [])[:limit]]
    
    async def fetch_ticker_mentions(self, ticker: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Fetch mentions for a specific ticker - no auth required."""
        url = f"{self.BASE_URL}/filter/ticker/{ticker.upper()}"
        params = {"limit": min(limit, 100)}
        await self.rate_limiter.acquire("apewisdom_public")
        async with self.session.get(url, params=params) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("results", [])
    
    async def health_check(self) -> bool:
        try:
            await self.rate_limiter.acquire("apewisdom_public")
            async with self.session.get(f"{self.BASE_URL}/filter/all", params={"limit": 1}) as resp:
                return resp.status == 200
        except Exception:
            return False