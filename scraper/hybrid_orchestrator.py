import asyncio
import logging
from typing import List, Dict, Optional
from datetime import datetime
from dataclasses import dataclass

from scraper.fintech_clients.factory import FintechClientFactory
from scraper.fintech_clients.normalizer import FintechNormalizer
from scraper.fintech_clients.base import FintechMessage
from scraper.reddit_client import RedditUniversalScraper
from scraper.health_monitor import HealthMonitor
from scraper.data_fusion import DataFusionEngine
from config import load_hybrid_weights
from psychological.orchestrator import create_psychological_orchestrator

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
    Automated Hybrid Fallback Architecture - Paradigm 4 Delta.
    
    Primary: PsychologicalOrchestrator (Reddit + NLP + Velocity + State Machine)
    State Machine)
    Supplementary: Fintech APIs (StockTwits, ApeWisdom) - confirmation only
    """

    def __init__(self):
        self.factory = FintechClientFactory()
        self.normalizer = FintechNormalizer(
            ticker_blacklist=set(),
            validation_keywords=set()
        )
        self.health_monitor = HealthMonitor()
        self.fusion_engine = DataFusionEngine()
        self.hybrid_config = load_hybrid_weights()
        self.reddit_scraper = RedditUniversalScraper()
        self.psychological_orchestrator = None

    async def initialize(self):
        """Initialize the psychological orchestrator as primary"""
        self.psychological_orchestrator = await create_psychological_orchestrator()

    async def scrape_all(self, tickers: List[str] = None) -> Dict[str, ScrapeResult]:
        """Main entry point: Psychological pillar primary, fintech supplementary."""
        results = {}
        
        # Phase 1: Primary - Psychological Pillar (Contrarian Sentiment Engine)
        if not self.psychological_orchestrator:
            await self.initialize()
            
        logger.info("Running primary psychological pipeline...")
        psych_results = await self.psychological_orchestrator.run_full_pipeline(
            tickers or ["AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "AMD", "META"]
        )
        
        primary_result = psych_results["primary_result"]
        results["psychological_primary"] = ScrapeResult(
            source="psychological_primary",
            messages_count=primary_result.vectors_committed,
            tickers_found=primary_result.tickers_processed,
            duration_ms=0,
            errors=primary_result.errors
        )

        # Phase 2: Supplementary - Fintech APIs (confirmation only)
        logger.info("Running supplementary fintech validation...")
        health = await self.factory.health_check_all()
        healthy_sources = self.factory.get_healthy_sources(health)
        
        if healthy_sources:
            fintech_results = await self._scrape_fintech_sources(healthy_sources, tickers)
            results.update(fintech_results)
        else:
            logger.warning("No healthy fintech sources available")

        # Phase 3: Fuse and persist (regime-level fusion happens in psychological orchestrator)
        all_messages = self._collect_all_messages(results)
        fused_signals = self.fusion_engine.fuse(all_messages)
        await self._persist_fused_signals(fused_signals)

        return results

    async def _scrape_fintech_sources(
        self,
        healthy_sources: List[str],
        tickers: List[str] = None
    ) -> Dict[str, ScrapeResult]:
        """Scrape all healthy fintech sources in parallel for supplementary validation."""
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

    def _collect_all_messages(self, results: Dict[str, ScrapeResult]) -> List[FintechMessage]:
        """Collect messages from all successful scrapes."""
        return []

    async def _persist_fused_signals(self, signals: List[Dict]) -> None:
        """Persist fused signals to database with provenance."""
        pass

    async def _scrape_fintech_only(self, tickers: List[str] = None) -> Dict[str, ScrapeResult]:
        """Scrape only fintech sources (no psychological primary)."""
        results = {}
        
        health = await self.factory.health_check_all()
        healthy_sources = self.factory.get_healthy_sources(health)
        logger.info(f"Health check: {healthy_sources} healthy, {set(health.keys()) - set(healthy_sources)} unhealthy")

        fintech_results = await self._scrape_fintech_sources(healthy_sources, tickers)
        results.update(fintech_results)

        all_messages = self._collect_all_messages(results)
        fused_signals = self.fusion_engine.fuse(all_messages)
        await self._persist_fused_signals(fused_signals)

        return results

    def get_regime_status(self, ticker: str) -> Optional[Dict]:
        """Get current psychological regime status for a ticker"""
        if self.psychological_orchestrator:
            return self.psychological_orchestrator.get_regime_status(ticker)
        return None