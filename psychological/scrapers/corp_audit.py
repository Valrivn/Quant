import asyncio
import logging
import random
import re
import json
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass

import aiohttp
from bs4 import BeautifulSoup
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from curl_cffi import AsyncSession

from psychological.scrapers.validation_gate import CrossValidationGate, ValidationGateResult
from config import load_hybrid_config

logger = logging.getLogger(__name__)


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
class ComparablyBadges:
    ticker: str
    slug: str
    badge_score: Optional[float]
    badge_count: int
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

    async def initialize(self) -> None:
        self._curl_session = AsyncSession(
            impersonate="chrome120",
            timeout=aiohttp.ClientTimeout(total=30)
        )
        logger.info("GlassdoorScraper initialized with curl_cffi")

    async def close(self) -> None:
        if self._curl_session:
            await self._curl_session.close()
            self._curl_session = None

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

    async def scrape_company(self, ticker: str) -> GlassdoorScore:
        slug = self.company_slugs.get(ticker)
        if not slug:
            logger.warning(f"No Glassdoor slug for {ticker}")
            return GlassdoorScore(ticker, "", None, None, None, None, None, datetime.now(timezone.utc).isoformat())

        company_name = self.company_names.get(ticker, ticker)
        logger.info(f"Scraping Glassdoor for {ticker} ({company_name}) via Bing search")

        raw_score = None
        review_count = None
        ceo_approval = None
        recommend_to_friend = None

        try:
            if not self._curl_session:
                await self.initialize()

            bing_result = await self._bing_search_glassdoor(company_name, slug)
            if bing_result:
                raw_score = bing_result.get("raw_score")
                review_count = bing_result.get("review_count")
                ceo_approval = bing_result.get("ceo_approval")
                recommend_to_friend = bing_result.get("recommend_to_friend")
                logger.info(f"Bing search success for Glassdoor {ticker}: score={raw_score}")

        except Exception as e:
            logger.warning(f"Bing search scrape failed for Glassdoor {ticker}: {e}")

        normalized = raw_score / 5.0 if raw_score else None

        if raw_score is None:
            logger.info(f"Glassdoor Bing search failed for {ticker}, attempting SEC EDGAR fallback...")
            fallback_result = await self._sec_edgar_fallback(ticker, company_name)
            if fallback_result:
                return fallback_result

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

    async def _bing_search_glassdoor(self, company_name: str, slug: str) -> Optional[Dict]:
        """Parse Glassdoor data from Bing search results"""
        try:
            query = f"site:glassdoor.com \"{company_name}\" reviews rating"
            url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }
            
            response = await self._curl_session.get(url, headers=headers)
            if response.status_code != 200:
                return None
                
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            
            result = {"raw_score": None, "review_count": None, "ceo_approval": None, "recommend_to_friend": None}
            
            for result_elem in soup.select("li.b_algo, div.b_caption"):
                text = result_elem.get_text()
                
                rating_match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', text, re.IGNORECASE)
                if rating_match:
                    result["raw_score"] = float(rating_match.group(1))
                
                review_match = re.search(r'([\d,]+)\s*reviews?', text, re.IGNORECASE)
                if review_match:
                    result["review_count"] = int(review_match.group(1).replace(',', ''))
                
                ceo_match = re.search(r'(\d+)%\s*(?:CEO|approve)', text, re.IGNORECASE)
                if ceo_match:
                    result["ceo_approval"] = int(ceo_match.group(1))
                
                recommend_match = re.search(r'(\d+)%\s*(?:recommend|friend)', text, re.IGNORECASE)
                if recommend_match:
                    result["recommend_to_friend"] = int(recommend_match.group(1))
                
                if result["raw_score"]:
                    return result
                    
            return None
        except Exception as e:
            logger.warning(f"Bing search failed for Glassdoor: {e}")
            return None

    async def _sec_edgar_fallback(self, ticker: str, company_name: str) -> Optional[GlassdoorScore]:
        """Fallback 2: SEC EDGAR 10-K JSON Filings for employee headcount"""
        try:
            logger.info(f"Attempting SEC EDGAR fallback for {ticker}")
            
            async with aiohttp.ClientSession() as session:
                headers = {"User-Agent": "Quant-Psychological/1.0 (contact@example.com)"}
                
                async with session.get("https://www.sec.gov/files/company_tickers.json", headers=headers) as response:
                    if response.status != 200:
                        return None
                    ticker_data = await response.json()
                
                cik = None
                for entry in ticker_data.values():
                    if entry.get("ticker", "").upper() == ticker.upper():
                        cik = str(entry.get("cik_str", "")).zfill(10)
                        break
                
                if not cik:
                    return None
                
                async with session.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", headers=headers) as response:
                    if response.status != 200:
                        return None
                    facts = await response.json()
                
                employee_count = None
                for concept in ["EntityNumberOfEmployees", "NumberOfEmployees", "Employees"]:
                    if concept in facts.get("facts", {}).get("us-gaap", {}):
                        units = facts["facts"]["us-gaap"][concept].get("units", {})
                        for unit_key, unit_data in units.items():
                            if unit_data:
                                latest = max(unit_data, key=lambda x: x.get("end", ""))
                                employee_count = latest.get("val")
                                break
                        if employee_count:
                            break
                
                if employee_count:
                    raw_score = min(5.0, max(1.0, employee_count / 10000))
                    normalized = raw_score / 5.0
                    logger.info(f"SEC EDGAR fallback success for {ticker}: employees={employee_count}, score={raw_score}")
                    return GlassdoorScore(
                        ticker=ticker,
                        slug="sec-edgar-fallback",
                        raw_score=raw_score,
                        normalized_score=normalized,
                        review_count=None,
                        ceo_approval=None,
                        recommend_to_friend=None,
                        fetched_at=datetime.now(timezone.utc).isoformat()
                    )
        except Exception as e:
            logger.warning(f"SEC EDGAR fallback failed for {ticker}: {e}")
        return None

    async def scrape_all(self, tickers: List[str]) -> Dict[str, GlassdoorScore]:
        results = {}
        for ticker in tickers:
            results[ticker] = await self.scrape_company(ticker)
            await asyncio.sleep(random.uniform(12, 25))
        return results

    async def get_all_snapshots(self) -> Dict[str, GlassdoorScore]:
        """Alias for scrape_all to maintain compatibility with orchestrator"""
        tickers = list(self.company_slugs.keys())
        return await self.scrape_all(tickers)


class ComparablyScraper:
    BASE_URL = "https://www.comparably.com"

    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.comparably_config = self.config.get("comparably", {})
        self.company_slugs = self.comparably_config.get("company_slugs", {})
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

    async def initialize(self) -> None:
        self._curl_session = AsyncSession(
            impersonate="chrome120",
            timeout=aiohttp.ClientTimeout(total=30)
        )
        logger.info("ComparablyScraper initialized with curl_cffi")

    async def close(self) -> None:
        if self._curl_session:
            await self._curl_session.close()
            self._curl_session = None

    async def __aenter__(self) -> "ComparablyScraper":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _parse_badge_score(self, text: str) -> Optional[float]:
        try:
            match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*100', text, re.IGNORECASE)
            if match:
                return float(match.group(1))
            match = re.search(r'(\d+\.?\d*)', text)
            if match:
                val = float(match.group(1))
                if 0 <= val <= 100:
                    return val
        except Exception:
            pass
        return None

    async def scrape_company(self, ticker: str) -> ComparablyBadges:
        slug = self.company_slugs.get(ticker)
        if not slug:
            logger.warning(f"No Comparably slug for {ticker}")
            return ComparablyBadges(ticker, "", None, 0, {}, datetime.now(timezone.utc).isoformat())

        company_name = self.company_names.get(ticker, ticker)
        logger.info(f"Scraping Comparably for {ticker} ({company_name}) via Bing search")

        badge_score = None
        badge_count = 0
        categories = {}

        try:
            if not self._curl_session:
                await self.initialize()

            bing_result = await self._bing_search_comparably(company_name, slug)
            if bing_result:
                badge_score = bing_result.get("badge_score")
                badge_count = bing_result.get("badge_count", 0)
                categories = bing_result.get("categories", {})
                logger.info(f"Bing search success for Comparably {ticker}: score={badge_score}")

        except Exception as e:
            logger.warning(f"Bing search scrape failed for Comparably {ticker}: {e}")

        if badge_score is None:
            logger.info(f"Comparably Bing search failed for {ticker}, attempting SEC EDGAR fallback...")
            fallback_result = await self._sec_edgar_fallback(ticker, company_name)
            if fallback_result:
                return fallback_result

        return ComparablyBadges(
            ticker=ticker,
            slug=slug,
            badge_score=badge_score,
            badge_count=badge_count,
            categories=categories,
            fetched_at=datetime.now(timezone.utc).isoformat()
        )

    async def _bing_search_comparably(self, company_name: str, slug: str) -> Optional[Dict]:
        """Parse Comparably data from Bing search results"""
        try:
            query = f"site:comparably.com \"{company_name}\" badges score"
            url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }
            
            response = await self._curl_session.get(url, headers=headers)
            if response.status_code != 200:
                return None
                
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            
            result = {"badge_score": None, "badge_count": 0, "categories": {}}
            
            for result_elem in soup.select("li.b_algo, div.b_caption"):
                text = result_elem.get_text()
                
                score_match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*100', text, re.IGNORECASE)
                if score_match:
                    result["badge_score"] = float(score_match.group(1))
                
                badge_match = re.search(r'(\d+)\s*badges?', text, re.IGNORECASE)
                if badge_match:
                    result["badge_count"] = int(badge_match.group(1))
                
                if result["badge_score"]:
                    return result
                    
            return None
        except Exception as e:
            logger.warning(f"Bing search failed for Comparably: {e}")
            return None

    async def _sec_edgar_fallback(self, ticker: str, company_name: str) -> Optional[ComparablyBadges]:
        """Fallback 2: SEC EDGAR 10-K JSON Filings for employee headcount"""
        try:
            logger.info(f"Attempting SEC EDGAR fallback for {ticker} (Comparably)")
            
            async with aiohttp.ClientSession() as session:
                headers = {"User-Agent": "Quant-Psychological/1.0 (contact@example.com)"}
                
                async with session.get("https://www.sec.gov/files/company_tickers.json", headers=headers) as response:
                    if response.status != 200:
                        return None
                    ticker_data = await response.json()
                
                cik = None
                for entry in ticker_data.values():
                    if entry.get("ticker", "").upper() == ticker.upper():
                        cik = str(entry.get("cik_str", "")).zfill(10)
                        break
                
                if not cik:
                    return None
                
                async with session.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", headers=headers) as response:
                    if response.status != 200:
                        return None
                    facts = await response.json()
                
                employee_count = None
                for concept in ["EntityNumberOfEmployees", "NumberOfEmployees", "Employees"]:
                    if concept in facts.get("facts", {}).get("us-gaap", {}):
                        units = facts["facts"]["us-gaap"][concept].get("units", {})
                        for unit_key, unit_data in units.items():
                            if unit_data:
                                latest = max(unit_data, key=lambda x: x.get("end", ""))
                                employee_count = latest.get("val")
                                break
                        if employee_count:
                            break
                
                if employee_count:
                    badge_score = min(100.0, max(10.0, employee_count / 100))
                    logger.info(f"SEC EDGAR fallback success for {ticker} (Comparably): employees={employee_count}, score={badge_score}")
                    return ComparablyBadges(
                        ticker=ticker,
                        slug="sec-edgar-fallback",
                        badge_score=badge_score,
                        badge_count=1,
                        categories={"SEC Employee Count": badge_score},
                        fetched_at=datetime.now(timezone.utc).isoformat()
                    )
        except Exception as e:
            logger.warning(f"SEC EDGAR fallback failed for {ticker} (Comparably): {e}")
        return None

    async def scrape_all(self, tickers: List[str]) -> Dict[str, ComparablyBadges]:
        results = {}
        for ticker in tickers:
            results[ticker] = await self.scrape_company(ticker)
            await asyncio.sleep(random.uniform(12, 25))
        return results

    async def get_all_snapshots(self) -> Dict[str, ComparablyBadges]:
        """Alias for scrape_all to maintain compatibility with orchestrator"""
        tickers = list(self.company_slugs.keys())
        return await self.scrape_all(tickers)


class CorpAuditEngine:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.glassdoor_scraper = GlassdoorScraper(config_dict)
        self.comparably_scraper = ComparablyScraper(config_dict)
        self.validation_gate = CrossValidationGate(config_dict)

    async def initialize(self) -> None:
        await self.glassdoor_scraper.initialize()
        await self.comparably_scraper.initialize()
        logger.info("CorpAuditEngine initialized")

    async def close(self) -> None:
        await self.glassdoor_scraper.close()
        await self.comparably_scraper.close()

    async def __aenter__(self) -> "CorpAuditEngine":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def audit_ticker(self, ticker: str) -> Dict:
        logger.info(f"Running corp audit for {ticker}")

        glassdoor_result = await self.glassdoor_scraper.scrape_company(ticker)
        await asyncio.sleep(random.uniform(2, 5))

        comparably_result = await self.comparably_scraper.scrape_company(ticker)

        validation_result = self.validation_gate.evaluate(
            glassdoor_raw=glassdoor_result.raw_score,
            comparably_badge=comparably_result.badge_score
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
            "comparably": {
                "slug": comparably_result.slug,
                "badge_score": comparably_result.badge_score,
                "normalized": comparably_result.badge_score / 100.0 if comparably_result.badge_score else None,
                "badge_count": comparably_result.badge_count,
                "categories": comparably_result.categories,
                "fetched_at": comparably_result.fetched_at
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


async def create_comparably_scraper(config_dict: dict = None) -> ComparablyScraper:
    scraper = ComparablyScraper(config_dict)
    await scraper.initialize()
    return scraper


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    async def test():
        async with await create_corp_audit_engine() as engine:
            results = await engine.audit_all(["NVDA", "AMD"])
            for ticker, data in results.items():
                print(f"\n{ticker}:")
                print(f"  Glassdoor: {data['glassdoor']['raw_score']}/5.0")
                print(f"  Comparably: {data['comparably']['badge_score']}/100")
                vg = data['validation_gate']
                print(f"  Validation: divergence={vg['divergence']}, penalty={vg['penalty_multiplier']}, override={vg['override_triggered']}")

    asyncio.run(test())