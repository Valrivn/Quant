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
from curl_cffi import AsyncSession
from config import load_hybrid_config
from psychological.scrapers.nodriver_scraper import NodriverSession, NodriverConfig, scrape_with_nodriver
from psychological.scrapers.cdp_stealth import (
    build_cdp_evasion_script, build_cdp_cmds, detect_cloudflare,
    random_viewport, random_user_agent, CLOUDFLARE_SIGNALS,
)

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

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> "CorpAnonymousScraper":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def scrape_company(self, ticker: str):
        from types import SimpleNamespace
        slug_map = self._get_indeed_slugs()
        slug = slug_map.get(ticker, ticker)
        company = self.company_mappings.get(ticker, ticker)

        # Tier 1: Google Jobs JSON-LD for Indeed employer ratings (zero Cloudflare exposure)
        rev_data = await self._fetch_indeed_rating_from_google(company)
        if rev_data is None:
            rev_data = await self._fetch_indeed_google_jsonld(company)

        # Tier 2: Nodriver CDP stealth session (if JSON-LD failed)
        if rev_data is None:
            rev_data = await self._fetch_indeed_reviews_nodriver(ticker, slug)

        job_count = await self.get_job_count(ticker)
        if rev_data:
            return SimpleNamespace(
                overall_rating=rev_data.get("overall_rating"),
                job_count=job_count,
                ceo_approval=rev_data.get("ceo_approval"),
                work_wellbeing_score=None,
                ceo_name=None,
                review_count=rev_data.get("review_count"),
                raw_score=rev_data.get("overall_rating"),
            )
        return SimpleNamespace(
            overall_rating=None,
            job_count=job_count,
            ceo_approval=None,
            work_wellbeing_score=None,
            ceo_name=None,
            review_count=None,
            raw_score=None,
        )
        
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
        
    def _get_indeed_slugs(self) -> Dict[str, str]:
        return {
            "NVDA": "Nvidia",
            "AVGO": "Broadcom",
            "INTC": "Intel",
            "AMD": "AMD",
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
        
    async def _fetch_indeed_rating_from_google(self, company: str) -> Optional[Dict]:
        """Fetch Indeed company rating from Google Jobs JSON-LD.
        Queries google.com for Indeed employer page, parses AggregateRating
        from <script type='application/ld+json'> blocks.
        Zero Cloudflare exposure — uses curl_cffi with rotating UA + Referer.
        """
        result = {"overall_rating": None, "review_count": None, "ceo_approval": None, "ceo_name": None}
        try:
            query = f"{company} employer rating indeed.com"
            url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
            ua = random_user_agent()
            headers = {
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.google.com/",
            }
            async with AsyncSession(timeout=30) as sess:
                resp = await sess.get(url, headers=headers)
                if resp.status_code != 200:
                    return None

                soup = BeautifulSoup(resp.text, "html.parser")

                # Parse all JSON-LD blocks for AggregateRating
                for script in soup.select("script[type='application/ld+json']"):
                    try:
                        data = json.loads(script.string)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    items = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        agg = item.get("aggregateRating") or {}
                        if isinstance(agg, dict) and agg.get("ratingValue"):
                            try:
                                val = float(agg["ratingValue"])
                                if 0 <= val <= 5:
                                    result["overall_rating"] = val
                                elif val <= 100:
                                    result["overall_rating"] = val / 20.0
                            except (ValueError, TypeError):
                                pass
                            if agg.get("ratingCount"):
                                try:
                                    result["review_count"] = int(float(agg["ratingCount"]))
                                except (ValueError, TypeError):
                                    pass
                # Also scrape snippet text for rating patterns
                if result["overall_rating"] is None:
                    for div in soup.select("div[data-snc], span.aCOpRe, div[role='heading']"):
                        text = div.get_text(separator=" ", strip=True)
                        m = re.search(r'(\d\.\d)\s*(?:out of|/)\s*5', text, re.IGNORECASE)
                        if m:
                            result["overall_rating"] = float(m.group(1))

                if result["overall_rating"] is not None:
                    logger.info("Google JSON-LD rating success for %s: rating=%s, reviews=%s",
                                company, result["overall_rating"], result["review_count"])

        except Exception as e:
            logger.warning("Google JSON-LD rating fetch failed for %s: %s", company, e)

        if result["overall_rating"] is not None:
            return result
        return None

    async def _fetch_indeed_google_jsonld(self, company: str) -> Optional[Dict]:
        """Indeed rating fallback: Google Jobs JSON-LD.
        Queries google.com for "INDIVIDUAL EMPLOYER company reviews" and extracts
        rating from <script type='application/ld+json'> AggregateRating schema.
        Uses curl_cffi with rotating User-Agent (not impersonate) and Referer:
        https://www.google.com to minimize detection.
        """
        import asyncio
        import json

        result = {"overall_rating": None, "review_count": None, "ceo_approval": None, "ceo_name": None}
        try:
            query = f"{company} employer reviews indeed.com"
            url = f"https://www.google.com/search?q={query.replace(' ', '+')}"

            async with AsyncSession(impersonate="chrome124", timeout=30) as sess:
                resp = await sess.get(url, headers={"Referer": "https://www.google.com/"})
                if resp.status_code != 200:
                    return None

                soup = BeautifulSoup(resp.text, "html.parser")

                # Parse all JSON-LD blocks for relevant schemas
                for script in soup.select("script[type='application/ld+json']"):
                    try:
                        data = json.loads(script.string)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    items = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
                    for item in items:
                        if not isinstance(item, dict):
                            continue

                        # Look for JobPosting or Organization with aggregateRating
                        if item.get("@type") not in ("JobPosting", "Organization", "AggregateRating"):
                            continue

                        agg = item.get("aggregateRating") or {}
                        if not isinstance(agg, dict):
                            continue

                        if agg.get("ratingValue"):
                            try:
                                val = float(agg["ratingValue"])
                                # Handle both 0-5 and 0-100 scales
                                if 0 <= val <= 5:
                                    result["overall_rating"] = val
                                elif val <= 100:
                                    result["overall_rating"] = val / 20.0
                            except (ValueError, TypeError):
                                pass

                        if agg.get("ratingCount"):
                            try:
                                result["review_count"] = int(float(agg["ratingCount"]))
                            except (ValueError, TypeError):
                                pass

                        # Some schemas include reviewBody with review count or employee satisfaction
                        if "reviewCount" in item:
                            try:
                                result["review_count"] = int(float(item["reviewCount"]))
                            except (ValueError, TypeError):
                                pass

                        # Extract CEO approval if present in review text
                        if "reviewBody" in item and item["reviewBody"]:
                            review_text = item["reviewBody"]
                            ceo_match = re.search(r'(\d{1,3})%\s*(?:CEO|approve)', review_text, re.IGNORECASE)
                            if ceo_match:
                                result["ceo_approval"] = int(ceo_match.group(1))
                            ceo_name_match = re.search(r'CEO\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)', review_text)
                            if ceo_name_match:
                                result["ceo_name"] = ceo_name_match.group(1)

                if result["overall_rating"] is not None:
                    logger.info(
                        "Google JSON-LD Indeed fallback success for %s: rating=%s, reviews=%s, ceo_approval=%s, ceo=%s",
                        company, result["overall_rating"], result.get("review_count"),
                        result.get("ceo_approval"), result.get("ceo_name")
                    )

        except Exception as e:
            logger.warning("Google JSON-LD Indeed fallback failed for %s: %s", company, e)

        if result["overall_rating"] is not None:
            return result
        return None

    async def _fetch_indeed_reviews_nodriver(self, ticker: str, slug: str) -> Optional[Dict]:
        """Scrape Indeed company review page using nodriver with CDP stealth emulation.
        Warms up session on Indeed homepage first to resolve Cloudflare Turnstile,
        then navigates to company page on the same tab. Extracts overall_rating,
        ceo_approval, review_count.
        """
        try:
            import nodriver as uc
        except ImportError:
            logger.warning("nodriver not available, skipping Indeed review scrape")
            return None

        logger.info("Fetching Indeed reviews for %s (%s) via nodriver CDP stealth with homepage warmup", ticker, slug)

        for attempt in range(3):
            vp = random_viewport()
            ua = random_user_agent()
            browser = None
            session = None
            try:
                pool = None
                try:
                    from psychological.scrapers.corp_audit import _get_nodriver_pool
                    pool = _get_nodriver_pool()
                    session = await pool.acquire()

                    tab = await session._browser.get("https://www.indeed.com")
                    cdp_cmds = build_cdp_cmds(vp)
                    for cmd in cdp_cmds:
                        try:
                            await tab.send(cmd["cmd"], cmd["params"])
                        except Exception:
                            pass
                        await asyncio.sleep(random.uniform(2, 4))
                    script = build_cdp_evasion_script(vp, ua)
                    try:
                        await tab.evaluate(script)
                    except Exception:
                        pass

                    await asyncio.sleep(random.uniform(6, 10))

                    html_check = await tab.get_content()
                    if html_check and detect_cloudflare(html_check):
                        logger.warning("Indeed warmup attempt %d: Cloudflare present on homepage for %s", attempt + 1, ticker)
                        try:
                            await pool.release(session)
                        except Exception:
                            pass
                        session = None
                        await asyncio.sleep(random.uniform(12, 25))
                        continue

                    logger.info("Indeed homepage warmup clear for %s, navigating to company page", ticker)

                    vp2 = random_viewport()
                    ua2 = random_user_agent()
                    cdp_cmds2 = build_cdp_cmds(vp2)
                    for cmd2 in cdp_cmds2:
                        try:
                            await tab.send(cmd2["cmd"], cmd2["params"])
                        except Exception:
                            pass
                        await asyncio.sleep(random.uniform(2, 4))
                    script2 = build_cdp_evasion_script(vp2, ua2)
                    try:
                        await tab.evaluate(script2)
                    except Exception:
                        pass

                    await tab.get(f"https://www.indeed.com/cmp/{slug}")
                    await asyncio.sleep(random.uniform(10, 20))
                    try:
                        await tab.wait_for(
                            ".cmp-Rating-count, [data-testid='rating-value'], .css-1kg2p8c",
                            timeout=15,
                        )
                    except Exception:
                        pass
                    html = await tab.get_content()
                    try:
                        await pool.release(session)
                    except Exception:
                        pass
                    session = None
                except Exception:
                    if session and pool:
                        try:
                            await pool.release(session)
                        except Exception:
                            pass
                        session = None
                    extra_args = [
                        "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-features=IsolateOrigins,site-per-process",
                        "--disable-session-crashed-bubble", "--disable-infobars",
                        "--disable-background-timer-throttling",
                        "--disable-renderer-backgrounding",
                        "--disable-background-networking",
                        "--disable-component-update", "--disable-default-apps", "--disable-sync",
                    ]
                    browser = await uc.start(
                        headless=True, browser_args=extra_args, user_agent=ua, sandbox=False,
                    )
                    tab = await browser.get("https://www.indeed.com")
                    cdp_cmds = build_cdp_cmds(vp)
                    for cmd in cdp_cmds:
                        try:
                            await tab.send(cmd["cmd"], cmd["params"])
                        except Exception:
                            pass
                        await asyncio.sleep(random.uniform(2, 4))
                    script = build_cdp_evasion_script(vp, ua)
                    try:
                        await tab.evaluate(script)
                    except Exception:
                        pass

                    await asyncio.sleep(random.uniform(6, 10))

                    html_check = await tab.get_content()
                    if html_check and detect_cloudflare(html_check):
                        logger.warning("Indeed warmup attempt %d (uc): Cloudflare present on homepage for %s", attempt + 1, ticker)
                        if browser:
                            try:
                                await browser.stop()
                            except Exception:
                                pass
                            browser = None
                        await asyncio.sleep(random.uniform(12, 25))
                        continue

                    vp2 = random_viewport()
                    ua2 = random_user_agent()
                    cdp_cmds2 = build_cdp_cmds(vp2)
                    for cmd2 in cdp_cmds2:
                        try:
                            await tab.send(cmd2["cmd"], cmd2["params"])
                        except Exception:
                            pass
                        await asyncio.sleep(random.uniform(2, 4))
                    script2 = build_cdp_evasion_script(vp2, ua2)
                    try:
                        await tab.evaluate(script2)
                    except Exception:
                        pass

                    await tab.get(f"https://www.indeed.com/cmp/{slug}")
                    await asyncio.sleep(random.uniform(10, 20))
                    try:
                        await tab.wait_for(
                            ".cmp-Rating-count, [data-testid='rating-value'], .css-1kg2p8c",
                            timeout=15,
                        )
                    except Exception:
                        pass
                    html = await tab.get_content()

                if html and len(html) > 500 and not detect_cloudflare(html):
                    extracted = self._extract_indeed_review_data(html)
                    if extracted.get("overall_rating") is not None:
                        logger.info(
                            "Indeed reviews success for %s: rating=%s, reviews=%s, ceo=%s",
                            ticker, extracted["overall_rating"],
                            extracted.get("review_count"), extracted.get("ceo_approval"),
                        )
                        if browser:
                            try:
                                await browser.stop()
                            except Exception:
                                pass
                        return extracted
                    logger.warning(
                        "Indeed reviews nodriver attempt %d: no rating extracted for %s",
                        attempt + 1, ticker,
                    )
                else:
                    logger.warning(
                        "Indeed reviews nodriver attempt %d: Cloudflare or short HTML for %s (%d bytes)",
                        attempt + 1, ticker, len(html) if html else 0,
                    )
            except Exception as e:
                logger.warning(
                    "Indeed reviews nodriver attempt %d failed for %s: %s",
                    attempt + 1, ticker, e,
                )
            finally:
                if browser:
                    try:
                        await browser.stop()
                    except Exception:
                        pass
            await asyncio.sleep(random.uniform(12, 25))

        logger.warning("Indeed reviews nodriver exhausted retries for %s", ticker)
        return None

    def _extract_indeed_review_data(self, html: str) -> Dict:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator=" ", strip=True)
        result = {
            "overall_rating": None,
            "review_count": None,
            "ceo_approval": None,
            "ceo_name": None,
        }
        rating_match = (
            re.search(r'(\d\.\d)\s*★', text) or
            re.search(r'(\d\.\d)\s*/\s*5', text) or
            re.search(r'(\d\.\d)\s*out of 5', text, re.IGNORECASE)
        )
        if rating_match:
            result["overall_rating"] = float(rating_match.group(1))
        ceo_match = re.search(r'(\d{1,3})%\s*(?:CEO|approve)', text, re.IGNORECASE)
        if ceo_match:
            result["ceo_approval"] = int(ceo_match.group(1))
        rev_match = re.search(r'([\d,]+)\s*Reviews', text, re.IGNORECASE)
        if rev_match:
            result["review_count"] = int(rev_match.group(1).replace(",", ""))
        ceo_name_match = re.search(r'CEO\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)', text)
        if ceo_name_match:
            result["ceo_name"] = ceo_name_match.group(1)
        logger.info(
            "Extracted Indeed review data: rating=%s, reviews=%s, ceo=%s",
            result["overall_rating"], result["review_count"], result["ceo_approval"],
        )
        return result

    async def get_job_count(self, ticker: str) -> Optional[int]:
        cache_key = self._get_cache_key(ticker)
        if cache_key in self._cache:
            cached_time, cached_count = self._cache[cache_key]
            if time.time() - cached_time < self.cache_duration:
                return cached_count
                
        company = self.company_mappings.get(ticker, ticker)
        
        async with aiohttp.ClientSession() as session:
            # Tier 1: Adzuna API (zero-friction, no bot detection)
            adzuna_data = await self._fetch_adzuna(session, company)
            if adzuna_data:
                count = adzuna_data.get("count", 0)
                self._cache[cache_key] = (time.time(), count)
                return count

            # Tier 2: JobSpy (dedicated TLS/UA evasion for Indeed — no Cloudflare)
            fallback_count = await self._fetch_jobspy_fallback(session, company)
            if fallback_count is not None:
                self._cache[cache_key] = (time.time(), fallback_count)
                return fallback_count

            # Tier 3: Adzuna Web UI parser
            fallback_count = await self._fetch_adzuna_web_ui_fallback(session, company)
            if fallback_count is not None:
                self._cache[cache_key] = (time.time(), fallback_count)
                return fallback_count

            # Tier 4: Nodriver CDP stealth (Cloudflare bypass with browser automation)
            fallback_count = await self._fetch_nodriver_fallback(company)
            if fallback_count is not None:
                self._cache[cache_key] = (time.time(), fallback_count)
                return fallback_count

            # Tier 5: General career index fallback
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

IndeedScraper = CorpAnonymousScraper


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