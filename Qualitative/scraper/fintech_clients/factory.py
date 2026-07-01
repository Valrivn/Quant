import asyncio
import logging
from typing import List, Dict, Optional
from .base import BaseFintechClient, FintechHealth
from .stocktwits import StockTwitsClient
from .apewisdom import ApeWisdomClient
from .rate_limiter import RateLimiter
from config import load_fintech_credentials

logger = logging.getLogger(__name__)


class FintechClientFactory:
    """Factory for creating and managing fintech API clients."""

    def __init__(self):
        self.credentials = load_fintech_credentials()
        self.rate_limiter = RateLimiter()
        self._clients: Dict[str, BaseFintechClient] = {}

    def get_client(self, source: str) -> BaseFintechClient:
        """Get or create a client for the given source."""
        if source in self._clients:
            return self._clients[source]

        if source == "stocktwits":
            client = StockTwitsClient(self.credentials.get("stocktwits", {}).get("api_key", ""), self.rate_limiter)
        elif source == "apewisdom":
            client = ApeWisdomClient(self.credentials.get("apewisdom", {}).get("api_key", ""), self.rate_limiter)
        else:
            raise ValueError(f"Unknown fintech source: {source}")

        self._clients[source] = client
        return client

    def get_healthy_sources(self, health_results: Dict[str, FintechHealth]) -> List[str]:
        """Filter sources that are healthy."""
        return [source for source, health in health_results.items() if health.is_healthy]

    async def health_check_all(self) -> Dict[str, FintechHealth]:
        """Check health of all configured sources."""
        sources = ["stocktwits", "apewisdom"]
        results = {}
        for source in sources:
            try:
                client = self.get_client(source)
                async with client:
                    results[source] = await client.health_check()
            except Exception as e:
                logger.error(f"Health check failed for {source}: {e}")
                results[source] = FintechHealth(
                    source=source, is_healthy=False, last_success=None,
                    consecutive_failures=1, rate_limit_remaining=0, rate_limit_reset=None, error_message=str(e)
                )
        return results

    async def close_all(self):
        """Close all client sessions."""
        for client in self._clients.values():
            if hasattr(client, 'session') and client.session:
                await client.session.close()
        self._clients.clear()