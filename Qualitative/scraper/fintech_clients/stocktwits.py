import aiohttp
import asyncio
from typing import List, Optional
from datetime import datetime
from .base import BaseFintechClient, FintechMessage, FintechHealth


class StockTwitsClient(BaseFintechClient):
    """StockTwits API v2 client for symbol streams and trending."""

    BASE_URL = "https://api.stocktwits.com/api/2"

    def __init__(self, api_key: str, rate_limiter: "RateLimiter"):
        super().__init__(api_key, rate_limiter)
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.api_key}"}
        )
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
        """Fetch messages for symbols (e.g., AAPL, $AAPL)."""
        all_messages = []
        for ticker in tickers:
            symbol = ticker.lstrip("$")
            url = f"{self.BASE_URL}/streams/symbol/{symbol}.json"
            params = {"limit": min(limit, 30)}
            if since:
                params["since"] = int(since.timestamp())

            data = await self._rate_limited_request(self._get, url, params)
            messages = data.get("messages", [])
            all_messages.extend([self.normalize_message(m, ticker) for m in messages])
        return all_messages[:limit]

    async def fetch_trending(self, limit: int = 50) -> List[FintechMessage]:
        """Fetch trending symbols and messages."""
        url = f"{self.BASE_URL}/trending/symbols.json"
        data = await self._rate_limited_request(self._get, url)
        symbols = [s["symbol"] for s in data.get("symbols", [])[:limit]]
        return await self.fetch_messages(symbols, limit=limit)

    async def health_check(self) -> FintechHealth:
        """Check rate limit via trending endpoint."""
        try:
            await self._rate_limited_request(self._get, f"{self.BASE_URL}/trending/symbols.json")
            return FintechHealth(
                source="stocktwits", is_healthy=True, last_success=datetime.utcnow(),
                consecutive_failures=0, rate_limit_remaining=200, rate_limit_reset=None, error_message=None
            )
        except Exception as e:
            return FintechHealth(
                source="stocktwits", is_healthy=False, last_success=None,
                consecutive_failures=1, rate_limit_remaining=0, rate_limit_reset=None, error_message=str(e)
            )

    def normalize_message(self, raw: Dict[str, Any], ticker: str) -> FintechMessage:
        """Normalize StockTwits message to unified schema."""
        return FintechMessage(
            source="stocktwits",
            source_id=str(raw["id"]),
            ticker=ticker.upper(),
            text=raw["body"],
            sentiment_score=self._parse_sentiment(raw.get("entities", {}).get("sentiment")),
            author=raw["user"]["username"],
            created_at=datetime.fromisoformat(raw["created_at"].replace("Z", "+00:00")),
            engagement={"likes": raw.get("likes", {}).get("total", 0)},
            url=f"https://stocktwits.com/{raw['user']['username']}/message/{raw['id']}",
            metadata={"symbols": [s["symbol"] for s in raw.get("symbols", [])]}
        )

    def _parse_sentiment(self, sentiment_obj: Optional[Dict]) -> Optional[float]:
        if not sentiment_obj:
            return None
        return 1.0 if sentiment_obj.get("basic") == "Bullish" else -1.0 if sentiment_obj.get("basic") == "Bearish" else 0.0

    async def _get(self, url: str, params: Dict = None) -> Dict:
        async with self.session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()