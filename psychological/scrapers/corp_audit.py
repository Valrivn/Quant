import asyncio
import logging
import random
import re
import os
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass

from bs4 import BeautifulSoup
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from curl_cffi import AsyncSession

from psychological.scrapers.validation_gate import CrossValidationGate, ValidationGateResult
from psychological.scrapers.browserless_client import BrowserlessClient, create_browserless_client
from config import load_hybrid_config

logger = logging.getLogger(__name__)


class RotatingProxyPool:
    """Rotating proxy pool ensuring no outbound request originates from host IP.
    All scraped targets receive traffic through anonymized proxy endpoints,
    matching the anonymity mandate: never make direct raw calls to target hosts.
    """

    def __init__(self, proxies: Optional[List[str]] = None):
        self._proxies: List[str] = proxies or []
        self._index = 0
        self._lock = asyncio.Lock()
        self._session_proxy: Optional[str] = None

    @classmethod
    def from_config(cls, config_dict: dict) -> "RotatingProxyPool":
        proxy_config = config_dict.get("proxy_pool", {})
        proxies = proxy_config.get("proxies", [])
        if not proxies or proxies == ["http://127.0.0.1:8080"]:
            env_proxies = os.environ.get("PROXY_POOL", "")
            if env_proxies:
                proxies = [p.strip() for p in env_proxies.split(",") if p.strip()]
        
        # If no custom proxies configured, dynamically fetch free public proxies
        if not proxies or proxies == ["http://127.0.0.1:8080"]:
            logger.info("No custom proxies configured. Fetching free proxy lists dynamically...")
            proxies = cls.fetch_free_proxies()

        if not proxies:
            logger.warning("No proxies configured in proxy_pool or PROXY_POOL env. "
                           "Falling back to localhost — anonymity will be degraded.")
            proxies = ["http://127.0.0.1:8080"]
        return cls(proxies=proxies)

    @staticmethod
    def fetch_free_proxies() -> List[str]:
        import urllib.request
        import ssl
        urls = [
            "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
            "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all"
        ]
        proxies = []
        ctx = ssl._create_unverified_context()
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, context=ctx, timeout=5) as response:
                    text = response.read().decode('utf-8')
                    found = re.findall(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+\b', text)
                    for f in found:
                        proxies.append(f"http://{f}")
            except Exception as e:
                logger.debug(f"Failed to fetch proxy list from {url}: {e}")
        logger.info(f"Fetched {len(proxies)} free proxies dynamically.")
        return proxies

    async def acquire(self) -> Optional[str]:
        if not self._proxies:
            return None
        async with self._lock:
            proxy = self._proxies[self._index % len(self._proxies)]
            self._index += 1
            return proxy

    def attach_to_session(self, session: AsyncSession) -> None:
        self._session_proxy = None

    @property
    def proxy_count(self) -> int:
        return len(self._proxies)


@dataclass
class GlassdoorScore:
    ticker: str
    slug: str
    raw_score: Optional[float]
    normalized_score: Optional[float]
    review_count: Optional[int]
    ceo_approval: Optional[int]
    recommend_to_friend: Optional[int]
    fetched_at: str


@dataclass
class G2EmployerScore:
    ticker: str
    slug: str
    overall_rating: Optional[float]
    review_count: Optional[int]
    would_recommend_pct: Optional[float]
    categories: Dict[str, float]
    fetched_at: str


class GlassdoorScraper:
    BASE_URL = "https://www.glassdoor.com"

    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.glassdoor_config = self.config.get("glassdoor", {})
        self.company_slugs = self.glassdoor_config.get("company_slugs", {})
        self.vader = SentimentIntensityAnalyzer()
        self.company_names = {
            "NVDA": "NVIDIA",
            "AMD": "Advanced Micro Devices",
            "MSFT": "Microsoft",
            "GOOGL": "Google",
            "META": "Meta",
            "TSLA": "Tesla",
            "AAPL": "Apple",
            "AMZN": "Amazon",
        }
        self._curl_session: Optional[AsyncSession] = None
        self._proxy_pool: Optional[RotatingProxyPool] = None
        self._browserless: Optional[BrowserlessClient] = None

    async def initialize(self) -> None:
        self._proxy_pool = RotatingProxyPool.from_config(self.config)
        proxy_url = await self._proxy_pool.acquire()
        self._curl_session = AsyncSession(
            impersonate="chrome124",
            proxy=proxy_url,
            timeout=30
        )
        self._browserless = await create_browserless_client(self.config)
        logger.info("GlassdoorScraper initialized with curl_cffi chrome124 + rotating proxy pool (%d proxies) + browserless",
                     self._proxy_pool.proxy_count)

    async def close(self) -> None:
        if self._curl_session:
            await self._curl_session.close()
            self._curl_session = None
        if self._browserless:
            await self._browserless.close()
            self._browserless = None

    async def __aenter__(self) -> "GlassdoorScraper":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _parse_score(self, text: str) -> Optional[float]:
        try:
            match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', text, re.IGNORECASE)
            if match:
                return float(match.group(1))
            match = re.search(r'(\d+\.?\d*)', text)
            if match:
                val = float(match.group(1))
                if 0 <= val <= 5:
                    return val
        except Exception:
            pass
        return None

    def _parse_percentage(self, text: str) -> Optional[int]:
        try:
            match = re.search(r'(\d+)%', text)
            if match:
                return int(match.group(1))
        except Exception:
            pass
        return None

    async def _rotate_proxy(self) -> None:
        if not self._proxy_pool or not self._curl_session:
            return
        proxy_url = await self._proxy_pool.acquire()
        if proxy_url:
            await self._curl_session.close()
            self._curl_session = AsyncSession(
                impersonate="chrome124",
                proxy=proxy_url,
                timeout=30
            )
            logger.debug("Rotated to new proxy: %s", proxy_url)

    async def scrape_company(self, ticker: str) -> GlassdoorScore:
        slug = self.company_slugs.get(ticker)
        if not slug:
            logger.warning("No Glassdoor slug for %s", ticker)
            return GlassdoorScore(ticker, "", None, None, None, None, None, datetime.now(timezone.utc).isoformat())

        company_name = self.company_names.get(ticker, ticker)
        logger.info("Scraping Glassdoor for %s (%s) directly", ticker, company_name)

        raw_score = None
        review_count = None
        ceo_approval = None
        recommend_to_friend = None

        try:
            if not self._curl_session:
                await self.initialize()

            result = await self._scrape_glassdoor_direct(slug)
            if result:
                raw_score = result.get("raw_score")
                review_count = result.get("review_count")
                ceo_approval = result.get("ceo_approval")
                recommend_to_friend = result.get("recommend_to_friend")
                logger.info("Direct Glassdoor scrape success for %s: score=%s", ticker, raw_score)

        except Exception as e:
            logger.warning("Direct Glassdoor scrape failed for %s: %s", ticker, e)

        normalized = raw_score / 5.0 if raw_score else None

        if raw_score is None:
            logger.info("Glassdoor direct scrape failed for %s, retrying with proxy rotation...", ticker)
            await self._rotate_proxy()
            try:
                result = await self._scrape_glassdoor_direct(slug)
                if result:
                    raw_score = result.get("raw_score")
                    review_count = result.get("review_count")
                    ceo_approval = result.get("ceo_approval")
                    recommend_to_friend = result.get("recommend_to_friend")
                    normalized = raw_score / 5.0 if raw_score else None
                    logger.info("Glassdoor proxy-rotate retry success for %s: score=%s", ticker, raw_score)
            except Exception as e:
                logger.warning("Glassdoor proxy-rotate retry also failed for %s: %s", ticker, e)

        if raw_score is None and self._browserless:
            logger.info("Glassdoor proxy-rotate failed for %s, attempting browserless fallback...", ticker)
            try:
                result = await self._scrape_glassdoor_browserless(slug)
                if result:
                    raw_score = result.get("raw_score")
                    review_count = result.get("review_count")
                    ceo_approval = result.get("ceo_approval")
                    recommend_to_friend = result.get("recommend_to_friend")
                    normalized = raw_score / 5.0 if raw_score else None
                    logger.info("Glassdoor browserless fallback success for %s: score=%s", ticker, raw_score)
            except Exception as e:
                logger.warning("Glassdoor browserless fallback also failed for %s: %s", ticker, e)

        return GlassdoorScore(
            ticker=ticker,
            slug=slug,
            raw_score=raw_score,
            normalized_score=normalized,
            review_count=review_count,
            ceo_approval=ceo_approval,
            recommend_to_friend=recommend_to_friend,
            fetched_at=datetime.now(timezone.utc).isoformat()
        )

    async def _scrape_glassdoor_browserless(self, slug: str) -> Optional[Dict]:
        """Glassdoor scraping via browserless (JavaScript execution for Cloudflare challenges)."""
        if not self._browserless:
            return None

        try:
            url = f"{self.BASE_URL}/Reviews/{slug}-reviews-SRCH_KE0,{len(slug)}.htm"

            result = await self._browserless.scrape(
                url=url,
                wait_for="div[data-test='overallRating'], span[data-test='overall-rating'], .ratingNumber",
                wait_until="networkidle2",
                timeout=60000,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.glassdoor.com/index.htm",
                }
            )

            if not result.success:
                logger.warning("Browserless Glassdoor fetch failed for slug=%s: %s", slug, result.error)
                return None

            html = result.html
            soup = BeautifulSoup(html, "html.parser")

            result_dict: Dict = {"raw_score": None, "review_count": None,
                                "ceo_approval": None, "recommend_to_friend": None}

            overall_rating_elem = soup.select_one(
                "div[data-test='overallRating'], "
                "span[data-test='overall-rating'], "
                ".ratingNumber, "
                "[class*='rating'] [class*='number'], "
                ".bigRating strong, "
                ".ratingNum"
            )
            if overall_rating_elem:
                score = self._parse_score(overall_rating_elem.get_text(strip=True))
                if score:
                    result_dict["raw_score"] = score

            page_text = soup.get_text()

            if result_dict["raw_score"] is None:
                score_match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', page_text, re.IGNORECASE)
                if score_match:
                    result_dict["raw_score"] = float(score_match.group(1))

            review_match = re.search(r'([\d,]+)\s*(?:review|Reviews)', page_text, re.IGNORECASE)
            if review_match:
                result_dict["review_count"] = int(review_match.group(1).replace(",", ""))

            ceo_match = re.search(r'(\d+)%\s*(?:approve|CEO)', page_text, re.IGNORECASE)
            if ceo_match:
                result_dict["ceo_approval"] = int(ceo_match.group(1))

            recommend_match = re.search(r'(\d+)%\s*(?:recommend|would recommend)', page_text, re.IGNORECASE)
            if recommend_match:
                result_dict["recommend_to_friend"] = int(recommend_match.group(1))

            return result_dict

        except Exception as e:
            logger.warning("Browserless Glassdoor scrape exception: %s", e)
            return None

    async def _scrape_glassdoor_direct(self, slug: str) -> Optional[Dict]:
        """Direct Glassdoor reviews page scraping via curl_cffi + proxy."""
        try:
            url = f"{self.BASE_URL}/Reviews/{slug}-reviews-SRCH_KE0,{len(slug)}.htm"

            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.glassdoor.com/index.htm",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }

            response = await self._curl_session.get(url, headers=headers)
            if response.status_code != 200:
                logger.warning("Glassdoor direct fetch returned %d for slug=%s", response.status_code, slug)
                return None

            html = response.text
            soup = BeautifulSoup(html, "html.parser")

            result: Dict = {"raw_score": None, "review_count": None,
                            "ceo_approval": None, "recommend_to_friend": None}

            overall_rating_elem = soup.select_one(
                "div[data-test='overallRating'], "
                "span[data-test='overall-rating'], "
                ".ratingNumber, "
                "[class*='rating'] [class*='number'], "
                ".bigRating strong, "
                ".ratingNum"
            )
            if overall_rating_elem:
                score = self._parse_score(overall_rating_elem.get_text(strip=True))
                if score:
                    result["raw_score"] = score

            page_text = soup.get_text()

            if result["raw_score"] is None:
                score_match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', page_text, re.IGNORECASE)
                if score_match:
                    result["raw_score"] = float(score_match.group(1))

            review_match = re.search(r'([\d,]+)\s*(?:review|Reviews)', page_text, re.IGNORECASE)
            if review_match:
                result["review_count"] = int(review_match.group(1).replace(",", ""))

            ceo_match = re.search(r'(\d+)%\s*(?:approve|CEO)', page_text, re.IGNORECASE)
            if ceo_match:
                result["ceo_approval"] = int(ceo_match.group(1))

            recommend_match = re.search(r'(\d+)%\s*(?:recommend|would recommend)', page_text, re.IGNORECASE)
            if recommend_match:
                result["recommend_to_friend"] = int(recommend_match.group(1))

            return result

        except Exception as e:
            logger.warning("Glassdoor direct scrape exception: %s", e)
            return None

    async def scrape_all(self, tickers: List[str]) -> Dict[str, GlassdoorScore]:
        results = {}
        for ticker in tickers:
            results[ticker] = await self.scrape_company(ticker)
            await asyncio.sleep(random.uniform(12, 25))
        return results

    async def get_all_snapshots(self) -> Dict[str, GlassdoorScore]:
        tickers = list(self.company_slugs.keys())
        return await self.scrape_all(tickers)


class G2EmployerScraper:
    BASE_URL = "https://www.g2.com"

    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.g2_config = self.config.get("g2_capterra", {})
        self.company_slugs = self.config.get("glassdoor", {}).get("company_slugs", {})
        self.company_names = {
            "NVDA": "nvidia",
            "AMD": "advanced-micro-devices",
            "MSFT": "microsoft",
            "GOOGL": "google",
            "META": "meta",
            "TSLA": "tesla",
            "AAPL": "apple",
            "AMZN": "amazon",
        }
        self.vader = SentimentIntensityAnalyzer()
        self._curl_session: Optional[AsyncSession] = None
        self._proxy_pool: Optional[RotatingProxyPool] = None

    async def initialize(self) -> None:
        self._proxy_pool = RotatingProxyPool.from_config(self.config)
        proxy_url = await self._proxy_pool.acquire()
        self._curl_session = AsyncSession(
            impersonate="chrome124",
            proxy=proxy_url,
            timeout=30
        )
        logger.info("G2EmployerScraper initialized with curl_cffi chrome124 + rotating proxy pool (%d proxies)",
                     self._proxy_pool.proxy_count)

    async def close(self) -> None:
        if self._curl_session:
            await self._curl_session.close()
            self._curl_session = None

    async def __aenter__(self) -> "G2EmployerScraper":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _rotate_proxy(self) -> None:
        if not self._proxy_pool or not self._curl_session:
            return
        proxy_url = await self._proxy_pool.acquire()
        if proxy_url:
            await self._curl_session.close()
            self._curl_session = AsyncSession(
                impersonate="chrome124",
                proxy=proxy_url,
                timeout=30
            )
            logger.debug("G2 rotated to new proxy: %s", proxy_url)

    def _parse_rating(self, text: str) -> Optional[float]:
        try:
            match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*(?:5|10)', text, re.IGNORECASE)
            if match:
                val = float(match.group(1))
                return val
            match = re.search(r'(\d+\.?\d*)', text)
            if match:
                val = float(match.group(1))
                if 0 <= val <= 5:
                    return val
        except Exception:
            pass
        return None

    def _normalize_rating(self, rating: float, scale: int = 5) -> float:
        if scale == 10:
            return rating / 2.0
        return rating

    async def scrape_company(self, ticker: str) -> G2EmployerScore:
        slug = self.company_slugs.get(ticker)
        if not slug:
            logger.warning("No G2 slug for %s", ticker)
            return G2EmployerScore(ticker, "", None, 0, None, {}, datetime.now(timezone.utc).isoformat())

        logger.info("Scraping G2 for %s (%s) directly", ticker, slug)

        overall_rating = None
        review_count = 0
        would_recommend_pct = None
        categories = {}

        try:
            if not self._curl_session:
                await self.initialize()

            result = await self._scrape_g2_direct(slug)
            if result:
                overall_rating = result.get("overall_rating")
                review_count = result.get("review_count", 0)
                would_recommend_pct = result.get("would_recommend_pct")
                categories = result.get("categories", {})
                logger.info("Direct G2 scrape success for %s: rating=%s", ticker, overall_rating)

        except Exception as e:
            logger.warning("Direct G2 scrape failed for %s: %s", ticker, e)

        if overall_rating is None:
            logger.info("G2 direct scrape failed for %s, retrying with proxy rotation...", ticker)
            await self._rotate_proxy()
            try:
                result = await self._scrape_g2_direct(slug)
                if result:
                    overall_rating = result.get("overall_rating")
                    review_count = result.get("review_count", 0)
                    would_recommend_pct = result.get("would_recommend_pct")
                    categories = result.get("categories", {})
                    logger.info("G2 proxy-rotate retry success for %s: rating=%s", ticker, overall_rating)
            except Exception as e:
                logger.warning("G2 proxy-rotate retry also failed for %s: %s", ticker, e)

        return G2EmployerScore(
            ticker=ticker,
            slug=slug,
            overall_rating=overall_rating,
            review_count=review_count,
            would_recommend_pct=would_recommend_pct,
            categories=categories,
            fetched_at=datetime.now(timezone.utc).isoformat()
        )

    async def _scrape_g2_direct(self, slug: str) -> Optional[Dict]:
        """Direct G2 product reviews page scraping via curl_cffi + proxy."""
        try:
            url = f"{self.BASE_URL}/products/{slug}/reviews"

            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.g2.com/",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }

            response = await self._curl_session.get(url, headers=headers)
            if response.status_code != 200:
                logger.warning("G2 direct fetch returned %d for slug=%s", response.status_code, slug)
                return None

            html = response.text
            soup = BeautifulSoup(html, "html.parser")

            result: Dict = {"overall_rating": None, "review_count": 0,
                            "would_recommend_pct": None, "categories": {}}

            rating_elem = soup.select_one(
                "[data-testid='rating-value'], "
                "[class*='star-rating'] [class*='rating'], "
                "[class*='overall-rating'] span, "
                ".product-page__rating-number, "
                "[itemprop='ratingValue']"
            )
            if rating_elem:
                parsed = self._parse_rating(rating_elem.get_text(strip=True))
                if parsed:
                    result["overall_rating"] = parsed

            page_text = soup.get_text()

            if result["overall_rating"] is None:
                rating_match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', page_text, re.IGNORECASE)
                if rating_match:
                    result["overall_rating"] = float(rating_match.group(1))

            count_match = re.search(r'([\d,]+)\s*(?:review|Review|Reviews)', page_text, re.IGNORECASE)
            if count_match:
                result["review_count"] = int(count_match.group(1).replace(",", ""))

            recommend_match = re.search(r'(\d+)%\s*(?:would recommend|recommend)', page_text, re.IGNORECASE)
            if recommend_match:
                result["would_recommend_pct"] = float(recommend_match.group(1))

            category_sections = soup.select("[class*='category-rating'], [class*='criteria-rating']")
            for section in category_sections:
                label_el = section.select_one("[class*='label'], [class*='name'], h4, h5")
                value_el = section.select_one("[class*='value'], [class*='number'], [class*='rating']")
                if label_el and value_el:
                    label = label_el.get_text(strip=True)
                    value = self._parse_rating(value_el.get_text(strip=True))
                    if label and value is not None:
                        result["categories"][label] = value

            return result

        except Exception as e:
            logger.warning("G2 direct scrape exception: %s", e)
            return None

    async def scrape_all(self, tickers: List[str]) -> Dict[str, G2EmployerScore]:
        results = {}
        for ticker in tickers:
            results[ticker] = await self.scrape_company(ticker)
            await asyncio.sleep(random.uniform(12, 25))
        return results

    async def get_all_snapshots(self) -> Dict[str, G2EmployerScore]:
        tickers = list(self.company_names.keys())
        return await self.scrape_all(tickers)


class CorpAuditEngine:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.glassdoor_scraper = GlassdoorScraper(config_dict)
        self.g2_scraper = G2EmployerScraper(config_dict)
        self.validation_gate = CrossValidationGate(config_dict)

    async def initialize(self) -> None:
        await self.glassdoor_scraper.initialize()
        await self.g2_scraper.initialize()
        logger.info("CorpAuditEngine initialized (Glassdoor + G2 via rotating proxy pool)")

    async def close(self) -> None:
        await self.glassdoor_scraper.close()
        await self.g2_scraper.close()

    async def __aenter__(self) -> "CorpAuditEngine":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def audit_ticker(self, ticker: str) -> Dict:
        logger.info("Running corp audit for %s", ticker)

        glassdoor_result = await self.glassdoor_scraper.scrape_company(ticker)
        await asyncio.sleep(random.uniform(2, 5))

        g2_result = await self.g2_scraper.scrape_company(ticker)

        g2_100 = (g2_result.overall_rating * 20.0) if g2_result.overall_rating is not None else None
        validation_result = self.validation_gate.evaluate(
            glassdoor_raw=glassdoor_result.raw_score,
            comparably_badge=g2_100
        )

        audit_result = {
            "ticker": ticker,
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "glassdoor": {
                "slug": glassdoor_result.slug,
                "raw_score": glassdoor_result.raw_score,
                "normalized": glassdoor_result.normalized_score,
                "review_count": glassdoor_result.review_count,
                "ceo_approval": glassdoor_result.ceo_approval,
                "recommend_to_friend": glassdoor_result.recommend_to_friend,
                "fetched_at": glassdoor_result.fetched_at
            },
            "g2": {
                "slug": g2_result.slug,
                "overall_rating": g2_result.overall_rating,
                "normalized": g2_result.overall_rating / 5.0 if g2_result.overall_rating else None,
                "review_count": g2_result.review_count,
                "would_recommend_pct": g2_result.would_recommend_pct,
                "categories": g2_result.categories,
                "fetched_at": g2_result.fetched_at
            },
            "validation_gate": {
                "normalized_glassdoor": validation_result.normalized_glassdoor,
                "normalized_comparably": validation_result.normalized_comparably,
                "divergence": validation_result.divergence,
                "penalty_multiplier": validation_result.penalty_multiplier,
                "override_triggered": validation_result.override_triggered,
                "confidence_floor": validation_result.confidence_floor,
                "kappa": validation_result.kappa
            }
        }

        return audit_result

    async def audit_all(self, tickers: List[str]) -> Dict[str, Dict]:
        results = {}
        for ticker in tickers:
            results[ticker] = await self.audit_ticker(ticker)
        return results


async def create_corp_audit_engine(config_dict: dict = None) -> CorpAuditEngine:
    engine = CorpAuditEngine(config_dict)
    await engine.initialize()
    return engine


async def create_glassdoor_scraper(config_dict: dict = None) -> GlassdoorScraper:
    scraper = GlassdoorScraper(config_dict)
    await scraper.initialize()
    return scraper


async def create_g2_employer_scraper(config_dict: dict = None) -> G2EmployerScraper:
    scraper = G2EmployerScraper(config_dict)
    await scraper.initialize()
    return scraper


# Backward-compat aliases — keep old ComparablyScraper name resolving
ComparablyScraper = G2EmployerScraper
G2Scraper = G2EmployerScraper
G2Score = G2EmployerScore
ComparablyBadges = G2EmployerScore
create_comparably_scraper = create_g2_employer_scraper
create_g2_scraper = create_g2_employer_scraper


if __name__ == "__main__":
    import os
    import sys
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    async def run_audit():
        tickers = ["NVDA", "AMD"]
        if len(sys.argv) > 1:
            tickers = [sys.argv[1]]
        
        async with await create_corp_audit_engine() as engine:
            results = await engine.audit_all(tickers)
            for ticker, data in results.items():
                print(f"\nAudit results for {ticker}:")
                print(f"  Glassdoor: raw_score={data['glassdoor']['raw_score']}/5.0, reviews={data['glassdoor']['review_count']}, ceo={data['glassdoor']['ceo_approval']}%, recommend={data['glassdoor']['recommend_to_friend']}%")
                print(f"  G2: overall_rating={data['g2']['overall_rating']}/5.0, reviews={data['g2']['review_count']}, would_recommend={data['g2']['would_recommend_pct']}%")
                vg = data['validation_gate']
                print(f"  Validation Gate: divergence={vg['divergence']}, penalty={vg['penalty_multiplier']}, override_triggered={vg['override_triggered']}")

    asyncio.run(run_audit())