import asyncio
import logging
import random
import os
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from contextlib import asynccontextmanager

import nodriver as uc
from config import load_hybrid_config

logger = logging.getLogger(__name__)

is_root = os.geteuid() == 0


@dataclass
class NodriverConfig:
    headless: bool = True
    browser_executable_path: Optional[str] = None
    browser_args: Optional[List[str]] = None
    min_delay: float = 12.0
    max_delay: float = 25.0
    page_load_timeout: int = 30


class NodriverSession:
    def __init__(self, config: NodriverConfig = None):
        self.config = config or NodriverConfig()
        self._browser: Optional[uc.Browser] = None
        self._tab: Optional[uc.Tab] = None
        self._initialized = False
        self._last_request_time = 0.0

    async def initialize(self) -> None:
        if self._initialized:
            return

        logger.info("Initializing Nodriver session...")

        browser_executable = self.config.browser_executable_path or os.getenv("CHROME_BINARY_PATH")
        if not browser_executable:
            try:
                hybrid_config = load_hybrid_config()
                browser_executable = hybrid_config.get("psychological", {}).get("browser_binary_path")
            except Exception:
                pass

        if not browser_executable:
            browser_executable = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"

        if not os.path.exists(browser_executable):
            logger.warning(f"Browser executable not found at {browser_executable}, using default")
            browser_executable = None

        browser_args = self.config.browser_args or [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-blink-features=AutomationControlled",
        ]
        
        self._browser = await uc.start(
            headless=self.config.headless,
            browser_executable_path=browser_executable,
            browser_args=browser_args,
            sandbox=not is_root,
        )

        self._tab = await self._browser.get("about:blank")
        self._initialized = True
        logger.info("Nodriver session initialized")

    async def close(self) -> None:
        if self._browser and self._initialized:
            logger.info("Closing Nodriver session...")
            try:
                await self._browser.stop()
            except Exception as e:
                logger.error(f"Error closing Nodriver session: {e}")
            self._browser = None
            self._tab = None
            self._initialized = False
            logger.info("Nodriver session closed")

    async def __aenter__(self) -> "NodriverSession":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _apply_rate_limit(self) -> None:
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_time
        min_delay = self.config.min_delay + random.uniform(0, 2)
        max_delay = self.config.max_delay + random.uniform(0, 3)

        if elapsed < min_delay:
            sleep_time = min_delay - elapsed
            logger.debug(f"Rate limiting: sleeping {sleep_time:.2f}s")
            await asyncio.sleep(sleep_time)

        self._last_request_time = asyncio.get_event_loop().time()

    async def get(self, url: str, wait_for: str = None, timeout: int = None) -> bool:
        await self._apply_rate_limit()
        try:
            if not self._initialized:
                await self.initialize()

            self._tab = await self._browser.get(url)
            
            if wait_for:
                await self._tab.wait_for(wait_for, timeout=timeout or self.config.page_load_timeout)
            
            return True
        except Exception as e:
            logger.error(f"Error navigating to {url}: {e}")
            return False

    async def get_content(self) -> str:
        if not self._tab:
            return ""
        return await self._tab.get_content()

    async def find_element(self, selector: str, timeout: int = 10):
        if not self._tab:
            return None
        try:
            return await self._tab.select(selector, timeout=timeout)
        except Exception:
            return None

    async def find_elements(self, selector: str, timeout: int = 10):
        if not self._tab:
            return []
        try:
            return await self._tab.select_all(selector, timeout=timeout)
        except Exception:
            return []

    async def evaluate(self, script: str):
        if not self._tab:
            return None
        try:
            return await self._tab.evaluate(script)
        except Exception as e:
            logger.error(f"Error evaluating script: {e}")
            return None

    async def scroll_down(self, pixels: int = 500):
        if not self._tab:
            return
        await self._tab.evaluate(f"window.scrollBy(0, {pixels})")
        await asyncio.sleep(random.uniform(0.5, 1.5))

    async def scroll_to_bottom(self):
        if not self._tab:
            return
        await self._tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(random.uniform(1, 2))

    def get_tab(self):
        return self._tab


class NodriverSessionPool:
    def __init__(self, max_sessions: int = 2, config: NodriverConfig = None):
        self.max_sessions = max_sessions
        self.config = config or NodriverConfig()
        self._sessions: List[NodriverSession] = []
        self._available: asyncio.Queue = asyncio.Queue()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        for i in range(self.max_sessions):
            session = NodriverSession(self.config)
            await session.initialize()
            self._sessions.append(session)
            await self._available.put(session)
        self._initialized = True
        logger.info(f"Initialized Nodriver session pool with {self.max_sessions} sessions")

    async def acquire(self) -> NodriverSession:
        if not self._initialized:
            await self.initialize()
        return await self._available.get()

    async def release(self, session: NodriverSession) -> None:
        await self._available.put(session)

    async def close_all(self) -> None:
        for session in self._sessions:
            await session.close()
        self._sessions.clear()
        logger.info("All Nodriver sessions in pool closed")

    @asynccontextmanager
    async def session(self):
        session = await self.acquire()
        try:
            yield session
        finally:
            await self.release(session)


_default_pool: Optional[NodriverSessionPool] = None


def get_default_pool(config: NodriverConfig = None) -> NodriverSessionPool:
    global _default_pool
    if _default_pool is None:
        _default_pool = NodriverSessionPool(config=config)
    return _default_pool


async def close_default_pool() -> None:
    global _default_pool
    if _default_pool:
        await _default_pool.close_all()
        _default_pool = None


async def create_nodriver_session(config: NodriverConfig = None) -> NodriverSession:
    session = NodriverSession(config)
    await session.initialize()
    return session


async def scrape_with_nodriver(
    url: str,
    wait_for: str = None,
    config: NodriverConfig = None,
    extract_fn=None
) -> Any:
    async with await create_nodriver_session(config) as session:
        success = await session.get(url, wait_for=wait_for)
        if not success:
            return None
        
        if extract_fn:
            return await extract_fn(session)
        
        return await session.get_content()


if __name__ == "__main__":
    async def test():
        config = NodriverConfig(headless=True)
        async with await create_nodriver_session(config) as session:
            success = await session.get("https://old.reddit.com/r/hardware/", wait_for="div.thing")
            print(f"Page loaded: {success}")
            if success:
                content = await session.get_content()
                print(f"Content length: {len(content)}")

    asyncio.run(test())