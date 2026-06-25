import os
import asyncio
import random
import logging
import time
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from seleniumbase import SB
from config import load_hybrid_config

logger = logging.getLogger(__name__)


@dataclass
class ScraperConfig:
    headless: bool = True
    uc_mode: bool = True
    disable_images: bool = True
    user_agent: str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    min_delay: float = 12.0
    max_delay: float = 25.0
    page_load_timeout: int = 30
    implicit_wait: int = 10
    binary_location: Optional[str] = None


class UnifiedScraperSession:
    def __init__(self, config: ScraperConfig = None):
        self.config = config or ScraperConfig()
        self._sb: Optional[SB] = None
        self._sb_context = None
        self._initialized = False
        self._request_count = 0
        self._last_request_time = 0.0

    def __enter__(self) -> "UnifiedScraperSession":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _sync_initialize(self) -> None:
        if self._initialized:
            return

        logger.info("Initializing SeleniumBase UC session...")
        
        # Determine the custom browser binary location (e.g., Brave)
        binary_location = self.config.binary_location or os.getenv("CHROME_BINARY_PATH")
        if not binary_location:
            try:
                hybrid_config = load_hybrid_config()
                binary_location = hybrid_config.get("psychological", {}).get("browser_binary_path")
            except Exception:
                pass

        if binary_location:
            logger.info(f"Using custom browser binary at: {binary_location}")

        self._sb_context = SB(
            uc=self.config.uc_mode,
            headless=self.config.headless,
            agent=self.config.user_agent,
            page_load_strategy="eager",
            binary_location=binary_location
        )
        self._sb = self._sb_context.__enter__()
        self._sb.driver.set_page_load_timeout(self.config.page_load_timeout)
        self._sb.driver.implicitly_wait(self.config.implicit_wait)
        self._initialized = True
        logger.info("SeleniumBase UC session initialized")

    def initialize(self) -> None:
        self._sync_initialize()

    def _sync_close(self) -> None:
        if self._sb_context and self._initialized:
            logger.info("Closing SeleniumBase UC session...")
            try:
                self._sb_context.__exit__(None, None, None)
            except Exception as e:
                logger.error(f"Error closing SeleniumBase session: {e}")
            self._sb_context = None
            self._sb = None
            self._initialized = False
            logger.info("SeleniumBase UC session closed")

    def close(self) -> None:
        self._sync_close()

    def get_driver(self):
        if not self._initialized:
            self.initialize()
        return self._sb.driver if self._sb else None

    def get_sb(self) -> SB:
        if not self._initialized:
            self.initialize()
        return self._sb

    def _sync_get(self, url: str, wait_for: str = None, timeout: int = None) -> bool:
        if not self._initialized:
            self._sync_initialize()
        self._sb.get(url)
        if wait_for:
            self._sb.wait_for_element(wait_for, timeout=timeout or self.config.page_load_timeout)
        self._request_count += 1
        return True

    async def throttled_get(self, url: str, wait_for: str = None, timeout: int = None) -> bool:
        await self._apply_rate_limit()
        try:
            return self._sync_get(url, wait_for, timeout)
        except Exception as e:
            logger.error(f"Error navigating to {url}: {e}")
            return False

    async def _apply_rate_limit(self) -> None:
        now = time.time()
        elapsed = now - self._last_request_time
        min_delay = self.config.min_delay + random.uniform(0, 2)
        max_delay = self.config.max_delay + random.uniform(0, 3)

        if elapsed < min_delay:
            sleep_time = min_delay - elapsed
            logger.debug(f"Rate limiting: sleeping {sleep_time:.2f}s")
            await asyncio.sleep(sleep_time)

        self._last_request_time = time.time()


class ScraperSessionPool:
    def __init__(self, max_sessions: int = 3, config: ScraperConfig = None):
        self.max_sessions = max_sessions
        self.config = config or ScraperConfig()
        self._sessions: list[UnifiedScraperSession] = []
        self._available: asyncio.Queue = asyncio.Queue()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        for i in range(self.max_sessions):
            session = UnifiedScraperSession(self.config)
            session.initialize()
            self._sessions.append(session)
            await self._available.put(session)
        self._initialized = True
        logger.info(f"Initialized session pool with {self.max_sessions} sessions")

    async def acquire(self) -> UnifiedScraperSession:
        if not self._initialized:
            await self.initialize()
        return await self._available.get()

    async def release(self, session: UnifiedScraperSession) -> None:
        await self._available.put(session)

    async def close_all(self) -> None:
        for session in self._sessions:
            session.close()
        self._sessions.clear()
        logger.info("All sessions in pool closed")

    @asynccontextmanager
    async def session(self):
        session = await self.acquire()
        try:
            yield session
        finally:
            await self.release(session)


_default_pool: Optional[ScraperSessionPool] = None


def get_default_pool(config: ScraperConfig = None) -> ScraperSessionPool:
    global _default_pool
    if _default_pool is None:
        _default_pool = ScraperSessionPool(config=config)
    return _default_pool


async def close_default_pool() -> None:
    global _default_pool
    if _default_pool:
        await _default_pool.close_all()
        _default_pool = None


async def create_scraper_session(config: ScraperConfig = None) -> UnifiedScraperSession:
    session = UnifiedScraperSession(config)
    session.initialize()
    return session


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    async def test():
        config = ScraperConfig(headless=True)
        async with get_default_pool(config).session() as session:
            success = await session.throttled_get("https://old.reddit.com/r/hardware/", wait_for="div.thing")
            print(f"Page loaded: {success}")
            if success:
                title = session.get_sb().get_title()
                print(f"Page title: {title}")

    asyncio.run(test())