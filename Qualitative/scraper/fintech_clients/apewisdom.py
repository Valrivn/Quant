import aiohttp
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone
from .base import BaseFintechClient, FintechMessage, FintechHealth
from .rate_limiter import TokenBucketRateLimiter


class ApeWisdomClient(BaseFintechClient):
    """ApeWisdom API client for WallStreetBets mentions and sentiment - no API key required for public endpoints.
    
    Note: The ApeWisdom API provides trending data with aggregated metrics (mentions, upvotes)
    but does not provide individual message content via public endpoints. We synthesize
    messages from trending data for sentiment analysis.
    """

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
        """Fetch trending data and synthesize messages for requested tickers."""
        all_messages = []
        url = f"{self.BASE_URL}/filter/all"
        params = {"limit": 200}
        data = await self._rate_limited_request(self._get, url, params)
        trending = data.get("results", [])
        
        trending_map = {item["ticker"].upper(): item for item in trending}
        
        for ticker in tickers:
            ticker_upper = ticker.upper()
            if ticker_upper in trending_map:
                item = trending_map[ticker_upper]
                messages = self._synthesize_messages_from_trending(item, ticker_upper)
                all_messages.extend(messages)
        
        return all_messages[:limit]

    async def fetch_trending(self, limit: int = 50) -> List[FintechMessage]:
        """Fetch trending tickers and synthesize messages from aggregated data."""
        url = f"{self.BASE_URL}/filter/all"
        params = {"limit": limit}
        data = await self._rate_limited_request(self._get, url, params)
        trending = data.get("results", [])
        
        all_messages = []
        for item in trending:
            messages = self._synthesize_messages_from_trending(item, item["ticker"])
            all_messages.extend(messages)
        
        return all_messages[:limit]

    def _synthesize_messages_from_trending(self, item: Dict[str, Any], ticker: str) -> List[FintechMessage]:
        """Create synthetic messages from trending aggregated data."""
        mentions = item.get("mentions", 0)
        upvotes = item.get("upvotes", 0)
        rank = item.get("rank", 0)
        rank_change = item.get("rank_24h_ago", 0) - rank if item.get("rank_24h_ago") else 0
        
        sentiment = self._estimate_sentiment(upvotes, mentions, rank_change)
        
        text = f"{item.get('name', ticker)} ({ticker}) trending on WallStreetBets: {mentions} mentions, {upvotes} upvotes, rank #{rank}"
        if rank_change > 0:
            text += f" (up {rank_change} spots in 24h)"
        elif rank_change < 0:
            text += f" (down {abs(rank_change)} spots in 24h)"
        
        msg = FintechMessage(
            source="apewisdom",
            source_id=f"trending_{ticker}_{int(datetime.now(timezone.utc).timestamp())}",
            ticker=ticker,
            text=text,
            sentiment_score=sentiment,
            author="apewisdom_trending",
            created_at=datetime.now(timezone.utc),
            engagement={"upvotes": upvotes, "mentions": mentions},
            url=f"https://apewisdom.io/ticker/{ticker}",
            metadata={
                "subreddit": "wallstreetbets",
                "rank": rank,
                "mentions_24h_ago": item.get("mentions_24h_ago", 0),
                "rank_24h_ago": item.get("rank_24h_ago", 0),
                "synthesized": True
            }
        )
        return [msg]

    def _estimate_sentiment(self, upvotes: int, mentions: int, rank_change: int) -> float:
        """Estimate sentiment from trending metrics."""
        if mentions == 0:
            return 0.0
        
        upvote_ratio = upvotes / max(mentions, 1)
        base_sentiment = min(upvote_ratio / 20.0, 1.0)
        
        if rank_change > 5:
            base_sentiment += 0.2
        elif rank_change < -5:
            base_sentiment -= 0.2
        
        return max(-1.0, min(1.0, base_sentiment))

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