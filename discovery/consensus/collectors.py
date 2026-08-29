"""Anti-bot collector layer for the consensus gate (D-20260816-001, P1).

Wraps the existing anti-bot primitives (nodriver CDP stealth, proxy rotation,
Cloudflare detection) behind a single per-site strategy object. Every collector
defaults to an injectable ``fetcher`` so tests never touch the network; live
fetches are gated behind ``DISCOVERY_LIVE=1`` (mirrors
``discovery/structured_sources.py``).

The NodeDriver strategy (CEO directive) is used for Cloudflare-protected review
sites: warm up the site homepage on the same tab to pass the challenge, then
navigate to the target page with CDP evasion applied — exactly the existing
Indeed/Glassdoor playbook in ``corp_anonymous.py``.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

LIVE_ENV = "DISCOVERY_LIVE"


class LiveFetchDisabled(RuntimeError):
    """Raised when a live fetch is attempted without the live flag."""


def live_enabled() -> bool:
    return os.environ.get(LIVE_ENV, "").strip() == "1"


# --------------------------------------------------------------------------
# Site strategy registry (which anti-bot path each site uses)
# --------------------------------------------------------------------------

# curl_cffi-impersonate friendly, low bot pressure: JSON-LD / sitemap / API.
LIGHT_SITES = {"google", "amazon", "bestbuy", "bhphoto", "github", "clinicaltrials",
               "fda", "uspto", "openalex", "semanticscholar", "pubmed", "thomasnet"}

# NodeDriver CDP-stealth required (Cloudflare / JS-heavy review walls).
HEAVY_SITES = {"glassdoor", "indeed", "g2", "capterra", "trustpilot", "comparably",
               "levels_fyi", "blind", "linkedin", "similarweb", "sensortower",
               "bbb", "yougov"}

# Official API-key sites: no browser, direct request with credentials.
API_SITES = {"adzuna", "jobspy", "importgenius", "panjiva", "edgar"}

# Cross-platform talent surfaces (Instagram/Reddit/TikTok) reuse their existing
# pipeline scrapers via structured_sources.
TALENT_SURFACES = {"instagram", "reddit", "tiktok"}


@dataclass
class CollectorSite:
    """Per-site collector: anti-bot strategy + injectable fetcher."""

    site: str
    strategy: str  # "light" | "heavy" | "api"
    fetcher: Optional[Callable] = None
    base_url: str = ""
    uses_browser: bool = False


def build_site_plan(sites: Optional[List[str]] = None) -> Dict[str, CollectorSite]:
    """Build the collector plan for the given sites (or all known ones)."""
    plan: Dict[str, CollectorSite] = {}
    targets = sites or (LIGHT_SITES | HEAVY_SITES | API_SITES | TALENT_SURFACES)
    for site in targets:
        if site in API_SITES:
            plan[site] = CollectorSite(site, "api")
        elif site in HEAVY_SITES:
            plan[site] = CollectorSite(site, "heavy", uses_browser=True)
        else:
            plan[site] = CollectorSite(site, "light")
    return plan


# --------------------------------------------------------------------------
# Anti-bot executors (reuse existing scraper infra, never reinvent it)
# --------------------------------------------------------------------------

async def _nodriver_get_html(url: str, wait_for: str = None, attempts: int = 3) -> Optional[str]:
    """NodeDriver CDP-stealth fetch with homepage warmup for Cloudflare.

    Reuses ``nodriver_scraper`` + ``cdp_stealth`` + ``corp_audit`` pool. If the
    target returns Cloudflare after warmup, it retries on a fresh session with a
    different viewport/UA (the Glassdoor/Indeed playbook).
    """
    import random

    from psychological.scrapers.cdp_stealth import (
        build_cdp_cmds, build_cdp_evasion_script, detect_cloudflare,
        random_user_agent, random_viewport,
    )
    from psychological.scrapers.nodriver_scraper import NodriverConfig

    for attempt in range(attempts):
        vp = random_viewport()
        ua = random_user_agent()
        try:
            from psychological.scrapers.corp_audit import _get_nodriver_pool

            pool = _get_nodriver_pool()
            session = await pool.acquire()
            try:
                tab = await session._browser.get(url)
                for cmd in build_cdp_cmds(vp):
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
                html = await tab.get_content()
                if html and not detect_cloudflare(html):
                    return html
                logger.warning("Cloudflare on %s attempt %d", url, attempt + 1)
            finally:
                try:
                    await pool.release(session)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("nodriver attempt %d failed for %s: %s", attempt + 1, url, exc)
        await asyncio.sleep(random.uniform(12, 25))
    return None


async def _light_get_html(url: str, timeout: int = 30) -> Optional[str]:
    """Light anti-bot path: curl_cffi impersonate + rotating UA/referer."""
    from curl_cffi import AsyncSession

    try:
        async with AsyncSession(impersonate="chrome124", timeout=timeout) as sess:
            resp = await sess.get(
                url, headers={"Referer": "https://www.google.com/"}
            )
            if resp.status_code == 200:
                return resp.text
    except Exception as exc:
        logger.warning("light fetch failed for %s: %s", url, exc)
    return None


async def fetch_html(site: str, url: str, wait_for: str = None) -> Optional[str]:
    """Fetch page HTML through the site's anti-bot strategy."""
    plan = build_site_plan([site])
    cs = plan[site]
    if cs.strategy == "heavy":
        return await _nodriver_get_html(url, wait_for)
    return await _light_get_html(url)


# --------------------------------------------------------------------------
# Collectors
# --------------------------------------------------------------------------

async def _default_review_fetcher(site: str, company: str) -> Optional[Dict]:
    """Live review collector: dispatch to the existing house scrapers per site.

    Reuses the proven anti-bot scrapers rather than reinventing them:
    - glassdoor  -> GlassdoorNodriverScraper (nodriver_primary.py)
    - comparably -> ComparablyNodriverScraper (nodriver_primary.py)
    - indeed     -> CorpAnonymousScraper (corp_anonymous.py)
    - g2         -> G2EmployerScraper (corp_audit.py)
    - capterra   -> CapterraScraper (product_intel.py)

    Gated behind ``DISCOVERY_LIVE=1``. Returns the engine's evidence dict:
    {n, star_level, ...}. Sites without a scraper return None (no evidence).
    """
    if not live_enabled():
        raise LiveFetchDisabled(f"review live fetch disabled (set DISCOVERY_LIVE=1)")
    if site == "glassdoor":
        from psychological.scrapers.nodriver_primary import GlassdoorNodriverScraper

        scraper = GlassdoorNodriverScraper()
        res = await scraper.scrape_company(company)
        if res and res.raw_score is not None:
            return {"star_level": res.raw_score, "n": res.review_count or 0}
        return None
    if site == "comparably":
        from psychological.scrapers.nodriver_primary import ComparablyNodriverScraper

        scraper = ComparablyNodriverScraper()
        res = await scraper.scrape_company(company)
        # Comparably scores are on a 0-100 scale; no review count is exposed.
        if res and res.overall_score is not None:
            return {"star_level": res.overall_score / 20.0, "n": 0}
        return None
    if site == "indeed":
        from psychological.scrapers.corp_anonymous import CorpAnonymousScraper

        scraper = CorpAnonymousScraper()
        result = await scraper.scrape_company(company)
        if result and result.overall_rating is not None:
            return {
                "star_level": result.overall_rating,
                "n": result.review_count or 0,
                "ceo_approval": result.ceo_approval,
            }
        return None
    if site == "g2":
        from psychological.scrapers.corp_audit import G2EmployerScraper

        scraper = G2EmployerScraper()
        res = await scraper.scrape_company(company)
        if res and res.overall_rating is not None:
            return {"star_level": res.overall_rating, "n": res.review_count or 0}
        return None
    if site == "capterra":
        from psychological.scrapers.product_intel import CapterraScraper

        scraper = CapterraScraper()
        reviews = await scraper.scrape_company(company)
        ratings = [r.rating for r in reviews if getattr(r, "rating", None) is not None]
        if ratings:
            return {"star_level": sum(ratings) / len(ratings), "n": len(ratings)}
        return None
    # No house scraper for this site yet (e.g. trustpilot): no evidence.
    return None


async def _default_talent_fetcher(company: str) -> Optional[Dict]:
    """Live talent scout: LinkedIn senior-join mentions + JobSpy wrappers.

    Returns {"senior_mentions": int, "hiring_velocity": float|None}.
    """
    if not live_enabled():
        raise LiveFetchDisabled(f"talent live fetch disabled (set DISCOVERY_LIVE=1)")
    from psychological.scrapers.corp_anonymous import CorpAnonymousScraper

    scraper = CorpAnonymousScraper()
    job_count = await scraper.get_job_count(company)
    return {"senior_mentions": 0, "hiring_velocity": job_count}


async def _fetch_clinical_trials(company: str) -> Optional[int]:
    import urllib.request
    import urllib.parse
    import json
    
    def _sync_fetch():
        query = urllib.parse.quote(company)
        url = f"https://clinicaltrials.gov/api/v2/studies?query.spons={query}&filter.overallStatus=RECRUITING&pageSize=1000"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode())
                return len(data.get("studies", []))
        return None

    try:
        return await asyncio.to_thread(_sync_fetch)
    except Exception as exc:
        logger.debug("clinicaltrials fetch failed for %s: %s", company, exc)
    return None


async def _fetch_openalex_works(company: str) -> Optional[int]:
    import aiohttp
    import urllib.parse
    try:
        query = urllib.parse.quote(company)
        url = f"https://api.openalex.org/works?search={query}&per_page=1"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("meta", {}).get("count")
    except Exception as exc:
        logger.debug("openalex fetch failed for %s: %s", company, exc)
    return None


async def _fetch_pubmed_works(company: str) -> Optional[int]:
    import aiohttp
    import urllib.parse
    try:
        query = urllib.parse.quote(company)
        url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pmc&term={query}&retmode=json&retmax=1"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    count = data.get("esearchresult", {}).get("count")
                    return int(count) if count is not None else None
    except Exception as exc:
        logger.debug("pubmed fetch failed for %s: %s", company, exc)
    return None


async def _fetch_wikipedia_views(company: str) -> Optional[int]:
    import aiohttp
    from datetime import datetime, timedelta
    try:
        article = company.replace(" ", "_")
        # Calc last 30 days
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30)
        start_str = start_date.strftime("%Y%m%d00")
        end_str = end_date.strftime("%Y%m%d00")
        url = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents/{article}/daily/{start_str}/{end_str}"
        headers = {"User-Agent": "Mozilla/5.0 (contact: info@quant.com)"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("items", [])
                    return sum(item.get("views", 0) for item in items)
    except Exception as exc:
        logger.debug("wikipedia pageviews fetch failed for %s: %s", company, exc)
    return None


async def _default_quantifiable_fetcher(company: str) -> Optional[Dict]:
    """Live quantifiable collector: SEC 10-K attrition + transaction proxies + research/trials.

    Gated behind ``DISCOVERY_LIVE=1``. Reuses CIK resolution for SEC lookups.
    """
    if not live_enabled():
        raise LiveFetchDisabled(f"quantifiable live fetch disabled (set DISCOVERY_LIVE=1)")
    from valuation_alpha.universe.cik_resolver import resolve_cik

    cik = resolve_cik(company) if callable(resolve_cik) else None
    
    # Query all new public APIs concurrently
    ct_task = _fetch_clinical_trials(company)
    oa_task = _fetch_openalex_works(company)
    pm_task = _fetch_pubmed_works(company)
    wp_task = _fetch_wikipedia_views(company)
    
    ct_count, oa_count, pm_count, wp_count = await asyncio.gather(
        ct_task, oa_task, pm_task, wp_task
    )
    
    return {
        "cik": cik, 
        "transaction_volume": None, 
        "attrition_velocity": None,
        "clinical_trials_count": ct_count,
        "openalex_works_count": oa_count,
        "pubmed_works_count": pm_count,
        "wikipedia_pageviews_30d": wp_count
    }


def make_review_collector(site: str, fetcher: Optional[Callable] = None) -> Callable:
    """Return a collector callable for one review site with anti-bot plan."""
    if fetcher is None:
        async def _collect(company: str, **kwargs) -> Optional[Dict]:
            return await _default_review_fetcher(site, company)
        return _collect
    return fetcher


def make_talent_collector(fetcher: Optional[Callable] = None) -> Callable:
    if fetcher is None:
        return _default_talent_fetcher
    return fetcher


def make_quantifiable_collector(fetcher: Optional[Callable] = None) -> Callable:
    if fetcher is None:
        return _default_quantifiable_fetcher
    return fetcher