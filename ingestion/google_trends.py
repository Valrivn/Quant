"""Production-Grade Google Trends Scraper & Ingestion Pipeline.

Supports company/product-level tracking with automatic keyword tier generation,
anti-scrape protections (rotating residential proxy pool, cookie jars, request budgeting,
fingerprint rotation, exponential backoff, CAPTCHA handling), parallel job queue,
progress checkpointing, and incremental Parquet writes to the Data Lake.

Modules supported:
- interest_over_time
- interest_by_region
- related_queries (top / rising)
- related_topics (top / rising)
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import logging
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

from ingestion.data_lake import DataLakeStore

logger = logging.getLogger(__name__)

# Pool of realistic User-Agent headers & browser fingerprints
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

BROWSER_PROFILES = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Accept-Language": "en-US,en;q=0.9",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not.A/Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "Accept-Language": "en-US,en;q=0.9",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
        "Accept-Language": "en-US,en;q=0.5",
    },
]

# Sector macro keyword defaults
SECTOR_MACRO_TEMPLATES: Dict[str, List[str]] = {
    "semiconductors": ["semiconductor demand", "chip shortage", "AI chips", "GPU demand", "wafer capacity"],
    "technology": ["cloud spending", "enterprise software", "AI cloud", "SaaS demand", "cybersecurity demand"],
    "software": ["cloud spending", "enterprise software", "AI cloud", "SaaS demand", "software license"],
    "automotive": ["EV demand", "electric vehicle adoption", "lithium battery cost", "auto sales", "EV charging"],
    "ev": ["EV demand", "electric vehicle adoption", "lithium battery cost", "auto sales", "EV charging"],
    "consumer electronics": ["smartphone demand", "consumer spending", "PC market", "wearables demand"],
    "retail": ["e-commerce growth", "consumer confidence", "holiday shopping", "retail sales"],
    "e-commerce": ["e-commerce growth", "consumer confidence", "holiday shopping", "retail sales"],
    "financials": ["interest rates", "credit demand", "banking sector", "loan growth"],
    "banking": ["interest rates", "credit demand", "banking sector", "loan growth"],
    "healthcare": ["pharma innovation", "drug approval", "healthcare spending", "biotech demand"],
    "energy": ["oil demand", "renewable energy", "energy transition", "power grid"],
    "default": ["market demand", "industry growth", "sector outlook", "supply chain"],
}


# ============================================================================
# 1. Company & Keyword Framework
# ============================================================================

@dataclass
class CompanySpec:
    """Specification for a company to track via Google Trends."""
    ticker: str
    name: str
    sector: str
    ceo: Optional[str] = None
    products: List[str] = field(default_factory=list)
    competitors: List[str] = field(default_factory=list)
    custom_keywords: Dict[str, List[str]] = field(default_factory=dict)


class KeywordGenerator:
    """Generates keyword tiers (macro, company, product, competitive) for companies."""

    DEFAULT_LIMITS = {
        "macro": 3,
        "company": 4,
        "product": 5,
        "competitive": 3,
    }

    @classmethod
    def generate_keywords(
        cls,
        company: CompanySpec,
        tier_limits: Optional[Dict[str, int]] = None,
    ) -> Dict[str, List[str]]:
        """Auto-generate keyword tiers for a given company.

        Returns:
            Dict[str, List[str]]: tier_name -> list of keywords
        """
        limits = {**cls.DEFAULT_LIMITS, **(tier_limits or {})}
        tiers: Dict[str, List[str]] = {}

        # 1. Macro tier
        if "macro" in company.custom_keywords:
            macro_kws = list(company.custom_keywords["macro"])
        else:
            sec_lower = company.sector.lower().strip()
            matched_template = None
            for s_key, t_kws in SECTOR_MACRO_TEMPLATES.items():
                if s_key in sec_lower:
                    matched_template = t_kws
                    break
            if not matched_template:
                matched_template = SECTOR_MACRO_TEMPLATES["default"]
            macro_kws = list(matched_template)

        tiers["macro"] = macro_kws[: limits.get("macro", 3)]

        # 2. Company tier
        if "company" in company.custom_keywords:
            comp_kws = list(company.custom_keywords["company"])
        else:
            comp_kws = [company.ticker, company.name]
            if company.ceo:
                comp_kws.append(f"{company.name} CEO")
            comp_kws.append(f"{company.name} earnings")

        tiers["company"] = cls._dedupe(comp_kws)[: limits.get("company", 4)]

        # 3. Product tier
        if "product" in company.custom_keywords:
            prod_kws = list(company.custom_keywords["product"])
        elif company.products:
            prod_kws = list(company.products)
        else:
            prod_kws = [f"{company.name} products", f"{company.name} pricing"]

        tiers["product"] = cls._dedupe(prod_kws)[: limits.get("product", 5)]

        # 4. Competitive tier
        if "competitive" in company.custom_keywords:
            comp_kws = list(company.custom_keywords["competitive"])
        else:
            comp_kws = []
            for comp in company.competitors:
                comp_kws.append(f"{company.name} vs {comp}")
            # If products and competitors exist, add product vs product comparison
            if company.products and company.competitors:
                comp_kws.append(f"{company.products[0]} vs {company.competitors[0]}")

            if not comp_kws:
                comp_kws = [f"{company.name} competitors", f"{company.name} alternatives"]

        tiers["competitive"] = cls._dedupe(comp_kws)[: limits.get("competitive", 3)]

        return tiers

    @staticmethod
    def _dedupe(items: List[str]) -> List[str]:
        seen = set()
        res = []
        for item in items:
            cleaned = item.strip()
            if cleaned and cleaned.lower() not in seen:
                seen.add(cleaned.lower())
                res.append(cleaned)
        return res


# ============================================================================
# 2. Anti-Scrape Protections: Proxy Pool & Request Budgeting
# ============================================================================

def build_proxy_url(proxy_item: Union[str, Dict[str, Any]]) -> Optional[str]:
    """Helper to convert proxy dict or string to HTTP Basic Auth proxy URL string.
    
    Supports resolution of ENV:VAR_NAME pattern for secure credential handling.
    """
    if not proxy_item:
        return None
    if isinstance(proxy_item, str):
        url_str = proxy_item
        # Check env var replacement in string if present
        if url_str.startswith("ENV:"):
            var_name = url_str[4:]
            return os.getenv(var_name)
        return url_str
    if isinstance(proxy_item, dict):
        host = proxy_item.get("host")
        port = proxy_item.get("port")
        user = proxy_item.get("user", "")
        password = proxy_item.get("pass", proxy_item.get("password", ""))

        if user.startswith("ENV:"):
            user = os.getenv(user[4:], "")
        if password.startswith("ENV:"):
            password = os.getenv(password[4:], "")

        if not host:
            return None

        if user and password:
            return f"http://{user}:{password}@{host}:{port}"
        elif user:
            return f"http://{user}@{host}:{port}"
        else:
            return f"http://{host}:{port}"
    return None


class ProxyTracker:
    """Tracks rate limits, requests/hour budget, and cooldown state for a single proxy."""

    def __init__(
        self,
        proxy_input: Optional[Union[str, Dict[str, Any]]] = None,
        max_requests_per_hour: int = 120,
        proxy_url: Optional[Union[str, Dict[str, Any]]] = None,
    ):
        target_input = proxy_input if proxy_input is not None else proxy_url
        self.proxy_input = target_input
        self.proxy_url = build_proxy_url(target_input) if target_input else None
        self.provider = target_input.get("provider", "generic") if isinstance(target_input, dict) else "generic"
        self.max_requests_per_hour = max_requests_per_hour
        self.request_timestamps: deque = deque()
        self.cooldown_until: float = 0.0
        self.success_count: int = 0
        self.error_count: int = 0
        self.captcha_count: int = 0
        self.session: requests.Session = requests.Session()
        self._lock = threading.Lock()
        self._init_session()

    def _init_session(self) -> None:
        """Initialize session headers and warm up cookies."""
        headers = random.choice(BROWSER_PROFILES)
        self.session.headers.update(headers)
        if self.proxy_url:
            self.session.proxies = {"http": self.proxy_url, "https": self.proxy_url}
        try:
            res = self.session.get("https://trends.google.com/trends/", timeout=12)
            if res.status_code == 200:
                logger.debug(f"Session initialized for proxy {self.proxy_url or 'DIRECT'} (provider={self.provider})")
        except Exception as exc:
            logger.debug(f"Session warmup note for proxy {self.proxy_url or 'DIRECT'}: {exc}")

    def is_available(self) -> bool:
        """Check if proxy is out of cooldown and within hourly request budget."""
        with self._lock:
            now = time.time()
            if now < self.cooldown_until:
                return False

            # Purge timestamps older than 1 hour (3600s)
            while self.request_timestamps and (now - self.request_timestamps[0]) > 3600:
                self.request_timestamps.popleft()

            return len(self.request_timestamps) < self.max_requests_per_hour

    def record_request(self) -> None:
        """Record a sent request timestamp."""
        with self._lock:
            self.request_timestamps.append(time.time())

    def record_captcha_or_block(self, cooldown_seconds: float = 300.0) -> None:
        """Put proxy in cooldown following CAPTCHA or 429/403 block."""
        with self._lock:
            self.captcha_count += 1
            self.cooldown_until = time.time() + cooldown_seconds
            logger.warning(
                f"Proxy {self.proxy_url or 'DIRECT'} placed in cooldown for {cooldown_seconds}s "
                f"(total CAPTCHAs/blocks={self.captcha_count})"
            )
            # Re-initialize session to reset cookies
            self._init_session()

    def record_success(self) -> None:
        with self._lock:
            self.success_count += 1

    def record_error(self) -> None:
        with self._lock:
            self.error_count += 1


class ProxyPool:
    """Manages a pool of residential proxies with rate limit budgets and session persistence."""

    def __init__(
        self,
        proxy_urls: Optional[List[Union[str, Dict[str, Any]]]] = None,
        max_requests_per_hour_per_proxy: int = 120,
    ):
        self.proxy_urls = proxy_urls or []
        self.max_requests_per_hour = max_requests_per_hour_per_proxy
        self._trackers: List[ProxyTracker] = []
        self._lock = threading.Lock()
        self._rr_index = 0

        if self.proxy_urls:
            for url_or_dict in self.proxy_urls:
                self._trackers.append(ProxyTracker(url_or_dict, max_requests_per_hour_per_proxy))
        else:
            # Direct connection fallback tracker
            self._trackers.append(ProxyTracker(None, max_requests_per_hour_per_proxy))

    def checkout_proxy(self) -> ProxyTracker:
        """Acquire an available proxy tracker from pool. Blocks/waits if all are cooling down."""
        start_wait = time.time()
        while True:
            with self._lock:
                n = len(self._trackers)
                for _ in range(n):
                    tracker = self._trackers[self._rr_index]
                    self._rr_index = (self._rr_index + 1) % n
                    if tracker.is_available():
                        return tracker

            # If all proxies busy or cooling down, wait briefly and retry
            time.sleep(2.0)
            if time.time() - start_wait > 120.0:
                logger.warning("Proxy pool checkout timeout exceeded 120s, utilizing round-robin fallback.")
                with self._lock:
                    return self._trackers[0]


# ============================================================================
# 3. Enhanced Core Scraper (GoogleTrendsScraper)
# ============================================================================

class GoogleTrendsScraper:
    """Scraper for Google Trends data via direct web endpoint interaction."""

    EXPLORE_URL = "https://trends.google.com/trends/api/explore"
    MULTILINE_URL = "https://trends.google.com/trends/api/widgetdata/multiline"
    GEO_URL = "https://trends.google.com/trends/api/widgetdata/comparedgeo"
    RELATED_URL = "https://trends.google.com/trends/api/widgetdata/relatedsearches"

    def __init__(
        self,
        min_delay: float = 2.0,
        max_delay: float = 8.0,
        max_retries: int = 5,
        proxy_pool: Optional[ProxyPool] = None,
    ):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.proxy_pool = proxy_pool or ProxyPool()
        self._last_request_time = 0.0

        # Maintain single fallback session for backwards compatibility when no proxy pool passed
        self.session = requests.Session()
        self._init_session()

    def _init_session(self) -> None:
        """Initialize session headers and obtain cookies from Google Trends main page."""
        profile = random.choice(BROWSER_PROFILES)
        self.session.headers.update(profile)
        try:
            res = self.session.get("https://trends.google.com/trends/", timeout=15)
            if res.status_code == 200:
                logger.debug("Google Trends default session warmed up.")
        except Exception as exc:
            logger.warning(f"Google Trends session warmup warning: {exc}")

    def _throttle(self) -> None:
        """Apply randomized delay (2-8s) between requests to avoid rate limits."""
        elapsed = time.time() - self._last_request_time
        target_wait = random.uniform(self.min_delay, self.max_delay)
        if elapsed < target_wait:
            time.sleep(target_wait - elapsed)
        self._last_request_time = time.time()

    def _clean_json_response(self, text: str) -> Dict[str, Any]:
        """Strip Google's security prefix ')]}'\\n' and parse JSON response."""
        cleaned = text.lstrip(")]}'\n")
        return json.loads(cleaned)

    def _check_captcha_or_rate_limit(self, response: requests.Response) -> bool:
        """Detect whether response is a 429, 403, or CAPTCHA challenge page."""
        if response.status_code in (429, 403):
            return True
        text_lower = response.text.lower()
        if "captcha" in text_lower or "recaptcha" in text_lower or "unusual traffic" in text_lower or "google.com/sorry" in text_lower:
            return True
        return False

    def _http_get(self, url: str, params: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        """Execute GET request with proxy checkout, budget tracking, rate limits, and retries.

        Returns:
            Tuple[Dict[str, Any], str]: (parsed_json_data, proxy_used_string)
        """
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            tracker = self.proxy_pool.checkout_proxy()
            proxy_str = tracker.proxy_url or "DIRECT"

            # Rotate user-agent header profile per request
            profile = random.choice(BROWSER_PROFILES)
            tracker.session.headers.update(profile)

            try:
                tracker.record_request()
                res = tracker.session.get(url, params=params, timeout=30)

                if self._check_captcha_or_rate_limit(res):
                    tracker.record_captcha_or_block(cooldown_seconds=180.0 * attempt)
                    backoff = (self.min_delay * (2 ** attempt)) + random.uniform(1.0, 3.0)
                    logger.warning(
                        f"Google Trends rate limit / CAPTCHA detected (status={res.status_code}, proxy={proxy_str}). "
                        f"Attempt {attempt}/{self.max_retries}. Backing off {backoff:.2f}s..."
                    )
                    time.sleep(backoff)
                    continue

                res.raise_for_status()
                data = self._clean_json_response(res.text)
                tracker.record_success()
                return data, proxy_str

            except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
                tracker.record_error()
                if attempt == self.max_retries:
                    logger.error(f"Google Trends GET {url} failed after {self.max_retries} attempts: {exc}")
                    raise
                backoff = (self.min_delay * (2 ** attempt)) + random.uniform(1.0, 3.0)
                time.sleep(backoff)

        raise RuntimeError(f"Google Trends request failed: {url}")

    def _safe_http_get(self, url: str, params: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        """Wrapper around _http_get ensuring compatibility when _http_get is mocked to return a dict."""
        res = self._http_get(url, params)
        if isinstance(res, tuple):
            return res
        return res, "DIRECT"

    def _get_widgets(
        self,
        keywords: List[str],
        timeframe: str = "today 5-y",
        geo: str = "US",
    ) -> Tuple[Dict[str, Dict[str, Any]], str]:
        """Fetch widget configurations and tokens for keyword batch (max 5 keywords)."""
        if len(keywords) > 5:
            raise ValueError("Google Trends supports maximum 5 keywords per request batch.")

        comparison_items = [{"keyword": kw, "geo": geo, "time": timeframe} for kw in keywords]
        req_payload = {
            "comparisonItem": comparison_items,
            "category": 0,
            "property": "",
        }

        params = {
            "hl": "en-US",
            "tz": "360",
            "req": json.dumps(req_payload),
        }

        data, proxy_used = self._safe_http_get(self.EXPLORE_URL, params)
        widgets = {}
        for w in data.get("widgets", []):
            w_id = w.get("id", "")
            widgets[w_id] = {
                "token": w.get("token"),
                "req": w.get("request"),
            }
        return widgets, proxy_used

    def get_interest_over_time(
        self,
        keywords: List[str],
        timeframe: str = "today 5-y",
        geo: str = "US",
    ) -> pd.DataFrame:
        """Fetch interest over time for up to 5 keywords.

        Returns DataFrame with 'date' index and columns for each keyword score (0-100).
        """
        widgets, _ = self._get_widgets(keywords, timeframe, geo)
        timeseries_widget = widgets.get("TIMESERIES")
        if not timeseries_widget:
            logger.warning("No TIMESERIES widget returned by Google Trends.")
            return pd.DataFrame()

        params = {
            "hl": "en-US",
            "tz": "360",
            "req": json.dumps(timeseries_widget["req"]),
            "token": timeseries_widget["token"],
        }

        res_data, _ = self._safe_http_get(self.MULTILINE_URL, params)
        timeline = res_data.get("default", {}).get("timelineData", [])

        rows = []
        for point in timeline:
            dt_str = point.get("formattedTime", "")
            ts = point.get("time")
            if ts:
                dt_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")

            values = point.get("value", [])
            row = {"date": dt_str}
            for idx, kw in enumerate(keywords):
                row[kw] = values[idx] if idx < len(values) else 0
            rows.append(row)

        df = pd.DataFrame(rows)
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
            df.set_index("date", inplace=True)
            df.sort_index(inplace=True)
        return df

    def get_interest_by_region(
        self,
        keywords: List[str],
        timeframe: str = "today 5-y",
        geo: str = "US",
        resolution: str = "REGION",
    ) -> pd.DataFrame:
        """Fetch interest by region for up to 5 keywords.

        Returns DataFrame with 'geo_code', 'geo_name', and keyword score columns.
        """
        widgets, _ = self._get_widgets(keywords, timeframe, geo)
        geo_widget = widgets.get("GEO_MAP")
        if not geo_widget:
            logger.warning("No GEO_MAP widget returned by Google Trends.")
            return pd.DataFrame()

        req_payload = dict(geo_widget["req"])
        req_payload["resolution"] = resolution

        params = {
            "hl": "en-US",
            "tz": "360",
            "req": json.dumps(req_payload),
            "token": geo_widget["token"],
        }

        res_data, _ = self._safe_http_get(self.GEO_URL, params)
        geo_data = res_data.get("default", {}).get("geoMapData", [])

        rows = []
        for item in geo_data:
            code = item.get("geoCode", "")
            name = item.get("geoName", "")
            values = item.get("value", [])
            row = {"geo_code": code, "geo_name": name}
            for idx, kw in enumerate(keywords):
                row[kw] = values[idx] if idx < len(values) else 0
            rows.append(row)

        return pd.DataFrame(rows)

    def get_related_queries(
        self,
        keywords: List[str],
        timeframe: str = "today 5-y",
        geo: str = "US",
    ) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        """Fetch top and rising related queries per keyword.

        Returns dict mapping keyword -> {'top': [...], 'rising': [...]}.
        """
        results = {}
        for kw in keywords:
            try:
                widgets, _ = self._get_widgets([kw], timeframe, geo)
                # RELATED_QUERIES widget id may be RELATED_QUERIES or RELATED_QUERIES_0
                rel_key = next((k for k in widgets.keys() if k.startswith("RELATED_QUERIES")), None)
                related_widget = widgets.get(rel_key) if rel_key else None

                if not related_widget:
                    results[kw] = {"top": [], "rising": []}
                    continue

                params = {
                    "hl": "en-US",
                    "tz": "360",
                    "req": json.dumps(related_widget["req"]),
                    "token": related_widget["token"],
                }

                res_data, _ = self._safe_http_get(self.RELATED_URL, params)
                ranked_lists = res_data.get("default", {}).get("rankedList", [])

                top_list = []
                rising_list = []
                for rl in ranked_lists:
                    rel_type = rl.get("rankedKeyword", [])
                    is_rising = False
                    for item in rel_type:
                        if ("formattedValue" in item and "%" in str(item["formattedValue"])) or item.get("formattedValue") == "Breakout":
                            is_rising = True
                            break

                    parsed = [
                        {
                            "query": item.get("query"),
                            "value": item.get("value"),
                            "formatted_value": item.get("formattedValue"),
                        }
                        for item in rel_type
                    ]

                    if is_rising:
                        rising_list.extend(parsed)
                    else:
                        top_list.extend(parsed)

                results[kw] = {"top": top_list, "rising": rising_list}
            except Exception as exc:
                logger.warning(f"Failed to fetch related queries for keyword '{kw}': {exc}")
                results[kw] = {"top": [], "rising": [], "error": str(exc)}

        return results

    def get_related_topics(
        self,
        keywords: List[str],
        timeframe: str = "today 5-y",
        geo: str = "US",
    ) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        """Fetch top and rising related topics per keyword.

        Returns dict mapping keyword -> {'top': [...], 'rising': [...]}.
        """
        results = {}
        for kw in keywords:
            try:
                widgets, _ = self._get_widgets([kw], timeframe, geo)
                rel_key = next((k for k in widgets.keys() if k.startswith("RELATED_TOPICS")), None)
                related_widget = widgets.get(rel_key) if rel_key else None

                if not related_widget:
                    results[kw] = {"top": [], "rising": []}
                    continue

                params = {
                    "hl": "en-US",
                    "tz": "360",
                    "req": json.dumps(related_widget["req"]),
                    "token": related_widget["token"],
                }

                res_data, _ = self._safe_http_get(self.RELATED_URL, params)
                ranked_lists = res_data.get("default", {}).get("rankedList", [])

                top_list = []
                rising_list = []
                for idx, rl in enumerate(ranked_lists):
                    rel_type = rl.get("rankedKeyword", [])
                    parsed = []
                    for item in rel_type:
                        t_info = item.get("topic", {})
                        parsed.append({
                            "topic_title": t_info.get("title", ""),
                            "topic_type": t_info.get("type", ""),
                            "value": item.get("value"),
                            "formatted_value": item.get("formattedValue"),
                        })

                    if idx == 0:
                        top_list.extend(parsed)
                    else:
                        rising_list.extend(parsed)

                results[kw] = {"top": top_list, "rising": rising_list}
            except Exception as exc:
                logger.warning(f"Failed to fetch related topics for keyword '{kw}': {exc}")
                results[kw] = {"top": [], "rising": [], "error": str(exc)}

        return results

    def get_related_topics(
        self,
        keywords: List[str],
        timeframe: str = "today 5-y",
        geo: str = "US",
    ) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        """Fetch top and rising related topics per keyword.

        Returns dict mapping keyword -> {'top': [...], 'rising': [...]}.
        """
        results = {}
        for kw in keywords:
            try:
                widgets, _ = self._get_widgets([kw], timeframe, geo)
                rel_key = next((k for k in widgets.keys() if k.startswith("RELATED_TOPICS")), None)
                related_widget = widgets.get(rel_key) if rel_key else None

                if not related_widget:
                    results[kw] = {"top": [], "rising": []}
                    continue

                params = {
                    "hl": "en-US",
                    "tz": "360",
                    "req": json.dumps(related_widget["req"]),
                    "token": related_widget["token"],
                }

                res_data, _ = self._http_get(self.RELATED_URL, params)
                ranked_lists = res_data.get("default", {}).get("rankedList", [])

                top_list = []
                rising_list = []
                for idx, rl in enumerate(ranked_lists):
                    rel_type = rl.get("rankedKeyword", [])
                    parsed = []
                    for item in rel_type:
                        t_info = item.get("topic", {})
                        parsed.append({
                            "topic_title": t_info.get("title", ""),
                            "topic_type": t_info.get("type", ""),
                            "value": item.get("value"),
                            "formatted_value": item.get("formattedValue"),
                        })

                    # Usually list 0 is top, list 1 is rising
                    if idx == 0:
                        top_list.extend(parsed)
                    else:
                        rising_list.extend(parsed)

                results[kw] = {"top": top_list, "rising": rising_list}
            except Exception as exc:
                logger.warning(f"Failed to fetch related topics for keyword '{kw}': {exc}")
                results[kw] = {"top": [], "rising": [], "error": str(exc)}

        return results


def chunk_keywords(keywords: List[str], max_size: int = 5) -> List[List[str]]:
    """Divide a keyword list into batches of max 5 keywords."""
    return [keywords[i : i + max_size] for i in range(0, len(keywords), max_size)]


# ============================================================================
# 4. Checkpointing Engine
# ============================================================================

class CheckpointStore:
    """SQLite-backed thread-safe progress checkpoint store for resuming interrupted jobs."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None):
        if db_path:
            self.db_path = Path(db_path)
        else:
            self.db_path = Path(__file__).resolve().parents[1] / "data" / "checkpoints" / "google_trends_checkpoint.db"
        
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS scraper_checkpoints (
                        job_id TEXT PRIMARY KEY,
                        ticker TEXT NOT NULL,
                        tier TEXT NOT NULL,
                        module TEXT NOT NULL,
                        geo TEXT NOT NULL,
                        timeframe TEXT NOT NULL,
                        status TEXT NOT NULL,
                        rows_written INTEGER DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        error_msg TEXT
                    )
                """)
                conn.commit()

    def is_completed(self, job_id: str) -> bool:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "SELECT status FROM scraper_checkpoints WHERE job_id = ?",
                    (job_id,),
                )
                row = cursor.fetchone()
                return bool(row and row[0] == "COMPLETED")

    def mark_completed(
        self,
        job_id: str,
        ticker: str,
        tier: str,
        module: str,
        geo: str,
        timeframe: str,
        rows_written: int = 0,
    ) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO scraper_checkpoints
                    (job_id, ticker, tier, module, geo, timeframe, status, rows_written, updated_at, error_msg)
                    VALUES (?, ?, ?, ?, ?, ?, 'COMPLETED', ?, ?, NULL)
                    """,
                    (job_id, ticker, tier, module, geo, timeframe, rows_written, now_str),
                )
                conn.commit()

    def mark_failed(
        self,
        job_id: str,
        ticker: str,
        tier: str,
        module: str,
        geo: str,
        timeframe: str,
        error_msg: str,
    ) -> None:
        now_str = datetime.now(timezone.utc).isoformat()
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO scraper_checkpoints
                    (job_id, ticker, tier, module, geo, timeframe, status, rows_written, updated_at, error_msg)
                    VALUES (?, ?, ?, ?, ?, ?, 'FAILED', 0, ?, ?)
                    """,
                    (job_id, ticker, tier, module, geo, timeframe, now_str, str(error_msg)),
                )
                conn.commit()

    def reset_failed(self) -> int:
        """Reset failed jobs back to non-completed state so they can be re-run."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("DELETE FROM scraper_checkpoints WHERE status = 'FAILED'")
                conn.commit()
                return cursor.rowcount


# ============================================================================
# 5. Job Queue & Parallel Architecture (ParallelGoogleTrendsPipeline)
# ============================================================================

@dataclass
class JobTask:
    """Represents a single scraping task unit in the worker queue."""
    job_id: str
    company: CompanySpec
    tier: str
    keywords: List[str]
    geo: str
    timeframe: str
    module: str  # interest_over_time, interest_by_region, related_queries, related_topics


class ParallelGoogleTrendsPipeline:
    """Production parallel worker pool pipeline for company/product-level Google Trends scraping."""

    def __init__(
        self,
        data_lake: Optional[DataLakeStore] = None,
        proxy_pool: Optional[ProxyPool] = None,
        checkpoint_store: Optional[CheckpointStore] = None,
        max_workers: int = 3,
        min_delay: float = 2.0,
        max_delay: float = 6.0,
    ):
        self.data_lake = data_lake or DataLakeStore()
        self.proxy_pool = proxy_pool or ProxyPool()
        self.checkpoint_store = checkpoint_store or CheckpointStore()
        self.max_workers = max_workers
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.scraper = GoogleTrendsScraper(
            min_delay=min_delay,
            max_delay=max_delay,
            proxy_pool=self.proxy_pool,
        )

    def run_company_pipeline(
        self,
        companies: List[CompanySpec],
        geos: Optional[List[str]] = None,
        timeframes: Optional[List[str]] = None,
        modules: Optional[List[str]] = None,
        tier_limits: Optional[Dict[str, int]] = None,
        domain: str = "google_trends",
    ) -> Dict[str, Any]:
        """Execute full company pipeline across companies, keyword tiers, geos, timeframes, and modules.

        Args:
            companies: List of CompanySpec objects
            geos: List of geographic region codes (defaults to ["US"])
            timeframes: List of timeframes (defaults to ["today 5-y"])
            modules: List of modules to run (defaults to all 4)
            tier_limits: Override for keyword tier counts
            domain: Target Data Lake domain namespace

        Returns:
            Dict[str, Any] summary report
        """
        geos = geos or ["US"]
        timeframes = timeframes or ["today 5-y"]
        modules = modules or [
            "interest_over_time",
            "interest_by_region",
            "related_queries",
            "related_topics",
        ]

        # 1. Build list of job tasks
        tasks: List[JobTask] = []
        for comp in companies:
            tier_keywords = KeywordGenerator.generate_keywords(comp, tier_limits=tier_limits)
            for tier, kws in tier_keywords.items():
                if not kws:
                    continue
                # Split keywords into 5-keyword batches
                kw_batches = chunk_keywords(kws, max_size=5)
                for b_idx, kw_batch in enumerate(kw_batches):
                    for geo in geos:
                        for timeframe in timeframes:
                            for mod in modules:
                                raw_id = f"{comp.ticker}_{tier}_{mod}_{geo}_{timeframe}_b{b_idx}_{'_'.join(kw_batch)}"
                                job_id = hashlib.md5(raw_id.encode("utf-8")).hexdigest()[:16]
                                tasks.append(JobTask(
                                    job_id=job_id,
                                    company=comp,
                                    tier=tier,
                                    keywords=kw_batch,
                                    geo=geo,
                                    timeframe=timeframe,
                                    module=mod,
                                ))

        total_tasks = len(tasks)
        pending_tasks = [t for t in tasks if not self.checkpoint_store.is_completed(t.job_id)]
        skipped_count = total_tasks - len(pending_tasks)

        logger.info(
            f"Parallel Google Trends Pipeline initialized: {len(companies)} company(ies), "
            f"{total_tasks} total jobs ({skipped_count} skipped via checkpoint, {len(pending_tasks)} pending)."
        )

        completed_jobs = 0
        failed_jobs = 0
        saved_paths: List[str] = []

        if pending_tasks:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(pending_tasks))) as executor:
                future_to_task = {
                    executor.submit(self._execute_job_task, task, domain): task
                    for task in pending_tasks
                }

                for future in as_completed(future_to_task):
                    task = future_to_task[future]
                    try:
                        res_paths, rows = future.result()
                        completed_jobs += 1
                        saved_paths.extend(res_paths)
                        self.checkpoint_store.mark_completed(
                            job_id=task.job_id,
                            ticker=task.company.ticker,
                            tier=task.tier,
                            module=task.module,
                            geo=task.geo,
                            timeframe=task.timeframe,
                            rows_written=rows,
                        )
                        logger.info(
                            f"Completed job [{completed_jobs}/{len(pending_tasks)}]: "
                            f"{task.company.ticker} | tier={task.tier} | module={task.module} ({rows} rows)"
                        )
                    except Exception as exc:
                        failed_jobs += 1
                        logger.error(
                            f"Failed job task {task.company.ticker} ({task.tier}/{task.module}): {exc}"
                        )
                        self.checkpoint_store.mark_failed(
                            job_id=task.job_id,
                            ticker=task.company.ticker,
                            tier=task.tier,
                            module=task.module,
                            geo=task.geo,
                            timeframe=task.timeframe,
                            error_msg=str(exc),
                        )

        summary = {
            "status": "SUCCESS" if failed_jobs == 0 else "PARTIAL_FAILURE",
            "total_tasks": total_tasks,
            "skipped_tasks": skipped_count,
            "completed_tasks": completed_jobs,
            "failed_tasks": failed_jobs,
            "saved_file_count": len(saved_paths),
            "saved_paths": saved_paths,
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }
        return summary

    def _execute_job_task(self, task: JobTask, domain: str) -> Tuple[List[str], int]:
        """Execute a single JobTask and save results to Data Lake with enriched dimensions."""
        now_str = datetime.now(timezone.utc).isoformat()
        saved_paths: List[str] = []
        rows_count = 0

        # Dynamic series_id naming pattern for clean data lake partition cataloging
        kw_slug = task.keywords[0].replace(" ", "_").lower()
        base_series_id = f"{task.company.ticker}_{task.tier}_{task.module}_{task.geo}_{task.job_id[:6]}"

        if task.module == "interest_over_time":
            df = self.scraper.get_interest_over_time(
                keywords=task.keywords,
                timeframe=task.timeframe,
                geo=task.geo,
            )
            if not df.empty:
                # Format dataframe to include dimension metadata columns
                df_reset = df.reset_index()
                date_col = "date" if "date" in df_reset.columns else df_reset.columns[0]
                val_vars = [k for k in task.keywords if k in df_reset.columns]
                if not val_vars:
                    val_vars = [c for c in df_reset.columns if c != date_col]

                if val_vars:
                    df_long = df_reset.melt(
                        id_vars=[date_col],
                        value_vars=val_vars,
                        var_name="keyword",
                        value_name="interest_score",
                    )
                    if date_col != "date":
                        df_long.rename(columns={date_col: "date"}, inplace=True)
                    df_long["ticker"] = task.company.ticker
                    df_long["company_name"] = task.company.name
                    df_long["sector"] = task.company.sector
                    df_long["tier"] = task.tier
                    df_long["geo"] = task.geo
                    df_long["timeframe"] = task.timeframe
                    df_long["ingested_at"] = now_str

                    p = self.data_lake.save_dataframe(
                        domain=domain,
                        series_id=base_series_id,
                        df=df_long,
                        metadata={
                            "ticker": task.company.ticker,
                            "company_name": task.company.name,
                            "sector": task.company.sector,
                            "tier": task.tier,
                            "keywords": task.keywords,
                            "geo": task.geo,
                            "timeframe": task.timeframe,
                            "module": task.module,
                        },
                    )
                    saved_paths.append(str(p))
                    rows_count = len(df_long)

        elif task.module == "interest_by_region":
            df = self.scraper.get_interest_by_region(
                keywords=task.keywords,
                timeframe=task.timeframe,
                geo=task.geo,
            )
            if not df.empty:
                val_vars = [k for k in task.keywords if k in df.columns]
                if not val_vars:
                    val_vars = [c for c in df.columns if c not in ("geo_code", "geo_name")]

                if val_vars:
                    df_long = df.melt(
                        id_vars=["geo_code", "geo_name"],
                        value_vars=val_vars,
                        var_name="keyword",
                        value_name="interest_score",
                    )
                    df_long["ticker"] = task.company.ticker
                    df_long["company_name"] = task.company.name
                    df_long["sector"] = task.company.sector
                    df_long["tier"] = task.tier
                    df_long["geo"] = task.geo
                    df_long["timeframe"] = task.timeframe
                    df_long["ingested_at"] = now_str

                    p = self.data_lake.save_dataframe(
                        domain=domain,
                        series_id=base_series_id,
                        df=df_long,
                        metadata={
                            "ticker": task.company.ticker,
                            "company_name": task.company.name,
                            "sector": task.company.sector,
                            "tier": task.tier,
                            "keywords": task.keywords,
                            "geo": task.geo,
                            "timeframe": task.timeframe,
                            "module": task.module,
                        },
                    )
                    saved_paths.append(str(p))
                    rows_count = len(df_long)

        elif task.module == "related_queries":
            res = self.scraper.get_related_queries(
                keywords=task.keywords,
                timeframe=task.timeframe,
                geo=task.geo,
            )

            # Flatten to tabular dataframe for Data Lake parquet storage
            flattened_rows = []
            for kw, item in res.items():
                for q_type in ["top", "rising"]:
                    for entry in item.get(q_type, []):
                        flattened_rows.append({
                            "keyword": kw,
                            "query_type": q_type,
                            "query": entry.get("query"),
                            "value": entry.get("value"),
                            "formatted_value": entry.get("formatted_value"),
                            "ticker": task.company.ticker,
                            "company_name": task.company.name,
                            "sector": task.company.sector,
                            "tier": task.tier,
                            "geo": task.geo,
                            "timeframe": task.timeframe,
                            "ingested_at": now_str,
                        })

            if flattened_rows:
                df_rel = pd.DataFrame(flattened_rows)
                p = self.data_lake.save_dataframe(
                    domain=domain,
                    series_id=base_series_id,
                    df=df_rel,
                    metadata={
                        "ticker": task.company.ticker,
                        "tier": task.tier,
                        "module": task.module,
                    },
                )
                saved_paths.append(str(p))
                rows_count = len(df_rel)
            else:
                p = self.data_lake.save_json(
                    domain=domain,
                    series_id=base_series_id,
                    data=res,
                    metadata={"ticker": task.company.ticker, "tier": task.tier},
                )
                saved_paths.append(str(p))
                rows_count = len(res)

        elif task.module == "related_topics":
            res = self.scraper.get_related_topics(
                keywords=task.keywords,
                timeframe=task.timeframe,
                geo=task.geo,
            )

            flattened_rows = []
            for kw, item in res.items():
                for t_kind in ["top", "rising"]:
                    for entry in item.get(t_kind, []):
                        flattened_rows.append({
                            "keyword": kw,
                            "topic_kind": t_kind,
                            "topic_title": entry.get("topic_title"),
                            "topic_type": entry.get("topic_type"),
                            "value": entry.get("value"),
                            "formatted_value": entry.get("formatted_value"),
                            "ticker": task.company.ticker,
                            "company_name": task.company.name,
                            "sector": task.company.sector,
                            "tier": task.tier,
                            "geo": task.geo,
                            "timeframe": task.timeframe,
                            "ingested_at": now_str,
                        })

            if flattened_rows:
                df_top = pd.DataFrame(flattened_rows)
                p = self.data_lake.save_dataframe(
                    domain=domain,
                    series_id=base_series_id,
                    df=df_top,
                    metadata={
                        "ticker": task.company.ticker,
                        "tier": task.tier,
                        "module": task.module,
                    },
                )
                saved_paths.append(str(p))
                rows_count = len(df_top)
            else:
                p = self.data_lake.save_json(
                    domain=domain,
                    series_id=base_series_id,
                    data=res,
                    metadata={"ticker": task.company.ticker, "tier": task.tier},
                )
                saved_paths.append(str(p))
                rows_count = len(res)

        return saved_paths, rows_count


# ============================================================================
# 6. Legacy / Simple Ingestion Pipeline Compatibility Wrapper
# ============================================================================

class GoogleTrendsIngestionPipeline:
    """Orchestrates Google Trends data ingestion into the Data Lake (backwards compatible)."""

    def __init__(
        self,
        data_lake: Optional[DataLakeStore] = None,
        scraper: Optional[GoogleTrendsScraper] = None,
    ):
        self.data_lake = data_lake or DataLakeStore()
        self.scraper = scraper or GoogleTrendsScraper()

    def ingest_keywords(
        self,
        keywords: List[str],
        timeframe: str = "today 5-y",
        geo: str = "US",
        batch_id: Optional[str] = None,
        domain: str = "google_trends",
    ) -> Dict[str, Any]:
        """Ingest Google Trends data for an arbitrary list of keywords.

        Automatically splits keywords into batches of max 5, extracts interest_over_time,
        interest_by_region, and related_queries, and persists to Data Lake.
        """
        start_time = time.time()
        batches = chunk_keywords(keywords, max_size=5)
        batch_name = batch_id or f"batch_{int(time.time())}"

        logger.info(
            f"Starting Google Trends ingestion for {len(keywords)} keywords "
            f"across {len(batches)} batch(es) into domain '{domain}'"
        )

        all_time_dfs = []
        all_region_dfs = []
        all_related_queries = {}

        for b_idx, kw_batch in enumerate(batches):
            logger.info(f"Processing Google Trends batch {b_idx + 1}/{len(batches)}: {kw_batch}")

            # 1. Interest Over Time
            try:
                df_time = self.scraper.get_interest_over_time(kw_batch, timeframe=timeframe, geo=geo)
                if not df_time.empty:
                    all_time_dfs.append(df_time)
            except Exception as exc:
                logger.error(f"Interest over time failed for batch {kw_batch}: {exc}")

            # 2. Interest By Region
            try:
                df_region = self.scraper.get_interest_by_region(kw_batch, timeframe=timeframe, geo=geo)
                if not df_region.empty:
                    all_region_dfs.append(df_region)
            except Exception as exc:
                logger.error(f"Interest by region failed for batch {kw_batch}: {exc}")

            # 3. Related Queries
            try:
                related = self.scraper.get_related_queries(kw_batch, timeframe=timeframe, geo=geo)
                all_related_queries.update(related)
            except Exception as exc:
                logger.error(f"Related queries failed for batch {kw_batch}: {exc}")

        # Combine Interest Over Time across batches
        combined_time_df = pd.concat(all_time_dfs, axis=1) if all_time_dfs else pd.DataFrame()

        # Combine Interest By Region across batches
        if all_region_dfs:
            combined_region_df = all_region_dfs[0]
            for r_df in all_region_dfs[1:]:
                combined_region_df = pd.merge(
                    combined_region_df,
                    r_df,
                    on=["geo_code", "geo_name"],
                    how="outer",
                )
        else:
            combined_region_df = pd.DataFrame()

        meta = {
            "batch_id": batch_name,
            "keywords": keywords,
            "timeframe": timeframe,
            "geo": geo,
            "time_series_rows": len(combined_time_df),
            "region_rows": len(combined_region_df),
            "related_queries_count": len(all_related_queries),
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "execution_time_sec": round(time.time() - start_time, 3),
        }

        saved_paths = {}

        if not combined_time_df.empty:
            time_path = self.data_lake.save_dataframe(
                domain=domain,
                series_id=f"{batch_name}_time",
                df=combined_time_df,
                metadata=meta,
            )
            saved_paths["interest_over_time"] = str(time_path)

        if not combined_region_df.empty:
            region_path = self.data_lake.save_dataframe(
                domain=domain,
                series_id=f"{batch_name}_region",
                df=combined_region_df,
                metadata=meta,
            )
            saved_paths["interest_by_region"] = str(region_path)

        if all_related_queries:
            related_path = self.data_lake.save_json(
                domain=domain,
                series_id=f"{batch_name}_related",
                data=all_related_queries,
                metadata=meta,
            )
            saved_paths["related_queries"] = str(related_path)

        report = {
            "batch_id": batch_name,
            "domain": domain,
            "status": "SUCCESS",
            "metadata": meta,
            "saved_paths": saved_paths,
        }

        logger.info(f"Google Trends ingestion complete for batch {batch_name} in {meta['execution_time_sec']}s")
        return report
