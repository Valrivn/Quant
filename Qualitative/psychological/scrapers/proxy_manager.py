import asyncio
import logging
import random
import re
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from curl_cffi import AsyncSession

logger = logging.getLogger(__name__)

PROXY_SOURCES = [
    "https://free-proxy-list.net/",
    "https://www.sslproxies.org/",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
    "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
]

JA4_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}


@dataclass
class RateLimiterConfig:
    min_delay: float = 12.0
    max_delay: float = 25.0
    jitter: float = 2.0


class RateLimiter:
    def __init__(self, config: Optional[RateLimiterConfig] = None):
        self.config = config or RateLimiterConfig()
        self._last_request: float = 0.0

    async def acquire(self) -> None:
        now = time.time()
        elapsed = now - self._last_request
        delay = random.uniform(self.config.min_delay, self.config.max_delay + self.config.jitter)
        if elapsed < delay:
            sleep_for = delay - elapsed
            logger.debug(f"Rate limiter: sleeping {sleep_for:.2f}s")
            await asyncio.sleep(sleep_for)
        self._last_request = time.time()


class ProxyManager:
    def __init__(self, config_dict: Optional[dict] = None):
        self.config = config_dict or {}
        self.proxy_config = self.config.get("proxy", {})
        self.custom_proxies: List[str] = self.proxy_config.get("custom_proxies", [])
        self.proxy_sources: List[str] = self.proxy_config.get("sources", PROXY_SOURCES)
        self.min_proxies: int = self.proxy_config.get("min_proxies", 3)
        self.refresh_interval: int = self.proxy_config.get("refresh_interval", 300)
        self.test_url: str = self.proxy_config.get("test_url", "https://httpbin.org/ip")
        self.test_timeout: int = self.proxy_config.get("test_timeout", 10)
        self.max_proxies_to_test: int = self.proxy_config.get("max_test", 50)

        self._working_proxies: List[str] = []
        self._current_index: int = 0
        self._last_refresh: float = 0.0
        self._lock = asyncio.Lock()
        self._rate_limiter = RateLimiter(
            RateLimiterConfig(
                min_delay=self.proxy_config.get("proxy_min_delay", 3.0),
                max_delay=self.proxy_config.get("proxy_max_delay", 8.0),
                jitter=self.proxy_config.get("proxy_jitter", 1.0),
            )
        )

    async def initialize(self) -> None:
        if self.custom_proxies:
            self._working_proxies = list(self.custom_proxies)
            logger.info(f"ProxyManager: using {len(self._working_proxies)} custom proxies")
        else:
            self._last_refresh = 0.0

    async def _ensure_proxies(self) -> None:
        now = time.time()
        if self._working_proxies and now - self._last_refresh < self.refresh_interval:
            return
        if self._working_proxies and now - self._last_refresh < self.refresh_interval:
            return
        try:
            await asyncio.wait_for(self._refresh_proxies(), timeout=45.0)
        except asyncio.TimeoutError:
            logger.warning("ProxyManager: proxy refresh timed out after 45s")

    async def _refresh_proxies(self) -> None:
        self._last_refresh = time.time()
        raw_proxies: set = set()

        for source in self.proxy_sources:
            try:
                extracted = await self._extract_from_source(source)
                raw_proxies.update(extracted)
                logger.info(f"ProxyManager: extracted {len(extracted)} from {source}")
            except Exception as e:
                logger.debug(f"ProxyManager: source {source} failed: {e}")

        if not raw_proxies:
            logger.warning("ProxyManager: no proxies extracted from any source")
            return

        proxy_list = list(raw_proxies)
        random.shuffle(proxy_list)
        validated = await self._validate_proxies(proxy_list[:self.max_proxies_to_test])

        if validated:
            self._working_proxies = validated
            self._current_index = 0
            logger.info(f"ProxyManager: {len(validated)} working proxies in pool")
        elif not self._working_proxies:
            logger.warning("ProxyManager: no working proxies found, will retry on next rotation")

    async def _extract_from_source(self, url: str) -> List[str]:
        async with AsyncSession(impersonate="chrome120", timeout=self.test_timeout) as session:
            resp = await session.get(url, headers=JA4_HEADERS, timeout=self.test_timeout)
            if resp.status_code != 200:
                return []
            text = resp.text

        if any(tag in text for tag in ["<table", "<tr", "<td", "<th"]):
            soup = BeautifulSoup(text, "html.parser")
            proxies = []
            for row in soup.select("table.table tbody tr, table#proxylisttable tbody tr, table tbody tr"):
                cells = row.find_all("td")
                if len(cells) >= 2:
                    ip = cells[0].get_text(strip=True)
                    port = cells[1].get_text(strip=True)
                    if ip and port and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                        proxies.append(f"{ip}:{port}")
            if proxies:
                return proxies

        lines = text.strip().split("\n")
        proxies = []
        for line in lines:
            line = line.strip()
            if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+$", line):
                proxies.append(line)
        return proxies

    async def _validate_proxies(self, proxies: List[str]) -> List[str]:
        working: List[str] = []
        sem = asyncio.Semaphore(10)

        async def _test(proxy: str) -> Optional[str]:
            async with sem:
                try:
                    async with AsyncSession(
                        impersonate="chrome120",
                        timeout=self.test_timeout,
                        proxies={"http": f"http://{proxy}", "https": f"http://{proxy}"},
                    ) as sess:
                        resp = await sess.get(
                            self.test_url, headers=JA4_HEADERS, timeout=self.test_timeout
                        )
                        if resp.status_code == 200:
                            return proxy
                except Exception:
                    pass
                return None

        tasks = [_test(p) for p in proxies]
        results = await asyncio.gather(*tasks)
        working = [r for r in results if r]
        return working

    async def get_proxy(self) -> Optional[str]:
        async with self._lock:
            await self._ensure_proxies()
            if not self._working_proxies:
                logger.debug("ProxyManager: no proxies available")
                return None
            proxy = self._working_proxies[self._current_index % len(self._working_proxies)]
            self._current_index += 1
            proxy_url = f"http://{proxy}"
            logger.debug(f"ProxyManager: rotating to {proxy_url}")
            return proxy_url

    async def close(self) -> None:
        pass


def build_ja4_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = dict(JA4_HEADERS)
    if extra:
        headers.update(extra)
    return headers
