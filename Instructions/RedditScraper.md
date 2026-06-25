Hybrid Fallback Architecture Implementation Plan
📋 Glossary Table: Files to Create/Edit
#	File Path	Type	Purpose
NEW FILES - Fintech API Layer (Option B Primary)	 	 	 
1	scraper/fintech_clients/__init__.py	New	Package init for fintech API clients
2	scraper/fintech_clients/base.py	New	Abstract base class for all fintech API clients
3	scraper/fintech_clients/stocktwits.py	New	StockTwits API client (symbol streams, messages, trending)
4	scraper/fintech_clients/apewisdom.py	New	ApeWisdom API client (wallstreetbets mentions, sentiment, trending)
5	scraper/fintech_clients/factory.py	New	Factory pattern for client instantiation & health checks
6	scraper/fintech_clients/rate_limiter.py	New	Token bucket rate limiter with per-API quotas
7	scraper/fintech_clients/normalizer.py	New	Normalizes fintech API responses to unified schema
NEW FILES - Hybrid Orchestration	 	 	 
8	scraper/hybrid_orchestrator.py	New	Main orchestrator: routes to primary (fintech) → fallback (reddit)
9	scraper/health_monitor.py	New	Circuit breaker, health checks, automatic failover logic
10	scraper/data_fusion.py	New	Merges/normalizes signals from multiple sources with provenance
NEW FILES - Configuration & Schema	 	 	 
11	config/fintech_credentials.yaml	New	API keys, endpoints, rate limits for StockTwits/ApeWisdom
12	config/hybrid_weights.yaml	New	Source weights, failover thresholds, priority ordering
13	db/schema_fintech.py	New	Extended schema: fintech_sources, api_health, source_provenance
MODIFIED FILES - Integration	 	 	 
14	scraper/engine.py	Edit	Add fintech-specific sentiment processing, ticker extraction
15	scraper/reddit_client.py	Edit	Refactor as fallback client; add circuit breaker integration
16	run_scraper.py	Edit	Add scrape-fintech, scrape-hybrid, health-check commands
17	db/connection.py	Edit	Add fintech tables, connection pooling for multiple sources
18	db/schema.py	Edit	Extend with fintech tables, source tracking, API health logs
19	db/feature_store.py	Edit	Add source-aware features, provenance tracking
20	dashboard/tab_sentiment_risk.py	Edit	Add source breakdown, health status, failover indicators
21	config/weights.py	Edit	Load hybrid weights, support multi-source configs
22	config/constants.py	Edit	Add fintech-specific constants, validation keywords
23	backtesting/backtest.py	Edit	Support multi-source backtesting with source attribution
24	optimization/optuna_search.py	Edit	Optimize across source weights + subreddit weights
25	tests/test_fintech_clients.py	New	Unit tests for fintech clients, normalizer, health monitor
26	tests/test_hybrid_orchestrator.py	New	Integration tests for failover, data fusion, circuit breaker
🏗️ Skeleton Structures
1. scraper/fintech_clients/base.py — Abstract Base Client
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Dict, Optional, Any
from datetime import datetime

@dataclass
class FintechMessage:
    """Unified message schema across all fintech sources."""
    source: str                    # "stocktwits", "apewisdom", "reddit"
    source_id: str                 # Native ID from source API
    ticker: str                    # Canonical ticker (e.g., "AAPL")
    text: str                      # Full message text
    sentiment_score: Optional[float]  # Source-provided sentiment if available
    author: str                    # Username/handle
    created_at: datetime           # UTC timestamp
    engagement: Dict[str, int]     # {"likes": 10, "comments": 5, "retweets": 2}
    url: str                       # Permalink to original message
    metadata: Dict[str, Any]       # Source-specific extra fields

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
    def normalize_message(self, raw: Dict[str, Any]) -> FintechMessage:
        """Convert source-specific JSON to unified FintechMessage."""
        pass
    
    async def _rate_limited_request(self, func, *args, **kwargs):
        """Execute request with rate limiting."""
        await self.rate_limiter.acquire(self.source_name)
        return await func(*args, **kwargs)
2. scraper/fintech_clients/stocktwits.py — StockTwits Client
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
            params = {"limit": min(limit, 30)}  # API max 30 per request
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
        # Trending returns symbols, need to fetch messages for each
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
        if not sentiment_obj: return None
        return 1.0 if sentiment_obj.get("basic") == "Bullish" else -1.0 if sentiment_obj.get("basic") == "Bearish" else 0.0.0
    
    async def _get(self, url: str, params: Dict = None) -> Dict:
        async with self.session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()
3. scraper/fintech_clients/apewisdom.py — ApeWisdom Client
import aiohttp
from typing import List, Optional
from datetime import datetime
from .base import BaseFintechClient, FintechMessage, FintechHealth

class ApeWisdomClient(BaseFintechClient):
    """ApeWisdom API client for WallStreetBets mentions and sentiment."""
    
    BASE_URL = "https://apewisdom.io/api/v1.0"
    
    def __init__(self, api_key: str, rate_limiter: "RateLimiter"):
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
        """Fetch mentions for tickers from WallStreetBets."""
        all_messages = []
        for ticker in tickers:
            url = f"{self.BASE_URL}/filter/ticker/{ticker.upper()}"
            params = {"limit": min(limit, 100), "key": self.api_key}
            data = await self._rate_limited_request(self._get, url, params)
            mentions = data.get("results", [])
            all_messages.extend([self.normalize_message(m, ticker) for m in mentions])
        return all_messages[:limit]
    
    async def fetch_trending(self, limit: int = 50) -> List[FintechMessage]:
        """Fetch trending tickers from WallStreetBets."""
        url = f"{self.BASE_URL}/filter/all"
        params = {"limit": limit, "key": self.api_key}
        data = await self._rate_limited_request(self._get, url, params)
        # Returns aggregated ticker data, not individual messages
        # Need to fetch messages for top tickers
        top_tickers = [r["ticker"] for r in data.get("results", [])[:limit]]
        return await self.fetch_messages(top_tickers, limit=limit)
    
    async def health_check(self) -> FintechHealth:
        try:
            await self._rate_limited_request(self._get, f"{self.BASE_URL}/filter/all", {"limit": 1, "key": self.api_key})
            return FintechHealth(source="apewisdom", is_healthy=True, last_success=datetime.utcnow(),
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
            sentiment_score=raw.get("sentiment"),  # -1 to 1 if provided
            author=raw.get("author", "unknown"),
            created_at=datetime.fromtimestamp(raw["created_utc"]) if raw.get("created_utc") else datetime.utcnow(),
            engagement={"upvotes": raw.get("score", 0), "comments": raw.get("num_comments", 0)},
            url=raw.get("url", ""),
            metadata={"subreddit": raw.get("subreddit", "wallstreetbets")}
        )
    
    async def _get(self, url: str, params: Dict = None) -> Dict:
        async with self.session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()
4. scraper/fintech_clients/rate_limiter.py — Token Bucket Rate Limiter
import asyncio
import time
from typing import Dict, Optional
from dataclasses import dataclass
from threading import Lock

@dataclass
class RateLimitConfig:
    requests_per_window: int
    window_seconds: int
    burst_allowance: int = 0  # Extra tokens for bursts

class TokenBucketRateLimiter:
    """Thread-safe token bucket rate limiter per API source."""
    
    def __init__(self, configs: Dict[str, RateLimitConfig]):
        self.configs = configs
        self.buckets: Dict[str, Dict] = {}
        self._lock = Lock()
        self._init_buckets()
    
    def _init_buckets(self):
        for source, config in self.configs.items():
            self.buckets[source] = {
                "tokens": config.requests_per_window + config.burst_allowance,
                "max_tokens": config.requests_per_window + config.burst_allowance,
                "refill_rate": config.requests_per_window / config.window_seconds,
                "last_refill": time.monotonic(),
                "config": config
            }
    
    async def acquire(self, source: str, tokens: int = 1) -> None:
        """Block until tokens available."""
        while True:
            with self._lock:
                bucket = self.buckets.get(source)
                if not bucket:
                    return  # No limit configured
                
                now = time.monotonic()
                elapsed = now - bucket["last_refill"]
                bucket["tokens"] = min(
                    bucket["max_tokens"],
                    bucket["tokens"] + elapsed * bucket["refill_rate"]
                )
                bucket["last_refill"] = now
                
                if bucket["tokens"] >= tokens:
                    bucket["tokens"] -= tokens
                    return
            
            await asyncio.sleep(0.1)  # Wait before retry
    
    def get_remaining(self, source: str) -> int:
        with self._lock:
            bucket = self.buckets.get(source)
            if not bucket: return -1
            now = time.monotonic()
            elapsed = now - bucket["last_refill"]
            tokens = min(bucket["max_tokens"], bucket["tokens"] + elapsed * bucket["refill_rate"])
            return int(tokens)
    
    def get_reset_time(self, source: str) -> Optional[float]:
        with self._lock:
            bucket = self.buckets.get(source)
            if not bucket or bucket["tokens"] >= 1: return None
            return bucket["last_refill"] + (1 - bucket["tokens"]) / bucket["refill_rate"]
5. scraper/fintech_clients/factory.py — Client Factory
from typing import Dict, List, Optional
from .base import BaseFintechClient, FintechHealth
from .stocktwits import StockTwitsClient
from .apewisdom import ApeWisdomClient
from .rate_limiter import TokenBucketRateLimiter, RateLimitConfig
from config import load_fintech_credentials

class FintechClientFactory:
    """Factory for creating and managing fintech API clients."""
    
    def __init__(self):
        self.credentials = load_fintech_credentials()
        self.rate_limiter = TokenBucketRateLimiter({
            "stocktwits": RateLimitConfig(requests_per_window=200, window_seconds=60),  # 200/min
            "apewisdom": RateLimitConfig(requests_per_window=100, window_seconds=60),   # 100/min
        })
        self._clients: Dict[str, BaseFintechClient] = {}
    
    def get_client(self, source: str) -> BaseFintechClient:
        """Get or create client for source."""
        if source in self._clients:
            return self._clients[source]
        
        creds = self.credentials.get(source, {})
        if source == "stocktwits":
            client = StockTwitsClient(creds.get("access_token", ""), self.rate_limiter)
        elif source == "apewisdom":
            client = ApeWisdomClient(creds.get("api_key", ""), self.rate_limiter)
        else:
            raise ValueError(f"Unknown fintech source: {source}")
        
        self._clients[source] = client
        return client
    
    async def health_check_all(self) -> Dict[str, FintechHealth]:
        """Check health of all configured clients."""
        results = {}
        for source in ["stocktwits", "apewisdom"]:
            try:
                client = self.get_client(source)
                results[source] = await client.health_check()
            except Exception as e:
                results[source] = FintechHealth(
                    source=source, is_healthy=False, last_success=None,
                    consecutive_failures=1, rate_limit_remaining=0, rate_limit_reset=None, error_message=str(e)
                )
        return results
    
    def get_healthy_sources(self, health: Dict[str, FintechHealth]) -> List[str]:
        """Return list of healthy sources, prioritized by config."""
        priority = self.credentials.get("priority", ["stocktwits", "apewisdom"])
        return [s for s in priority if health.get(s, FintechHealth(source=s, is_healthy=False)).is_healthy]
6. scraper/fintech_clients/normalizer.py — Response Normalizer
from typing import List, Dict, Any
from datetime import datetime
from .base import FintechMessage

class FintechNormalizer:
    """Normalizes and enriches messages from all fintech sources."""
    
    # Ticker extraction patterns per source
    TICKER_PATTERNS = {
        "stocktwits": r"\$([A-Z]{1,5})\b",
        "apewisdom": r"\b([A-Z]{1,5})\b",  # Already extracted by API
        "reddit": r"\b([A-Z]{1,5})\b",
    }
    
    # Source reliability weights for fusion
    SOURCE_RELIABILITY = {
        "stocktwits": 0.9,   # Structured, ticker-tagged, financial focus
        "apewisdom": 0.85,   # WSB-focused, aggregated, good sentiment
        "reddit": 0.6,       # Noisy, requires heavy filtering
    }
    
    def __init__(self, ticker_blacklist: set, validation_keywords: set):
        self.ticker_blacklist = ticker_blacklist
        self.validation_keywords = validation_keywords
    
    def normalize_batch(self, messages: List[FintechMessage]) -> List[FintechMessage]:
        """Normalize and enrich a batch of messages."""
        normalized = []
        for msg in messages:
            # Ensure ticker is canonical
            msg.ticker = self._canonicalize_ticker(msg.ticker)
            
            # Add source reliability weight
            msg.metadata["source_reliability"] = self.SOURCE_RELIABILITY.get(msg.source, 0.5)
            
            # Add extraction confidence
            msg.metadata["extraction_confidence"] = self._compute_confidence(msg)
            
            normalized.append(msg)
        return normalized
    
    def _canonicalize_ticker(self, ticker: str) -> str:
        """Map to canonical ticker (uppercase, strip $)."""
        return ticker.upper().lstrip("$")
    
    def _compute_confidence(self, msg: FintechMessage) -> float:
        """Compute confidence based on source + context."""
        base = msg.metadata.get("source_reliability", 0.5)
        
        # Boost if source provides explicit ticker tagging
        if msg.source in ("stocktwits", "apewisdom"):
            base += 0.15
        
        # Boost if engagement is high
        engagement = sum(msg.engagement.values())
        if engagement > 100: base += 0.1
        elif engagement > 10: base += 0.05
        
        return min(base, 1.0)
    
    def deduplicate(self, messages: List[FintechMessage]) -> List[FintechMessage]:
        """Remove duplicates by (source, source_id)."""
        seen = set()
        unique = []
        for msg in messages:
            key = (msg.source, msg.source_id)
            if key not in seen:
                seen.add(key)
                unique.append(msg)
        return unique
7. scraper/hybrid_orchestrator.py — Main Orchestrator
import asyncio
import logging
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from dataclasses import dataclass

from scraper.fintech_clients.factory import FintechClientFactory
from scraper.fintech_clients.normalizer import FintechNormalizer
from scraper.reddit_client import RedditUniversalScraper
from scraper.health_monitor import HealthMonitor, CircuitBreaker
from scraper.data_fusion import DataFusionEngine
from config import load_hybrid_weights

logger = logging.getLogger(__name__)

@dataclass
class ScrapeResult:
    source: str
    messages_count: int
    tickers_found: List[str]
    duration_ms: int
    errors: List[str]

class HybridOrchestrator:
    """
    Automated Hybrid Fallback Architecture (Option C).
    
    Primary: Fintech APIs (StockTwits, ApeWisdom) - structured, reliable, fast
    Fallback: Reddit PRAW scraper - comprehensive, noisy, slower
    
    Flow:
    1. Check health of all fintech sources
    2. Fetch from healthy fintech sources (parallel)
    3. Normalize, deduplicate, fuse signals
    4. If coverage < threshold OR fintech all unhealthy → trigger Reddit fallback
    5. Merge Reddit data with fintech data (provenance tracked)
    6. Persist unified signals to database
    """
    
    def __init__(self):
        self.factory = FintechClientFactory()
        self.normalizer = FintechNormalizer(
            ticker_blacklist=set(),  # Loaded from config
            validation_keywords=set()
        )
        self.health_monitor = HealthMonitor()
        self.fusion_engine = DataFusionEngine()
        self.hybrid_config = load_hybrid_weights()
        self.reddit_scraper = RedditUniversalScraper()
    
    async def scrape_all(self, tickers: List[str] = None) -> Dict[str, ScrapeResult]:
        """Main entry point: orchestrate hybrid scrape."""
        results = {}
        
        # Phase 1: Health check all sources
        health = await self.factory.health_check_all()
        healthy_sources = self.factory.get_healthy_sources(health)
        logger.info(f"Health check: {healthy_sources} healthy, {set(health.keys()) - set(healthy_sources)} unhealthy")
        
        # Phase 2: Primary - Fintech APIs (parallel)
        fintech_results = await self._scrape_fintech_sources(healthy_sources, tickers)
        results.update(fintech_results)
        
        # Phase 3: Check coverage threshold
        total_fintech_messages = sum(r.messages_count for r in fintech_results.values())
        coverage_threshold = self.hybrid_config.get("min_fintech_messages", 50)
        
        needs_fallback = (
            total_fintech_messages < coverage_threshold or
            len(healthy_sources) == 0 or
            self.hybrid_config.get("force_reddit_fallback", False)
        )
        
        # Phase 4: Fallback - Reddit (if needed)
        if needs_fallback:
            logger.info(f"Fintech coverage ({total_fintech_messages}) below threshold ({coverage_threshold}), triggering Reddit fallback")
            reddit_result = await self._scrape_reddit_fallback(tickers)
            results["reddit"] = reddit_result
        else:
            logger.info(f"Fintech coverage sufficient ({total_fintech_messages} messages), skipping Reddit fallback")
        
        # Phase 5: Fuse and persist
        all_messages = self._collect_all_messages(results)
        fused_signals = self.fusion_engine.fuse(all_messages)
        await self._persist_fused_signals(fused_signals)
        
        return results
    
    async def _scrape_fintech_sources(
        self, 
        healthy_sources: List[str], 
        tickers: List[str] = None
    ) -> Dict[str, ScrapeResult]:
        """Scrape all healthy fintech sources in parallel."""
        async def scrape_source(source: str) -> ScrapeResult:
            start = datetime.utcnow()
            client = self.factory.get_client(source)
            try:
                messages = await client.fetch_messages(tickers or [], limit=200)
                trending = await client.fetch_trending(limit=50)
                all_msgs = self.normalizer.normalize_batch(messages + trending)
                all_msgs = self.normalizer.deduplicate(all_msgs)
                
                tickers_found = list(set(m.ticker for m in all_msgs))
                duration = int((datetime.utcnow() - start).total_seconds() * 1000)
                
                return ScrapeResult(
                    source=source, messages_count=len(all_msgs),
                    tickers_found=tickers_found, duration_ms=duration, errors=[]
                )
            except Exception as e:
                logger.error(f"Error scraping {source}: {e}")
                return ScrapeResult(source=source, messages_count=0, tickers_found=[], 
                                  duration_ms=int((datetime.utcnow() - start).total_seconds() * 1000), errors=[str(e)])
        
        tasks = [scrape_source(s) for s in healthy_sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        return {healthy_sources[i]: r for i, r in enumerate(results) if not isinstance(r, Exception)}
    
    async def _scrape_reddit_fallback(self, tickers: List[str] = None) -> ScrapeResult:
        """Run Reddit scraper as fallback."""
        start = datetime.utcnow()
        try:
            # Run in thread pool since RedditUniversalScraper is sync
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.reddit_scraper.scrape_all)
            
            # Count new messages from today
            from db.connection import get_db_connection
            today = datetime.utcnow().strftime("%Y-%m-%d")
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM daily_aggregations WHERE date=?", (today,))
            count = cursor.fetchone()[0]
            conn.close()
            
            return ScrapeResult(
                source="reddit="reddit", messages_count=count, tickers_found=tickers or [],
                duration_ms=int((datetime.utcnow() - start).total_seconds() * 1000), errors=[]
            )
        except Exception as e:
            logger.error(f"Reddit fallback failed: {e}")
            return ScrapeResult(source="reddit", messages_count=0, tickers_found=[],
                              duration_ms=int((datetime.utcnow() - start).total_seconds() * 1000), errors=[str(e)])
    
    def _collect_all_messages(self, results: Dict[str, ScrapeResult]) -> List[FintechMessage]:
        """Collect messages from all successful scrapes."""
        # This would query the database for today's messages per source
        # Implementation depends on storage strategy
        pass
    
    async def _persist_fused_signals(self, signals: List[Dict]) -> None:
        """Persist fused signals to database with provenance."""
        pass
8. scraper/health_monitor.py — Circuit Breaker & Health
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Callable, Awaitable
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

class CircuitState(Enum):
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery

@dataclass
class CircuitBreaker:
    """Circuit breaker for individual API sources."""
    source: str
    failure_threshold: int = 5
    success_threshold: int = 2
    timeout_seconds: int = 60
    
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: Optional[datetime] = None
    last_state_change: datetime = field(default_factory=datetime.utcnow)
    
    def record_success(self):
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                self._transition(CircuitState.CLOSED)
        elif self.state == CircuitState.CLOSED:
            self.failure_count = 0  # Reset on success
    
    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = datetime.utcnow()
        
        if self.state == CircuitState.CLOSED and self.failure_count >= self.failure_threshold:
            self._transition(CircuitState.OPEN)
        elif self.state == CircuitState.HALF_OPEN:
            self._transition(CircuitState.OPEN)
    
    def can_execute(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            if datetime.utcnow() - self.last_state_change > timedelta(seconds=self.timeout_seconds):
                self._transition(CircuitState.HALF_OPEN)
                return True
            return False
        return True  # HALF_OPEN
    
    def _transition(self, new_state: CircuitState):
        logger.warning(f"Circuit breaker {self.source}: {self.state.value} → {new_state.value}")
        self.state = new_state
        self.last_state_change = datetime.utcnow()
        if new_state == CircuitState.CLOSED:
            self.failure_count = 0
            self.success_count = 0
        elif new_state == CircuitState.HALF_OPEN:
            self.success_count = 0

class HealthMonitor:
    """Monitors health of all data sources, manages circuit breakers."""
    
    def __init__(self):
        self.circuit_breakers: Dict[str, CircuitBreaker] = {}
        self.health_history: Dict[str, list] = {}
    
    def get_breaker(self, source: str) -> CircuitBreaker:
        if source not in self.circuit_breakers:
            self.circuit_breakers[source] = CircuitBreaker(source=source)
        return self.circuit_breakers[source]
    
    def record_result(self, source: str, success: bool, latency_ms: int = 0):
        breaker = self.get_breaker(source)
        if success:
            breaker.record_success()
        else:
            breaker.record_failure()
        
        # Track history for dashboard
        if source not in self.health_history:
            self.health_history[source] = []
        self.health_history[source].append({
            "timestamp": datetime.utcnow(),
            "success": success,
            "latency_ms": latency_ms,
            "state": breaker.state.value
        })
        # Keep last 1000 entries
        if len(self.health_history[source]) > 1000:
            self.health_history[source] = self.health_history[source][-1000:]
    
    def get_source_status(self, source: str) -> Dict:
        breaker = self.get_breaker(source)
        recent = self.health_history.get(source, [])[-100:]
        success_rate = sum(1 for h in recent if h["success"]) / len(recent) if recent else 0
        avg_latency = sum(h["latency_ms"] for h in recent) / len(recent) if recent else 0
        
        return {
            "source": source,
            "circuit_state": breaker.state.value,
            "success_rate_100": success_rate,
            "avg_latency_ms": avg_latency,
            "failure_count": breaker.failure_count,
            "can_execute": breaker.can_execute()
        }
    
    def get_all_status(self) -> Dict[str, Dict]:
        return {s: self.get_source_status(s) for s in self.circuit_breakers.keys()}
    
    async def execute_with_circuit(self, source: str, func: Callable[[], Awaitable], *args, **kwargs):
        """Execute function with circuit breaker protection."""
        breaker = self.get_breaker(source)
        if not breaker.can_execute():
            raise CircuitOpenError(f"Circuit breaker OPEN for {source}")
        
        try:
            result = await func(*args, **kwargs)
            self.record_result(source, True)
            return result
        except Exception as e:
            self.record_result(source, False)
            raise

class CircuitOpenError(Exception):
    pass
9. scraper/data_fusion.py — Multi-Source Signal Fusion
import pandas as pd
import numpy as np
from typing import List, Dict, Any
from datetime import datetime
from collections import defaultdict
from scraper.fintech_clients.base import FintechMessage

class DataFusionEngine:
    """
    Fuses signals from multiple sources with provenance tracking.
    
    Strategy:
    - Weight by source reliability (StockTwits > ApeWisdom > Reddit)
    - Weight by engagement (likes, comments, upvotes)
    - Weight by recency (exponential decay)
    - Track source provenance for auditability
    """
    
    def __init__(self, source_weights: Dict[str, float] = None):
        self.source_weights = source_weights or {
            "stocktwits": 0.5,
            "apewisdom": 0.3,
            "reddit": 0.2
        }
        self.recency_half_life_hours = 24
    
    def fuse(self, messages: List[FintechMessage]) -> List[Dict]:
        """Fuse messages into per-ticker daily signals with provenance."""
        if not messages:
            return []
        
        # Group by ticker and date
        grouped = defaultdict(lambda: defaultdict(list))
        for msg in messages:
            date_key = msg.created_at.strftime("%Y-%m-%d")
            grouped[msg.ticker][date_key].append(msg)
        
        fused_signals = []
        for ticker, dates in grouped.items():
            for date_str, msgs in dates.items():
                signal = self._fuse_ticker_day(ticker, date_str, msgs)
                fused_signals.append(signal)
        
        return fused_signals
    
    def _fuse_ticker_day(self, ticker: str, date_str: str, messages: List[FintechMessage]) -> Dict:
        """Fuse all messages for a ticker on a given day."""
        # Compute weights per message
        weighted_sentiments = []
        provenance = []
        total_weight = 0.0
        
        for msg in messages:
            # Source reliability weight
            src_weight = self.source_weights.get(msg.source, 0.1)
            
            # Engagement weight (log scale)
            engagement = sum(msg.engagement.values())
            eng_weight = np.log1p(engagement) / 10.0  # Normalize
            
            # Recency weight (exponential decay)
            hours_old = (datetime.utcnow() - msg.created_at).total_seconds() / 3600
            recency_weight = np.exp(-hours_old / self.recency_half_life_hours)
            
            # Extraction confidence
            conf_weight = msg.metadata.get("extraction_confidence", 0.5)
            
            # Combined weight
            combined_weight = src_weight * (1 + eng_weight) * recency_weight * conf_weight
            
            # Get sentiment (use source-provided if available, else compute)
            sentiment = msg.sentiment_score if msg.sentiment_score is not None else 0.0
            
            weighted_sentiments.append(sentiment * combined_weight)
            total_weight += combined_weight
            
            provenance.append({
                "source": msg.source,
                "source_id": msg.source_id,
                "weight": combined_weight,
                "sentiment": sentiment,
                "engagement": engagement,
                "author": msg.author,
                "url": msg.url
            })
        
        composite_sentiment = sum(weighted_sentiments) / total_weight if total_weight > 0 else 0.0
        
        # Category breakdown (if messages have category metadata)
        category_breakdown = self._compute_category_breakdown(messages)
        
        return {
            "ticker": ticker,
            "date": date_str,
            "composite_sentiment": composite_sentiment,
            "total_weight": total_weight,
            "message_count": len(messages),
            "sources": list(set(m.source for m in messages)),
            "provenance": provenance,
            "category_breakdown": category_breakdown,
            "created_at": datetime.utcnow().isoformat()
        }
    
    def _compute_category_breakdown(self, messages: List[FintechMessage]) -> Dict:
        """Break down sentiment by category if available."""
        # For fintech sources, infer category from ticker/sector
        # For Reddit, use subreddit category
        breakdown = defaultdict(lambda: {"sentiment": 0.0, "weight": 0.0, "count": 0})
        
        for msg in messages:
            category = msg.metadata.get("category", "unknown")
            src_weight = self.source_weights.get(msg.source, 0.1)
            breakdown[category]["sentiment"] += (msg.sentiment_score or 0) * src_weight
            breakdown[category]["weight"] += src_weight
            breakdown[category]["count"] += 1
        
        return {
            cat: {
                "weighted_sentiment": data["sentiment"] / data["weight"] if data["weight"] > 0 else 0,
                "total_weight": data["weight"],
                "message_count": data["count"]
            }
            for cat, data in breakdown.items()
        }
10. config/fintech_credentials.yaml — Fintech API Config
# Fintech API Credentials & Configuration
# NEVER commit real keys - use environment variables in production

stocktwits:
  access_token: "${STOCKTWITS_ACCESS_TOKEN}"  # OAuth2 token
  client_id: "${STOCKTWITS_CLIENT_ID}"
  client_secret: "${STOCKTWITS_CLIENT_SECRET}"
  base_url: "https://api.stocktwits.com/api/2"
  rate_limit:
    requests_per_minute: 200
    requests_per_hour: 1000

apewisdom:
  api_key: "${APEWISDOM_API_KEY}"
  base_url: "https://apewisdom.io/api/v1.0"
  rate_limit:
    requests_per_minute: 100
    requests_per_hour: 500

# Source priority for hybrid orchestration
priority:
  - "stocktwits"
  - "apewisdom"
  - "reddit"

# Failover thresholds
failover:
  min_fintech_messages: 50          # Trigger Reddit if fewer messages
  max_fintech_latency_ms: 5000      # Trigger fallback if too slow
  circuit_breaker_threshold: 5      # Failures before circuit opens
  circuit_breaker_timeout: 60       # Seconds before half-open
11. config/hybrid_weights.yaml — Hybrid Source Weights
# Hybrid Architecture Weight Configuration
# Controls how signals from different sources are combined

# Source-level weights (must sum to 1.0)
source_weights:
  stocktwits: 0.50      # Primary: structured, ticker-tagged, real-time
  apewisdom: 0.30       # Secondary: WSB-focused, aggregated sentiment
  reddit: 0.20          # Fallback: comprehensive but noisy

# Category weights (applied within each source)
category_weights:
  macro_geopolitical: 0.25
  fundamental_institutional: 0.25
  tech_product: 0.30
  retail_options: 0.20

# Subreddit weights (for Reddit fallback only)
subreddit_weights:
  macro_geopolitical:
    geopolitics: 0.35
    economics: 0.30
    supplychain: 0.35
  fundamental_institutional:
    SecurityAnalysis: 0.55
    ValueInvesting: 0.45
  tech_product:
    hardware: 0.50
    LocalLLaMA: 0.50
  retail_options:
    wallstreetbets: 0.40
    stocks: 0.25
    investing: 0.20
    options: 0.15

# Fusion parameters
fusion:
  recency_half_life_hours: 24
  engagement_log_scale: true
  min_messages_per_ticker: 3
  confidence_threshold: 0.3

# Backtesting optimization (extends existing)
optimization:
  enabled: true
  lookback_days: 252
  objective: "information_coefficient"
  rebalance_frequency: "monthly"
  method: "bayesian_optimization"
  # New: optimize source weights too
  optimize_source_weights: true
  source_weight_bounds:
    stocktwits: [0.3, 0.7]
    apewisdom: [0.1, 0.5]
    reddit: [0.0, 0.4]
12. db/schema_fintech.py — Extended Database Schema
import sqlite3
from datetime import datetime, timezone

def create_fintech_tables(conn: sqlite3.Connection) -> None:
    """Create tables for fintech API data and hybrid orchestration."""
    cursor = conn.cursor()
    
    # Fintech raw messages (unified schema)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fintech_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,                    -- stocktwits, apewisdom, reddit
            source_id TEXT NOT NULL,                 -- Native ID from source
            ticker TEXT NOT NULL,
            text TEXT,
            sentiment_score REAL,                    -- Source-provided sentiment
            author TEXT,
            created_utc INTEGER NOT NULL,
            scraped_at INTEGER NOT NULL,
            engagement_likes INTEGER DEFAULT 0,
            engagement_comments INTEGER DEFAULT 0,
            engagement_shares INTEGER DEFAULT 0,
            url TEXT,
            metadata_json TEXT,                      -- JSON blob for source-specific fields
            UNIQUE(source, source_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fintech_ticker_date ON fintech_messages(ticker, created_utc)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fintech_source_date ON fintech_messages(source, created_utc)")
    
    # API health monitoring
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_health_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            checked_at INTEGER NOT NULL,
            is_healthy BOOLEAN NOT NULL,
            latency_ms INTEGER,
            rate_limit_remaining INTEGER,
            rate_limit_reset INTEGER,
            error_message TEXT,
            consecutive_failures INTEGER DEFAULT 0
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_health_source_time ON api_health_log(source, checked_at)")
    
    # Circuit breaker state persistence
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS circuit_breaker_state (
            source TEXT PRIMARY KEY,
            state TEXT NOT NULL,                     -- closed, open, half_open
            failure_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            last_failure_at INTEGER,
            last_state_change_at INTEGER,
            updated_at INTEGER
        )
    """)
    
    # Source provenance for fused signals
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signal_provenance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            source TEXT NOT NULL,
            source_weight REAL NOT NULL,
            message_count INTEGER NOT NULL,
            weighted_sentiment REAL NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(ticker, date, source)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_provenance_ticker_date ON signal_provenance(ticker, date)")
    
    # Hybrid scrape runs log
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hybrid_scrape_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at INTEGER NOT NULL,
            completed_at INTEGER,
            fintech_sources_attempted TEXT,          -- JSON array
            fintech_sources_succeeded TEXT,          -- JSON array
            reddit_fallback_triggered BOOLEAN DEFAULT 0,
            total_messages_fintech INTEGER DEFAULT 0,
            total_messages_reddit INTEGER DEFAULT 0,
            total_tickers INTEGER DEFAULT 0,
            status TEXT,                             -- success, partial, failed
            error_message TEXT
        )
    """)
    
    conn.commit()

def migrate_existing_schema(conn: sqlite3.Connection) -> None:
    """Add columns to existing tables for hybrid support."""
    cursor = conn.cursor()
    
    # Add source column to daily_aggregations if not exists
    cursor.execute("PRAGMA table_info(daily_aggregations)")
    columns = [row[1] for row in cursor.fetchall()]
    if "source" not in columns:
        cursor.execute("ALTER TABLE daily_aggregations ADD COLUMN source TEXT DEFAULT 'reddit'")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_agg_source ON daily_aggregations(source)")
    
    # Add source to composite_scores
    cursor.execute("PRAGMA table_info(composite_scores)")
    columns = [row[1] for row in cursor.fetchall()]
    if "source_breakdown" not in columns:
        cursor.execute("ALTER TABLE composite_scores ADD COLUMN source_breakdown TEXT")  # JSON
    
    conn.commit()
13. scraper/reddit_client.py — Refactored as Fallback
# Key changes to existing RedditUniversalScraper:
# 1. Add circuit breaker integration
# 2. Add async wrapper for hybrid orchestrator
# 3. Add source tagging to all messages
# 4. Reduce default limits when used as fallback

class RedditUniversalScraper:
    def __init__(self, circuit_breaker: CircuitBreaker = None):
        # ... existing init ...
        self.circuit_breaker = circuit_breaker
        self.source_name = "reddit"
    
    async def scrape_fallback_async(self, tickers: List[str] = None) -> ScrapeResult:
        """Async wrapper for hybrid orchestrator."""
        if self.circuit_breaker and not self.circuit_breaker.can_execute():
            raise CircuitOpenError("Reddit circuit breaker OPEN")
        
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._scrape_fallback_sync, tickers)
    
    def _scrape_fallback_sync(self, tickers: List[str] = None) -> ScrapeResult:
        """Synchronous fallback scrape with reduced scope."""
        # Only scrape high-priority subreddits when fallback
        priority_subs = ["wallstreetbets", "stocks", "options", "SecurityAnalysis"]
        # ... existing scrape logic but filtered to priority_subs ...
        pass
14. run_scraper.py — Extended CLI
# New commands to add:
# python run_scraper.py scrape-fintech        # Scrape only fintech APIs
# python run_scraper.py scrape-hybrid         # Full hybrid orchestration
# python run_scraper.py health-check          # Check all source health
# python run_scraper.py circuit-status        # Show circuit breaker states

def cmd_scrape_fintech(args):
    """Scrape only fintech APIs (StockTwits, ApeWisdom)."""
    orchestrator = HybridOrchestrator()
    # Run only fintech phase
    asyncio.run(orchestrator._scrape_fintech_only(args.tickers))

def cmd_scrape_hybrid(args):
    """Run full hybrid orchestration (fintech primary + reddit fallback)."""
    orchestrator = HybridOrchestrator()
    results = asyncio.run(orchestrator.scrape_all(args.tickers))
    # Print summary

def cmd_health_check(args):
    """Check health of all data sources."""
    factory = FintechClientFactory()
    health = asyncio.run(factory.health_check_all())
    for source, h in health.items():
        status = "✅ HEALTHY" if h.is_healthy else "❌ UNHEALTHY"
        print(f"{source}: {status} (failures: {h.consecutive_failures})")

def cmd_circuit_status(args):
    """Show circuit breaker states."""
    monitor = HealthMonitor()
    status = monitor.get_all_status()
    for source, s in status.items():
        print(f"{source}: {s['circuit_state']} | Success: {s['success_rate_100']:.1%} | Latency: {s['avg_latency_ms']:.0f}ms")
✅ Overall Architecture Checklist
Component	Status	Validation Criteria
Fintech API Layer (Primary - Option B)	 	 
StockTwits client	☐ Planned	Fetches symbol streams, trending, handles OAuth2
ApeWisdom client	☐ Planned	Fetches WSB mentions, trending tickers, sentiment
Rate limiter (token bucket)	☐ Planned	Enforces per-API quotas, thread-safe, async
Client factory	☐ Planned	Instantiates clients, manages credentials, health checks
Response normalizer	☐ Planned	Unified schema, deduplication, confidence scoring
Hybrid Orchestration (Option C)	 	 
Hybrid orchestrator	☐ Planned	Routes primary→fallback, coverage thresholds
Health monitor	☐ Planned	Circuit breakers, success rates, latency tracking
Data fusion engine	☐ Planned	Provenance-weighted fusion, category breakdown
Database Extensions	 	 
fintech_messages table	☐ Planned	Unified raw message storage with source tracking
api_health_log table	☐ Planned	Historical health for dashboard/alerting
circuit_breaker_state table	☐ Planned	Persistent circuit state across restarts
signal_provenance table	☐ Planned	Per-source contribution to composite signals
hybrid_scrape_runs table	☐ Planned	Audit trail of hybrid scrape executions
Configuration	 	 
fintech_credentials.yaml	☐ Planned	API keys, endpoints, rate limits (env var substitution)
hybrid_weights.yaml	☐ Planned	Source weights, failover thresholds, fusion params
Integration & CLI	 	 
run_scraper.py new commands	☐ Planned	scrape-fintech, scrape-hybrid, health-check, circuit-status
Reddit client refactor	☐ Planned	Circuit breaker aware, async wrapper, reduced scope fallback
Feature store updates	☐ Planned	Source-aware features, provenance in pivoted data
Dashboard & Monitoring	 	 
Source health panel	☐ Planned	Real-time circuit state, success rates, latency
Source breakdown charts	☐ Planned	Stacked sentiment by source, not just category
Failover indicators	☐ Planned	Visual alerts when Reddit fallback activates
Backtesting & Optimization	 	 
Multi-source backtest	☐ Planned	Attribute returns to source contributions
Source weight optimization	☐ Planned	Optuna optimizes source_weights + category_weights
Drift detection per source	☐ Planned	Monitor IC decay per source independently
Testing & Quality	 	 
Unit tests: fintech clients	☐ Planned	Mock API responses, test normalization, rate limiting
Unit tests: health monitor	☐ Planned	Circuit breaker transitions, failure thresholds
Integration tests: orchestrator	☐ Planned	Failover scenarios, data fusion correctness
Load tests	☐ Planned	Concurrent API calls, rate limit compliance
Documentation & Compliance	 	 
API usage compliance	☐ Planned	Respect ToS, rate limits, attribution requirements
Data retention policy	☐ Planned	Fintech raw messages: 90 days, aggregates: indefinite
Architecture decision log	☐ Planned	ADR for hybrid choice, failover thresholds
🔑 Key Architectural Decisions
Rationale
StockTwits/ApeWisdom are HTTP APIs; async enables parallel fetching
Isolates failures; one API down doesn't block others
Audit trail for admissions/portfolio review; debug weight optimization
Not just "API failed" but "insufficient signal" triggers Reddit
Bayesian optimization finds optimal stocktwits/apewisdom/reddit blend
Enables single fusion engine, single feature store, single dashboard
Reduces PRAW calls 80-90%, avoids Cloudflare/rate-limit fragility
🎯 Next Steps
Create fintech client package (scraper/fintech_clients/) with base, stocktwits, apewisdom, factory, rate_limiter, normalizer
Add configuration files (config/fintech_credentials.yaml, config/hybrid_weights.yaml)
Extend database schema (db/schema_fintech.py + migration in db/schema.py)
Build hybrid orchestrator with health monitor and data fusion
Refactor Reddit client as circuit-breaker-aware fallback
Update CLI with new hybrid commands
Extend dashboard with source health and provenance views
Add comprehensive tests (unit + integration)
Run end-to-end validation with paper trading signals
Questions for you:
StockTwits API access: Do you have OAuth2 credentials, or should I implement the public (unauthenticated) endpoints with lower rate limits?
ApeWisdom API: Do you have an API key, or should I implement the free tier limits?
Failover threshold: Is 50 fintech messages/day the right trigger for Reddit fallback, or should it be ticker-specific?
Source weights: Should the initial weights be 50/30/20 (StockTwits/ApeWisdom/Reddit) or do you want different defaults?
Deployment: Will this run on a schedule (cron) or as a long-running service? Affects connection pooling and health check frequency.
# Todos
Analyze current architecture and design Hybrid Fallback Architecture (Option C)
Create glossary table of files to be created/edited
Define skeleton structure for each new/modified file
Create overall architecture checklist
+ Thought: 786ms
The implementation plan is complete. Here's the summary:
Hybrid Fallback Architecture (Option C) — Implementation Plan
📋 26 Files Total
13 New Files: Fintech API clients (StockTwits, ApeWisdom), rate limiter, factory, normalizer, hybrid orchestrator, health monitor, data fusion, config files, extended schema, tests
13 Modified Files: Reddit client (fallback refactor), CLI, dashboard, feature store, backtesting, optimization, database, constants, weights
🏗️ Core Architecture
┌─────────────────────────────────────────────────────────────┐
│                    HYBRID ORCHESTRATOR                       │
│  1. Health Check All Sources (Circuit Breakers)             │
│  2. PRIMARY: Fetch from healthy fintech APIs (parallel)     │
│     ├── StockTwits (symbol streams, trending)               │
│     └── ApeWisdom (WSB mentions, sentiment)                 │
│  3. Coverage Check: messages ≥ threshold?                   │
│  4. FALLBACK: Reddit PRAW (only if needed)                  │
│  5. FUSE: Provenance-weighted signal fusion                 │
│  6. PERSIST: Unified schema with source attribution         │
└─────────────────────────────────────────────────────────────┘
✅ Key Differentiators from Current Architecture
Aspect	Current (Option A Only)	Hybrid (Option C)
Primary Engine	Reddit PRAW (fragile)	StockTwits + ApeWisdom APIs (structured)
Fallback	None	Reddit (triggered by coverage/health)
Rate Limits	Cloudflare walls, CSS breaks	Token bucket per API, circuit breakers
Signal Quality	Noisy, heavy filtering	Pre-tagged tickers, source sentiment
Compute	Headless, DOM rendering	Native requests.get() → JSON
Audit Trail	Limited	Full provenance per source
❓ 5 Clarifying Questions
StockTwits OAuth2 — Do you have credentials, or use public endpoints (lower limits)?
ApeWisdom API key — Have one, or use free tier?
Fallback threshold — 50 messages/day global, or per-ticker?
Initial source weights — 50/30/20 (ST/AW/Reddit) OK?
Deployment model — Cron job or long-running service?
Ready to implement when you confirm these details.
▣  Plan · Nemotron 3 Ultra Free · 26m 7s
1. StockTwits OAuth2 — Public, Keyless Reads
The Reality: You do not need an OAuth2 app token, client secret, or credentials. The endpoint https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json is completely open for public reads.

The Constraint: It enforces a standard IP-based rate limit (roughly 200 requests per hour per IP address). As a local tool for an individual user, you will never hit this limit during normal use.

2. ApeWisdom API — True Keyless Open Data
The Reality: You do not need an API key or a developer account. ApeWisdom leaves their primary query endpoints completely public and open for the open-source developer community.

The Constraint: Just like StockTwits, it limits requests by matching your local IP address. Since it's just your app making a request every few days on Tuesdays and Fridays, you will stay completely under their radar.

3. Fallback Threshold — Per-Ticker / Response-Driven
The Choice: Per-Ticker & HTTP Status Driven is the best option.

The Implementation: Do not write a rigid countdown tracker into your code. Instead, design your fallback to trigger purely based on the web response code. If a request returns an HTTP 429 (Too Many Requests) or a connection timeout, your code flags that specific ticker as "Throttled" and instantly routes the request to the next alternative adapter. This approach handles rate limits dynamically without requiring hardcoded limits.

4. Initial Source Weights — The Hybrid Strategy Fit
The Choice: Since the main foundation of your multi-factor plan is tracking specific Reddit categories, your configuration should reflect that hierarchy:

Scenario A (Native Mirror Works): Reddit holds 100% weight. Your VADER logic parses the exact text threads from r/LocalLLaMA or r/supplychain.

Scenario B (Native Mirror Blocked): The fallback initiates. ApeWisdom takes 60% weight (because it is pre-scraped Reddit data), and StockTwits takes 40% weight (providing broader retail market text). This architecture ensures your system remains aligned with your original goals even during a fallback event.

5. Deployment Model — On-Demand Execution
The Choice: On-Demand Task Execution is the right choice for your local app.

The Implementation: Since your goal is to build a self-contained application that boots up with a single double-click and runs locally on someone's laptop, you should avoid background cron daemons or 24/7 terminal processes. They waste battery and can cause background process lockups.

When the user hits the "Run Scraper Now" button on your PyWebView frontend, your background FastAPI app executes a single, rapid one-time sync function to query the endpoints, saves the rows to your local SQLite file, and goes completely idle. 
+ Thought: 26.9s
Excellent clarifications. This significantly simplifies the architecture. Let me update the plan:
Updated Hybrid Fallback Architecture Plan
🔑 Key Changes from Clarifications
Aspect	Original Plan	Updated Plan
Auth	OAuth2 + API keys	Zero credentials - all public endpoints
Rate Limiting	Token bucket per API	Simple retry/backoff on HTTP 429/timeout
Fallback Trigger	Coverage count threshold	Per-ticker HTTP 429/timeout
Source Weights	Fixed 50/30/20	Dynamic: Reddit 100% → (ApeWisdom 60% + StockTwits 40%)
Execution	Scheduled/cron	On-demand via PyWebView → FastAPI → SQLite
📋 Revised File Glossary (22 files, down from 26)
#	File	Type	Change
NEW - Fintech Clients (Keyless)	 	 	 
1	scraper/fintech_clients/__init__.py	New	Package init
2	scraper/fintech_clients/base.py	New	Abstract base with retry/backoff
3	scraper/fintech_clients/stocktwits.py	New	Public endpoint client (no auth)
4	scraper/fintech_clients/apewisdom.py	New	Public endpoint client (no auth)
5	scraper/fintech_clients/factory.py	New	Client factory (no credentials)
6	scraper/fintech_clients/normalizer.py	New	Unified schema normalizer
NEW - Hybrid Orchestration	 	 	 
7	scraper/hybrid_orchestrator.py	New	Per-ticker fallback on 429/timeout
8	scraper/health_monitor.py	New	Simple circuit breaker (no persistent state needed)
9	scraper/data_fusion.py	New	Dynamic weight fusion (Scenario A/B)
NEW - Config (Simplified)	 	 	 
10	config/hybrid_config.yaml	New	Single config: dynamic weights, retry params, endpoints
MODIFIED	 	 	 
11	scraper/engine.py	Edit	Add fintech sentiment processing
12	scraper/reddit_client.py	Edit	Add per-ticker 429/timeout detection
13	run_scraper.py	Edit	scrape-hybrid command (on-demand)
14	db/connection.py	Edit	On-demand connection (no pooling)
15	db/schema.py	Edit	Add source column, fintech_messages table
16	db/feature_store.py	Edit	Source-aware features
17	dashboard/tab_sentiment_risk.py	Edit	Show active scenario (A/B), source breakdown
18	config/weights.py	Edit	Load dynamic hybrid config
19	config/constants.py	Edit	Add fintech validation keywords
20	backtesting/backtest.py	Edit	Multi-source backtest with scenario awareness
21	optimization/optuna_search.py	Edit	Optimize within Scenario B weights
22	tests/test_hybrid.py	New	Integration tests for fallback logic
Removed: rate_limiter.py, fintech_credentials.yaml, schema_fintech.py, circuit_breaker_state table, api_health_log table
🏗️ Revised Skeleton Structures
scraper/fintech_clients/base.py — Keyless Base with Retry
import asyncio
import aiohttp
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from datetime import datetime

@dataclass
class FintechMessage:
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

class BaseFintechClient(ABC):
    """Keyless public API client with exponential backoff on 429/timeout."""
    
    def __init__(self, base_url: str, max_retries: int = 3, base_delay: float = 1.0):
        self.base_url = base_url
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=10)
        self.session = aiohttp.ClientSession(timeout=timeout)
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    async def _get_with_backoff(self, url: str, params: Dict = None) -> Dict:
        """GET with exponential backoff on 429/timeout."""
        for attempt in range(self.max_retries):
            try:
                async with self.session.get(url, params=params) as resp:
                    if resp.status == 429:
                        raise RateLimitedError(f"429 from {url}")
                    resp.raise_for_status()
                    return await resp.json()
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                if attempt == self.max_retries - 1:
                    raise
                delay = self.base_delay * (2 ** attempt)
                await asyncio.sleep(delay)
        raise RuntimeError("Max retries exceeded")
    
    @abstractmethod
    async def fetch_messages(self, tickers: List[str], limit: int = 100) -> List[FintechMessage]:
        pass
    
    @abstractmethod
    async def fetch_trending(self, limit: int = 50) -> List[FintechMessage]:
        pass
    
    @abstractmethod
    def normalize_message(self, raw: Dict, ticker: str) -> FintechMessage:
        pass

class RateLimitedError(Exception):
    """Raised when HTTP 429 received - triggers per-ticker fallback."""
    pass
scraper/hybrid_orchestrator.py — Per-Ticker Fallback Logic
import asyncio
import logging
from typing import List, Dict, Set
from dataclasses import dataclass
from datetime import datetime

from scraper.fintech_clients.factory import FintechClientFactory
from scraper.fintech_clients.normalizer import FintechNormalizer
from scraper.reddit_client import RedditUniversalScraper
from scraper.data_fusion import DataFusionEngine
from config import load_hybrid_config

logger = logging.getLogger(__name__)

@dataclass
class TickerResult:
    ticker: str
    source: str              # "stocktwits", "apewisdom", "reddit"
    messages: List[FintechMessage]
    fallback_triggered: bool = False
    fallback_reason: str = ""  # "429", "timeout", "error"

class HybridOrchestrator:
    """
    Per-Ticker HTTP-Status-Driven Fallback (Option C).
    
    For EACH ticker independently:
    1. Try StockTwits → if 429/timeout → try ApeWisdom
    2. Try ApeWisdom → if 429/timeout → mark for Reddit fallback
    3. After all tickers processed, if ANY need fallback → run Reddit for those tickers only
    4. Fuse with dynamic weights:
       - Scenario A (no fallback): Reddit 100%
       - Scenario B (fallback used): ApeWisdom 60%, StockTwits 40%
    """
    
    def __init__(self):
        self.factory = FintechClientFactory()
        self.normalizer = FintechNormalizer()
        self.fusion_engine = DataFusionEngine()
        self.config = load_hybrid_config()
        self.reddit_scraper = RedditUniversalScraper()
    
    async def scrape_tickers(self, tickers: List[str]) -> Dict[str, TickerResult]:
        """Scrape all tickers with per-ticker fallback."""
        results = {}
        
        # Phase 1: Try fintech sources per ticker
        async with self.factory.create_clients() as clients:
            for ticker in tickers:
                result = await self._scrape_single_ticker(ticker, clients)
                results[ticker] = result
        
        # Phase 2: Collect tickers needing Reddit fallback
        fallback_tickers = [t for t, r in results.items() if r.fallback_triggered]
        
        if fallback_tickers:
            logger.info(f"Fallback triggered for {len(fallback_tickers)} tickers: {fallback_tickers}")
            reddit_results = await self._scrape_reddit_fallback(fallback_tickers)
            # Merge Reddit messages into results
            for ticker, msgs in reddit_results.items():
                if ticker in results:
                    results[ticker].messages.extend(msgs)
                    results[ticker].source = "hybrid"
                else:
                    results[ticker] = TickerResult(ticker=ticker, source="reddit", messages=msgs,
                                                  fallback_triggered=True, fallback_reason="reddit_only")
        
        # Phase 3: Fuse and persist
        all_messages = [msg for r in results.values() for msg in r.messages]
        fused = self.fusion_engine.fuse(all_messages, self._determine_scenario(results))
        await self._persist(fused)
        
        return results
    
    async def _scrape_single_ticker(self, ticker: str, clients: Dict) -> TickerResult:
        """Try StockTwits → ApeWisdom for a single ticker."""
        messages = []
        fallback_triggered = False
        fallback_reason = ""
        
        # Try StockTwits first
        try:
            st_msgs = await clients["stocktwits"].fetch_messages([ticker], limit=100)
            messages.extend(self.normalizer.normalize_batch(st_msgs))
        except RateLimitedError:
            fallback_triggered = True
            fallback_reason = "429_stocktwits"
            logger.warning(f"{ticker}: StockTwits 429, trying ApeWisdom")
        except asyncio.TimeoutError:
            fallback_triggered = True
            fallback_reason = "timeout_stocktwits"
            logger.warning(f"{ticker}: StockTwits timeout, trying ApeWisdom")
        
        # Try ApeWisdom if StockTwits failed or as supplement
        if fallback_triggered or self.config.get("always_query_both", True):
            try:
                aw_msgs = await clients["apewisdom"].fetch_messages([ticker], limit=100)
                messages.extend(self.normalizer.normalize_batch(aw_msgs))
            except RateLimitedError:
                if not fallback_triggered:
                    fallback_triggered = True
                    fallback_reason = "429_apewisdom"
                logger.warning(f"{ticker}: ApeWisdom 429, will use Reddit fallback")
            except asyncio.TimeoutError:
                if not fallback_triggered:
                    fallback_triggered = True
                    fallback_reason = "timeout_apewisdom"
                logger.warning(f"{ticker}: ApeWisdom timeout, will use Reddit fallback")
        
        # Deduplicate
        messages = self.normalizer.deduplicate(messages)
        
        return TickerResult(
            ticker=ticker,
            source="fintech" if not fallback_triggered else "partial_fintech",
            messages=messages,
            fallback_triggered=fallback_triggered,
            fallback_reason=fallback_reason
        )
    
    def _determine_scenario(self, results: Dict[str, TickerResult]) -> str:
        """Determine which weight scenario to use."""
        any_fallback = any(r.fallback_triggered for r in results.values())
        return "B" if any_fallback else "A"
    
    async def _scrape_reddit_fallback(self, tickers: List[str]) -> Dict[str, List[FintechMessage]]:
        """Run Reddit scraper for specific tickers only."""
        # RedditUniversalScraper now supports ticker-filtered scraping
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.reddit_scraper.scrape_tickers, tickers)
config/hybrid_config.yaml — Single Dynamic Config
# Hybrid Architecture Configuration
# No credentials needed - all public endpoints

# API Endpoints (public, keyless)
endpoints:
  stocktwits:
    base_url: "https://api.stocktwits.com/api/2"
    symbol_stream: "/streams/symbol/{ticker}.json"
    trending: "/trending/symbols.json"
  apewisdom:
    base_url: "https://apewisdom.io/api/v1.0"
    ticker_filter: "/filter/ticker/{ticker}"
    trending: "/filter/all"

# Retry/Backoff Settings
retry:
  max_retries: 3
  base_delay_seconds: 1.0
  max_delay_seconds: 10.0
  timeout_seconds: 10

# Dynamic Weight Scenarios
scenarios:
  A:  # Reddit working - primary source
    name: "Reddit Primary"
    description: "Native Reddit mirrors accessible, full category taxonomy active"
    weights:
      reddit: 1.0
      stocktwits: 0.0
      apewisdom: 0.0
    category_weights:
      macro_geopolitical: 0.25
      fundamental_institutional: 0.25
      tech_product: 0.30
      retail_options: 0.20
  
  B:  # Reddit blocked - fintech fallback
    name: "Fintech Fallback"
    description: "Reddit mirrors blocked/throttled, using pre-scraped Reddit (ApeWisdom) + StockTwits"
    weights:
      reddit: 0.0
      apewisdom: 0.60    # Pre-scraped WSB data
      stocktwits: 0.40   # Broader retail sentiment
    # Category weights still apply within each source
    category_weights:
      macro_geopolitical: 0.25
      fundamental_institutional: 0.25
      tech_product: 0.30
      retail_options: 0.20

# Fallback Behavior
fallback:
  trigger_on: ["429", "timeout", "connection_error"]
  per_ticker: true                    # Independent fallback per ticker
  always_query_both_fintech: true     # Query both ST + AW even if first succeeds
  reddit_priority_subreddits:         # When fallback, only scrape these
    - "wallstreetbets"
    - "stocks"
    - "options"
    - "SecurityAnalysis"
    - "ValueInvesting"
    - "LocalLLaMA"
    - "hardware"
    - "geopolitics"
    - "economics"
    - "supplychain"

# Fusion Parameters
fusion:
  recency_half_life_hours: 24
  engagement_log_scale: true
  min_messages_per_ticker: 3
  confidence_threshold: 0.3

# Backtesting (extends existing)
optimization:
  enabled: true
  lookback_days: 252
  objective: "information_coefficient"
  rebalance_frequency: "monthly"
  method: "bayesian_optimization"
  # Only optimize Scenario B weights (Scenario A is fixed Reddit 100%)
  optimize_scenario_b: true
  scenario_b_bounds:
    apewisdom: [0.4, 0.8]
    stocktwits: [0.2, 0.6]
scraper/reddit_client.py — Per-Ticker 429 Detection
# Key additions to RedditUniversalScraper:

async def scrape_tickers(self, tickers: List[str]) -> Dict[str, List[FintechMessage]]:
    """Scrape only priority subreddits for specific tickers (fallback mode)."""
    # Filter to priority subs from config
    # Use PRAW with ticker-aware search
    # Return dict: ticker -> List[FintechMessage]
    pass

def _check_rate_limit_response(self, response) -> bool:
    """Check if PRAW response indicates rate limit (429)."""
    # PRAW raises prawcore.exceptions.TooManyRequests on 429
    pass
✅ Updated Architecture Checklist
Component	Status	Validation
Fintech Clients (Keyless)	 	 
StockTwits public client	☐ Planned	GET /api/2/streams/symbol/{ticker}.json
ApeWisdom public client	☐ Planned	GET /api/v1.0/filter/ticker/{ticker}
Exponential backoff on 429/timeout	☐ Planned	3 retries, 1s/2s/4s delays
Hybrid Orchestration	 	 
Per-ticker fallback logic	☐ Planned	Independent per ticker
Scenario A/B weight switching	☐ Planned	Dynamic based on fallback
Reddit priority-sub fallback	☐ Planned	Only 10 priority subs
Database	 	 
fintech_messages table	☐ Planned	Unified schema
source column on aggregations	☐ Planned	Track provenance
CLI & Execution	 	 
scrape-hybrid command	☐ Planned	On-demand, single sync
FastAPI background endpoint	☐ Planned	/api/scrape/hybrid
Dashboard	 	 
Scenario indicator (A/B)	☐ Planned	Visual badge
Source breakdown chart	☐ Planned	Stacked by source
Fallback ticker list	☐ Planned	Show which tickers fell back
❓ One Final Clarification
PyWebView + FastAPI Integration: You mentioned "PyWebView frontend" and "background FastAPI app". 
Is the FastAPI app already in the codebase? (I didn't see it in the file list)
Should run_scraper.py scrape-hybrid be the FastAPI endpoint handler, or a separate module?
Does the PyWebView app exist yet, or is that future work?
This affects whether I create a new api/ module or extend run_scraper.py with a FastAPI wrapper.
▣  Plan · Nemotron 3 Ultra Free · 5m 20s
1. Is the FastAPI app already in your codebase?
No, it does not exist yet. Looking closely at your directory structure from your screenshots (like optuna_search.py, stream_quant.py, scraper/), your project is currently running purely as a Streamlit application.

The FastAPI backend and PyWebView frontend are the structural upgrades we designed to transition your project into a self-contained, 1-click desktop app for your college applications.

2. Should run_scraper.py be the FastAPI handler or a separate module?
It should remain a separate module, wrapped by FastAPI. You want to maintain strict modularity (separation of concerns). Keep run_scraper.py (or your hybrid_engine.py) as a pure, standalone script that can be executed completely on its own via the terminal or imported as a python module.

When you build your API layer, you will create a new, separate file at your root level—for example, app_launcher.py. This file will import your scraper functions and map them to a FastAPI URL path, like this:

Python
# Inside your new app_launcher.py (Root level)
from fastapi import FastAPI, BackgroundTasks
from scraper.hybrid_engine import HybridSocialEngine  # Your modular script

app = FastAPI()
engine = HybridSocialEngine()

@app.post("/api/scrape/trigger")
def trigger_scrape(ticker: str, category: str, subreddit: str, background_tasks: BackgroundTasks):
    # FastAPI handles the asynchronous background thread execution effortlessly
    background_tasks.add_task(engine.execute_sync_pipeline, ticker, category, subreddit)
    return {"status": "processing", "message": "Hybrid delta-sync loop initialized."}
3. Does the PyWebView app exist yet?
No, that is future work. Right now, your local testing is perfectly positioned to happen on Streamlit.

Because we decoupled your scraper from PRAW and pivoted to the keyless Option C Hybrid Engine (ApeWisdom + StockTwits), your data requests complete in fractions of a second. This means you can keep testing your backend changes directly inside your existing Streamlit tabs (stream_quant.py) by executing your local run commands without worrying about freezing your UI. 
+ Thought: 19.1s
Perfect. This clarifies the architecture boundaries. Let me finalize the comprehensive plan.
Final Hybrid Fallback Architecture Plan (Option C)
🎯 Scope: Core Hybrid Engine Only
In Scope: Keyless fintech clients, per-ticker fallback logic, dynamic weight fusion, Streamlit integration
Out of Scope: FastAPI (app_launcher.py), PyWebView frontend, background daemons — future structural upgrades
📋 Final File Glossary (20 Files)
#	File	Type	Purpose
NEW - Fintech Clients (Keyless, Public APIs)	 	 	 
1	scraper/fintech_clients/__init__.py	New	Package exports
2	scraper/fintech_clients/base.py	New	Abstract base with exponential backoff (429/timeout)
3	scraper/fintech_clients/stocktwits.py	New	GET /api/2/streams/symbol/{ticker}.json client
4	scraper/fintech_clients/apewisdom.py	New	GET /api/v1.0/filter/ticker/{ticker} client
5	scraper/fintech_clients/factory.py	New	Client factory (no credentials, context manager)
6	scraper/fintech_clients/normalizer.py	New	Unified FintechMessage schema, deduplication
NEW - Hybrid Orchestration	 	 	 
7	scraper/hybrid_engine.py	New	Main entry point (replaces run_scraper.py import) — per-ticker fallback, Scenario A/B weights
8	scraper/data_fusion.py	New	Provenance-weighted fusion, category breakdown
NEW - Config	 	 	 
9	config/hybrid_config.yaml	New	Endpoints, retry params, Scenario A/B weights, fallback rules
MODIFIED - Integration	 	 	 
10	scraper/engine.py	Edit	Add fintech sentiment processing, reuse VADER + custom lexicon
11	scraper/reddit_client.py	Edit	Add scrape_tickers(tickers: List[str]) for priority-sub fallback
12	run_scraper.py	Edit	Add scrape-hybrid command → calls HybridSocialEngine().execute_sync_pipeline()
13	db/schema.py	Edit	Add fintech_messages table, source column on daily_aggregations
14	db/connection.py	Edit	On-demand connections (no pooling needed for Streamlit)
15	db/feature_store.pystore.py`	Edit	Source-aware pivoted features, scenario indicator
16	dashboard/tab_sentiment_risk.py	Edit	Show active scenario (A/B), source breakdown, fallback tickers
17	config/weights.py	Edit	Load hybrid_config.yaml, expose SCENARIO_A_WEIGHTS, SCENARIO_B_WEIGHTS
18	config/constants.py	Edit	Add fintech validation keywords, ticker patterns
19	backtesting/backtest.py	Edit	Multi-source backtest with scenario awareness
20	optimization/optuna_search.py	Edit	Optimize Scenario B weights (ApeWisdom/StockTwits blend)
TESTS	 	 	 
21	tests/test_hybrid_engine.py	New	Integration tests: fallback triggers, fusion correctness
🏗️ Core Skeleton: scraper/hybrid_engine.py (Main Entry Point)
"""
HybridSocialEngine — Standalone, importable, CLI-callable.
Used by: run_scraper.py (today), app_launcher.py (future FastAPI), Streamlit (direct import).
"""

import asyncio
import logging
from typing import List, Dict, Optional
from dataclasses import dataclass
from datetime import datetime

from scraper.fintech_clients.factory import FintechClientFactory
from scraper.fintech_clients.normalizer import FintechNormalizer
from scraper.reddit_client import RedditUniversalScraper
from scraper.data_fusion import DataFusionEngine
from config import load_hybrid_config, SCENARIO_A_WEIGHTS, SCENARIO_B_WEIGHTS
from db.jobs import record_hybrid_run

logger = logging.getLogger(__name__)

@dataclass
class TickerResult:
    ticker: str
    source: str                    # "stocktwits", "apewisdom", "reddit", "hybrid"
    messages: List["FintechMessage"]
    fallback_triggered: bool = False
    fallback_reason: str = ""      # "429", "timeout", "error"

class HybridSocialEngine:
    """
    Per-Ticker HTTP-Status-Driven Fallback (Option C).
    
    Flow per ticker:
    1. StockTwits → if 429/timeout → ApeWisdom
    2. ApeWisdom → if 429/timeout → mark for Reddit fallback
    3. After all tickers: run Reddit for fallback tickers only (priority subs)
    4. Fuse with dynamic weights:
       - Scenario A (no fallback): Reddit 100% (full category taxonomy)
       - Scenario B (fallback used): ApeWisdom 60% + StockTwits 40%
    """
    
    def __init__(self):
        self.factory = FintechClientFactory()
        self.normalizer = FintechNormalizer()
        self.fusion = DataFusionEngine()
        self.config = load_hybrid_config()
        self.reddit = RedditUniversalScraper()
    
    def execute_sync_pipeline(self, tickers: Optional[List[str]] = None) -> Dict:
        """Synchronous entry point for CLI / FastAPI background task / Streamlit."""
        return asyncio.run(self._execute_async(tickers))
    
    async def _execute_async(self, tickers: Optional[List[str]]) -> Dict:
        start = datetime.utcnow()
        tickers = tickers or self._get_default_tickers()
        
        # Phase 1: Fintech per ticker
        results = await self._scrape_fintech_per_ticker(tickers)
        
        # Phase 2: Reddit fallback for throttled tickers
        fallback_tickers = [t for t, r in results.items() if r.fallback_triggered]
        if fallback_tickers:
            reddit_msgs = await self._scrape_reddit_fallback(fallback_tickers)
            for t, msgs in reddit_msgs.items():
                results[t].messages.extend(msgs)
                results[t].source = "hybrid"
        
        # Phase 3: Fuse & persist
        all_msgs = [m for r in results.values() for m in r.messages]
        scenario = "B" if fallback_tickers else "A"
        fused = self.fusion.fuse(all_msgs, scenario)
        self._persist_fused(fused, scenario)
        
        # Record run
        record_hybrid_run(
            started_at=start,
            completed_at=datetime.utcnow(),
            scenario=scenario,
            tickers_processed=len(tickers),
            fallback_tickers=fallback_tickers,
            total_messages=len(all_msgs)
        )
        
        return {
            "scenario": scenario,
            "tickers": {t: {"source": r.source, "count": len(r.messages), "fallback": r.fallback_triggered} 
                       for t, r in results.items()},
            "fused_signals": fused
        }
    
    async def _scrape_fintech_per_ticker(self, tickers: List[str]) -> Dict[str, TickerResult]:
        results = {}
        async with self.factory.create_clients() as clients:
            for ticker in tickers:
                results[ticker] = await self._try_fintech_sources(ticker, clients)
        return results
    
    async def _try_fintech_sources(self, ticker: str, clients: Dict) -> TickerResult:
        msgs = []
        fallback = False
        reason = ""
        
        # StockTwits first
        try:
            st = await clients["stocktwits"].fetch_messages([ticker], limit=100)
            msgs.extend(self.normalizer.normalize_batch(st))
        except (RateLimitedError, asyncio.TimeoutError) as e:
            fallback, reason = True, f"stocktwits_{type(e).__name__}"
        
        # ApeWisdom (always query for redundancy, or only on fallback)
        if self.config["fallback"]["always_query_both_fintech"] or fallback:
            try:
                aw = await clients["apewisdom"].fetch_messages([ticker], limit=100)
                msgs.extend(self.normalizer.normalize_batch(aw))
            except (RateLimitedError, asyncio.TimeoutError) as e:
                if not fallback:
                    fallback, reason = True, f"apewisdom_{type(e).__name__}"
        
        return TickerResult(
            ticker=ticker,
            source="fintech" if not fallback else "partial_fintech",
            messages=self.normalizer.deduplicate(msgs),
            fallback_triggered=fallback,
            fallback_reason=reason
        )
    
    async def _scrape_reddit_fallback(self, tickers: List[str]) -> Dict[str, List]:
        # Run sync Reddit scraper in thread pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.reddit.scrape_tickers, tickers)
    
    def _persist_fused(self, fused: List[Dict], scenario: str):
        # Write to daily_aggregations with source="hybrid" + scenario in metadata
        # Write provenance to signal_provenance table
        pass
    
    def _get_default_tickers(self) -> List[str]:
        # From config or portfolio holdings
        return ["AAPL", "MSFT", "NVDA", "TSLA", "SPY", "QQQ"]  # placeholder
⚙️ Config: config/hybrid_config.yaml
endpoints:
  stocktwits:
    base_url: "https://api.stocktwits.com/api/2"
    symbol_stream: "/streams/symbol/{ticker}.json"
    trending: "/trending/symbols.json"
  apewisdom:
    base_url: "https://apewisdom.io/api/v1.0"
    ticker_filter: "/filter/ticker/{ticker}"
    trending: "/filter/all"

retry:
  max_retries: 3
  base_delay_seconds: 1.0
  timeout_seconds: 10

scenarios:
  A:
    name: "Reddit Primary"
    weights: {reddit: 1.0, stocktwits: 0.0, apewisdom: 0.0}
    category_weights:
      macro_geopolitical: 0.25
      fundamental_institutional: 0.25
      tech_product: 0.30
      retail_options: 0.20
  
  B:
    name: "Fintech Fallback"
    weights: {reddit: 0.0, apewisdom: 0.60, stocktwits: 0.40}
    category_weights:
      macro_geopolitical: 0.25
      fundamental_institutional: 0.25
      tech_product: 0.30
      retail_options: 0.20

fallback:
  trigger_on: ["429", "timeout", "connection_error"]
  per_ticker: true
  always_query_both_fintech: true
  reddit_priority_subreddits:
    - "wallstreetbets"
    - "stocks"
    - "options"
    - "SecurityAnalysis"
    - "ValueInvesting"
    - "LocalLLaMA"
    - "hardware"
    - "geopolitics"
    - "economics"
    - "supplychain"

fusion:
  recency_half_life_hours: 24
  engagement_log_scale: true
  min_messages_per_ticker: 3

optimization:
▣  Plan · Nemotron 3 Ultra Free · interrupted
continue
▣  Plan · Nemotron 3 Ultra Free · interrupted
continue
+ Thought: 1.3s
# Todos
Finalize comprehensive implementation plan for Hybrid Fallback Architecture
Present final plan with all clarifications incorporated
+ Thought: 538ms
Final Implementation Plan: Hybrid Fallback Architecture (Option C)
📋 File Glossary — 22 Files Total
#	File Path	Type	Purpose
NEW — Fintech API Clients (Keyless, Public Endpoints)	 	 	 
1	scraper/fintech_clients/__init__.py	New	Package exports
2	scraper/fintech_clients/base.py	New	Abstract base with exponential backoff on 429/timeout
3	scraper/fintech_clients/stocktwits.py	New	GET /api/2/streams/symbol/{ticker}.json client
4	scraper/fintech_clients/apewisdom.py	New	GET /api/v1.0/filter/ticker/{ticker} client
5	scraper/fintech_clients/factory.py	New	Client factory + context manager for async sessions
6	scraper/fintech_clients/normalizer.py	New	Unified FintechMessage schema, deduplication, confidence
NEW — Hybrid Orchestration Core	 	 	 
7	scraper/hybrid_orchestrator.py	New	Per-ticker fallback: ST → AW → Reddit on 429/timeout
8	scraper/data_fusion.py	New	Dynamic weight fusion (Scenario A: Reddit 100% / Scenario B: AW 60% + ST 40%)
9	scraper/health_monitor.py	New	Simple in-memory circuit breaker (no persistence needed)
NEW — Configuration	 	 	 
10	config/hybrid_config.yaml	New	Endpoints, retry params, dynamic scenarios, fusion settings
MODIFIED — Integration Layer	 	 	 
11	scraper/engine.py	Edit	Add fintech sentiment processing, reuse VADER + custom lexicon
12	scraper/reddit_client.py	Edit	Add scrape_tickers(tickers) for priority-sub fallback mode
13	run_scraper.py	Edit	Add scrape-hybrid command (standalone, importable)
14	db/connection.py	Edit	On-demand connections (no pooling), WAL mode
15	db/schema.py	Edit	Add fintech_messages table, source column on aggregations
16	db/feature_store.py	Edit	Source-aware pivoted features, provenance tracking
17	dashboard/tab_sentiment_risk.py	Edit	Scenario badge (A/B), source breakdown, fallback ticker list
18	config/weights.py	Edit	Load hybrid_config.yaml, expose scenario weights
19	config/constants.py	Edit	Add fintech validation keywords, entity resolution
20	backtesting/backtest.py	Edit	Multi-source backtest with scenario awareness
21	optimization/optuna_search.py	Edit	Optimize Scenario B weights (AW/ST blend)
22	tests/test_hybrid.py	New	Integration tests: fallback triggers, fusion correctness
🏗️ Core Skeleton Structures
scraper/fintech_clients/base.py
class BaseFintechClient(ABC):
    def __init__(self, base_url: str, max_retries: int = 3, base_delay: float = 1.0):
        self.base_url = base_url
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self): ...
    async def __aexit__(self, *args): ...
    
    async def _get_with_backoff(self, url: str, params: Dict) -> Dict:
        # Exponential backoff on 429/timeout
        for attempt in range(self.max_retries):
            try:
                async with self.session.get(url, params=params) as resp:
                    if resp.status == 429:
                        raise RateLimitedError("429")
                    resp.raise_for_status()
                    return await resp.json()
            except (asyncio.TimeoutError, aiohttp.ClientError):
                if attempt == self.max_retries - 1: raise
                await asyncio.sleep(self.base_delay * (2 ** attempt))
    
    @abstractmethod
    async def fetch_messages(self, tickers: List[str], limit: int) -> List[FintechMessage]: ...
    @abstractmethod
    async def fetch_trending(self, limit: int) -> List[FintechMessage]: ...
    @abstractmethod
    def normalize_message(self, raw: Dict, ticker: str) -> FintechMessage: ...

class RateLimitedError(Exception): pass
scraper/hybrid_orchestrator.py — Per-Ticker Fallback Logic
class HybridOrchestrator:
    async def scrape_tickers(self, tickers: List[str]) -> Dict[str, TickerResult]:
        results = {}
        
        # Phase 1: Try fintech per ticker independently
        async with self.factory.create_clients() as clients:
            for ticker in tickers:
                results[ticker] = await self._scrape_single_ticker(ticker, clients)
        
        # Phase 2: Collect tickers needing Reddit fallback
        fallback_tickers = [t for t, r in results.items() if r.fallback_triggered]
        if fallback_tickers:
            reddit_results = await self._scrape_reddit_fallback(fallback_tickers)
            # Merge into results
        
        # Phase 3: Fuse with dynamic scenario weights
        scenario = "B" if any(r.fallback_triggered for r in results.values()) else "A"
        fused = self.fusion_engine.fuse(all_messages, scenario)
        await self._persist(fused)
        return results
    
    async def _scrape_single_ticker(self, ticker: str, clients: Dict) -> TickerResult:
        # Try StockTwits → on 429/timeout → try ApeWisdom → on 429/timeout → mark fallback
        # Returns TickerResult with messages, fallback_triggered, fallback_reason
config/hybrid_config.yaml — Single Source of Truth
endpoints:
  stocktwits:
    base_url: "https://api.stocktwits.com/api/2"
    symbol_stream: "/streams/symbol/{ticker}.json"
  apewisdom:
    base_url: "https://apewisdom.io/api/v1.0"
    ticker_filter: "/filter/ticker/{ticker}"

retry:
  max_retries: 3
  base_delay_seconds: 1.0
  timeout_seconds: 10

scenarios:
  A:  # Reddit working
continue
continue
+ Thought: 1.4s
scenarios:
  A:  # Reddit working
    name: "Reddit Primary"
    weights:
      reddit: 1.0
      stocktwits: 0.0
      apewisdom: 0.0
    category_weights:
      macro_geopolitical: 0.25
      fundamental_institutional: 0.25
      tech_product: 0.30
      retail_options: 0.20
  
  B:  # Reddit blocked → fintech fallback
    name: "Fintech Fallback"
    weights:
      reddit: 0.0
      apewisdom: 0.60    # Pre-scraped WSB data
      stocktwits: 0.40   # Broader retail sentiment
    category_weights:
      macro_geopolitical: 0.25
      fundamental_institutional: 0.25
      tech_product: 0.30
      retail_options: 0.20

fallback:
  trigger_on: ["429", "timeout", "connection_error"]
  per_ticker: true
  always_query_both_fintech: true
  reddit_priority_subreddits:
    - "wallstreetbets"
    - "stocks"
    - "options"
    - "SecurityAnalysis"
    - "ValueInvesting"
    - "LocalLLaMA"
    - "hardware"
    - "geopolitics"
    - "economics"
    - "supplychain"

fusion:
  recency_half_life_hours: 24
  engagement_log_scale: true
  min_messages_per_ticker: 3
  confidence_threshold: 0.3

optimization:
  enabled: true
  lookback_days: 252
  objective: "information_coefficient"
  rebalance_frequency: "monthly"
  method: "bayesian_optimization"
  optimize_scenario_b: true
  scenario_b_bounds:
    apewisdom: [0.4, 0.8]
    stocktwits: [0.2, 0.6]
✅ Architecture Checklist
Component	Status	Validation Criteria
Fintech Clients	 	 
StockTwits client	☐ Planned	Public endpoint, 3-retries backoff, normalizes to FintechMessage
ApeWisdom client	☐ Planned	Public endpoint, 3-retries backoff, normalizes to FintechMessage
Factory + context manager	☐ Planned	async with factory.create_clients() as clients:
Normalizer + deduplication	☐ Planned	Unified schema, confidence scoring, source reliability weights
Hybrid Orchestration	 	 
Per-ticker fallback	☐ Planned	Independent ST→AW→Reddit per ticker
Scenario A/B weight switching	☐ Planned	Dynamic based on any fallback triggered
Reddit priority-sub fallback	☐ Planned	Only 10 subs when fallback activates
Database	 	 
fintech_messages table	☐ Planned	source, source_id, ticker, text, sentiment, engagement, metadata_json
source column on daily_aggregations	☐ Planned	Track provenance per aggregation
signal_provenance table	☐ Planned	Per-source contribution to composite
CLI & Execution	 	 
scrape-hybrid command	☐ Planned	python run_scraper.py scrape-hybrid --tickers AAPL,TSLA
Standalone importable module	☐ Planned	from scraper.hybrid_orchestrator import HybridOrchestrator
Dashboard (Streamlit)	 	 
Scenario badge (A/B)	☐ Planned	Visual indicator in sidebar
Source breakdown chart	☐ Planned	Stacked bar by source (not just category)
Fallback ticker list	☐ Planned	Show which tickers fell back + reason
Backtesting & Optimization	 	 
Multi-source backtest	☐ Planned	Attribute returns to source contributions
Scenario B weight optimization	☐ Planned	Optuna on AW/ST blend only
Testing	 	 
Fallback trigger tests	☐ Planned	Mock 429/timeout → verify Reddit called
Fusion correctness tests	☐ Planned	Verify weights match scenario
🔄 Execution Flow (On-Demand)
User clicks "Run Scraper" in Streamlit/PyWebView
         │
         ▼
┌────────────────────────────────────────┐
│  run_scraper.py scrape-hybrid          │
│  (or FastAPI /api/scrape/trigger)      │
└────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────┐
│  HybridOrchestrator.scrape_tickers()   │
│  1. Load tickers from args/config      │
│  2. For EACH ticker independently:     │
│     ├── Try StockTwits                 │
│     │   └── 429/timeout? → Try AW      │
│     ├── Try ApeWisdom                  │
│     │   └── 429/timeout? → Mark fallback│
│  3. If ANY fallback: run Reddit for    │
│     those tickers only (priority subs) │
│  4. Fuse with Scenario A or B weights  │
│  5. Persist to SQLite (WAL mode)       │
└────────────────────────────────────────┘
         │
         ▼
┌────────────────────────────────────────┐
│  Dashboard updates on next refresh     │
│  - Scenario badge (A/B)                │
│  - Source breakdown                    │
│  - Fallback tickers listed             │
└────────────────────────────────────────┘
📦 Dependencies to Add
# requirements.txt additions
aiohttp>=3.9.0          # Async HTTP for fintech clients
pydantic>=2.0           # Optional: for FintechMessage validation
❓ Final Clarifying Questions
Ticker universe: Should scrape-hybrid accept tickers via CLI arg (--tickers AAPL,TSLA), read from a config file, or derive from watchlist in DB?
Reddit fallback scope: When fallback triggers, scrape only the 10 priority subreddits for the failed tickers, or all 11 subreddits from taxonomy?
Streamlit integration: Should stream_quant.py get a new "Run Hybrid Scrape" button that calls HybridOrchestrator directly (same process), or should it POST to a future FastAPI endpoint?
Historical backfill: Does the hybrid orchestrator need a backfill-historical mode for initial data population, or only forward-looking on-demand scrapes?
ApeWisdom rate limit handling: Their free tier is ~100 req/min. With per-ticker sequential calls, we'll be fine, but should we add a small delay between tickers as courtesy?
Ready to implement when you confirm these details.
# Todos
Finalize comprehensive implementation plan for Hybrid Fallback Architecture
Present final plan with all clarifications incorporated
+ Thought: 3.3s
Plan complete. 22 files, per-ticker HTTP-status-driven fallback, dynamic Scenario A/B weights, zero credentials, on-demand execution. Ready for implementation when you confirm the 5 clarifying questions.
▣  Plan · Nemotron 3 Ultra Free · 2m 32s
1. Ticker Universe Resolution
The Choice: Derive dynamically from a watchlist table inside your local SQLite database.

The Reasoning: Hardcoding ticker strings inside an external YAML/config file requires manual file modifications, while passing them via CLI strings (--tickers AAPL,TSLA) creates a clunky developer workflow. By storing your tracked tickers in a local database table, your Streamlit front-end (and your future PyWebView GUI) can simply execute a standard SQL query—INSERT INTO watchlist (ticker) VALUES ('ARM');—and the background orchestrator automatically detects and tracks the new asset on its next sync cycle. It transforms your app into a cohesive data ecosystem.

2. Reddit Fallback Scope
The Choice: Scrape selectively only the specific multi-class subreddits relevant to that failed asset's taxonomy.

The Reasoning: If a native mirror fails for a specific ticker, running all 11 subreddits sequentially across your entire watch universe will introduce unnecessary network latency and burn through your keyless IP rate-limit budgets. If a high-conviction tech asset like NVDA drops into a fallback state, your engine should look exclusively inside its mapped categories (e.g., tech_product and retail_options). Do not waste system resources searching for hardware parameters inside r/supplychain or r/geopolitics unless your multi-factor threat model explicitly flags an active macro contagion event.

3. Streamlit Integration
The Choice: Call the HybridOrchestrator class directly within the same process for now, but isolate it completely behind a clean Python method.

The Reasoning: Since the FastAPI app instance does not exist in your codebase yet, writing network POST routing requests right now will break your current Streamlit execution environment. Instead, construct a clean, decoupled class call under your interface button: HybridOrchestrator().sync_active_watchlist(). Running it natively keeps your staging sandbox fluid. Because the logic is neatly contained inside a class method, when you are ready to launch your desktop wrapper down the road, you can easily pull it out of Streamlit and drop it into a FastAPI endpoint handler in less than 30 seconds.

4. Historical Backfill
The Choice: The orchestrator should strictly run forward-looking on-demand updates; historical baseline data must be handled via a separate, offline seeding script.

The Reasoning: Keyless public API channels function as real-time rolling streams—they do not support deep historical time-series queries back to 2021. Attempting to force a live scraper to pull 5 years of historical delta lines over a standard residential network connection will result in immediate IP limits or timeouts. Your engine should be engineered for dual-mode execution: run a static database seed script once to populate your local database tables with historical baselines, and let your orchestrator focus exclusively on capturing quick forward-looking deltas during your Tuesday/Friday sync loops.

5. ApeWisdom Rate-Limit Handling
The Choice: Yes, implement a structured, randomized courtesy delay (e.g., time.sleep(random.uniform(1.5, 3.0))) between ticker requests.

The Reasoning: Public keyless endpoints track sudden spikes in traffic. Even if you stay well under their macro threshold of 100 requests per minute, looping through a watchlist of 15 tickers with zero delay sends requests at identical millisecond intervals. Security systems easily flag this uniform signature as an automated bot and temporarily block the originating IP. Introducing a randomized human-like heartbeat sleep completely smooths out your network footprint, keeps your scraper under the perimeter radar, and serves as an excellent demonstration of professional data engineering practices for anyone auditing your source code repository. 
+ Thought: 20.6s
Perfect. All clarifications incorporated. Here's the finalized implementation plan:
Final Implementation Plan: Hybrid Fallback Architecture (Option C)
📋 File Glossary — 23 Files (+1 seeding script)
#	File Path	Type	Purpose
NEW — Fintech API Clients (Keyless, Public Endpoints)	 	 	 
1	scraper/fintech_clients/__init__.py	New	Package exports
2	scraper/fintech_clients/base.py	New	Abstract base with exponential backoff + randomized courtesy delay
3	scraper/fintech_clients/stocktwits.py	New	GET /api/2/streams/symbol/{ticker}.json client
4	scraper/fintech_clients/apewisdom.py	New	GET /api/v1.0/filter/ticker/{ticker} client + courtesy delay
5	scraper/fintech_clients/factory.py	New	Client factory + async context manager
6	scraper/fintech_clients/normalizer.py	New	Unified FintechMessage schema, deduplication, confidence
NEW — Hybrid Orchestration Core	 	 	 
7	scraper/hybrid_orchestrator.py	New	Per-ticker fallback: ST → AW → Reddit (taxonomy-scoped) on 429/timeout
8	scraper/data_fusion.py	New	Dynamic weight fusion (Scenario A: Reddit 100% / Scenario B: AW 60% + ST 40%)
9	scraper/health_monitor.py	New	Simple in-memory circuit breaker
NEW — Configuration & Seeding	 	 	 
10	config/hybrid_config.yaml	New	Endpoints, retry params, dynamic scenarios, fusion settings
11	scripts/seed_historical.py	New	Offline historical backfill (separate from orchestrator)
MODIFIED — Integration Layer	 	 	 
12	scraper/engine.py	Edit	Add fintech sentiment processing, reuse VADER + custom lexicon
13	scraper/reddit_client.py	Edit	Add scrape_tickers(tickers, categories) for taxonomy-scoped fallback
14	run_scraper.py	Edit	Add scrape-hybrid command (standalone, importable)
15	db/connection.py	Edit	On-demand connections, WAL mode
16	db/schema.py	Edit	Add watchlist table, fintech_messages table, source column
17	db/feature_store.py	Edit	Source-aware pivoted features, provenance tracking
18	dashboard/tab_sentiment_risk.py	Edit	Scenario badge (A/B), source breakdown, fallback ticker list
19	config/weights.py	Edit	Load hybrid_config.yaml, expose scenario weights
20	config/constants.py	Edit	Add fintech validation keywords, entity resolution
21	backtesting/backtest.py	Edit	Multi-source backtest with scenario awareness
22	optimization/optuna_search.py	Edit	Optimize Scenario B weights (AW/ST blend)
23	tests/test_hybrid.py	New	Integration tests: fallback triggers, fusion correctness
🏗️ Key Skeleton Updates from Clarifications
db/schema.py — Add watchlist Table
def create_tables(conn):
    # ... existing tables ...
    
    # Watchlist table for dynamic ticker universe
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            ticker TEXT PRIMARY KEY,
            added_at INTEGER NOT NULL,
            active BOOLEAN DEFAULT 1,
            notes TEXT
        )
    """)
    
    # Fintech raw messages
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fintech_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,           -- stocktwits, apewisdom, reddit
            source_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            text TEXT,
            sentiment_score REAL,
            author TEXT,
            created_utc INTEGER NOT NULL,
            scraped_at INTEGER NOT NULL,
            engagement_likes INTEGER DEFAULT 0,
            engagement_comments INTEGER DEFAULT 0,
            engagement_shares INTEGER DEFAULT 0,
            url TEXT,
            metadata_json TEXT,
            UNIQUE(source, source_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fintech_ticker_date ON fintech_messages(ticker, created_utc)")
    
    # Add source column to daily_aggregations
    cursor.execute("PRAGMA table_info(daily_aggregations)")
    if "source" not in [r[1] for r in cursor.fetchall()]:
        cursor.execute("ALTER TABLE daily_aggregations ADD COLUMN source TEXT DEFAULT 'reddit'")
scraper/fintech_clients/apewisdom.py — Courtesy Delay
import random
import asyncio

class ApeWisdomClient(BaseFintechClient):
    async def fetch_messages(self, tickers: List[str], limit: int = 100) -> List[FintechMessage]:
        all_messages = []
        for i, ticker in enumerate(tickers):
            # Courtesy delay between tickers (except first)
            if i > 0:
                delay = random.uniform(1.5, 3.0)
                await asyncio.sleep(delay)
            
            url = f"{self.base_url}/filter/ticker/{ticker.upper()}"
            data = await self._get_with_backoff(url, {"limit": min(limit, 100)})
            messages = data.get("results", [])
            all_messages.extend([self.normalize_message(m, ticker) for m in messages])
        return all_messages[:limit]
scraper/hybrid_orchestrator.py — Taxonomy-Scoped Fallback
class HybridOrchestrator:
    def __init__(self):
        # Load ticker→category mapping from config
        self.ticker_taxonomy = self._load_ticker_taxonomy()  # {ticker: [categories]}
    
    async def sync_active_watchlist(self) -> Dict[str, TickerResult]:
        """Main entry: called by Streamlit button / future FastAPI endpoint."""
        # 1. Get active tickers from watchlist table
        tickers = self._get_active_watchlist()
        
        # 2. Scrape with per-ticker fallback
        results = await self.scrape_tickers(tickers)
        
        # 3. Persist & return
        return results
    
    async def _scrape_single_ticker(self, ticker: str, clients: Dict) -> TickerResult:
        # Try StockTwits → ApeWisdom
        # On fallback: determine relevant categories for this ticker
        categories = self.ticker_taxonomy.get(ticker, ["retail_options"])
        # Only scrape those category's subreddits
        reddit_msgs = await self._scrape_reddit_fallback(ticker, categories)
scripts/seed_historical.py — Offline Historical Seeding
#!/usr/bin/env python3
"""
One-time historical backfill script.
Run ONCE to populate baseline data from 2021-present.
Uses Reddit PRAW (full history) since fintech APIs don't support deep history.
"""
from scraper.reddit_client import RedditUniversalScraper
from db.connection import init_db

def seed_historical(lookback_days: int = 1095):  # 3 years
    init_db()
    scraper = RedditUniversalScraper()
    # Scrape all subreddits with historical pagination
    scraper.scrape_historical(lookback_days=lookback_days)
    print(f"Historical seed complete: {lookback_days} days")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1095)
    args = parser.parse_args()
    seed_historical(args.days)
dashboard/tab_sentiment_risk.py — Streamlit Integration
def render_sentiment_risk_tab(primary_ticker: str):
    # ... existing code ...
    
    # Hybrid Scrape Button
    if st.button("🔄 Run Hybrid Scrape (Active Watchlist)"):
        with st.spinner("Running hybrid orchestrator..."):
            from scraper.hybrid_orchestrator import HybridOrchestrator
            orchestrator = HybridOrchestrator()
            results = orchestrator.sync_active_watchlist()  # Direct call, same process
            
            # Show results
            fallback_count = sum(1 for r in results.values() if r.fallback_triggered)
            st.success(f"Complete: {len(results)} tickers, {fallback_count} fallbacks")
            st.rerun()
    
    # Scenario Badge
    scenario = get_active_scenario()  # Query from latest hybrid_scrape_runs
    if scenario == "A":
        st.success("🟢 Scenario A: Reddit Primary (100%)")
    else:
        st.warning("🟡 Scenario B: Fintech Fallback (AW 60% + ST 40%)")
✅ Complete Architecture Checklist
Component	Status	Validation
Database	 	 
watchlist table	☐ Planned	Dynamic ticker universe, CRUD via Streamlit
fintech_messages table	☐ Planned	Unified raw storage with provenance
source column on aggregations	☐ Planned	Track source per aggregation
signal_provenance table	☐ Planned	Per-source contribution to composite
Fintech Clients	 	 
StockTwits client	☐ Planned	Public endpoint, 3-retries backoff
ApeWisdom client	☐ Planned	Public endpoint + courtesy delay (1.5-3s)
Factory + context manager	☐ Planned	async with factory.create_clients()
Normalizer	☐ Planned	Unified schema, confidence, deduplication
Hybrid Orchestration	 	 
Per-ticker fallback	☐ Planned	Independent ST→AW→Reddit per ticker
Taxonomy-scoped Reddit fallback	☐ Planned	Only relevant category subreddits
Scenario A/B weight switching	☐ Planned	Dynamic based on any fallback
sync_active_watchlist() method	☐ Planned	Clean entry point for Streamlit/FastAPI
Historical Seeding	 	 
seed_historical.py	☐ Planned	One-time PRAW backfill (3 years)
Separate from orchestrator	☐ Planned	No API history attempts
CLI & Execution	 	 
scrape-hybrid command	☐ Planned	python run_scraper.py scrape-hybrid
Standalone importable	☐ Planned	from scraper.hybrid_orchestrator import HybridOrchestrator
Dashboard	 	 
"Run Hybrid Scrape" button	☐ Planned	Calls HybridOrchestrator().sync_active_watchlist()
Scenario badge (A/B)	☐ Planned	Visual indicator
Source breakdown chart	☐ Planned	Stacked by source
Fallback ticker list	☐ Planned	Show failed tickers + reason
Backtesting & Optimization	 	 
Multi-source backtest	☐ Planned	Attribute returns to source
Scenario B optimization	☐ Planned	Optuna on AW/ST blend
Testing	 	 
Fallback trigger tests	☐ Planned	Mock 429/timeout → verify Reddit called
Taxonomy-scoped fallback tests	☐ Planned	Verify only relevant subs scraped
Fusion correctness tests	☐ Planned	Verify weights match scenario
🔄 Final Execution Flow
Streamlit: User clicks "Run Hybrid Scrape"
         │
         ▼
HybridOrchestrator().sync_active_watchlist()
         │
         ├── 1. SELECT ticker FROM watchlist WHERE active=1
         │
         ├── 2. For EACH ticker (parallel-ish):
         │    ├── Try StockTwits (with backoff)
         │    │   └── 429/timeout? → Try ApeWisdom
         │    ├── Try ApeWisdom (with backoff + courtesy delay)
         │    │   └── 429/timeout? → Mark fallback + get categories
         │
         ├── 3. If ANY fallback:
         │    └── For each failed ticker:
         │         └── Scrape ONLY its mapped category subreddits
         │
         ├── 4. Fuse with Scenario A (no fallback) or B (fallback) weights
         │
         ├── 5. Persist to SQLite (WAL mode)
         │
         └── 6. Return results → Streamlit reruns → Dashboard updates
📦 Dependencies
aiohttp>=3.9.0
pydantic>=2.0          # Optional, for message validation
🎯 Ready for Implementation
The plan is complete with all 5 clarifications integrated:
✅ Dynamic ticker universe from watchlist table
✅ Taxonomy-scoped Reddit fallback (per-ticker categories)
✅ Direct Streamlit class call (FastAPI-ready isolation)
✅ Separate seed_historical.py for offline backfill
✅ ApeWisdom courtesy delay (1.5-3s randomized)
No further questions. Ready to implement when you give the go-ahead.