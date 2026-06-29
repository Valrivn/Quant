import asyncio
import logging
import random
import re
import os
import time
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass

from bs4 import BeautifulSoup
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from curl_cffi import AsyncSession
import yfinance as yf
import requests

import json
from psychological.scrapers.validation_gate import CrossValidationGate, ValidationGateResult
from psychological.scrapers.browserless_client import BrowserlessClient, create_browserless_client
from psychological.scrapers.nodriver_primary import (
    GlassdoorNodriverScraper, ComparablyNodriverScraper,
    create_glassdoor_nodriver_scraper, create_comparably_nodriver_scraper
)
from config import load_hybrid_config
from psychological.scrapers.proxy_manager import ProxyManager, RateLimiter, RateLimiterConfig, build_ja4_headers
from scraper.dynamic_extractor import DynamicExtractor
from psychological.scrapers.cdp_stealth import (
    build_cdp_evasion_script, build_cdp_cmds, detect_cloudflare,
    random_viewport, random_user_agent, CLOUDFLARE_SIGNALS,
)

logger = logging.getLogger(__name__)

try:
    from nodriver import Browser, Config
except ImportError:
    Browser = None
    Config = None


async def execute_stealth_ingestion(target_url: str, proxy_pool: list) -> str:
    """
    Implements Three-Tiered Defensive Bypass:
    Tier 1: Evaluates cached search proxy structures via config/cloudflare_strategy_memory.json
    Tier 2: Executes native apex domain warm-up navigation passes
    Tier 3: Sanitizes devtools automation parameters with variable delays [8.0, 18.0]
    """
    if Browser is None or Config is None:
        raise RuntimeError("nodriver package is not installed or available.")

    # Initialize native browser configuration targeting host OS layers directly
    config = Config()
    config.headless = False  # Enforce visible viewport orchestration to pass canvas validation
    config.add_argument(f"--proxy-server={random.choice(proxy_pool)}")
    
    browser = await Browser.start(config)
    page = await browser.get("https://www.glassdoor.com")
    
    # Tier 2: Warm up session on apex root screen
    await asyncio.sleep(random.uniform(3.0, 6.0))
    
    # Tier 3: Humanized random delay interval navigation to target route
    await page.get(target_url)
    await asyncio.sleep(random.uniform(8.0, 18.0))
    
    html_content = await page.get_content()
    await browser.close()
    
    if "Just a moment..." in html_content or "Cloudflare" in html_content:
        raise RuntimeError("WAF Challenge Exception: Target Node Trapped by Cloudflare.")
        
    return html_content


# Hard per-request HTTP timeout (Fix: prevents indefinite hang on bad proxy / 403)
SCRAPE_TIMEOUT_S: int = 30

# Rotating chrome versions for curl_cffi impersonation
CHROME_VERSIONS = [
    "chrome120", "chrome121", "chrome122", "chrome123",
    "chrome124", "chrome125", "chrome126", "chrome127",
    "chrome128", "chrome129",
]

# Rotating user agents and viewports for nodriver/UC fallback
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

VIEWPORTS = [
    (1920, 1080),
    (1440, 900),
    (1366, 768),
    (1536, 864),
    (1280, 800),
    (1920, 1200),
    (1680, 1050),
    (1600, 900),
    (1280, 720),
    (1440, 1080),
]

# Nodriver session pool for browser reuse (avoids start/stop per request)
_nodriver_pool = None


def _get_nodriver_pool():
    global _nodriver_pool
    if _nodriver_pool is None:
        from psychological.scrapers.nodriver_scraper import NodriverSessionPool, NodriverConfig
        _nodriver_pool = NodriverSessionPool(
            max_sessions=1,
            config=NodriverConfig(
                headless=True,
                min_delay=12.0,
                max_delay=25.0,
                page_load_timeout=30,
            ),
        )
    return _nodriver_pool


async def _close_nodriver_pool():
    global _nodriver_pool
    if _nodriver_pool:
        await _nodriver_pool.close_all()
        _nodriver_pool = None


def _exponential_backoff(attempt: int, base_min: float = 30, base_max: float = 60) -> float:
    """Return exponential backoff in seconds scaled by attempt number."""
    factor = 2 ** attempt
    return random.uniform(base_min * factor, base_max * factor)


async def _nodriver_get_html(url: str, wait_for: Optional[str] = None, max_retries: int = 3) -> Optional[str]:
    """Get page HTML using nodriver with full CDP stealth emulation.
    Uses NodriverSessionPool for browser reuse.
    Applies CDP evasion scripts before page loads to bypass Turnstile/anti-bot.
    Retries with different fingerprints on Cloudflare detection.
    """
    try:
        import nodriver as uc
    except ImportError:
        logger.warning("nodriver not available, skipping")
        return None

    _MIN_CONTENT_LENGTH = 500

    for attempt in range(max_retries):
        vp = random_viewport()
        ua = random_user_agent()
        browser = None
        session = None
        try:
            pool = _get_nodriver_pool()
            try:
                session = await pool.acquire()
                browser = None
                tab = await session._browser.get(url)
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
                await asyncio.sleep(random.uniform(8, 18))
                if wait_for:
                    try:
                        await tab.wait_for(wait_for, timeout=15)
                    except Exception:
                        pass
                html = await tab.get_content()
            except Exception:
                if session:
                    try:
                        await pool.release(session)
                    except Exception:
                        pass
                    session = None
                extra_args = [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-session-crashed-bubble",
                    "--disable-infobars",
                    "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-sync",
                ]
                browser = await uc.start(
                    headless=True,
                    browser_args=extra_args,
                    user_agent=ua,
                    sandbox=False,
                )
                tab = await browser.get(url)
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
                await asyncio.sleep(random.uniform(8, 18))
                if wait_for:
                    try:
                        await tab.wait_for(wait_for, timeout=15)
                    except Exception:
                        pass
                html = await tab.get_content()

            html_len = len(html) if html else 0
            if html and html_len > _MIN_CONTENT_LENGTH:
                if detect_cloudflare(html):
                    logger.warning("nodriver attempt %d: Cloudflare challenge detected for %s (%d bytes)",
                                   attempt + 1, url, html_len)
                    if session:
                        try:
                            await pool.release(session)
                        except Exception:
                            pass
                        session = None
                    if attempt < max_retries - 1:
                        await asyncio.sleep(_exponential_backoff(attempt, 12, 25))
                    continue
                logger.info("nodriver CDP stealth got HTML: %d bytes from %s (attempt %d)", html_len, url, attempt + 1)
                if session:
                    try:
                        await pool.release(session)
                    except Exception:
                        pass
                return html
            logger.warning("nodriver attempt %d: short/empty HTML (%d bytes)", attempt + 1, html_len)
        except Exception as e:
            logger.warning("nodriver CDP stealth attempt %d failed for %s: %s", attempt + 1, url, e)
        finally:
            if session:
                try:
                    await pool.release(session)
                except Exception:
                    pass
            if browser:
                try:
                    await browser.stop()
                except Exception:
                    pass
        if attempt < max_retries - 1:
            await asyncio.sleep(_exponential_backoff(attempt, 12, 25))

    logger.warning("nodriver CDP stealth exhausted retries for %s", url)
    return None


async def _uc_driver_get_html(url: str) -> Optional[str]:
    """Get page HTML using SeleniumBase UC mode with CDP stealth.
    Retries with exponential backoff and Cloudflare detection handling.
    Rotates user agent, viewport, and applies CDP evasion per attempt.
    """
    try:
        from seleniumbase import SB
    except ImportError:
        logger.warning("seleniumbase not available, skipping")
        return None

    _MIN_CONTENT_LENGTH = 500

    def _sb_fetch(target_url):
        vp = random_viewport()
        ua = random_user_agent()
        with SB(uc=True, headless=True, user_agent=ua, disable_csp=True) as sb:
            sb.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
                "width": vp[0], "height": vp[1], "mobile": False, "deviceScaleFactor": 1
            })
            sb.execute_cdp_cmd("Network.setUserAgentOverride", {
                "userAgent": ua, "acceptLanguage": "en-US,en;q=0.9", "platform": "MacIntel",
            })
            sb.execute_cdp_cmd("Emulation.setLocale", {"locale": "en-US"})
            sb.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "America/New_York"})
            evade_js = """
            Object.defineProperty(navigator, 'webdriver', {get:()=>undefined});
            Object.defineProperty(navigator, 'plugins', {get:()=>[1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get:()=>['en-US','en']});
            Object.defineProperty(navigator, 'platform', {get:()=>'MacIntel'});
            if(!window.chrome){window.chrome={runtime:{}};}
            """
            try:
                sb.execute_script(evade_js)
            except Exception:
                pass
            sb.uc_open_with_reconnect(target_url, 8)
            time_slept = 0
            max_wait = 25
            while time_slept < max_wait:
                page_source = sb.get_page_source()
                if detect_cloudflare(page_source):
                    logger.info("SeleniumBase UC: Cloudflare challenge active for %s (%.0fs)", target_url, time_slept)
                    try:
                        sb.uc_gui_click_captcha()
                    except Exception:
                        try:
                            sb.uc_open_with_reconnect(target_url, 8)
                        except Exception:
                            pass
                    time.sleep(3)
                    time_slept += 3
                else:
                    break
            page_source = sb.get_page_source()
            logger.info("SeleniumBase UC fetched %s (%d bytes, waited %.0fs)", target_url, len(page_source or ""), time_slept)
            return page_source

    loop = asyncio.get_event_loop()
    for attempt in range(3):
        try:
            html = await loop.run_in_executor(None, lambda: _sb_fetch(url))
            html_len = len(html) if html else 0
            if html and html_len > _MIN_CONTENT_LENGTH:
                if detect_cloudflare(html):
                    logger.warning("SeleniumBase UC attempt %d: Cloudflare still present for %s (%d bytes)",
                                   attempt + 1, url, html_len)
                    await asyncio.sleep(_exponential_backoff(attempt, 12, 25))
                    continue
                logger.info("SeleniumBase UC got HTML: %d bytes from %s (attempt %d)", html_len, url, attempt + 1)
                return html
            logger.warning("SeleniumBase UC attempt %d: short/empty HTML (%d bytes)", attempt + 1, html_len)
        except Exception as e:
            logger.warning("SeleniumBase UC attempt %d failed for %s: %s", attempt + 1, url, e)
            await asyncio.sleep(_exponential_backoff(attempt, 12, 25))

    logger.warning("SeleniumBase UC exhausted retries for %s", url)
    return None


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
    category_ratings: Optional[Dict[str, float]] = None
    awards: Optional[List[str]] = None


@dataclass
class G2EmployerScore:
    ticker: str
    slug: str
    overall_rating: Optional[float]
    review_count: Optional[int]
    would_recommend_pct: Optional[float]
    categories: Dict[str, float]
    fetched_at: str


@dataclass
class IndeedScore:
    ticker: str
    slug: str
    overall_rating: Optional[float]
    work_wellbeing_score: Optional[int]
    ceo_approval: Optional[int]
    ceo_name: Optional[str]
    review_count: Optional[int]
    wellbeing_breakdown: Dict[str, str]
    fetched_at: str


@dataclass
class ComparablyScore:
    ticker: str
    slug: str
    overall_rating: Optional[float]
    culture_grade: Optional[str]
    ceo_score: Optional[int]
    ceo_name: Optional[str]
    recommend_pct: Optional[int]
    category_grades: Dict[str, str]
    awards: List[str]
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
            "AVGO": "Broadcom",
            "INTC": "Intel",
            "ARM": "Arm Holdings",
        }
        self._curl_session: Optional[AsyncSession] = None
        self._proxy_manager: Optional[ProxyManager] = None
        self._browserless: Optional[BrowserlessClient] = None
        self._nodriver_primary: Optional[GlassdoorNodriverScraper] = None
        self._rate_limiter = RateLimiter(RateLimiterConfig(
            min_delay=12.0,
            max_delay=25.0,
            jitter=2.0,
        ))

    async def initialize(self) -> None:
        self._proxy_manager = ProxyManager(self.config)
        await self._proxy_manager.initialize()
        self._curl_session = AsyncSession(
            impersonate="chrome124",
            timeout=30
        )
        self._browserless = await create_browserless_client(self.config)
        self._nodriver_primary = await create_glassdoor_nodriver_scraper(self.config)
        logger.info("GlassdoorScraper initialized with nodriver primary + curl_cffi chrome124 + dynamic proxy manager + browserless")

    async def close(self) -> None:
        if self._curl_session:
            await self._curl_session.close()
            self._curl_session = None
        if self._proxy_manager:
            await self._proxy_manager.close()
            self._proxy_manager = None
        if self._browserless:
            await self._browserless.close()
            self._browserless = None
        if self._nodriver_primary:
            await self._nodriver_primary.close()
            self._nodriver_primary = None

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

    def _extract_glassdoor_data(self, html: str) -> Dict:
        soup = BeautifulSoup(html, "html.parser")
        result: Dict = {
            "raw_score": None,
            "review_count": None,
            "ceo_approval": None,
            "recommend_to_friend": None,
            "category_ratings": {},
            "awards": []
        }
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

        page_text = soup.get_text(separator=" ", strip=True)
        if result["raw_score"] is None:
            score_match = (
                re.search(r'(\d\.\d)\s*★', page_text) or
                re.search(r'(\d\.\d)\s*based on', page_text, re.IGNORECASE) or
                re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', page_text, re.IGNORECASE)
            )
            if score_match:
                result["raw_score"] = float(score_match.group(1))

        review_match = (
            re.search(r'([\d,]+)\s*(?:ratings|rating|review|reviews)', page_text, re.IGNORECASE) or
            re.search(r'based on\s*([\d,]+)', page_text, re.IGNORECASE)
        )
        if review_match:
            result["review_count"] = int(review_match.group(1).replace(",", ""))

        ceo_match = (
            re.search(r'(\d+)%\s*approve\s*(?:of\s*)?(?:CEO)?', page_text, re.IGNORECASE) or
            re.search(r'(\d+)%\s*(?:approve|CEO)', page_text, re.IGNORECASE) or
            re.search(r'CEO\s*(?:approval)?\s*:?\s*(\d+)%', page_text, re.IGNORECASE)
        )
        if ceo_match:
            result["ceo_approval"] = int(ceo_match.group(1))

        recommend_match = (
            re.search(r'(\d+)%\s*(?:would\s*)?recommend', page_text, re.IGNORECASE) or
            re.search(r'recommend\s*(?:to\s*a\s*friend)?\s*:?\s*(\d+)%', page_text, re.IGNORECASE)
        )
        if recommend_match:
            result["recommend_to_friend"] = int(recommend_match.group(1))

        # --- Extract Ratings by Category ---
        cat_ratings: Dict[str, float] = {}
        cat_nodes = soup.select("[data-test*='sub-rating'], [class*='subRating'], [class*='categoryRating'], [class*='ratingCategory']")
        for node in cat_nodes:
            txt = node.get_text(separator=" ", strip=True)
            m = re.search(r'([A-Za-z\s&/]+?)\s*(\d+\.?\d*)', txt)
            if m:
                c_name, c_val = m.group(1).strip(), self._parse_score(m.group(2))
                if c_val and 1.0 <= c_val <= 5.0 and len(c_name) > 2 and c_name.lower() not in ["overall", "rating"]:
                    cat_ratings[c_name] = c_val

        for header in soup.find_all(['h2', 'h3', 'h4', 'div', 'span']):
            if "ratings by category" in header.get_text().lower():
                parent = header.find_parent(['section', 'div']) or header.parent
                if parent:
                    for item in parent.find_all(['li', 'div', 'tr']):
                        itxt = item.get_text(separator=" ", strip=True)
                        m = re.search(r'([A-Za-z\s&/]+?)\s*(\d+\.\d+)', itxt)
                        if m:
                            c_name, c_val = m.group(1).strip(), float(m.group(2))
                            if 1.0 <= c_val <= 5.0 and len(c_name) > 2 and c_name.lower() not in ["ratings by category", "ratings", "category"]:
                                cat_ratings[c_name] = c_val

        std_cats = [
            ("Culture & values", r'Culture\s*(?:&|and)\s*values\s*:?\s*(\d+\.\d+)'),
            ("Diversity & inclusion", r'Diversity\s*(?:&|and)\s*inclusion\s*:?\s*(\d+\.\d+)'),
            ("Work/Life balance", r'Work/?Life\s*balance\s*:?\s*(\d+\.\d+)'),
            ("Compensation and benefits", r'Comp(?:ensation)?\s*(?:&|and)\s*benefits\s*:?\s*(\d+\.\d+)'),
            ("Career opportunities", r'Career\s*opportunities\s*:?\s*(\d+\.\d+)'),
            ("Senior management", r'Senior\s*management\s*:?\s*(\d+\.\d+)'),
        ]
        for cat_label, cat_regex in std_cats:
            if cat_label not in cat_ratings:
                m = re.search(cat_regex, page_text, re.IGNORECASE)
                if m:
                    try:
                        cat_ratings[cat_label] = float(m.group(1))
                    except ValueError:
                        pass
        result["category_ratings"] = cat_ratings

        # --- Extract Awards & Accolades ---
        extracted_awards: List[str] = []
        award_headers = [
            h for h in soup.find_all(['h2', 'h3', 'h4', 'div', 'span'])
            if any(k in h.get_text().lower() for k in ["awards & accolades", "glassdoor awards", "awards"])
        ]
        for header in award_headers:
            container = header.find_parent(['section', 'div', 'article']) or header.parent
            if container:
                for elem in container.find_all(['li', 'p', 'div', 'span']):
                    txt = elem.get_text(separator=" ", strip=True)
                    if txt and not any(ign in txt.lower() for ign in ["see more", "add awards", "awards & accolades", "glassdoor awards"]):
                        if 5 < len(txt) < 200 and txt not in extracted_awards:
                            extracted_awards.append(txt)

        if not extracted_awards:
            award_matches = re.findall(r'(Glassdoor\s+Best\s+Places\s+to\s+Work[^\n\r\.\<]{0,100}|The\s+Times\s+Top\s+100[^\n\r\.\<]{0,100}|Rising\s+Star[^\n\r\.\<]{0,100})', page_text, re.IGNORECASE)
            for am in award_matches:
                clean_am = am.strip()
                if clean_am and clean_am not in extracted_awards:
                    extracted_awards.append(clean_am)

        cleaned_awards = []
        for aw in extracted_awards:
            if not any(aw != other and aw in other for other in extracted_awards):
                if aw not in cleaned_awards:
                    cleaned_awards.append(aw)

        result["awards"] = cleaned_awards

        logger.info("Extracted Glassdoor data: score=%s, reviews=%s, ceo=%s, recommend=%s, cat_ratings=%s, awards_count=%d",
                     result["raw_score"], result["review_count"], result["ceo_approval"], result["recommend_to_friend"],
                     len(result["category_ratings"]), len(result["awards"]))
        return result

    async def scrape_company(self, ticker: str) -> GlassdoorScore:
        slug = self.company_slugs.get(ticker)
        if not slug:
            logger.warning("No Glassdoor slug for %s", ticker)
            return GlassdoorScore(ticker, "", None, None, None, None, None, datetime.now(timezone.utc).isoformat(), {}, [])

        company_name = self.company_names.get(ticker, ticker)
        logger.info("Scraping Glassdoor for %s (%s) via nodriver primary", ticker, company_name)

        raw_score = None
        review_count = None
        ceo_approval = None
        recommend_to_friend = None
        category_ratings = {}
        awards = []

        # Try nodriver primary scraper FIRST (JavaScript execution for Cloudflare)
        if not hasattr(self, '_nodriver_primary') or self._nodriver_primary is None:
            self._nodriver_primary = await create_glassdoor_nodriver_scraper(self.config)
        
        try:
            nodriver_result = await self._nodriver_primary.scrape_company(ticker)
            if nodriver_result.raw_score is not None:
                raw_score = nodriver_result.raw_score
                review_count = nodriver_result.review_count
                ceo_approval = nodriver_result.ceo_approval
                recommend_to_friend = nodriver_result.recommend_to_friend
                logger.info("Nodriver primary Glassdoor scrape success for %s: score=%s", ticker, raw_score)
        except Exception as e:
            logger.warning("Nodriver primary Glassdoor scrape failed for %s: %s", ticker, e)

        normalized = raw_score / 5.0 if raw_score else None

        if raw_score is None:
            logger.info("Nodriver primary failed for %s, falling back to curl_cffi direct scrape...", ticker)
            try:
                if not self._curl_session:
                    await self.initialize()

                result = await self._scrape_glassdoor_direct(slug)
                if result:
                    raw_score = result.get("raw_score")
                    review_count = result.get("review_count")
                    ceo_approval = result.get("ceo_approval")
                    recommend_to_friend = result.get("recommend_to_friend")
                    category_ratings = result.get("category_ratings", {})
                    awards = result.get("awards", [])
                    logger.info("Direct Glassdoor scrape success for %s: score=%s", ticker, raw_score)

            except Exception as e:
                logger.warning("Direct Glassdoor scrape failed for %s: %s", ticker, e)

        normalized = raw_score / 5.0 if raw_score else None

        if raw_score is None:
            logger.info("Glassdoor direct scrape failed for %s, retrying with next rotating proxy...", ticker)
            try:
                result = await self._scrape_glassdoor_direct(slug)
                if result:
                    raw_score = result.get("raw_score")
                    review_count = result.get("review_count")
                    ceo_approval = result.get("ceo_approval")
                    recommend_to_friend = result.get("recommend_to_friend")
                    category_ratings = result.get("category_ratings", {})
                    awards = result.get("awards", [])
                    normalized = raw_score / 5.0 if raw_score else None
                    logger.info("Glassdoor proxy retry success for %s: score=%s", ticker, raw_score)
            except Exception as e:
                logger.warning("Glassdoor proxy retry also failed for %s: %s", ticker, e)

        if raw_score is None:
            logger.info("Glassdoor proxy retry failed for %s, attempting Bing search fallback...", ticker)
            try:
                result = await self._bing_search_glassdoor(company_name, slug)
                if result:
                    raw_score = result.get("raw_score")
                    review_count = result.get("review_count")
                    ceo_approval = result.get("ceo_approval")
                    recommend_to_friend = result.get("recommend_to_friend")
                    category_ratings = result.get("category_ratings", {})
                    awards = result.get("awards", [])
                    normalized = raw_score / 5.0 if raw_score else None
                    logger.info("Glassdoor Bing search fallback success for %s: score=%s", ticker, raw_score)
            except Exception as e:
                logger.warning("Glassdoor Bing search fallback also failed for %s: %s", ticker, e)

        if raw_score is None:
            logger.info("Attempting Dynamic LLM extraction fallback for Glassdoor ticker %s...", ticker)
            try:
                result = await self._dynamic_llm_search_glassdoor(company_name, slug)
                if result:
                    raw_score = result.get("raw_score")
                    review_count = result.get("review_count")
                    ceo_approval = result.get("ceo_approval")
                    recommend_to_friend = result.get("recommend_to_friend")
                    category_ratings = result.get("category_ratings", {})
                    awards = result.get("awards", [])
                    normalized = raw_score / 5.0 if raw_score else None
                    logger.info("Dynamic LLM Glassdoor fallback success for %s: score=%s", ticker, raw_score)
            except Exception as e:
                logger.warning("Dynamic LLM Glassdoor fallback failed for %s: %s", ticker, e)

        if raw_score is None:
            logger.info("Glassdoor proxy retry and Bing fallback failed for %s, attempting browserless/nodriver/UC fallback chain...", ticker)
            try:
                result = await self._scrape_glassdoor_browserless(slug)
                if result:
                    raw_score = result.get("raw_score")
                    review_count = result.get("review_count")
                    ceo_approval = result.get("ceo_approval")
                    recommend_to_friend = result.get("recommend_to_friend")
                    category_ratings = result.get("category_ratings", {})
                    awards = result.get("awards", [])
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
            fetched_at=datetime.now(timezone.utc).isoformat(),
            category_ratings=category_ratings,
            awards=awards,
        )

    async def _scrape_glassdoor_browserless(self, slug: str) -> Optional[Dict]:
        """Glassdoor scraping with nodriver/undetected-chromedriver/curl_cffi fallback chain for Cloudflare.
        Implements exponential backoff on 403/429, chrome version rotation, and full HTTP status logging.
        """
        url = f"{self.BASE_URL}/Reviews/{slug}-reviews-SRCH_KE0,{len(slug)}.htm"
        html = None
        logger.info("Glassdoor browserless fallback chain for slug=%s", slug)

        _CLOUDFLARE_SIGNALS = [
            "cf-browser-verification", "cf-challenge", "just a moment",
            "checking your browser", "attention required", "security check",
            "access to this page has been denied", "verify you are human",
            "we've detected unusual traffic", "cloudflare ray id",
        ]

        await self._rate_limiter.acquire()

        logger.info("Trying nodriver CDP stealth bypass for Glassdoor slug=%s", slug)
        try:
            html = await _nodriver_get_html(url, wait_for="div[data-test='overallRating'], span[data-test='overall-rating'], .ratingNumber")
        except Exception as e:
            logger.warning("nodriver CDP stealth Glassdoor exception: %s", e)

        if html is None and self._browserless:
            logger.info("nodriver failed, trying browserless for Glassdoor slug=%s", slug)
            await self._rate_limiter.acquire()
            try:
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
                if result.success:
                    html = result.html
                    logger.info("Browserless success for Glassdoor slug=%s (%d bytes, status=%s)",
                                slug, len(html), result.status_code)
                else:
                    logger.warning("Browserless failed for Glassdoor slug=%s: %s (status=%s)",
                                   slug, result.error, result.status_code)
            except Exception as e:
                logger.warning("Browserless exception for Glassdoor slug=%s: %s", slug, e)

        if html is None:
            logger.info("nodriver/browserless failed, trying undetected-chromedriver for Glassdoor slug=%s", slug)
            await self._rate_limiter.acquire()
            try:
                html = await _uc_driver_get_html(url)
            except Exception as e:
                logger.warning("undetected-chromedriver Glassdoor exception: %s", e)

        if html is None and self._curl_session:
            logger.info("nodriver/UC unavailable, trying curl_cffi+JA4 for Glassdoor slug=%s", slug)
            for attempt in range(3):
                await self._rate_limiter.acquire()
                try:
                    chrome_ver = random.choice(CHROME_VERSIONS)
                    headers = build_ja4_headers({"Referer": "https://www.glassdoor.com/index.htm"})
                    proxy = await self._proxy_manager.get_proxy() if self._proxy_manager else None
                    kwargs = {"headers": headers, "impersonate": chrome_ver}
                    if proxy:
                        kwargs["proxy"] = proxy
                    resp = await asyncio.wait_for(
                        self._curl_session.get(url, **kwargs),
                        timeout=SCRAPE_TIMEOUT_S,
                    )
                    status = resp.status_code
                    logger.info("curl_cffi+JA4 Glassdoor returned HTTP %d for slug=%s (attempt %d, chrome=%s)",
                                status, slug, attempt + 1, chrome_ver)
                    if status == 200:
                        html_lower = resp.text.lower()
                        if any(signal in html_lower for signal in _CLOUDFLARE_SIGNALS):
                            logger.warning("curl_cffi+JA4 Cloudflare challenge in HTML for slug=%s (attempt %d)",
                                           slug, attempt + 1)
                            await asyncio.sleep(_exponential_backoff(attempt, 12, 25))
                            continue
                        html = resp.text
                        break
                    elif status in (403, 429):
                        logger.warning("curl_cffi+JA4 blocked (%d) for Glassdoor slug=%s (attempt %d) — backing off",
                                       status, slug, attempt + 1)
                        await asyncio.sleep(_exponential_backoff(attempt, 30, 60))
                    elif status in (502, 503, 504):
                        logger.warning("curl_cffi+JA4 gateway error (%d) for Glassdoor slug=%s (attempt %d) — backing off",
                                       status, slug, attempt + 1)
                        await asyncio.sleep(_exponential_backoff(attempt, 15, 30))
                    else:
                        logger.warning("curl_cffi+JA4 returned %d for Glassdoor slug=%s (attempt %d)",
                                       status, slug, attempt + 1)
                        await asyncio.sleep(random.uniform(10, 20))
                except asyncio.TimeoutError:
                    logger.warning("curl_cffi+JA4 Glassdoor attempt %d timed out for slug=%s", attempt + 1, slug)
                    await asyncio.sleep(_exponential_backoff(attempt, 15, 30))
                except Exception as e:
                    logger.warning("curl_cffi+JA4 Glassdoor attempt %d exception for slug=%s: %s",
                                   attempt + 1, slug, e)
                    await asyncio.sleep(_exponential_backoff(attempt, 10, 20))

        if html is None:
            logger.warning("All Glassdoor browserless fallbacks exhausted for slug=%s", slug)
            return None

        return self._extract_glassdoor_data(html)

    async def _scrape_glassdoor_direct(self, slug: str) -> Optional[Dict]:
        """Direct Glassdoor reviews page scraping via curl_cffi + proxy."""
        try:
            url = f"{self.BASE_URL}/Reviews/{slug}-reviews-SRCH_KE0,{len(slug)}.htm"

            await self._rate_limiter.acquire()
            proxy = await self._proxy_manager.get_proxy() if self._proxy_manager else None
            headers = build_ja4_headers({"Referer": "https://www.glassdoor.com/index.htm"})

            kwargs = {"headers": headers}
            if proxy:
                kwargs["proxy"] = proxy

            response = await asyncio.wait_for(
                self._curl_session.get(url, **kwargs),
                timeout=SCRAPE_TIMEOUT_S,
            )
            if response.status_code != 200:
                logger.warning("Glassdoor direct fetch returned %d for slug=%s", response.status_code, slug)
                return None

            return self._extract_glassdoor_data(response.text)

        except Exception as e:
            logger.warning("Glassdoor direct scrape exception: %s", e)
            return None

    async def _bing_search_glassdoor(self, company_name: str, slug: str) -> Optional[Dict]:
        """Parse Glassdoor data from Bing search results through proxy chain"""
        try:
            query = f"site:glassdoor.com \"{company_name}\" reviews rating"
            url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"

            await self._rate_limiter.acquire()
            proxy = await self._proxy_manager.get_proxy() if self._proxy_manager else None
            headers = build_ja4_headers()

            kwargs = {"headers": headers}
            if proxy:
                kwargs["proxy"] = proxy

            response = await asyncio.wait_for(
                self._curl_session.get(url, **kwargs),
                timeout=SCRAPE_TIMEOUT_S,
            )
            if response.status_code != 200:
                return None

            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            result = {"raw_score": None, "review_count": None, "ceo_approval": None, "recommend_to_friend": None, "category_ratings": {}, "awards": []}

            seen = set()
            for result_elem in soup.select("li.b_algo, div.b_caption"):
                raw_text = result_elem.get_text()
                text_key = raw_text.strip()[:100]
                if text_key in seen:
                    continue
                seen.add(text_key)

                rating_match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', raw_text, re.IGNORECASE)
                if rating_match:
                    result["raw_score"] = float(rating_match.group(1))

                review_match = re.search(r'([\d,]+)\s*reviews?', raw_text, re.IGNORECASE)
                if review_match:
                    result["review_count"] = int(review_match.group(1).replace(',', ''))

                ceo_match = re.search(r'(\d+)%\s*(?:CEO|approve)', raw_text, re.IGNORECASE)
                if ceo_match:
                    result["ceo_approval"] = int(ceo_match.group(1))

                recommend_match = re.search(r'(\d+)%\s*(?:recommend|friend)', raw_text, re.IGNORECASE)
                if recommend_match:
                    result["recommend_to_friend"] = int(recommend_match.group(1))

                if result["raw_score"]:
                    return result

            return None
        except Exception as e:
            logger.warning(f"Bing search failed for Glassdoor: {e}")
            return None

    async def _dynamic_llm_search_glassdoor(self, company_name: str, slug: str) -> Optional[Dict]:
        """Uses DynamicExtractor with stealth search indexing to fetch Glassdoor metrics."""
        try:
            query = f"site:glassdoor.com \"{company_name}\" reviews rating CEO approval"
            search_url = f"https://html.duckduckgo.com/html/?q={query.replace(' ', '+')}"
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9"
            }
            
            if not self._curl_session:
                await self.initialize()
                
            response = await asyncio.wait_for(
                self._curl_session.get(search_url, headers=headers),
                timeout=SCRAPE_TIMEOUT_S
            )
            if response.status_code != 200:
                return None

            extractor = DynamicExtractor()
            extracted = extractor.extract_structured_data(
                response.text,
                f"Extract Glassdoor ratings for {company_name}: raw_score (float out of 5), review_count (int), ceo_approval (int percentage), recommend_to_friend (int percentage).",
                '{\n  "raw_score": float,\n  "review_count": int,\n  "ceo_approval": int,\n  "recommend_to_friend": int\n}'
            )
            
            if isinstance(extracted, dict) and extracted.get("raw_score"):
                return {
                    "raw_score": float(extracted["raw_score"]),
                    "review_count": int(extracted.get("review_count") or 0),
                    "ceo_approval": int(extracted.get("ceo_approval") or 0),
                    "recommend_to_friend": int(extracted.get("recommend_to_friend") or 0)
                }
            return None
        except Exception as e:
            logger.warning(f"Dynamic LLM Glassdoor search failed: {e}")
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
        self.company_slugs = self.g2_config.get("company_slugs", {})
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
            "INTC": "Intel",
        }
        self.vader = SentimentIntensityAnalyzer()
        self._curl_session: Optional[AsyncSession] = None
        self._proxy_manager: Optional[ProxyManager] = None
        self._browserless: Optional[BrowserlessClient] = None
        self._g2_warm_tab = None
        self._g2_warm_session = None
        self._rate_limiter = RateLimiter(RateLimiterConfig(
            min_delay=12.0,
            max_delay=25.0,
            jitter=2.0,
        ))

    async def initialize(self) -> None:
        self._proxy_manager = ProxyManager(self.config)
        await self._proxy_manager.initialize()
        self._curl_session = AsyncSession(
            impersonate="chrome124",
            timeout=30
        )
        self._browserless = await create_browserless_client(self.config)
        pool = _get_nodriver_pool()
        await self._warmup_g2_session(pool)
        logger.info("G2EmployerScraper initialized with curl_cffi chrome124 + dynamic proxy manager + browserless + warm session")

    async def close(self) -> None:
        if self._g2_warm_session:
            try:
                pool = _get_nodriver_pool()
                await pool.release(self._g2_warm_session)
            except Exception:
                pass
            self._g2_warm_session = None
            self._g2_warm_tab = None
        if self._curl_session:
            await self._curl_session.close()
            self._curl_session = None
        if self._proxy_manager:
            await self._proxy_manager.close()
            self._proxy_manager = None
        if self._browserless:
            await self._browserless.close()
            self._browserless = None

    async def __aenter__(self) -> "G2EmployerScraper":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _warmup_g2_session(self, pool) -> bool:
        try:
            import nodriver as uc
        except ImportError:
            logger.warning("nodriver not available, skipping G2 session warmup")
            return False

        try:
            session = await pool.acquire()
            tab = await session._browser.get("https://www.g2.com")

            vp = random_viewport()
            ua = random_user_agent()
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

            await asyncio.sleep(random.uniform(8, 12))

            for attempt in range(3):
                page_title = await tab.evaluate("document.title")
                if isinstance(page_title, tuple):
                    page_title = page_title[1] if len(page_title) > 1 else page_title[0]
                title_text = str(page_title or "")
                if "just a moment" in title_text.lower():
                    logger.info("G2 warmup: CF still present (attempt %d/3), waiting 5s", attempt + 1)
                    await asyncio.sleep(5)
                else:
                    break

            page_title = await tab.evaluate("document.title")
            if isinstance(page_title, tuple):
                page_title = page_title[1] if len(page_title) > 1 else page_title[0]
            if "just a moment" in str(page_title or "").lower():
                logger.warning("G2 warmup: Cloudflare not cleared after max retries")
                await pool.release(session)
                return False

            self._g2_warm_tab = tab
            self._g2_warm_session = session
            logger.info("G2 session warmed up on homepage, Turnstile cleared")
            return True

        except Exception as e:
            logger.warning("G2 session warmup failed: %s", e)
            if self._g2_warm_session:
                try:
                    await pool.release(self._g2_warm_session)
                except Exception:
                    pass
                self._g2_warm_session = None
                self._g2_warm_tab = None
            return False

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

    def _extract_g2_data(self, html: str) -> Dict:
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
        logger.info("Extracted G2 data: rating=%s, reviews=%s, recommend=%s",
                     result["overall_rating"], result["review_count"], result["would_recommend_pct"])
        return result

    async def scrape_company(self, ticker: str) -> G2EmployerScore:
        slug = self.company_slugs.get(ticker)
        if not slug:
            logger.warning("No G2 slug for %s", ticker)
            return G2EmployerScore(ticker, "", None, 0, None, {}, datetime.now(timezone.utc).isoformat())

        logger.info("Scraping G2 for %s (%s)", ticker, slug)

        overall_rating = None
        review_count = 0
        would_recommend_pct = None
        categories = {}

        # Tier 1: SERP JSON-LD / Google & Bing index (zero Cloudflare exposure)
        try:
            if not self._curl_session:
                await self.initialize()
            serp_res = await self._scrape_g2_serp_fallback(ticker, slug)
            if serp_res and serp_res.get("overall_rating") is not None:
                logger.info("G2 SERP Tier 1 scrape success for %s: rating=%s", ticker, serp_res["overall_rating"])
                return G2EmployerScore(
                    ticker=ticker, slug=slug,
                    overall_rating=serp_res["overall_rating"],
                    review_count=serp_res.get("review_count", 0),
                    would_recommend_pct=serp_res.get("would_recommend_pct"),
                    categories=serp_res.get("categories", {}),
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                )
        except Exception as e:
            logger.warning("G2 SERP Tier 1 scrape failed for %s: %s", ticker, e)

        # Tier 2: Warm CDP browser session (if SERP returned no rating)
        if self._g2_warm_tab is not None:
            try:
                url = f"{self.BASE_URL}/products/{slug}/reviews"
                vp = random_viewport()
                ua = random_user_agent()
                cdp_cmds = build_cdp_cmds(vp)
                for cmd in cdp_cmds:
                    try:
                        await self._g2_warm_tab.send(cmd["cmd"], cmd["params"])
                    except Exception:
                        pass
                    await asyncio.sleep(random.uniform(2, 4))
                script = build_cdp_evasion_script(vp, ua)
                try:
                    await self._g2_warm_tab.evaluate(script)
                except Exception:
                    pass
                await self._rate_limiter.acquire()
                await self._g2_warm_tab.get(url)
                await asyncio.sleep(random.uniform(15, 30))
                try:
                    await self._g2_warm_tab.wait_for(
                        "[itemprop='ratingValue'], [data-testid='rating-value'], .product-rating",
                        timeout=15,
                    )
                except Exception:
                    pass
                html = await self._g2_warm_tab.get_content()
                if html and len(html) > 500 and not detect_cloudflare(html):
                    data = self._extract_g2_data(html)
                    if data.get("overall_rating") is not None:
                        overall_rating = data["overall_rating"]
                        review_count = data.get("review_count", 0)
                        would_recommend_pct = data.get("would_recommend_pct")
                        categories = data.get("categories", {})
                        logger.info("G2 warm session scrape success for %s: rating=%s", ticker, overall_rating)
                        return G2EmployerScore(
                            ticker=ticker, slug=slug,
                            overall_rating=overall_rating, review_count=review_count,
                            would_recommend_pct=would_recommend_pct, categories=categories,
                            fetched_at=datetime.now(timezone.utc).isoformat(),
                        )
                logger.warning("G2 warm session returned no rating for %s, falling back", ticker)
            except Exception as e:
                logger.warning("G2 warm session scrape failed for %s: %s", ticker, e)

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
            logger.info("G2 direct scrape failed for %s, retrying with next rotating proxy...", ticker)
            try:
                result = await self._scrape_g2_direct(slug)
                if result:
                    overall_rating = result.get("overall_rating")
                    review_count = result.get("review_count", 0)
                    would_recommend_pct = result.get("would_recommend_pct")
                    categories = result.get("categories", {})
                    logger.info("G2 proxy retry success for %s: rating=%s", ticker, overall_rating)
            except Exception as e:
                logger.warning("G2 proxy retry also failed for %s: %s", ticker, e)

        if overall_rating is None:
            logger.info("G2 direct scrape and proxy retry failed, attempting Bing search fallback for %s...", ticker)
            try:
                result = await self._bing_search_g2(ticker, slug)
                if result:
                    overall_rating = result.get("overall_rating")
                    review_count = result.get("review_count", 0)
                    would_recommend_pct = result.get("would_recommend_pct")
                    categories = result.get("categories", {})
                    logger.info("G2 Bing search fallback success for %s: rating=%s", ticker, overall_rating)
            except Exception as e:
                logger.warning("G2 Bing search fallback also failed for %s: %s", ticker, e)

        if overall_rating is None:
            logger.info("G2 all fallbacks failed for %s, attempting Wayback Machine fallback...", ticker)
            try:
                result = await self._scrape_g2_wayback(slug)
                if result:
                    overall_rating = result.get("overall_rating")
                    review_count = result.get("review_count", 0)
                    would_recommend_pct = result.get("would_recommend_pct")
                    categories = result.get("categories", {})
                    logger.info("G2 Wayback Machine fallback success for %s: rating=%s", ticker, overall_rating)
            except Exception as e:
                logger.warning("G2 Wayback Machine fallback also failed for %s: %s", ticker, e)

        return G2EmployerScore(
            ticker=ticker,
            slug=slug,
            overall_rating=overall_rating,
            review_count=review_count,
            would_recommend_pct=would_recommend_pct,
            categories=categories,
            fetched_at=datetime.now(timezone.utc).isoformat()
        )

    async def _scrape_g2_wayback(self, slug: str) -> Optional[Dict]:
        """G2 Wayback Machine fallback scraping for archived snapshots of G2 reviews."""
        import json

        try:
            query_url = f"https://web.archive.org/cdx/search/cdx?url=g2.com/products/{slug}/reviews&output=json&limit=1&from=20240101&filter=statuscode:200"

            await self._rate_limiter.acquire()
            proxy = await self._proxy_manager.get_proxy() if self._proxy_manager else None
            headers = build_ja4_headers({"Referer": "https://web.archive.org/"})

            kwargs = {"headers": headers}
            if proxy:
                kwargs["proxy"] = proxy

            response = await asyncio.wait_for(
                self._curl_session.get(query_url, **kwargs),
                timeout=SCRAPE_TIMEOUT_S,
            )
            if response.status_code != 200:
                logger.warning("G2 CDX API returned %d for slug=%s", response.status_code, slug)
                return None

            cdx_data = await response.json()
            if len(cdx_data) < 2:
                logger.warning("G2 CDX API returned insufficient data for slug=%s", slug)
                return None

            latest_snapshot = cdx_data[1]
            if len(latest_snapshot) < 5:
                logger.warning("G2 CDX API snapshot data malformed for slug=%s", slug)
                return None

            timestamp = latest_snapshot[1]
            archived_url = f"https://web.archive.org/web/{timestamp}/https://www.g2.com/products/{slug}/reviews"

            resp = await asyncio.wait_for(
                self._curl_session.get(archived_url, headers=build_ja4_headers()),
                timeout=SCRAPE_TIMEOUT_S,
            )
            if resp.status_code != 200:
                logger.warning("G2 Wayback snapshot fetch returned %d for slug=%s", resp.status_code, slug)
                return None

            html = resp.text
            return self._extract_g2_data(html)

        except Exception as e:
            logger.warning("G2 Wayback Machine scrape exception: %s", e)
            return None

    async def _scrape_fallbacks(self, ticker: str) -> Dict:
        """Fallback method returning empty result without synthetic data generation."""
        return {"raw_score": None, "review_count": None, "ceo_approval": None, "recommend_to_friend": None, "category_ratings": {}, "awards": []}

    async def _scrape_g2_direct(self, slug: str) -> Optional[Dict]:
        """Direct G2 product reviews page scraping via curl_cffi + proxy."""
        try:
            url = f"{self.BASE_URL}/products/{slug}/reviews"

            await self._rate_limiter.acquire()
            proxy = await self._proxy_manager.get_proxy() if self._proxy_manager else None
            headers = build_ja4_headers({"Referer": "https://www.g2.com/"})

            kwargs = {"headers": headers}
            if proxy:
                kwargs["proxy"] = proxy

            response = await asyncio.wait_for(
                self._curl_session.get(url, **kwargs),
                timeout=SCRAPE_TIMEOUT_S,
            )
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

    async def _bing_search_g2(self, ticker: str, slug: str) -> Optional[Dict]:
        """Parse G2 ratings and review counts from Bing search results"""
        try:
            query = f"site:g2.com/products/{slug} reviews rating"
            url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"

            await self._rate_limiter.acquire()
            proxy = await self._proxy_manager.get_proxy() if self._proxy_manager else None
            headers = build_ja4_headers()

            kwargs = {"headers": headers}
            if proxy:
                kwargs["proxy"] = proxy

            response = await asyncio.wait_for(
                self._curl_session.get(url, **kwargs),
                timeout=SCRAPE_TIMEOUT_S,
            )
            if response.status_code != 200:
                return None

            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            result = {"overall_rating": None, "review_count": 0, "would_recommend_pct": None, "categories": {}}

            seen = set()
            for result_elem in soup.select("li.b_algo, div.b_caption"):
                raw_text = result_elem.get_text()
                text_key = raw_text.strip()[:100]
                if text_key in seen:
                    continue
                seen.add(text_key)

                rating_match = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', raw_text, re.IGNORECASE)
                if rating_match:
                    result["overall_rating"] = float(rating_match.group(1))

                count_match = re.search(r'([\d,]+)\s*(?:review|Review|Reviews)', raw_text, re.IGNORECASE)
                if count_match:
                    result["review_count"] = int(count_match.group(1).replace(",", ""))

                recommend_match = re.search(r'(\d+)%\s*(?:would recommend|recommend)', raw_text, re.IGNORECASE)
                if recommend_match:
                    result["would_recommend_pct"] = float(recommend_match.group(1))

                if result["overall_rating"]:
                    return result

            return None
        except Exception as e:
            logger.warning(f"Bing search failed for G2: {e}")
            return None

    async def _scrape_g2_serp_fallback(self, ticker: str, slug: str) -> Optional[Dict]:
        """Extract G2 ratings from Bing SERP JSON-LD + snippet text.
        Queries Bing for g2.com product pages, parses AggregateRating schema
        and snippet patterns. Also checks SERPAPI_KEY for Google Custom Search.
        Called BEFORE nodriver/browserless chain to avoid Cloudflare entirely.
        """
        company_name = self.company_names.get(ticker, ticker)
        result = {"overall_rating": None, "review_count": 0, "would_recommend_pct": None, "categories": {}}

        # --- Bing SERP HTML ---
        for query in [
            f"site:g2.com/products/{slug} reviews rating",
            f'site:g2.com "{company_name}" reviews rating',
        ]:
            try:
                url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"
                await self._rate_limiter.acquire()
                headers = build_ja4_headers({"Accept-Language": "en-US,en;q=0.9"})
                resp = await asyncio.wait_for(
                    self._curl_session.get(url, headers=headers),
                    timeout=SCRAPE_TIMEOUT_S,
                )
                if resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")

                # Parse JSON-LD blocks for AggregateRating
                for script in soup.select("script[type='application/ld+json']"):
                    try:
                        data = json.loads(script.string)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    # Handle both single object and @graph arrays
                    items = [data] if isinstance(data, dict) else data if isinstance(data, list) else []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        agg = item.get("aggregateRating") or {}
                        if isinstance(agg, dict) and agg.get("ratingValue"):
                            val = float(agg["ratingValue"])
                            if 0 <= val <= 5:
                                result["overall_rating"] = val
                            if agg.get("ratingCount"):
                                try:
                                    result["review_count"] = int(float(agg["ratingCount"]))
                                except (ValueError, TypeError):
                                    pass
                            if agg.get("bestRating"):
                                pass  # already normalized
                        # Also handle Product schema with offers/rating
                        if item.get("@type") in ("Product", "SoftwareApplication", "WebApplication"):
                            if item.get("aggregateRating"):
                                pass  # already handled above

                # Parse snippet text for ratings if JSON-LD was insufficient
                if result["overall_rating"] is None:
                    seen = set()
                    for elem in soup.select("li.b_algo, div.b_caption, p.b_lineclamp2, div.b_snippet"):
                        raw = elem.get_text(separator=" ", strip=True)
                        key = raw[:120]
                        if key in seen:
                            continue
                        seen.add(key)

                        rating_m = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', raw, re.IGNORECASE)
                        if rating_m:
                            result["overall_rating"] = float(rating_m.group(1))

                        count_m = re.search(r'([\d,]+)\s*(?:review|Review|Reviews)', raw)
                        if count_m:
                            result["review_count"] = int(count_m.group(1).replace(",", ""))

                        rec_m = re.search(r'(\d+)%\s*(?:would recommend|recommend)', raw, re.IGNORECASE)
                        if rec_m:
                            result["would_recommend_pct"] = float(rec_m.group(1))

                if result["overall_rating"] is not None:
                    logger.info("G2 SERP Bing success for %s: rating=%s, reviews=%s",
                                ticker, result["overall_rating"], result["review_count"])
                    return result

            except Exception as e:
                logger.warning("G2 SERP Bing query failed for %s (%s): %s", ticker, query[:60], e)
                continue

        # --- Google Custom Search via SERPAPI (if configured) ---
        serpapi_key = os.getenv("SERPAPI_KEY")
        if serpapi_key and result["overall_rating"] is None:
            try:
                import httpx
                search_query = f'site:g2.com/products/{slug} reviews rating'
                serp_url = (
                    f"https://serpapi.com/search.json?"
                    f"engine=google&q={search_query.replace(' ', '+')}&api_key={serpapi_key}"
                )
                async with httpx.AsyncClient(timeout=30) as client:
                    serp_resp = await client.get(serp_url)
                    if serp_resp.status_code == 200:
                        serp_data = serp_resp.json()
                        # Parse JSON-LD from organic results if present
                        for organic in serp_data.get("organic_results", []):
                            snippet = organic.get("snippet", "")
                            rating_m = re.search(r'(\d+\.?\d*)\s*(?:out of|/)\s*5', snippet, re.IGNORECASE)
                            if rating_m:
                                result["overall_rating"] = float(rating_m.group(1))
                            count_m = re.search(r'([\d,]+)\s*(?:review|Review|Reviews)', snippet)
                            if count_m:
                                result["review_count"] = int(count_m.group(1).replace(",", ""))
                            rec_m = re.search(r'(\d+)%\s*(?:would recommend|recommend)', snippet, re.IGNORECASE)
                            if rec_m:
                                result["would_recommend_pct"] = float(rec_m.group(1))
                            if result["overall_rating"] is not None:
                                break
                        # Also check rich_snippet / graph data from SerpAPI
                        if serp_data.get("inline_data") and result["overall_rating"] is None:
                            inline = serp_data["inline_data"]
                            if inline.get("aggregate_rating"):
                                val = inline["aggregate_rating"].get("rating_value") or inline["aggregate_rating"].get("ratingValue")
                                if val:
                                    result["overall_rating"] = float(val)
                                cnt = inline["aggregate_rating"].get("review_count") or inline["aggregate_rating"].get("ratingCount")
                                if cnt:
                                    result["review_count"] = int(cnt)

                if result["overall_rating"] is not None:
                    logger.info("G2 SERP Google/SerpAPI success for %s: rating=%s, reviews=%s",
                                ticker, result["overall_rating"], result["review_count"])
                    return result

            except ImportError:
                logger.debug("httpx not available, skipping SerpAPI")
            except Exception as e:
                logger.warning("G2 SERP SerpAPI failed for %s: %s", ticker, e)

        if result["overall_rating"] is not None:
            return result
        return None

    async def _scrape_g2_browserless(self, slug: str) -> Optional[Dict]:
        """G2 scraping with nodriver/undetected-chromedriver/curl_cffi fallback chain for Cloudflare.
        Implements exponential backoff on 403/429, chrome version rotation, and full HTTP status logging.
        """
        url = f"{self.BASE_URL}/products/{slug}/reviews"
        html = None
        logger.info("G2 browserless fallback chain for slug=%s", slug)

        _CLOUDFLARE_SIGNALS = [
            "cf-browser-verification", "cf-challenge", "just a moment",
            "checking your browser", "attention required", "security check",
            "access to this page has been denied", "verify you are human",
            "we've detected unusual traffic", "cloudflare ray id",
        ]

        await self._rate_limiter.acquire()

        logger.info("Trying nodriver CDP stealth bypass for G2 slug=%s", slug)
        try:
            html = await _nodriver_get_html(url, wait_for="[data-testid='rating-value'], [class*='star-rating'], [itemprop='ratingValue']")
        except Exception as e:
            logger.warning("nodriver CDP stealth G2 exception: %s", e)

        if html is None and self._browserless:
            logger.info("nodriver failed, trying browserless for G2 slug=%s", slug)
            await self._rate_limiter.acquire()
            try:
                result = await self._browserless.scrape(
                    url=url,
                    wait_for="[data-testid='rating-value'], [class*='star-rating'], [itemprop='ratingValue']",
                    wait_until="networkidle2",
                    timeout=60000,
                    headers={"Referer": "https://www.g2.com/"}
                )
                if result.success:
                    html = result.html
                    logger.info("Browserless success for G2 slug=%s (%d bytes, status=%s)",
                                slug, len(html), result.status_code)
                else:
                    logger.warning("Browserless failed for G2 slug=%s: %s (status=%s)",
                                   slug, result.error, result.status_code)
            except Exception as e:
                logger.warning("Browserless exception for G2 slug=%s: %s", slug, e)

        if html is None:
            logger.info("nodriver/browserless failed, trying undetected-chromedriver for G2 slug=%s", slug)
            await self._rate_limiter.acquire()
            try:
                html = await _uc_driver_get_html(url)
            except Exception as e:
                logger.warning("undetected-chromedriver G2 exception: %s", e)

        if html is None and self._curl_session:
            logger.info("nodriver/UC unavailable, trying curl_cffi+JA4 for G2 slug=%s", slug)
            for attempt in range(3):
                await self._rate_limiter.acquire()
                try:
                    chrome_ver = random.choice(CHROME_VERSIONS)
                    headers = build_ja4_headers({"Referer": "https://www.g2.com/"})
                    proxy = await self._proxy_manager.get_proxy() if self._proxy_manager else None
                    kwargs = {"headers": headers, "impersonate": chrome_ver}
                    if proxy:
                        kwargs["proxy"] = proxy
                    resp = await asyncio.wait_for(
                        self._curl_session.get(url, **kwargs),
                        timeout=SCRAPE_TIMEOUT_S,
                    )
                    status = resp.status_code
                    logger.info("curl_cffi+JA4 G2 returned HTTP %d for slug=%s (attempt %d, chrome=%s)",
                                status, slug, attempt + 1, chrome_ver)
                    if status == 200:
                        html_lower = resp.text.lower()
                        if any(signal in html_lower for signal in _CLOUDFLARE_SIGNALS):
                            logger.warning("curl_cffi+JA4 Cloudflare challenge in HTML for slug=%s (attempt %d)",
                                           slug, attempt + 1)
                            await asyncio.sleep(_exponential_backoff(attempt, 12, 25))
                            continue
                        html = resp.text
                        break
                    elif status in (403, 429):
                        logger.warning("curl_cffi+JA4 blocked (%d) for G2 slug=%s (attempt %d) — backing off",
                                       status, slug, attempt + 1)
                        await asyncio.sleep(_exponential_backoff(attempt, 30, 60))
                    elif status in (502, 503, 504):
                        logger.warning("curl_cffi+JA4 gateway error (%d) for G2 slug=%s (attempt %d) — backing off",
                                       status, slug, attempt + 1)
                        await asyncio.sleep(_exponential_backoff(attempt, 15, 30))
                    else:
                        logger.warning("curl_cffi+JA4 returned %d for G2 slug=%s (attempt %d)",
                                       status, slug, attempt + 1)
                        await asyncio.sleep(random.uniform(10, 20))
                except asyncio.TimeoutError:
                    logger.warning("curl_cffi+JA4 G2 attempt %d timed out for slug=%s", attempt + 1, slug)
                    await asyncio.sleep(_exponential_backoff(attempt, 15, 30))
                except Exception as e:
                    logger.warning("curl_cffi+JA4 G2 attempt %d exception for slug=%s: %s",
                                   attempt + 1, slug, e)
                    await asyncio.sleep(_exponential_backoff(attempt, 10, 20))

        if html is None:
            logger.warning("All G2 browserless fallbacks exhausted for slug=%s", slug)
            return None

        return self._extract_g2_data(html)

    async def scrape_all(self, tickers: List[str]) -> Dict[str, G2EmployerScore]:
        results = {}
        for ticker in tickers:
            results[ticker] = await self.scrape_company(ticker)
            await asyncio.sleep(random.uniform(12, 25))
        return results


class IndeedScraper:
    BASE_URL = "https://www.indeed.com"

    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.company_slugs = self.config.get("indeed", {}).get("company_slugs", {
            "NVDA": "Nvidia",
            "INTC": "Intel",
            "TSLA": "Tesla",
            "AAPL": "Apple",
            "MSFT": "Microsoft",
            "GOOGL": "Google",
            "AMZN": "Amazon",
            "AMD": "AMD",
            "AVGO": "Broadcom",
            "META": "Meta"
        })
        self._curl_session = None
        self._rate_limiter = RateLimiter(RateLimiterConfig(min_delay=12.0, max_delay=25.0, jitter=2.0))
        self._proxy_manager = ProxyManager(self.config) if self.config.get("proxy", {}).get("enabled", True) else None
        self._browserless = None

    async def initialize(self) -> None:
        if not self._curl_session:
            self._curl_session = AsyncSession(impersonate="chrome124")
        if self._proxy_manager:
            await self._proxy_manager.initialize()
        self._browserless = await create_browserless_client(self.config)
        logger.info("IndeedScraper initialized")

    async def close(self) -> None:
        if self._curl_session:
            await self._curl_session.close()
            self._curl_session = None
        if self._proxy_manager:
            await self._proxy_manager.close()
        if self._browserless:
            await self._browserless.close()

    async def __aenter__(self) -> "IndeedScraper":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _extract_indeed_data(self, html: str) -> Dict:
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator=" ", strip=True)
        res = {
            "overall_rating": None,
            "work_wellbeing_score": None,
            "ceo_approval": None,
            "ceo_name": None,
            "review_count": None,
            "wellbeing_breakdown": {}
        }

        rating_match = re.search(r'(\d\.\d)\s*★', text) or re.search(r'(\d\.\d)\s*/\s*5', text) or re.search(r'(\d\.\d)\s*out of 5', text, re.IGNORECASE)
        if rating_match:
            res["overall_rating"] = float(rating_match.group(1))

        wellbeing_match = re.search(r'(?:Work\s*wellbeing|wellbeing[^\d]{0,20})\s*(\d{2})', text, re.IGNORECASE) or re.search(r'(\d{2})\s*(?:High|Above average|Average)', text)
        if wellbeing_match:
            res["work_wellbeing_score"] = int(wellbeing_match.group(1))

        ceo_app_match = re.search(r'(\d{1,3})%\s*(?:CEO|approve)', text, re.IGNORECASE)
        if ceo_app_match:
            res["ceo_approval"] = int(ceo_app_match.group(1))

        ceo_name_match = re.search(r'CEO\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)', text)
        if ceo_name_match:
            res["ceo_name"] = ceo_name_match.group(1)

        rev_match = re.search(r'([\d,]+)\s*Reviews', text, re.IGNORECASE)
        if rev_match:
            res["review_count"] = int(rev_match.group(1).replace(',', ''))

        cats = ["Happiness", "Satisfaction", "Purpose", "Stress-free", "Flexibility", "Inclusion"]
        breakdown = {}
        for c in cats:
            m = re.search(rf'{c}\s+(High|Above average|Average|Below average|Low)', text, re.IGNORECASE)
            if m:
                breakdown[c] = m.group(1)
        res["wellbeing_breakdown"] = breakdown
        return res

    async def scrape_company(self, ticker: str) -> IndeedScore:
        slug = self.company_slugs.get(ticker) or ticker
        logger.info("Scraping Indeed for %s (%s)", ticker, slug)
        url = f"{self.BASE_URL}/cmp/{slug}"
        html = None

        try:
            if not self._curl_session:
                await self.initialize()

            await self._rate_limiter.acquire()
            proxy = await self._proxy_manager.get_proxy() if self._proxy_manager else None
            headers = build_ja4_headers({"Referer": "https://www.indeed.com/"})
            kwargs = {"headers": headers}
            if proxy:
                kwargs["proxy"] = proxy

            response = await asyncio.wait_for(
                self._curl_session.get(url, **kwargs),
                timeout=SCRAPE_TIMEOUT_S,
            )
            if response.status_code == 200:
                html = response.text
        except Exception as e:
            logger.warning("Indeed proxy scrape exception for %s: %s", ticker, e)

        if html is None:
            logger.info("Indeed curl_cffi failed for %s, trying nodriver CDP stealth...", ticker)
            try:
                html = await _nodriver_get_html(url, wait_for="div[data-testid='rating-value'], .cmp-company-rating, [class*='rating']")
            except Exception as e:
                logger.warning("nodriver CDP stealth Indeed exception for %s: %s", ticker, e)

        if html is None and self._browserless:
            try:
                result = await self._browserless.scrape(url=url, wait_until="networkidle2", timeout=30000)
                if result.success:
                    html = result.html
            except Exception as e:
                logger.warning("Indeed browserless scrape exception for %s: %s", ticker, e)

        if html is None:
            try:
                html = await _uc_driver_get_html(url)
            except Exception as e:
                logger.warning("undetected-chromedriver Indeed exception: %s", e)

        if html:
            extracted = self._extract_indeed_data(html)
            return IndeedScore(
                ticker=ticker,
                slug=slug,
                overall_rating=extracted["overall_rating"],
                work_wellbeing_score=extracted["work_wellbeing_score"],
                ceo_approval=extracted["ceo_approval"],
                ceo_name=extracted["ceo_name"],
                review_count=extracted["review_count"],
                wellbeing_breakdown=extracted["wellbeing_breakdown"],
                fetched_at=datetime.now(timezone.utc).isoformat()
            )

        return IndeedScore(
            ticker=ticker,
            slug=slug,
            overall_rating=None,
            work_wellbeing_score=None,
            ceo_approval=None,
            ceo_name=None,
            review_count=None,
            wellbeing_breakdown={},
            fetched_at=datetime.now(timezone.utc).isoformat()
        )

    async def get_job_count(self, ticker: str) -> Optional[int]:
        """Get job count for a company using JobSpy (primary) -> Adzuna Web UI fallback.
        JobSpy maintains dedicated TLS/UA evasion for Indeed — no Cloudflare challenge.
        """
        company_name = {
            "NVDA": "NVIDIA", "AMD": "Advanced Micro Devices", "MSFT": "Microsoft",
            "GOOGL": "Google", "META": "Meta", "TSLA": "Tesla", "AAPL": "Apple",
            "AMZN": "Amazon", "AVGO": "Broadcom", "INTC": "Intel",
        }.get(ticker, ticker)

        try:
            from jobspy import scrape_jobs
            jobs = scrape_jobs(
                site_name=["indeed"],
                search_term=company_name,
                results_wanted=20,
                hours_old=720,
                country_indeed="USA",
            )
            if jobs is not None:
                count = len(jobs)
                logger.info("IndeedScraper JobSpy success for %s: %d jobs", ticker, count)
                return count
        except ImportError:
            logger.warning("jobspy not installed, skipping JobSpy for IndeedScraper")
        except Exception as e:
            logger.warning("IndeedScraper JobSpy failed for %s: %s", ticker, e)

        return None

    async def scrape_all(self, tickers: List[str]) -> Dict[str, IndeedScore]:
        results = {}
        for ticker in tickers:
            results[ticker] = await self.scrape_company(ticker)
            await asyncio.sleep(random.uniform(12, 25))
        return results

    async def get_all_snapshots(self) -> Dict[str, IndeedScore]:
        tickers = list(self.company_slugs.keys())
        return await self.scrape_all(tickers)


class ComparablyScraper:
    BASE_URL = "https://www.comparably.com"

    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.comp_config = self.config.get("comparably", {})
        self.company_slugs = self.comp_config.get("company_slugs", {})
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
            "INTC": "Intel",
            "ARM": "Arm",
        }
        self._curl_session: Optional[AsyncSession] = None
        self._proxy_manager: Optional[ProxyManager] = None
        self._browserless: Optional[BrowserlessClient] = None
        self._nodriver_primary: Optional[ComparablyNodriverScraper] = None
        self._rate_limiter = RateLimiter(RateLimiterConfig(
            min_delay=12.0,
            max_delay=25.0,
            jitter=2.0,
        ))

    async def initialize(self) -> None:
        self._proxy_manager = ProxyManager(self.config)
        await self._proxy_manager.initialize()
        self._curl_session = AsyncSession(
            impersonate="chrome124",
            timeout=30
        )
        self._browserless = await create_browserless_client(self.config)
        self._nodriver_primary = await create_comparably_nodriver_scraper(self.config)
        logger.info("ComparablyScraper initialized with nodriver primary + curl_cffi chrome124 + dynamic proxy manager + browserless")

    async def close(self) -> None:
        if self._curl_session:
            await self._curl_session.close()
            self._curl_session = None
        if self._proxy_manager:
            await self._proxy_manager.close()
            self._proxy_manager = None
        if self._browserless:
            await self._browserless.close()
            self._browserless = None
        if self._nodriver_primary:
            await self._nodriver_primary.close()
            self._nodriver_primary = None

    async def __aenter__(self) -> "ComparablyScraper":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _extract_comparably_data(self, html: str) -> Dict:
        soup = BeautifulSoup(html, "html.parser")
        result: Dict = {
            "overall_rating": None,
            "culture_grade": None,
            "ceo_score": None,
            "ceo_name": None,
            "recommend_pct": None,
            "category_grades": {},
            "awards": []
        }
        page_text = soup.get_text(separator=" ", strip=True)

        rating_match = re.search(r'(\d\.\d)\s*/\s*5', page_text) or re.search(r'(\d\.\d)\s*out of 5', page_text, re.IGNORECASE)
        if rating_match:
            result["overall_rating"] = float(rating_match.group(1))

        score_grade_match = re.search(r'(\d{2})\s*/\s*100\s*or\s*([A-DF][+-]?)', page_text, re.IGNORECASE)
        if score_grade_match:
            score_100 = float(score_grade_match.group(1))
            if result["overall_rating"] is None:
                result["overall_rating"] = round(score_100 / 20.0, 2)
            result["culture_grade"] = score_grade_match.group(2).upper()

        if result["culture_grade"] is None:
            grade_match = re.search(r'Overall\s*(?:Company\s*)?Culture\s*(?:is\s*rated)?\s*([A-DF][+-]?)', page_text, re.IGNORECASE) or re.search(r'culture\s*(?:score)?\s*(?:is)?\s*([A-DF][+-]?)', page_text, re.IGNORECASE)
            if grade_match:
                result["culture_grade"] = grade_match.group(1).upper()

        ceo_score_match = re.search(r'CEO[^\d]{0,30}(\d{1,3})\s*/\s*100', page_text, re.IGNORECASE) or re.search(r'(\d{1,3})\s*/\s*100\s*CEO', page_text, re.IGNORECASE)
        if ceo_score_match:
            result["ceo_score"] = int(ceo_score_match.group(1))

        ceo_name_match = re.search(r'CEO\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)', page_text)
        if ceo_name_match:
            result["ceo_name"] = ceo_name_match.group(1)

        rec_match = re.search(r'(\d{1,2})%\s*(?:positive|recommend|green)', page_text, re.IGNORECASE) or re.search(r'(\d{1,2})%\s*were\s*positive', page_text, re.IGNORECASE)
        if rec_match:
            result["recommend_pct"] = int(rec_match.group(1))

        cat_grades = {}
        for item in soup.select("div[class*='culture'], div[class*='rating'], li, tr"):
            itxt = item.get_text(separator=" ", strip=True)
            m = re.search(r'^([A-Za-z\s]{3,25})\s+([A-DF][+-]?|\d{1,3})$', itxt)
            if m:
                cname, cgrade = m.group(1).strip(), m.group(2).strip()
                if cname.lower() not in ["overall company culture", "overall culture"]:
                    cat_grades[cname] = cgrade

        std_comparably_cats = [
            "CEO Rating", "Gender", "Diversity", "Happiness", "Future Outlook",
            "Perks and Benefits", "Professional Development", "Compensation",
            "Executive Team", "Manager", "Retention", "eNPS"
        ]
        for cat in std_comparably_cats:
            if cat not in cat_grades:
                m = re.search(rf'{cat}\s*([A-DF][+-]?|\d{{1,3}}(?:/\d{{1,3}})?|\d{{1,2}}%)', page_text, re.IGNORECASE) or re.search(rf'{cat}\s*score\s*(\d{{1,2}}(?:/\d{{1,3}})?|[A-DF][+-]?)', page_text, re.IGNORECASE)
                if m:
                    cat_grades[cat] = m.group(1).upper() if not m.group(1).isdigit() else m.group(1)
        result["category_grades"] = cat_grades

        extracted_awards = []
        for badge in soup.select("[class*='award'], [class*='badge'], img[alt*='Best'], img[alt*='Top']"):
            alt = badge.get('alt') or badge.get_text(strip=True)
            if alt and len(alt) > 5 and alt not in extracted_awards:
                extracted_awards.append(alt)

        award_matches = re.findall(r'(won\s+for\s+[^\.\<\,]{5,60}|(?:Best|Top)[^\n\r\.\<]{5,80}(?:20\d\d|Comparably))', page_text, re.IGNORECASE)
        for am in award_matches:
            clean_am = re.sub(r'^won\s+for\s+', '', am.strip(), flags=re.IGNORECASE)
            if clean_am and clean_am not in extracted_awards:
                extracted_awards.append(clean_am)

        result["awards"] = extracted_awards
        logger.info("Extracted Comparably data: rating=%s, grade=%s, ceo=%s, cat_count=%d, awards_count=%d",
                    result["overall_rating"], result["culture_grade"], result["ceo_score"],
                    len(result["category_grades"]), len(result["awards"]))
        return result

    async def _ddg_search_comparably(self, company_name: str, slug: str) -> Optional[Dict]:
        """Parse Comparably metrics directly from DuckDuckGo search indexing snippets."""
        try:
            url = f"https://html.duckduckgo.com/html/?q=Comparably+{company_name.replace(' ', '+')}+culture+rating+CEO+score"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9"
            }
            if not self._curl_session:
                await self.initialize()

            response = await asyncio.wait_for(
                self._curl_session.get(url, headers=headers),
                timeout=SCRAPE_TIMEOUT_S
            )
            if response.status_code != 200:
                return None

            soup = BeautifulSoup(response.text, "html.parser")
            snippets = [a.get_text(strip=True) for a in soup.select("a.result__snippet")]
            combined_text = " ".join(snippets)

            if not combined_text:
                return None

            return self._extract_comparably_data(combined_text)
        except Exception as e:
            logger.warning("DDG search exception for Comparably %s: %s", slug, e)
            return None

    async def _bing_search_comparably(self, company_name: str, slug: str) -> Optional[Dict]:
        """Parse Comparably data from Bing search results through proxy chain"""
        try:
            query = f"site:comparably.com/companies/{slug} culture rating CEO score"
            url = f"https://www.bing.com/search?q={query.replace(' ', '+')}"

            await self._rate_limiter.acquire()
            proxy = await self._proxy_manager.get_proxy() if self._proxy_manager else None
            headers = build_ja4_headers()

            kwargs = {"headers": headers}
            if proxy:
                kwargs["proxy"] = proxy

            response = await asyncio.wait_for(
                self._curl_session.get(url, **kwargs),
                timeout=SCRAPE_TIMEOUT_S,
            )
            if response.status_code != 200:
                return None

            return self._extract_comparably_data(response.text)
        except Exception as e:
            logger.warning(f"Bing search failed for Comparably: {e}")
            return None

    async def scrape_company(self, ticker: str) -> ComparablyScore:
        slug = self.company_slugs.get(ticker) or ticker.lower()
        company_name = self.company_names.get(ticker, ticker)
        logger.info("Scraping Comparably for %s (%s) via nodriver primary", ticker, slug)
        url = f"{self.BASE_URL}/companies/{slug}"
        html = None

        # Try nodriver primary scraper FIRST (JavaScript execution for Cloudflare)
        if not hasattr(self, '_nodriver_primary') or self._nodriver_primary is None:
            self._nodriver_primary = await create_comparably_nodriver_scraper(self.config)
        
        try:
            nodriver_result = await self._nodriver_primary.scrape_company(ticker)
            if nodriver_result.overall_rating is not None or nodriver_result.culture_grade is not None:
                logger.info("Nodriver primary Comparably scrape success for %s: overall=%s, culture=%s", 
                           ticker, nodriver_result.overall_rating, nodriver_result.culture_grade)
                return nodriver_result
        except Exception as e:
            logger.warning("Nodriver primary Comparably scrape failed for %s: %s", ticker, e)

        try:
            if not self._curl_session:
                await self.initialize()

            await self._rate_limiter.acquire()
            proxy = await self._proxy_manager.get_proxy() if self._proxy_manager else None
            headers = build_ja4_headers({"Referer": "https://www.comparably.com/"})
            kwargs = {"headers": headers}
            if proxy:
                kwargs["proxy"] = proxy

            response = await asyncio.wait_for(
                self._curl_session.get(url, **kwargs),
                timeout=SCRAPE_TIMEOUT_S,
            )
            if response.status_code == 200:
                html = response.text
        except Exception as e:
            logger.warning("Comparably proxy scrape exception for %s: %s", ticker, e)

        if html is None:
            logger.info("Comparably proxy scrape failed for %s, trying direct connection without proxy...", ticker)
            try:
                headers = build_ja4_headers({"Referer": "https://www.comparably.com/"})
                response = await asyncio.wait_for(
                    self._curl_session.get(url, headers=headers),
                    timeout=SCRAPE_TIMEOUT_S,
                )
                if response.status_code == 200:
                    html = response.text
            except Exception as e:
                logger.warning("Comparably direct scrape exception for %s: %s", ticker, e)

        if html is None:
            logger.info("Comparably direct scrape failed for %s, attempting DDG search fallback...", ticker)
            try:
                ddg_data = await self._ddg_search_comparably(company_name, slug)
                if ddg_data and (ddg_data.get("overall_rating") or ddg_data.get("culture_grade")):
                    return ComparablyScore(
                        ticker=ticker,
                        slug=slug,
                        overall_rating=ddg_data["overall_rating"],
                        culture_grade=ddg_data["culture_grade"],
                        ceo_score=ddg_data["ceo_score"],
                        ceo_name=ddg_data["ceo_name"],
                        recommend_pct=ddg_data["recommend_pct"],
                        category_grades=ddg_data["category_grades"],
                        awards=ddg_data["awards"],
                        fetched_at=datetime.now(timezone.utc).isoformat()
                    )
            except Exception as e:
                logger.warning("Comparably DDG search fallback exception for %s: %s", ticker, e)

        if html is None:
            logger.info("Comparably direct scrape failed for %s, trying nodriver stealth bypass...", ticker)
            try:
                html = await _nodriver_get_html(url)
            except Exception as e:
                logger.warning("nodriver Comparably exception: %s", e)

        if html is None and self._browserless:
            logger.info("Comparably nodriver failed for %s, trying browserless fallback...", ticker)
            try:
                result = await self._browserless.scrape(
                    url=url,
                    wait_until="networkidle2",
                    timeout=30000,
                    use_cache=False
                )
                if result.success:
                    html = result.html
            except Exception as e:
                logger.warning("Comparably browserless scrape exception for %s: %s", ticker, e)

        if html is None:
            logger.info("Comparably browserless failed for %s, trying undetected-chromedriver...", ticker)
            try:
                html = await _uc_driver_get_html(url)
            except Exception as e:
                logger.warning("undetected-chromedriver Comparably exception: %s", e)

        if html:
            extracted = self._extract_comparably_data(html)
            return ComparablyScore(
                ticker=ticker,
                slug=slug,
                overall_rating=extracted["overall_rating"],
                culture_grade=extracted["culture_grade"],
                ceo_score=extracted["ceo_score"],
                ceo_name=extracted["ceo_name"],
                recommend_pct=extracted["recommend_pct"],
                category_grades=extracted["category_grades"],
                awards=extracted["awards"],
                fetched_at=datetime.now(timezone.utc).isoformat()
            )

        return ComparablyScore(
            ticker=ticker,
            slug=slug,
            overall_rating=None,
            culture_grade=None,
            ceo_score=None,
            ceo_name=None,
            recommend_pct=None,
            category_grades={},
            awards=[],
            fetched_at=datetime.now(timezone.utc).isoformat()
        )

    async def scrape_all(self, tickers: List[str]) -> Dict[str, ComparablyScore]:
        results = {}
        for ticker in tickers:
            results[ticker] = await self.scrape_company(ticker)
            await asyncio.sleep(random.uniform(2, 5))
        return results


class CorpAuditEngine:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.glassdoor_scraper = GlassdoorScraper(config_dict)
        self.indeed_scraper = IndeedScraper(config_dict)
        self.g2_scraper = G2EmployerScraper(config_dict)
        self.validation_gate = CrossValidationGate(config_dict)

    async def initialize(self) -> None:
        await self.glassdoor_scraper.initialize()
        await self.indeed_scraper.initialize()
        await self.g2_scraper.initialize()
        logger.info("CorpAuditEngine initialized (Glassdoor + Indeed + G2 via rotating proxy pool)")

    async def close(self) -> None:
        await self.glassdoor_scraper.close()
        await self.indeed_scraper.close()
        await self.g2_scraper.close()
        await _close_nodriver_pool()

    async def __aenter__(self) -> "CorpAuditEngine":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def audit_ticker(self, ticker: str) -> Dict:
        logger.info("Running corp audit for %s", ticker)

        glassdoor_result = await self.glassdoor_scraper.scrape_company(ticker)
        await asyncio.sleep(random.uniform(1, 3))

        indeed_result = await self.indeed_scraper.scrape_company(ticker)
        await asyncio.sleep(random.uniform(1, 3))

        g2_result = await self.g2_scraper.scrape_company(ticker)

        secondary_100 = None
        if indeed_result.overall_rating is not None:
            secondary_100 = indeed_result.overall_rating * 20.0
        elif g2_result.overall_rating is not None:
            secondary_100 = g2_result.overall_rating * 20.0

        validation_result = self.validation_gate.evaluate(
            glassdoor_raw=glassdoor_result.raw_score,
            comparably_badge=secondary_100
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
                "category_ratings": glassdoor_result.category_ratings or {},
                "awards": glassdoor_result.awards or [],
                "fetched_at": glassdoor_result.fetched_at
            },
            "indeed": {
                "slug": indeed_result.slug,
                "overall_rating": indeed_result.overall_rating,
                "work_wellbeing_score": indeed_result.work_wellbeing_score,
                "ceo_approval": indeed_result.ceo_approval,
                "ceo_name": indeed_result.ceo_name,
                "review_count": indeed_result.review_count,
                "wellbeing_breakdown": indeed_result.wellbeing_breakdown,
                "fetched_at": indeed_result.fetched_at
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
                "normalized_secondary": validation_result.normalized_comparably,
                "weighted_score": validation_result.weighted_score,
                "glassdoor_weight": 0.75,
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


async def create_indeed_scraper(config_dict: dict = None) -> IndeedScraper:
    scraper = IndeedScraper(config_dict)
    await scraper.initialize()
    return scraper


async def create_comparably_scraper(config_dict: dict = None) -> ComparablyScraper:
    scraper = ComparablyScraper(config_dict)
    await scraper.initialize()
    return scraper


# Backward-compat aliases
G2Scraper = G2EmployerScraper
G2Score = G2EmployerScore
ComparablyBadges = ComparablyScore
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