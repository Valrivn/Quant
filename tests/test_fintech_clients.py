import pytest
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

from scraper.fintech_clients.base import FintechMessage, FintechHealth
from scraper.fintech_clients.stocktwits import StockTwitsClient
from scraper.fintech_clients.apewisdom import ApeWisdomClient
from scraper.fintech_clients.rate_limiter import TokenBucketRateLimiter, RateLimitConfig
from scraper.fintech_clients.factory import FintechClientFactory
from scraper.fintech_clients.normalizer import FintechNormalizer

pytestmark = pytest.mark.asyncio


class TestRateLimiter:
    def test_token_bucket_initialization(self):
        configs = {
            "test": RateLimitConfig(requests_per_window=100, window_seconds=60, burst_allowance=10)
        }
        limiter = TokenBucketRateLimiter(configs)
        assert limiter.get_remaining("test") == 110

    @pytest.mark.asyncio
    async def test_acquire_tokens(self):
        configs = {
            "test": RateLimitConfig(requests_per_window=100, window_seconds=60)
        }
        limiter = TokenBucketRateLimiter(configs)
        await limiter.acquire("test", 5)
        assert limiter.get_remaining("test") == 95


class TestStockTwitsClient:
    @pytest.fixture
    def rate_limiter(self):
        return TokenBucketRateLimiter({
            "stocktwits": RateLimitConfig(requests_per_window=200, window_seconds=60)
        })

    @pytest.fixture
    def client(self, rate_limiter):
        return StockTwitsClient("test_token", rate_limiter)

    @pytest.mark.asyncio
    async def test_normalize_message(self, client):
        raw = {
            "id": 12345,
            "body": "AAPL to the moon! $AAPL",
            "user": {"username": "trader123"},
            "created_at": "2024-01-15T10:30:00Z",
            "likes": {"total": 42},
            "symbols": [{"symbol": "AAPL"}],
            "entities": {"sentiment": {"basic": "Bullish"}}
        }
        msg = client.normalize_message(raw, "AAPL")
        
        assert msg.source == "stocktwits"
        assert msg.source_id == "12345"
        assert msg.ticker == "AAPL"
        assert msg.text == "AAPL to the moon! $AAPL"
        assert msg.sentiment_score == 1.0
        assert msg.author == "trader123"
        assert msg.engagement["likes"] == 42

    @pytest.mark.asyncio
    async def test_parse_sentiment_bullish(self, client):
        assert client._parse_sentiment({"basic": "Bullish"}) == 1.0

    @pytest.mark.asyncio
    async def test_parse_sentiment_bearish(self, client):
        assert client._parse_sentiment({"basic": "Bearish"}) == -1.0

    @pytest.mark.asyncio
    async def test_parse_sentiment_none(self, client):
        assert client._parse_sentiment(None) is None
        assert client._parse_sentiment({"basic": "Neutral"}) == 0.0


class TestApeWisdomClient:
    @pytest.fixture
    def rate_limiter(self):
        return TokenBucketRateLimiter({
            "apewisdom": RateLimitConfig(requests_per_window=100, window_seconds=60)
        })

    @pytest.fixture
    def client(self, rate_limiter):
        return ApeWisdomClient("test_key", rate_limiter)

    @pytest.mark.asyncio
    async def test_normalize_message(self, client):
        raw = {
            "id": "mention_123",
            "title": "AAPL earnings play",
            "body": "Strong earnings expected",
            "sentiment": 0.8,
            "author": "wsb_trader",
            "created_utc": 1705315800,
            "score": 150,
            "num_comments": 25,
            "url": "https://reddit.com/r/wallstreetbets/comments/...",
            "subreddit": "wallstreetbets"
        }
        msg = client.normalize_message(raw, "AAPL")
        
        assert msg.source == "apewisdom"
        assert msg.source_id == "mention_123"
        assert msg.ticker == "AAPL"
        assert "AAPL earnings play" in msg.text
        assert msg.sentiment_score == 0.8
        assert msg.author == "wsb_trader"
        assert msg.engagement["upvotes"] == 150
        assert msg.engagement["comments"] == 25
        assert msg.metadata["subreddit"] == "wallstreetbets"


class TestFintechNormalizer:
    @pytest.fixture
    def normalizer(self):
        return FintechNormalizer(
            ticker_blacklist={"CEO", "IPO", "ETF"},
            validation_keywords={"buy", "sell", "stock"}
        )

    def test_canonicalize_ticker(self, normalizer):
        assert normalizer._canonicalize_ticker("$AAPL") == "AAPL"
        assert normalizer._canonicalize_ticker("aapl") == "AAPL"
        assert normalizer._canonicalize_ticker("$TSLA") == "TSLA"

    def test_compute_confidence_stocktwits(self, normalizer):
        msg = FintechMessage(
            source="stocktwits",
            source_id="1",
            ticker="AAPL",
            text="Test",
            sentiment_score=0.5,
            author="user",
            created_at=datetime.utcnow(),
            engagement={"likes": 100},
            url="",
            metadata={}
        )
        conf = normalizer._compute_confidence(msg)
        assert conf > 0.7  # stocktwits base 0.9 + 0.15 source + 0.1 engagement

    def test_compute_confidence_reddit(self, normalizer):
        msg = FintechMessage(
            source="reddit",
            source_id="1",
            ticker="AAPL",
            text="Test",
            sentiment_score=0.5,
            author="user",
            created_at=datetime.utcnow(),
            engagement={"likes": 5},
            url="",
            metadata={}
        )
        conf = normalizer._compute_confidence(msg)
        assert 0.5 <= conf <= 0.7  # reddit base 0.6

    def test_deduplicate(self, normalizer):
        msg1 = FintechMessage(source="stocktwits", source_id="1", ticker="AAPL", text="Test1",
                             sentiment_score=0.5, author="user", created_at=datetime.utcnow(),
                             engagement={}, url="", metadata={})
        msg2 = FintechMessage(source="stocktwits", source_id="1", ticker="AAPL", text="Test2",
                             sentiment_score=0.5, author="user", created_at=datetime.utcnow(),
                             engagement={}, url="", metadata={})
        msg3 = FintechMessage(source="apewisdom", source_id="1", ticker="AAPL", text="Test3",
                             sentiment_score=0.5, author="user", created_at=datetime.utcnow(),
                             engagement={}, url="", metadata={})
        
        unique = normalizer.deduplicate([msg1, msg2, msg3])
        assert len(unique) == 2  # msg1 and msg2 are duplicates (same source, source_id)


class TestFintechClientFactory:
    @patch('scraper.fintech_clients.factory.load_fintech_credentials')
    def test_get_client_stocktwits(self, mock_load_creds):
        mock_load_creds.return_value = {
            "stocktwits": {"access_token": "test_token"},
            "priority": ["stocktwits", "apewisdom"]
        }
        factory = FintechClientFactory()
        client = factory.get_client("stocktwits")
        assert isinstance(client, StockTwitsClient)

    @patch('scraper.fintech_clients.factory.load_fintech_credentials')
    def test_get_client_apewisdom(self, mock_load_creds):
        mock_load_creds.return_value = {
            "apewisdom": {"api_key": "test_key"},
            "priority": ["stocktwits", "apewisdom"]
        }
        factory = FintechClientFactory()
        client = factory.get_client("apewisdom")
        assert isinstance(client, ApeWisdomClient)

    @patch('scraper.fintech_clients.factory.load_fintech_credentials')
    def test_get_healthy_sources(self, mock_load_creds):
        mock_load_creds.return_value = {
            "stocktwits": {"access_token": "test_token"},
            "apewisdom": {"api_key": "test_key"},
            "priority": ["stocktwits", "apewisdom"]
        }
        factory = FintechClientFactory()
        
        health = {
            "stocktwits": FintechHealth(source="stocktwits", is_healthy=True, last_success=datetime.utcnow(),
                                       consecutive_failures=0, rate_limit_remaining=100, rate_limit_reset=None, error_message=None),
            "apewisdom": FintechHealth(source="apewisdom", is_healthy=False, last_success=None,
                                      consecutive_failures=1, rate_limit_remaining=0, rate_limit_reset=None, error_message="Error")
        }
        healthy = factory.get_healthy_sources(health)
        assert healthy == ["stocktwits"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])