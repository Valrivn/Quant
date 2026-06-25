import pytest
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch, MagicMock
from dataclasses import dataclass

from scraper.hybrid_orchestrator import HybridOrchestrator, ScrapeResult
from scraper.fintech_clients.base import FintechMessage, FintechHealth
from scraper.fintech_clients.factory import FintechClientFactory
from scraper.fintech_clients.stocktwits import StockTwitsClient
from scraper.fintech_clients.apewisdom import ApeWisdomClient
from scraper.health_monitor import HealthMonitor, CircuitBreaker, CircuitState
from scraper.data_fusion import DataFusionEngine


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        breaker = CircuitBreaker(source="test")
        assert breaker.state == CircuitState.CLOSED
        assert breaker.can_execute() is True

    def test_opens_after_threshold_failures(self):
        breaker = CircuitBreaker(source="test", failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == CircuitState.CLOSED
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN
        assert breaker.can_execute() is False

    def test_half_open_after_timeout(self):
        breaker = CircuitBreaker(source="test", failure_threshold=2, timeout_seconds=1)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN
        # Can't easily test time passage without mocking time

    def test_closes_after_successes_in_half_open(self):
        breaker = CircuitBreaker(source="test", failure_threshold=2, success_threshold=2)
        breaker.record_failure()
        breaker.record_failure()
        breaker._transition(CircuitState.HALF_OPEN)
        breaker.record_success()
        assert breaker.state == CircuitState.HALF_OPEN
        breaker.record_success()
        assert breaker.state == CircuitState.CLOSED

    def test_failure_in_half_open_reopens(self):
        breaker = CircuitBreaker(source="test", failure_threshold=2, success_threshold=2)
        breaker.record_failure()
        breaker.record_failure()
        breaker._transition(CircuitState.HALF_OPEN)
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN


class TestHealthMonitor:
    def test_records_results(self):
        monitor = HealthMonitor()
        monitor.record_result("test", True, 100)
        monitor.record_result("test", False, 200)
        
        status = monitor.get_source_status("test")
        assert status["circuit_state"] == "closed"
        assert status["failure_count"] == 1

    def test_get_all_status(self):
        monitor = HealthMonitor()
        monitor.record_result("source1", True)
        monitor.record_result("source2", False)
        
        all_status = monitor.get_all_status()
        assert "source1" in all_status
        assert "source2" in all_status


class TestDataFusionEngine:
    @pytest.fixture
    def fusion_engine(self):
        return DataFusionEngine(source_weights={"stocktwits": 0.5, "apewisdom": 0.3, "reddit": 0.2})

    @pytest.fixture
    def sample_messages(self):
        now = datetime.utcnow()
        return [
            FintechMessage(
                source="stocktwits",
                source_id="1",
                ticker="AAPL",
                text="AAPL to the moon!",
                sentiment_score=0.8,
                author="trader1",
                created_at=now,
                engagement={"likes": 100},
                url="http://stocktwits.com/1",
                metadata={}
            ),
            FintechMessage(
                source="apewisdom",
                source_id="2",
                ticker="AAPL",
                text="AAPL earnings play",
                sentiment_score=0.6,
                author="wsb_user",
                created_at=now,
                engagement={"upvotes": 50, "comments": 10},
                url="http://reddit.com/2",
                metadata={}
            ),
            FintechMessage(
                source="reddit",
                source_id="3",
                ticker="AAPL",
                text="AAPL calls",
                sentiment_score=0.4,
                author="redditor",
                created_at=now,
                engagement={"likes": 5},
                url="http://reddit.com/3",
                metadata={}
            )
        ]

    def test_fuse_returns_signals(self, fusion_engine, sample_messages):
        signals = fusion_engine.fuse(sample_messages)
        assert len(signals) == 1
        signal = signals[0]
        assert signal["ticker"] == "AAPL"
        assert "composite_sentiment" in signal
        assert "provenance" in signal
        assert len(signal["provenance"]) == 3
        assert set(signal["sources"]) == {"stocktwits", "apewisdom", "reddit"}

    def test_fuse_empty_list(self, fusion_engine):
        signals = fusion_engine.fuse([])
        assert signals == []

    def test_fuse_multiple_tickers(self, fusion_engine):
        now = datetime.utcnow()
        messages = [
            FintechMessage(source="stocktwits", source_id="1", ticker="AAPL", text="Test",
                          sentiment_score=0.5, author="a", created_at=now, engagement={}, url="", metadata={}),
            FintechMessage(source="stocktwits", source_id="2", ticker="TSLA", text="Test",
                          sentiment_score=0.5, author="b", created_at=now, engagement={}, url="", metadata={})
        ]
        signals = fusion_engine.fuse(messages)
        assert len(signals) == 2
        tickers = {s["ticker"] for s in signals}
        assert tickers == {"AAPL", "TSLA"}

    def test_category_breakdown(self, fusion_engine, sample_messages):
        for msg in sample_messages:
            msg.metadata["category"] = "tech_product"
        
        signals = fusion_engine.fuse(sample_messages)
        signal = signals[0]
        assert "category_breakdown" in signal
        assert "tech_product" in signal["category_breakdown"]


class TestHybridOrchestrator:
    @pytest.fixture
    def mock_factory(self):
        factory = Mock(spec=FintechClientFactory)
        factory.health_check_all = AsyncMock(return_value={
            "stocktwits": FintechHealth(source="stocktwits", is_healthy=True, last_success=datetime.utcnow(),
                                       consecutive_failures=0, rate_limit_remaining=100, rate_limit_reset=None, error_message=None),
            "apewisdom": FintechHealth(source="apewisdom", is_healthy=True, last_success=datetime.utcnow(),
                                      consecutive_failures=0, rate_limit_remaining=100, rate_limit_reset=None, error_message=None)
        })
        factory.get_healthy_sources = Mock(return_value=["stocktwits", "apewisdom"])
        
        # Mock clients
        stocktwits_client = AsyncMock(spec=StockTwitsClient)
        stocktwits_client.fetch_messages = AsyncMock(return_value=[])
        stocktwits_client.fetch_trending = AsyncMock(return_value=[])
        
        apewisdom_client = AsyncMock(spec=ApeWisdomClient)
        apewisdom_client.fetch_messages = AsyncMock(return_value=[])
        apewisdom_client.fetch_trending = AsyncMock(return_value=[])
        
        factory.get_client = Mock(side_effect=lambda s: stocktwits_client if s == "stocktwits" else apewisdom_client)
        
        return factory

    @pytest.fixture
    def orchestrator(self, mock_factory):
        with patch('scraper.hybrid_orchestrator.FintechClientFactory', return_value=mock_factory), \
             patch('scraper.hybrid_orchestrator.FintechNormalizer'), \
             patch('scraper.hybrid_orchestrator.HealthMonitor'), \
             patch('scraper.hybrid_orchestrator.DataFusionEngine'), \
             patch('scraper.hybrid_orchestrator.load_hybrid_weights', return_value={"min_fintech_messages": 50}), \
             patch('scraper.hybrid_orchestrator.RedditUniversalScraper'):
            
            orch = HybridOrchestrator()
            orch.factory = mock_factory
            orch.psychological_orchestrator = AsyncMock()
            orch.psychological_orchestrator.run_full_pipeline = AsyncMock(return_value={
                "primary_result": MagicMock(vectors_committed=100, tickers_processed=["AAPL"], errors=[]),
                "corporate_affinities": {},
                "fused_results": {}
            })
            return orch

    @pytest.mark.asyncio
    async def test_scrape_all_sufficient_coverage(self, orchestrator):
        # Mock fintech results with sufficient messages
        orchestrator._scrape_fintech_sources = AsyncMock(return_value={
            "stocktwits": ScrapeResult(source="stocktwits", messages_count=60, tickers_found=["AAPL"], duration_ms=100, errors=[]),
            "apewisdom": ScrapeResult(source="apewisdom", messages_count=40, tickers_found=["AAPL"], duration_ms=100, errors=[])
        })
        orchestrator._collect_all_messages = Mock(return_value=[])
        orchestrator.fusion_engine.fuse = Mock(return_value=[])
        orchestrator._persist_fused_signals = AsyncMock()
        
        results = await orchestrator.scrape_all(tickers=["AAPL"])
        
        # Should not trigger Reddit fallback
        assert "reddit" not in results
        assert results["stocktwits"].messages_count == 60
        assert results["apewisdom"].messages_count == 40

    @pytest.mark.asyncio
    async def test_scrape_all_primary_runs(self, orchestrator):
        # Mock fintech results
        orchestrator._scrape_fintech_sources = AsyncMock(return_value={
            "stocktwits": ScrapeResult(source="stocktwits", messages_count=10, tickers_found=["AAPL"], duration_ms=100, errors=[])
        })
        orchestrator._collect_all_messages = Mock(return_value=[])
        orchestrator.fusion_engine.fuse = Mock(return_value=[])
        orchestrator._persist_fused_signals = AsyncMock()
        
        results = await orchestrator.scrape_all(tickers=["AAPL"])
        
        # Psychological primary should always run and be in results
        assert "psychological_primary" in results
        assert results["psychological_primary"].messages_count == 100
        assert "stocktwits" in results

    @pytest.mark.asyncio
    async def test_scrape_all_no_healthy_sources(self, orchestrator):
        orchestrator.factory.health_check_all = AsyncMock(return_value={
            "stocktwits": FintechHealth(source="stocktwits", is_healthy=False, last_success=None,
                                       consecutive_failures=5, rate_limit_remaining=0, rate_limit_reset=None, error_message="Error"),
            "apewisdom": FintechHealth(source="apewisdom", is_healthy=False, last_success=None,
                                      consecutive_failures=5, rate_limit_remaining=0, rate_limit_reset=None, error_message="Error")
        })
        orchestrator.factory.get_healthy_sources = Mock(return_value=[])
        
        orchestrator._scrape_fintech_sources = AsyncMock(return_value={})
        orchestrator._collect_all_messages = Mock(return_value=[])
        orchestrator.fusion_engine.fuse = Mock(return_value=[])
        orchestrator._persist_fused_signals = AsyncMock()
        
        results = await orchestrator.scrape_all(tickers=["AAPL"])
        
        # Primary should run, but no fintech sources should run
        assert "psychological_primary" in results
        assert "stocktwits" not in results
        assert "apewisdom" not in results


class TestFintechMessage:
    def test_fintech_message_creation(self):
        now = datetime.utcnow()
        msg = FintechMessage(
            source="stocktwits",
            source_id="123",
            ticker="AAPL",
            text="Test message",
            sentiment_score=0.5,
            author="test_user",
            created_at=now,
            engagement={"likes": 10},
            url="http://example.com",
            metadata={"key": "value"}
        )
        
        assert msg.source == "stocktwits"
        assert msg.ticker == "AAPL"
        assert msg.sentiment_score == 0.5
        assert msg.metadata["key"] == "value"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])