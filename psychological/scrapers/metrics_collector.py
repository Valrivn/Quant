import asyncio
import logging
import time
import json
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from collections import defaultdict
from config import load_hybrid_config

logger = logging.getLogger(__name__)

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("redis.asyncio not available, metrics will use local storage only")


@dataclass
class FallbackMetric:
    source: str
    fallback_type: str
    success: bool
    timestamp: float
    duration_ms: float
    error: Optional[str] = None


@dataclass
class ScrapeMetric:
    url: str
    source: str
    success: bool
    duration_ms: float
    cache_hit: bool
    circuit_state: str
    timestamp: float
    error: Optional[str] = None


class MetricsCollector:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.metrics_config = self.config.get("metrics", {})
        self.enabled = self.metrics_config.get("enabled", True)
        
        # Redis config
        redis_config = self.metrics_config.get("redis", {})
        self.redis_enabled = redis_config.get("enabled", True) and REDIS_AVAILABLE
        self.redis_host = redis_config.get("host", "localhost")
        self.redis_port = redis_config.get("port", 6379)
        self.redis_db = redis_config.get("db", 0)
        self.redis_key_prefix = redis_config.get("key_prefix", "quant:metrics:")
        
        # Flush interval
        self.flush_interval = self.metrics_config.get("flush_interval_seconds", 60)
        
        # Local storage
        self._fallback_metrics: List[FallbackMetric] = []
        self._scrape_metrics: List[ScrapeMetric] = []
        self._counters: Dict[str, int] = defaultdict(int)
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, List[float]] = defaultdict(list)
        
        self._redis_client: Optional[redis.Redis] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._max_local_metrics = 10000

    async def initialize(self) -> None:
        if not self.enabled:
            return
            
        if self.redis_enabled:
            try:
                self._redis_client = redis.Redis(
                    host=self.redis_host,
                    port=self.redis_port,
                    db=self.redis_db,
                    decode_responses=True,
                    socket_timeout=5,
                    socket_connect_timeout=5,
                )
                await self._redis_client.ping()
                logger.info(f"Metrics collector connected to Redis at {self.redis_host}:{self.redis_port}")
            except Exception as e:
                logger.warning(f"Redis connection failed for metrics: {e}. Using local storage only.")
                self._redis_client = None
        
        # Start flush task
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.info("Metrics collector initialized")

    async def close(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self.flush()
        if self._redis_client:
            await self._redis_client.close()

    async def __aenter__(self) -> "MetricsCollector":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def record_fallback(
        self,
        source: str,
        fallback_type: str,
        success: bool,
        duration_ms: float,
        error: Optional[str] = None
    ) -> None:
        if not self.enabled:
            return
        
        metric = FallbackMetric(
            source=source,
            fallback_type=fallback_type,
            success=success,
            timestamp=time.time(),
            duration_ms=duration_ms,
            error=error
        )
        
        self._fallback_metrics.append(metric)
        self._counters[f"fallback.{source}.{fallback_type}.total"] += 1
        if success:
            self._counters[f"fallback.{source}.{fallback_type}.success"] += 1
        else:
            self._counters[f"fallback.{source}.{fallback_type}.failure"] += 1
        
        self._histograms[f"fallback.{source}.{fallback_type}.duration_ms"].append(duration_ms)
        
        if len(self._fallback_metrics) > self._max_local_metrics:
            self._fallback_metrics = self._fallback_metrics[-self._max_local_metrics:]

    def record_scrape(
        self,
        url: str,
        source: str,
        success: bool,
        duration_ms: float,
        cache_hit: bool,
        circuit_state: str,
        error: Optional[str] = None
    ) -> None:
        if not self.enabled:
            return
        
        metric = ScrapeMetric(
            url=url,
            source=source,
            success=success,
            duration_ms=duration_ms,
            cache_hit=cache_hit,
            circuit_state=circuit_state,
            timestamp=time.time(),
            error=error
        )
        
        self._scrape_metrics.append(metric)
        self._counters[f"scrape.{source}.total"] += 1
        if success:
            self._counters[f"scrape.{source}.success"] += 1
        else:
            self._counters[f"scrape.{source}.failure"] += 1
        if cache_hit:
            self._counters[f"scrape.{source}.cache_hit"] += 1
        
        self._histograms[f"scrape.{source}.duration_ms"].append(duration_ms)
        
        if len(self._scrape_metrics) > self._max_local_metrics:
            self._scrape_metrics = self._scrape_metrics[-self._max_local_metrics:]

    def increment_counter(self, name: str, value: int = 1) -> None:
        if not self.enabled:
            return
        self._counters[name] += value

    def set_gauge(self, name: str, value: float) -> None:
        if not self.enabled:
            return
        self._gauges[name] = value

    def observe_histogram(self, name: str, value: float) -> None:
        if not self.enabled:
            return
        self._histograms[name].append(value)
        if len(self._histograms[name]) > 1000:
            self._histograms[name] = self._histograms[name][-1000:]

    async def flush(self) -> None:
        if not self.enabled:
            return
            
        if self._redis_client:
            try:
                await self._flush_to_redis()
            except Exception as e:
                logger.warning(f"Failed to flush metrics to Redis: {e}")
        
        # Clear local buffers after successful flush
        self._fallback_metrics.clear()
        self._scrape_metrics.clear()
        # Keep counters and gauges

    async def _flush_to_redis(self) -> None:
        if not self._redis_client:
            return
            
        pipe = self._redis_client.pipeline()
        timestamp = int(time.time() * 1000)
        
        # Flush counters
        for name, value in self._counters.items():
            key = f"{self.redis_key_prefix}counter:{name}"
            pipe.incrby(key, value)
            pipe.expire(key, 86400 * 7)  # 7 day TTL
        
        # Flush gauges
        for name, value in self._gauges.items():
            key = f"{self.redis_key_prefix}gauge:{name}"
            pipe.set(key, value)
            pipe.expire(key, 86400 * 7)
        
        # Flush histograms (store as JSON arrays)
        for name, values in self._histograms.items():
            if values:
                key = f"{self.redis_key_prefix}histogram:{name}"
                pipe.lpush(key, *map(str, values))
                pipe.ltrim(key, 0, 999)
                pipe.expire(key, 86400 * 7)
        
        # Flush fallback metrics
        for metric in self._fallback_metrics:
            key = f"{self.redis_key_prefix}fallback:{metric.source}:{metric.fallback_type}"
            pipe.lpush(key, json.dumps(asdict(metric)))
            pipe.ltrim(key, 0, 999)
            pipe.expire(key, 86400 * 7)
        
        # Flush scrape metrics
        for metric in self._scrape_metrics:
            key = f"{self.redis_key_prefix}scrape:{metric.source}"
            pipe.lpush(key, json.dumps(asdict(metric)))
            pipe.ltrim(key, 0, 999)
            pipe.expire(key, 86400 * 7)
        
        await pipe.execute()

    async def _periodic_flush(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.flush_interval)
                await self.flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Periodic flush error: {e}")

    def get_stats(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "redis_connected": self._redis_client is not None,
            "local_fallback_metrics": len(self._fallback_metrics),
            "local_scrape_metrics": len(self._scrape_metrics),
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "histogram_sizes": {k: len(v) for k, v in self._histograms.items()},
        }

    def get_fallback_summary(self) -> Dict[str, Any]:
        summary = defaultdict(lambda: {"total": 0, "success": 0, "failure": 0, "avg_duration_ms": 0.0})
        for m in self._fallback_metrics:
            key = f"{m.source}.{m.fallback_type}"
            summary[key]["total"] += 1
            if m.success:
                summary[key]["success"] += 1
            else:
                summary[key]["failure"] += 1
        
        # Calculate averages
        for key in summary:
            durations = [m.duration_ms for m in self._fallback_metrics 
                        if f"{m.source}.{m.fallback_type}" == key]
            if durations:
                summary[key]["avg_duration_ms"] = sum(durations) / len(durations)
        
        return dict(summary)

    def get_scrape_summary(self) -> Dict[str, Any]:
        summary = defaultdict(lambda: {"total": 0, "success": 0, "failure": 0, "cache_hits": 0, "avg_duration_ms": 0.0})
        for m in self._scrape_metrics:
            summary[m.source]["total"] += 1
            if m.success:
                summary[m.source]["success"] += 1
            else:
                summary[m.source]["failure"] += 1
            if m.cache_hit:
                summary[m.source]["cache_hits"] += 1
        
        for key in summary:
            durations = [m.duration_ms for m in self._scrape_metrics if m.source == key]
            if durations:
                summary[key]["avg_duration_ms"] = sum(durations) / len(durations)
        
        return dict(summary)


_metrics_collector: Optional[MetricsCollector] = None


async def get_metrics_collector(config_dict: dict = None) -> MetricsCollector:
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector(config_dict)
        await _metrics_collector.initialize()
    return _metrics_collector


async def close_metrics_collector() -> None:
    global _metrics_collector
    if _metrics_collector:
        await _metrics_collector.close()
        _metrics_collector = None


if __name__ == "__main__":
    import asyncio
    
    async def test():
        collector = await get_metrics_collector()
        
        # Record some test metrics
        collector.record_fallback("glassdoor", "browserless", True, 1500)
        collector.record_fallback("glassdoor", "browserless", False, 5000, "timeout")
        collector.record_fallback("glassdoor", "yahoo_finance", True, 800)
        
        collector.record_scrape("https://glassdoor.com/NVDA", "glassdoor", True, 1200, False, "closed")
        collector.record_scrape("https://glassdoor.com/AMD", "glassdoor", True, 300, True, "closed")
        
        collector.increment_counter("custom.counter", 5)
        collector.set_gauge("custom.gauge", 42.5)
        collector.observe_histogram("custom.histogram", 100.0)
        
        await asyncio.sleep(1)
        
        print("Stats:", collector.get_stats())
        print("Fallback summary:", collector.get_fallback_summary())
        print("Scrape summary:", collector.get_scrape_summary())
        
        await close_metrics_collector()
    
    asyncio.run(test())