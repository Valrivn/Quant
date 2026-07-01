import asyncio
import logging
import random
import re
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass
from config import load_hybrid_config
from psychological.scrapers.nodriver_scraper import (
    NodriverSession, NodriverConfig, create_nodriver_session, scrape_with_nodriver
)

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
    source: str = "nodriver"


@dataclass
class ComparablyBadges:
    ticker: str
    slug: str
    overall_score: Optional[float]
    culture_score: Optional[float]
    ceo_score: Optional[float]
    diversity_score: Optional[float]
    compensation_score: Optional[float]
    work_life_balance_score: Optional[float]
    badge_score: Optional[int]  # 0-100 scale
    fetched_at: str
    source: str = "nodriver"


class GlassdoorNodriverScraper:
    BASE_URL = "https://www.glassdoor.com"

    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.glassdoor_config = self.config.get("glassdoor", {})
        self.company_slugs = self.glassdoor_config.get("company_slugs", {})
        self.company_names = {
            "NVDA": "NVIDIA",
            "AMD": "Advanced Micro Devices",
            "MSFT": "Microsoft",
            "GOOGL": "Google",
            "META": "Meta",
            "TSLA": "Tesla",
            "AAPL": "Apple",
            "AMZN": "Amazon",
            "AVGO": "Broadcom",
            "INTC": "Intel Corporation",
        }
        self.nodriver_config = NodriverConfig(
            headless=self.config.get("nodriver", {}).get("headless", True),
            min_delay=12.0,
            max_delay=25.0,
        )

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
            logger.warning("No Glassdoor slug for %s", ticker)
            return GlassdoorScore(ticker, "", None, None, None, None, None, 
                                datetime.now(timezone.utc).isoformat(), "nodriver")

        company_name = self.company_names.get(ticker, ticker)
        logger.info("Scraping Glassdoor for %s (%s) via nodriver", ticker, company_name)

        raw_score = None
        review_count = None
        ceo_approval = None
        recommend_to_friend = None

        try:
            url = f"{self.BASE_URL}/Reviews/{slug}-Reviews-SRCH_KE0,{len(slug)}.htm"
            
            async def extract(session: NodriverSession):
                nonlocal raw_score, review_count, ceo_approval, recommend_to_friend
                
                await asyncio.sleep(random.uniform(3, 5))
                
                # Wait for ratings to load
                await session._tab.wait_for("div[data-test='overallRating'], span[data-test='overall-rating'], .ratingNumber", timeout=10)
                await asyncio.sleep(2)
                
                # Try multiple selectors for overall rating
                overall_rating_elem = await session.find_element("div[data-test='overallRating']") or \
                                     await session.find_element("span[data-test='overall-rating']") or \
                                     await session.find_element(".ratingNumber") or \
                                     await session.find_element("[class*='rating'] [class*='number']") or \
                                     await session.find_element(".bigRating strong") or \
                                     await session.find_element(".ratingNum")
                
                if overall_rating_elem:
                    text = await overall_rating_elem.text_all
                    score = self._parse_score(text)
                    if score:
                        raw_score = score
                        logger.info("Found Glassdoor overall rating for %s: %s", ticker, raw_score)
                
                # Get page content for regex parsing
                html = await session.get_content()
                
                if raw_score is None:
                    score_match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', html, re.IGNORECASE)
                    if score_match:
                        raw_score = float(score_match.group(1))
                
                review_match = re.search(r'([\d,]+)\s*(?:review|Reviews)', html, re.IGNORECASE)
                if review_match:
                    review_count = int(review_match.group(1).replace(",", ""))
                
                ceo_match = re.search(r'(\d+)%\s*(?:approve|CEO)', html, re.IGNORECASE)
                if ceo_match:
                    ceo_approval = int(ceo_match.group(1))
                
                recommend_match = re.search(r'(\d+)%\s*(?:recommend|would recommend)', html, re.IGNORECASE)
                if recommend_match:
                    recommend_to_friend = int(recommend_match.group(1))
                
                return True

            await scrape_with_nodriver(url, wait_for="div[data-test='overallRating'], span[data-test='overall-rating'], .ratingNumber", 
                                     config=self.nodriver_config, extract_fn=extract)
            
            if raw_score:
                logger.info("Nodriver Glassdoor scrape success for %s: score=%s", ticker, raw_score)
            else:
                logger.warning("Nodriver Glassdoor scrape failed for %s: no rating found", ticker)

        except Exception as e:
            logger.warning("Nodriver Glassdoor scrape failed for %s: %s", ticker, e)

        normalized = raw_score / 5.0 if raw_score else None

        return GlassdoorScore(
            ticker=ticker,
            slug=slug,
            raw_score=raw_score,
            normalized_score=normalized,
            review_count=review_count,
            ceo_approval=ceo_approval,
            recommend_to_friend=recommend_to_friend,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            source="nodriver"
        )

    async def scrape_all(self, tickers: List[str]) -> Dict[str, GlassdoorScore]:
        results = {}
        for ticker in tickers:
            results[ticker] = await self.scrape_company(ticker)
            await asyncio.sleep(random.uniform(12, 25))
        return results


class ComparablyNodriverScraper:
    BASE_URL = "https://www.comparably.com"

    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.comparably_config = self.config.get("comparably", {})
        self.company_slugs = self.comparably_config.get("company_slugs", {})
        self.nodriver_config = NodriverConfig(
            headless=self.config.get("nodriver", {}).get("headless", True),
            min_delay=12.0,
            max_delay=25.0,
        )

    async def scrape_company(self, ticker: str) -> ComparablyBadges:
        slug = self.company_slugs.get(ticker)
        if not slug:
            logger.warning("No Comparably slug for %s", ticker)
            return ComparablyBadges(ticker, "", None, None, None, None, None, None, 
                                    datetime.now(timezone.utc).isoformat(), "nodriver")

        logger.info("Scraping Comparably for %s (%s) via nodriver", ticker, slug)

        overall_score = None
        culture_score = None
        ceo_score = None
        diversity_score = None
        compensation_score = None
        work_life_balance_score = None
        badge_score = None

        try:
            url = f"{self.BASE_URL}/companies/{slug}/badges"
            
            async def extract(session: NodriverSession):
                nonlocal overall_score, culture_score, ceo_score, diversity_score, compensation_score, work_life_balance_score, badge_score
                
                await asyncio.sleep(random.uniform(3, 5))
                
                # Wait for badges to load
                await session._tab.wait_for("[data-testid='badge-score'], .badge-score, .culture-score, .ceo-score", timeout=15)
                await asyncio.sleep(2)
                
                html = await session.get_content()
                
                # Parse overall score
                overall_elem = await session.find_element("[data-testid='overall-score']") or \
                              await session.find_element(".overall-score") or \
                              await session.find_element("[class*='overall'] [class*='score']")
                if overall_elem:
                    text = await overall_elem.text_all
                    match = re.search(r'(\d+\.?\d*)', text)
                    if match:
                        overall_score = float(match.group(1))
                
                # Parse category scores
                categories = {
                    "culture": culture_score,
                    "ceo": ceo_score,
                    "diversity": diversity_score,
                    "compensation": compensation_score,
                    "work_life_balance": work_life_balance_score,
                }
                
                for cat_name in categories.keys():
                    cat_elem = await session.find_element(f"[data-testid='{cat_name}-score']") or \
                               await session.find_element(f".{cat_name}-score") or \
                               await session.find_element(f"[class*='{cat_name}'] [class*='score']")
                    if cat_elem:
                        text = await cat_elem.text_all
                        match = re.search(r'(\d+\.?\d*)', text)
                        if match:
                            val = float(match.group(1))
                            if cat_name == "culture":
                                culture_score = val
                            elif cat_name == "ceo":
                                ceo_score = val
                            elif cat_name == "diversity":
                                diversity_score = val
                            elif cat_name == "compensation":
                                compensation_score = val
                            elif cat_name == "work_life_balance":
                                work_life_balance_score = val
                
                # Parse badge score (0-100)
                badge_elem = await session.find_element("[data-testid='badge-score']") or \
                            await session.find_element(".badge-score")
                if badge_elem:
                    text = await badge_elem.text_all
                    match = re.search(r'(\d+)', text)
                    if match:
                        badge_score = int(match.group(1))
                
                return True

            await scrape_with_nodriver(url, wait_for="[data-testid='badge-score'], .badge-score, .culture-score, .ceo-score", 
                                     config=self.nodriver_config, extract_fn=extract)
            
            if overall_score or badge_score:
                logger.info("Nodriver Comparably scrape success for %s: overall=%s, badge=%s", ticker, overall_score, badge_score)
            else:
                logger.warning("Nodriver Comparably scrape failed for %s: no scores found", ticker)

        except Exception as e:
            logger.warning("Nodriver Comparably scrape failed for %s: %s", ticker, e)

        return ComparablyBadges(
            ticker=ticker,
            slug=slug,
            overall_score=overall_score,
            culture_score=culture_score,
            ceo_score=ceo_score,
            diversity_score=diversity_score,
            compensation_score=compensation_score,
            work_life_balance_score=work_life_balance_score,
            badge_score=badge_score,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            source="nodriver"
        )

    async def scrape_all(self, tickers: List[str]) -> Dict[str, ComparablyBadges]:
        results = {}
        for ticker in tickers:
            results[ticker] = await self.scrape_company(ticker)
            await asyncio.sleep(random.uniform(12, 25))
        return results


async def create_glassdoor_nodriver_scraper(config_dict: dict = None) -> GlassdoorNodriverScraper:
    return GlassdoorNodriverScraper(config_dict)


async def create_comparably_nodriver_scraper(config_dict: dict = None) -> ComparablyNodriverScraper:
    return ComparablyNodriverScraper(config_dict)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    async def test():
        async with await create_glassdoor_nodriver_scraper() as scraper:
            result = await scraper.scrape_company("NVDA")
            print(f"Glassdoor NVDA: {result}")
        
        async with await create_comparably_nodriver_scraper() as scraper:
            result = await scraper.scrape_company("NVDA")
            print(f"Comparably NVDA: {result}")
    
    asyncio.run(test())