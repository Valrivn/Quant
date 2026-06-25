import asyncio
import logging
import os
import time
import re
import random
from typing import Dict, List, Optional
from datetime import datetime, timezone, timedelta
import aiohttp
from bs4 import BeautifulSoup
from config import load_hybrid_config
from psychological.scrapers.nodriver_scraper import NodriverSession, NodriverConfig, scrape_with_nodriver

logger = logging.getLogger(__name__)


class CorpAnonymousScraper:
    def __init__(self, config_dict: dict = None):
        hybrid_config = load_hybrid_config()
        self.config = config_dict or hybrid_config.get("psychological", {})
        self.adzuna_config = hybrid_config.get("adzuna", {})
        self.app_id = os.getenv("ADZUNA_APP_ID") or self.adzuna_config.get("app_id")
        self.app_key = os.getenv("ADZUNA_APP_KEY") or self.adzuna_config.get("app_key")
        self.base_url = self.adzuna_config.get("base_url", "https://api.adzuna.com/v1/api/jobs")
        self.country = self.adzuna_config.get("country", "us")
        self.company_mappings = self._get_company_mappings()
        self.cache_duration = 86400
        self._cache = {}
        
    def _get_company_mappings(self) -> Dict[str, str]:
        return {
            "NVDA": "NVIDIA",
            "AMD": "Advanced Micro Devices",
            "MSFT": "Microsoft",
            "GOOGL": "Google",
            "META": "Meta",
            "TSLA": "Tesla",
            "AAPL": "Apple",
            "AMZN": "Amazon",
        }
        
    def _get_cache_key(self, ticker: str) -> str:
        return f"adzuna_{ticker}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        
    async def _fetch_adzuna(self, session: aiohttp.ClientSession, company: str) -> Optional[Dict]:
        if not self.app_id or not self.app_key:
            logger.warning("Adzuna credentials not configured, using fallback")
            return None
            
        url = f"{self.base_url}/{self.country}/search/1"
        params = {
            "app_id": self.app_id,
            "app_key": self.app_key,
            "what": company,
            "results_per_page": 1,
            "content-type": "application/json"
        }
        
        try:
            async with session.get(url, params=params) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 429:
                    logger.warning("Adzuna rate limit hit")
                    return None
                else:
                    logger.warning(f"Adzuna API error {response.status}")
                    return None
        except Exception as e:
            logger.error(f"Error fetching Adzuna for {company}: {e}")
            return None
            
    async def _fetch_career_index_fallback(self, session: aiohttp.ClientSession, company: str) -> Optional[int]:
        try:
            url = f"https://www.indeed.com/jobs?q={company.replace(' ', '+')}"
            headers = {"User-Agent": "Mozilla/5.0 (compatible; QuantBot/1.0)"}
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    text = await response.text()
                    import re
                    match = re.search(r'(\d[\d,]*)\s*jobs', text, re.IGNORECASE)
                    if match:
                        return int(match.group(1).replace(',', ''))
        except Exception as e:
            logger.error(f"Career index fallback failed for {company}: {e}")
        return None

    async def _fetch_adzuna_web_ui_fallback(self, session: aiohttp.ClientSession, company: str) -> Optional[int]:
        """Fallback 1: Adzuna Public Web UI Search Parser"""
        try:
            logger.info(f"Attempting Adzuna Web UI fallback for {company}")
            url = f"https://www.adzuna.com/search?q={company.replace(' ', '+')}"
            headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    return None
                html = await response.text()
            
            soup = BeautifulSoup(html, "html.parser")
            
            for elem in soup.select("div.ui-search-results, div.search-results, div.job-count"):
                text = elem.get_text()
                match = re.search(r'(\d[\d,]*)\s*(?:jobs?|results?)', text, re.IGNORECASE)
                if match:
                    count = int(match.group(1).replace(',', ''))
                    logger.info(f"Adzuna Web UI fallback success for {company}: {count} jobs")
                    return count
            
            for elem in soup.find_all(text=re.compile(r'\d+.*jobs?', re.I)):
                match = re.search(r'(\d[\d,]*)\s*jobs?', elem, re.IGNORECASE)
                if match:
                    count = int(match.group(1).replace(',', ''))
                    logger.info(f"Adzuna Web UI fallback success for {company}: {count} jobs")
                    return count
                    
        except Exception as e:
            logger.warning(f"Adzuna Web UI fallback failed for {company}: {e}")
        return None

    async def _fetch_jobspy_fallback(self, session: aiohttp.ClientSession, company: str) -> Optional[int]:
        """Fallback 2: Python-JobSpy keyless providers (Indeed, LinkedIn public search)"""
        try:
            logger.info(f"Attempting JobSpy fallback for {company}")
            
            try:
                from jobspy import scrape_jobs
            except ImportError:
                logger.warning("JobSpy not installed, skipping JobSpy fallback")
                return None
            
            jobs = scrape_jobs(
                site_name=["indeed", "linkedin"],
                search_term=company,
                results_wanted=10,
                hours_old=720,
                country_indeed="USA",
            )
            
            if jobs is not None:
                count = len(jobs)
                logger.info(f"JobSpy fallback success for {company}: {count} jobs")
                return count
                
        except Exception as e:
            logger.warning(f"JobSpy fallback failed for {company}: {e}")
        return None

    async def _fetch_nodriver_fallback(self, company: str) -> Optional[int]:
        """Fallback 0: Nodriver CDP-based scraping for Indeed/LinkedIn job counts"""
        try:
            logger.info(f"Attempting Nodriver fallback for {company}")
            
            sources = [
                ("indeed", f"https://www.indeed.com/jobs?q={company.replace(' ', '+')}"),
                ("linkedin", f"https://www.linkedin.com/jobs/search/?keywords={company.replace(' ', '%20')}"),
            ]
            
            for source_name, url in sources:
                try:
                    config = NodriverConfig(headless=True)
                    
                    async def extract_count(session: NodriverSession):
                        await asyncio.sleep(random.uniform(3, 5))
                        await session.scroll_down(1500)
                        await asyncio.sleep(2)
                        
                        if source_name == "indeed":
                            count_text = await session.evaluate("""
                                () => {
                                    const elem = document.querySelector('div.jobsearch-JobCountAndSortPane-jobCount span, div[data-testid="job-count"]');
                                    return elem ? elem.textContent : '';
                                }
                            """)
                            # Handle nodriver returning tuple (success, result)
                            if isinstance(count_text, tuple):
                                count_text = count_text[1] if len(count_text) > 1 else count_text[0]
                            if count_text:
                                match = re.search(r'([\d,]+)', str(count_text).replace(',', ''))
                                if match:
                                    return int(match.group(1).replace(',', ''))
                        else:
                            elements = await session.find_elements("div.job-card-container, li.job-card")
                            return len(elements)
                        return 0
                    
                    count = await scrape_with_nodriver(url, config=config, extract_fn=extract_count)
                    if count and count > 0:
                        logger.info(f"Nodriver {source_name} fallback success for {company}: {count} jobs")
                        return count
                        
                except Exception as e:
                    logger.warning(f"Nodriver {source_name} fallback failed for {company}: {e}")
                    continue
                    
        except Exception as e:
            logger.warning(f"Nodriver fallback failed for {company}: {e}")
        return None
        
    async def get_job_count(self, ticker: str) -> Optional[int]:
        cache_key = self._get_cache_key(ticker)
        if cache_key in self._cache:
            cached_time, cached_count = self._cache[cache_key]
            if time.time() - cached_time < self.cache_duration:
                return cached_count
                
        company = self.company_mappings.get(ticker, ticker)
        
        async with aiohttp.ClientSession() as session:
            adzuna_data = await self._fetch_adzuna(session, company)
            if adzuna_data:
                count = adzuna_data.get("count", 0)
                self._cache[cache_key] = (time.time(), count)
                return count
                
            fallback_count = await self._fetch_nodriver_fallback(company)
            if fallback_count is not None:
                self._cache[cache_key] = (time.time(), fallback_count)
                return fallback_count
                
            fallback_count = await self._fetch_adzuna_web_ui_fallback(session, company)
            if fallback_count is not None:
                self._cache[cache_key] = (time.time(), fallback_count)
                return fallback_count
                
            fallback_count = await self._fetch_jobspy_fallback(session, company)
            if fallback_count is not None:
                self._cache[cache_key] = (time.time(), fallback_count)
                return fallback_count
                
            fallback_count = await self._fetch_career_index_fallback(session, company)
            if fallback_count is not None:
                self._cache[cache_key] = (time.time(), fallback_count)
                return fallback_count
                
        logger.warning(f"All job count sources failed for {ticker}")
        return None
        
    async def get_historical_snapshots(self, ticker: str) -> Dict:
        current_count = await self.get_job_count(ticker)
        if current_count is None:
            return {}
            
        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        
        return {
            "ticker": ticker,
            "company_name": self.company_mappings.get(ticker, ticker),
            "date": date_str,
            "job_count": current_count,
            "job_count_7d_ago": None,
            "job_count_30d_ago": None,
            "delta_7d_pct": None,
            "delta_30d_pct": None,
            "fetched_at": now.isoformat()
        }
        
    async def get_all_snapshots(self) -> Dict[str, Dict]:
        results = {}
        for ticker in self.company_mappings:
            logger.info(f"Fetching job data for {ticker}")
            snapshot = await self.get_historical_snapshots(ticker)
            if snapshot:
                results[ticker] = snapshot
            await asyncio.sleep(0.5)
        return results
        
    def calculate_sentiment_proxy(self, current: Dict, previous_7d: Dict = None, previous_30d: Dict = None) -> float:
        if not current or current.get("job_count") is None:
            return 0.0
            
        current_count = current["job_count"]
        deltas = []
        
        if previous_7d and previous_7d.get("job_count"):
            delta_7d = (current_count - previous_7d["job_count"]) / previous_7d["job_count"]
            deltas.append(delta_7d)
            
        if previous_30d and previous_30d.get("job_count"):
            delta_30d = (current_count - previous_30d["job_count"]) / previous_30d["job_count"]
            deltas.append(delta_30d)
            
        if not deltas:
            return 0.0
            
        avg_delta = sum(deltas) / len(deltas)
        return max(-1.0, min(1.0, avg_delta * 5))


async def create_corp_anonymous_scraper(config_dict: dict = None) -> CorpAnonymousScraper:
    return CorpAnonymousScraper(config_dict)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    async def test():
        scraper = await create_corp_anonymous_scraper()
        snapshots = await scraper.get_all_snapshots()
        for ticker, data in snapshots.items():
            print(f"{ticker}: {data}")
            
    asyncio.run(test())