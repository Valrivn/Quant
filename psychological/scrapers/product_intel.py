import asyncio
import logging
import random
import re
import json
from typing import Dict, List, Optional
from datetime import datetime, timezone
from dataclasses import dataclass

import aiohttp
from bs4 import BeautifulSoup
from config import load_hybrid_config
from curl_cffi import AsyncSession
from psychological.engineering_guards import (
    guard_nan, guard_division, guard_bounds, guard_utc_timestamp, ensure_utc,
    RateLimiter, timed_operation, safe_float, safe_int, with_timeout, sanitize_text
)

logger = logging.getLogger(__name__)


@dataclass
class G2Review:
    ticker: str
    platform: str
    product_name: str
    rating: Optional[float]
    review_text: str
    review_date: Optional[str]
    keywords_detected: List[str]
    fetched_at: str


@dataclass
class CapterraReview:
    ticker: str
    platform: str
    product_name: str
    rating: Optional[float]
    review_text: str
    review_date: Optional[str]
    keywords_detected: List[str]
    fetched_at: str


@dataclass
class AppStoreReview:
    ticker: str
    platform: str
    app_id: str
    app_name: str
    rating: Optional[float]
    review_text: str
    review_date: Optional[str]
    vader_compound: Optional[float]
    fetched_at: str


class G2Scraper:
    BASE_URL = "https://www.g2.com"

    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.g2_config = self.config.get("g2_capterra", {})
        self.keywords = self.g2_config.get("keywords", [])
        self.date_filter_days = self.g2_config.get("date_filter_days", 90)
        self.company_mappings = {
            "NVDA": "nvidia",
            "AMD": "advanced-micro-devices",
            "MSFT": "microsoft",
            "GOOGL": "google",
            "META": "meta",
            "TSLA": "tesla",
            "AAPL": "apple",
            "AMZN": "amazon",
        }
        self._curl_session: Optional[AsyncSession] = None

    async def initialize(self) -> None:
        self._curl_session = AsyncSession(
            impersonate="chrome120",
            timeout=aiohttp.ClientTimeout(total=30)
        )
        logger.info("G2Scraper initialized with curl_cffi")

    async def close(self) -> None:
        if self._curl_session:
            await self._curl_session.close()
            self._curl_session = None

    async def __aenter__(self) -> "G2Scraper":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _detect_keywords(self, text: str) -> List[str]:
        if not text:
            return []
        found = []
        text_lower = text.lower()
        for kw in self.keywords:
            if kw.lower() in text_lower:
                found.append(kw)
        return found

    async def scrape_company(self, ticker: str, session: aiohttp.ClientSession = None) -> List[G2Review]:
        slug = self.company_mappings.get(ticker)
        if not slug:
            logger.warning(f"No G2 slug for {ticker}")
            return []

        logger.info(f"Scraping G2 for {ticker} ({slug}) via Bing search")

        try:
            if not self._curl_session:
                await self.initialize()

            reviews = await self._bing_search_g2(ticker, slug)
            if reviews:
                logger.info(f"Bing search success for G2 {ticker}: {len(reviews)} reviews")
                return reviews

        except Exception as e:
            logger.warning(f"Bing search failed for G2 {ticker}: {e}")

        return []

    async def _bing_search_g2(self, ticker: str, slug: str) -> List[G2Review]:
        """Parse G2 reviews from Bing search results"""
        try:
            query = f"site:g2.com/products/{slug} reviews rating"
            url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }
            
            response = await self._curl_session.get(url, headers=headers)
            if response.status_code != 200:
                return []
                
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            reviews = []
            
            for result_elem in soup.select("li.b_algo, div.b_caption")[:10]:
                text = result_elem.get_text()
                
                rating_match = re.search(r'Rating:\s*(\d+\.?\d*)\s*/\s*5', text, re.IGNORECASE)
                if not rating_match:
                    rating_match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', text, re.IGNORECASE)
                
                rating = float(rating_match.group(1)) if rating_match else None
                
                review_text = text[:500] if len(text) > 50 else ""
                
                if rating:
                    keywords = self._detect_keywords(review_text)
                    reviews.append(G2Review(
                        ticker=ticker,
                        platform="g2-bing",
                        product_name=slug,
                        rating=rating,
                        review_text=review_text,
                        review_date=None,
                        keywords_detected=keywords,
                        fetched_at=datetime.now(timezone.utc).isoformat()
                    ))
            
            return reviews
        except Exception as e:
            logger.warning(f"Bing search failed for G2: {e}")
            return []

    async def _bing_search_fallback(self, ticker: str, slug: str, session: aiohttp.ClientSession) -> List[G2Review]:
        """Compatibility wrapper for ProductIntelEngine"""
        return await self._bing_search_g2(ticker, slug)


class CapterraScraper:
    BASE_URL = "https://www.capterra.com"

    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.capterra_config = self.config.get("g2_capterra", {})
        self.keywords = self.capterra_config.get("keywords", [])
        self.date_filter_days = self.capterra_config.get("date_filter_days", 90)
        self.company_mappings = {
            "NVDA": "nvidia",
            "AMD": "advanced-micro-devices",
            "MSFT": "microsoft",
            "GOOGL": "google",
            "META": "meta",
            "TSLA": "tesla",
            "AAPL": "apple",
            "AMZN": "amazon",
        }
        self._curl_session: Optional[AsyncSession] = None

    async def initialize(self) -> None:
        self._curl_session = AsyncSession(
            impersonate="chrome120",
            timeout=aiohttp.ClientTimeout(total=30)
        )
        logger.info("CapterraScraper initialized with curl_cffi")

    async def close(self) -> None:
        if self._curl_session:
            await self._curl_session.close()
            self._curl_session = None

    async def __aenter__(self) -> "CapterraScraper":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _detect_keywords(self, text: str) -> List[str]:
        if not text:
            return []
        found = []
        text_lower = text.lower()
        for kw in self.keywords:
            if kw.lower() in text_lower:
                found.append(kw)
        return found

    async def scrape_company(self, ticker: str, session: aiohttp.ClientSession = None) -> List[CapterraReview]:
        slug = self.company_mappings.get(ticker)
        if not slug:
            logger.warning(f"No Capterra slug for {ticker}")
            return []

        logger.info(f"Scraping Capterra for {ticker} ({slug}) via Bing search")

        try:
            if not self._curl_session:
                await self.initialize()

            reviews = await self._bing_search_capterra(ticker, slug)
            if reviews:
                logger.info(f"Bing search success for Capterra {ticker}: {len(reviews)} reviews")
                return reviews

        except Exception as e:
            logger.warning(f"Bing search failed for Capterra {ticker}: {e}")

        return []

    async def _bing_search_capterra(self, ticker: str, slug: str) -> List[CapterraReview]:
        """Parse Capterra reviews from Bing search results"""
        try:
            query = f"site:capterra.com/p/{slug} reviews rating"
            url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }
            
            response = await self._curl_session.get(url, headers=headers)
            if response.status_code != 200:
                return []
                
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            reviews = []
            
            for result_elem in soup.select("li.b_algo, div.b_caption")[:10]:
                text = result_elem.get_text()
                
                rating_match = re.search(r'Rating:\s*(\d+\.?\d*)\s*/\s*5', text, re.IGNORECASE)
                if not rating_match:
                    rating_match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', text, re.IGNORECASE)
                
                rating = float(rating_match.group(1)) if rating_match else None
                
                review_text = text[:500] if len(text) > 50 else ""
                
                if rating:
                    keywords = self._detect_keywords(review_text)
                    reviews.append(CapterraReview(
                        ticker=ticker,
                        platform="capterra-bing",
                        product_name=slug,
                        rating=rating,
                        review_text=review_text,
                        review_date=None,
                        keywords_detected=keywords,
                        fetched_at=datetime.now(timezone.utc).isoformat()
                    ))
            
            return reviews
        except Exception as e:
            logger.warning(f"Bing search failed for Capterra: {e}")
            return []

    async def _bing_search_fallback(self, ticker: str, slug: str, session: aiohttp.ClientSession) -> List[CapterraReview]:
        """Compatibility wrapper for ProductIntelEngine"""
        return await self._bing_search_capterra(ticker, slug)


class AppStoreScraper:
    BASE_URL = "https://itunes.apple.com"

    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.app_store_config = self.config.get("app_store", {})
        self.flagship_apps = self.app_store_config.get("flagship_apps", {})
        self.date_filter_days = self.app_store_config.get("date_filter_days", 90)

    async def scrape_company(self, ticker: str, session: aiohttp.ClientSession) -> List[AppStoreReview]:
        app_name = self.flagship_apps.get(ticker)
        if not app_name:
            logger.warning(f"No App Store app for {ticker}")
            return []

        url = f"{self.BASE_URL}/rss/customerreviews/id={app_name}/sortby=mostrecent/json"
        logger.info(f"Scraping App Store for {ticker} ({app_name})")

        try:
            async with session.get(url) as response:
                if response.status != 200:
                    logger.warning(f"App Store returned {response.status} for {ticker}")
                    return []
                text = await response.text()
                import json
                data = json.loads(text)
        except Exception as e:
            logger.error(f"Error fetching App Store for {ticker}: {e}")
            return []

        reviews = []
        entries = data.get("feed", {}).get("entry", [])

        for entry in entries[:50]:
            try:
                rating_elem = entry.get("im:rating", {})
                rating = float(rating_elem.get("label", 0)) if rating_elem else None

                text_elem = entry.get("content", {})
                review_text = text_elem.get("label", "")

                date_elem = entry.get("updated", {})
                review_date = date_elem.get("label", "") if date_elem else None

                vader_compound = self._vader_score(review_text)

                reviews.append(AppStoreReview(
                    ticker=ticker,
                    platform="apple",
                    app_id=app_name,
                    app_name=app_name,
                    rating=rating,
                    review_text=review_text,
                    review_date=review_date,
                    vader_compound=vader_compound,
                    fetched_at=datetime.now(timezone.utc).isoformat()
                ))
            except Exception as e:
                logger.debug(f"Error parsing App Store review: {e}")
                continue

        return reviews

    def _vader_score(self, text: str) -> Optional[float]:
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            analyzer = SentimentIntensityAnalyzer()
            return analyzer.polarity_scores(text)["compound"]
        except Exception:
            return None


class ProductIntelEngine:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.g2_scraper = G2Scraper(config_dict)
        self.capterra_scraper = CapterraScraper(config_dict)
        self.app_store_scraper = AppStoreScraper(config_dict)
        self.company_mappings = {
            "NVDA": "nvidia",
            "AMD": "advanced-micro-devices",
            "MSFT": "microsoft",
            "GOOGL": "google",
            "META": "meta",
            "TSLA": "tesla",
            "AAPL": "apple",
            "AMZN": "amazon",
        }

    async def initialize(self) -> None:
        await self.g2_scraper.initialize()
        await self.capterra_scraper.initialize()
        logger.info("ProductIntelEngine initialized with curl_cffi")

    async def close(self) -> None:
        await self.g2_scraper.close()
        await self.capterra_scraper.close()

    async def __aenter__(self) -> "ProductIntelEngine":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def gather_intel(self, tickers: List[str]) -> Dict[str, Dict]:
        results = {}

        for ticker in tickers:
            logger.info(f"Gathering product intel for {ticker}")

            g2_reviews = await self.g2_scraper.scrape_company(ticker)
            await asyncio.sleep(random.uniform(2, 5))

            capterra_reviews = await self.capterra_scraper.scrape_company(ticker)
            await asyncio.sleep(random.uniform(2, 5))

            async with aiohttp.ClientSession() as session:
                app_store_reviews = await self.app_store_scraper.scrape_company(ticker, session)

            results[ticker] = {
                "g2": [r.__dict__ for r in g2_reviews],
                "capterra": [r.__dict__ for r in capterra_reviews],
                "app_store": [r.__dict__ for r in app_store_reviews],
                "fetched_at": datetime.now(timezone.utc).isoformat()
            }

        return results

    async def get_all_snapshots(self) -> Dict[str, Dict]:
        """Alias for gather_intel to maintain compatibility with orchestrator"""
        tickers = list(self.company_mappings.keys())
        return await self.gather_intel(tickers)

    def compute_product_sentiment(self, reviews: List[Dict]) -> float:
        if not reviews:
            return 0.0

        vader_scores = [safe_float(r.get("vader_compound")) for r in reviews if r.get("vader_compound") is not None]
        ratings = [safe_float(r.get("rating")) for r in reviews if r.get("rating") is not None]

        scores = []
        if vader_scores:
            avg_vader = safe_float(sum(vader_scores) / len(vader_scores))
            scores.append(guard_bounds(avg_vader, -1.0, 1.0))
        if ratings:
            avg_rating = safe_float(sum(ratings) / len(ratings))
            normalized_rating = guard_bounds((avg_rating - 3) / 2, -1.0, 1.0)
            scores.append(normalized_rating)

        result = safe_float(sum(scores) / len(scores)) if scores else 0.0
        return guard_bounds(result, -1.0, 1.0)


async def create_product_intel_engine(config_dict: dict = None) -> ProductIntelEngine:
    engine = ProductIntelEngine(config_dict)
    await engine.initialize()
    return engine


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    async def test():
        engine = await create_product_intel_engine()
        results = await engine.gather_intel(["NVDA", "MSFT"])
        for ticker, data in results.items():
            print(f"\n{ticker}:")
            print(f"  G2: {len(data['g2'])} reviews")
            print(f"  Capterra: {len(data['capterra'])} reviews")
            print(f"  App Store: {len(data['app_store'])} reviews")

    asyncio.run(test())