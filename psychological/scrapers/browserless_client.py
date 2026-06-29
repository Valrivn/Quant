import asyncio
import logging
import json
import random
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import aiohttp
from config import load_hybrid_config
from psychological.scrapers.redis_cache import RedisCache, create_redis_cache
from psychological.scrapers.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, with_circuit_breaker, CircuitBreakerOpenError
from psychological.scrapers.metrics_collector import MetricsCollector, get_metrics_collector

logger = logging.getLogger(__name__)


@dataclass
class BrowserlessResult:
    success: bool
    html: Optional[str] = None
    error: Optional[str] = None
    status_code: Optional[int] = None


class BrowserlessClient:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.browserless_config = self.config.get("browserless", {})
        self.endpoint = self.browserless_config.get("endpoint", "http://localhost:3000")
        self.timeout = self.browserless_config.get("timeout", 60)
        self.max_retries = self.browserless_config.get("max_retries", 3)
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache: Optional[RedisCache] = None
        self._circuit_breaker: Optional[CircuitBreaker] = None
        self._metrics: Optional[MetricsCollector] = None
        self.cache_enabled = self.config.get("redis", {}).get("enabled", True)
        self.cache_ttl = self.config.get("redis", {}).get("default_ttl_seconds", 3600)
        
        # Circuit breaker config
        cb_config = self.config.get("circuit_breaker", {})
        self.cb_enabled = cb_config.get("enabled", True)
        self.cb_failure_threshold = cb_config.get("failure_threshold", 5)
        self.cb_timeout = cb_config.get("timeout", 60.0)
        
        # Metrics config
        metrics_config = self.config.get("metrics", {})
        self.metrics_enabled = metrics_config.get("enabled", True)

    async def initialize(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        )
        if self.cache_enabled:
            self._cache = RedisCache(self.config)
            await self._cache.initialize()
        if self.cb_enabled:
            cb_config = CircuitBreakerConfig(
                failure_threshold=self.cb_failure_threshold,
                timeout=self.cb_timeout,
            )
            self._circuit_breaker = CircuitBreaker("browserless", cb_config)
        if self.metrics_enabled:
            self._metrics = await get_metrics_collector(self.config)
        await self.health_check()
        logger.info(f"BrowserlessClient initialized with endpoint: {self.endpoint}")

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
        if self._cache:
            await self._cache.close()
            self._cache = None
        if self._metrics:
            self._metrics = None
        self._circuit_breaker = None

    async def __aenter__(self) -> "BrowserlessClient":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def health_check(self) -> bool:
        if not self._session:
            await self.initialize()
        try:
            async with self._session.get(f"{self.endpoint}/pressure") as response:
                if response.status == 200:
                    logger.info("Browserless health check passed")
                    return True
        except Exception as e:
            logger.warning(f"Browserless health check failed: {e}")
        return False

    async def scrape(
        self,
        url: str,
        wait_for: Optional[str] = None,
        wait_until: str = "networkidle2",
        timeout: Optional[int] = None,
        headers: Optional[Dict[str, str]] = None,
        script: Optional[str] = None,
        use_cache: bool = True,
        cache_ttl: Optional[int] = None,
    ) -> BrowserlessResult:
        if not self._session:
            await self.initialize()

        start_time = time.time()
        cache_hit = False
        circuit_state = "closed"

        # Generate cache key from request parameters
        cache_key = None
        if use_cache and self._cache:
            import hashlib
            key_data = f"{url}|{wait_for}|{wait_until}|{timeout}|{json.dumps(headers, sort_keys=True) if headers else ''}|{script or ''}"
            cache_key = hashlib.sha256(key_data.encode()).hexdigest()[:32]
            
            cached_result = await self._cache.get(cache_key)
            if cached_result:
                cache_hit = True
                logger.info(f"Cache HIT for {url}")
                
                if self._metrics:
                    self._metrics.record_scrape(
                        url=url,
                        source="browserless",
                        success=cached_result["success"],
                        duration_ms=(time.time() - start_time) * 1000,
                        cache_hit=True,
                        circuit_state="closed"
                    )
                
                return BrowserlessResult(
                    success=cached_result["success"],
                    html=cached_result["html"],
                    error=cached_result["error"],
                    status_code=cached_result["status_code"],
                )

        payload = {
            "url": url,
            "gotoOptions": {
                "waitUntil": wait_until,
                "timeout": timeout or self.timeout * 1000,
            }
        }

        if wait_for:
            payload["waitFor"] = wait_for

        if headers:
            payload["setExtraHTTPHeaders"] = headers

        if script:
            payload["script"] = script

        default_headers = {
            "Content-Type": "application/json",
        }

        async def _do_scrape():
            for attempt in range(self.max_retries):
                try:
                    async with self._session.post(
                        f"{self.endpoint}/content",
                        json=payload,
                        headers=default_headers
                    ) as response:
                        if response.status == 200:
                            html = await response.text()
                            return BrowserlessResult(success=True, html=html, status_code=200)
                        elif response.status == 429:
                            wait_time = (2 ** attempt) + random.uniform(1, 3)
                            logger.warning(f"Browserless rate limited, waiting {wait_time:.1f}s before retry {attempt + 1}/{self.max_retries}")
                            await asyncio.sleep(wait_time)

                            if self._metrics:
                                self._metrics.increment_counter("rate_limit.hits.browserless")
                            continue
                        else:
                            error_text = await response.text()
                            logger.warning(f"Browserless returned {response.status}: {error_text}")
                            raise Exception(f"HTTP {response.status}: {error_text}")
                except asyncio.TimeoutError:
                    logger.warning(f"Browserless timeout on attempt {attempt + 1}/{self.max_retries}")
                    if attempt == self.max_retries - 1:
                        raise Exception("Timeout after retries")
                    await asyncio.sleep(2 ** attempt)
                except Exception as e:
                    logger.warning(f"Browserless error on attempt {attempt + 1}/{self.max_retries}: {e}")
                    if attempt == self.max_retries - 1:
                        raise
                    await asyncio.sleep(2 ** attempt)
            raise Exception("Max retries exceeded")

        try:
            if self._circuit_breaker:
                circuit_state = self._circuit_breaker.state.value
                result = await self._circuit_breaker.call(_do_scrape)
            else:
                result = await _do_scrape()
            
            # Cache successful results
            if use_cache and self._cache and cache_key:
                cache_data = {
                    "success": result.success,
                    "html": result.html,
                    "error": result.error,
                    "status_code": result.status_code,
                }
                await self._cache.set(cache_key, cache_data, ttl=cache_ttl or self.cache_ttl)
                logger.info(f"Cached result for {url}")
            
            if self._metrics:
                self._metrics.record_scrape(
                    url=url,
                    source="browserless",
                    success=result.success,
                    duration_ms=(time.time() - start_time) * 1000,
                    cache_hit=cache_hit,
                    circuit_state=circuit_state,
                    error=result.error
                )
            
            return result
            
        except Exception as e:
            circuit_state = "open" if isinstance(e, CircuitBreakerOpenError) else "closed"
            
            if self._metrics:
                self._metrics.record_scrape(
                    url=url,
                    source="browserless",
                    success=False,
                    duration_ms=(time.time() - start_time) * 1000,
                    cache_hit=cache_hit,
                    circuit_state=circuit_state,
                    error=str(e)
                )
            
            # If circuit breaker is open, return cached result if available
            if isinstance(e, CircuitBreakerOpenError) and use_cache and self._cache and cache_key:
                cached_result = await self._cache.get(cache_key)
                if cached_result:
                    logger.info(f"Circuit breaker OPEN, returning stale cache for {url}")
                    
                    if self._metrics:
                        self._metrics.increment_counter("circuit_breaker.stale_cache_served")
                    
                    return BrowserlessResult(
                        success=cached_result["success"],
                        html=cached_result["html"],
                        error=cached_result["error"],
                        status_code=cached_result["status_code"],
                    )
            
            return BrowserlessResult(success=False, error=str(e))

    async def scrape_pdf(self, url: str) -> BrowserlessResult:
        if not self._session:
            await self.initialize()

        payload = {"url": url, "options": {"format": "A4"}}

        try:
            async with self._session.post(
                f"{self.endpoint}/pdf",
                json=payload,
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status == 200:
                    pdf_bytes = await response.read()
                    return BrowserlessResult(success=True, html=pdf_bytes.decode('latin-1'), status_code=200)
                else:
                    return BrowserlessResult(success=False, error=f"HTTP {response.status}")
        except Exception as e:
            return BrowserlessResult(success=False, error=str(e))

    async def screenshot(self, url: str, full_page: bool = True) -> BrowserlessResult:
        if not self._session:
            await self.initialize()

        payload = {
            "url": url,
            "options": {
                "fullPage": full_page,
                "type": "png"
            }
        }

        try:
            async with self._session.post(
                f"{self.endpoint}/screenshot",
                json=payload,
                headers={"Content-Type": "application/json"}
            ) as response:
                if response.status == 200:
                    png_bytes = await response.read()
                    import base64
                    png_b64 = base64.b64encode(png_bytes).decode('utf-8')
                    return BrowserlessResult(success=True, html=png_b64, status_code=200)
                else:
                    return BrowserlessResult(success=False, error=f"HTTP {response.status}")
        except Exception as e:
            return BrowserlessResult(success=False, error=str(e))


async def create_browserless_client(config_dict: dict = None) -> BrowserlessClient:
    client = BrowserlessClient(config_dict)
    await client.initialize()
    return client


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    async def test():
        async with await create_browserless_client() as client:
            result = await client.scrape("https://www.glassdoor.com/Reviews/NVIDIA-Reviews-E11505.htm")
            if result.success:
                print(f"Success! HTML length: {len(result.html)}")
                if "NVIDIA" in result.html:
                    print("Found NVIDIA in page")
            else:
                print(f"Failed: {result.error}")

    asyncio.run(test())