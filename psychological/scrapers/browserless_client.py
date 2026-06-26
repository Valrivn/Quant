import asyncio
import logging
import json
import random
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import aiohttp
from config import load_hybrid_config

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

    async def initialize(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        )
        await self.health_check()
        logger.info(f"BrowserlessClient initialized with endpoint: {self.endpoint}")

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "BrowserlessClient":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def health_check(self) -> bool:
        if not self._session:
            await self.initialize()
        try:
            async with self._session.get(f"{self.endpoint}/health") as response:
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
    ) -> BrowserlessResult:
        if not self._session:
            await self.initialize()

        payload = {
            "url": url,
            "options": {
                "waitUntil": wait_until,
                "timeout": timeout or self.timeout * 1000,
            }
        }

        if wait_for:
            payload["options"]["waitForSelector"] = wait_for

        if headers:
            payload["options"]["headers"] = headers

        if script:
            payload["options"]["script"] = script

        default_headers = {
            "Content-Type": "application/json",
        }

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
                        continue
                    else:
                        error_text = await response.text()
                        logger.warning(f"Browserless returned {response.status}: {error_text}")
                        return BrowserlessResult(
                            success=False,
                            error=f"HTTP {response.status}: {error_text}",
                            status_code=response.status
                        )
            except asyncio.TimeoutError:
                logger.warning(f"Browserless timeout on attempt {attempt + 1}/{self.max_retries}")
                if attempt == self.max_retries - 1:
                    return BrowserlessResult(success=False, error="Timeout after retries")
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.warning(f"Browserless error on attempt {attempt + 1}/{self.max_retries}: {e}")
                if attempt == self.max_retries - 1:
                    return BrowserlessResult(success=False, error=str(e))
                await asyncio.sleep(2 ** attempt)

        return BrowserlessResult(success=False, error="Max retries exceeded")

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