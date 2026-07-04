#!/usr/bin/env python3
"""
fred_scraper.py — Stealthy FRED Public Data Scraper

Scrapes Federal Reserve Economic Data (FRED) public HTML pages without requiring
an API key. Implements randomized delays, rotating user-agents, and circuit breaker
patterns matching the Qualitative Blueprint's anti-IP-ban infrastructure.

Data is cached locally to minimize request volume and prevent rate-limiting.
"""

import csv
import io
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = WORKSPACE_ROOT / "data" / "fred_cache"

# Rotating user-agent pool to prevent fingerprint blocking
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

# Known FRED series used by the ETF engine
FRED_SERIES_REGISTRY = {
    "GOLDPMGBD228NLBM": "Gold Fixing Price 3:00 P.M. (London Bullion Market)",
    "DFII10": "10-Year Treasury Inflation-Indexed Security, Constant Maturity",
    "M2SL": "M2 Money Stock",
    "BAA10Y": "Moody's Seasoned Baa Corporate Bond Yield Relative to 10-Year Treasury",
    "BAMLC0A4CBBB": "ICE BofA BBB US Corporate Index Effective Yield",
    "DGS10": "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity",
    "CPIAUCSL": "Consumer Price Index for All Urban Consumers: All Items",
    "GDP": "Gross Domestic Product",
    "UNRATE": "Unemployment Rate",
}


@dataclass
class FREDDataPoint:
    """Single observation from a FRED series."""
    date: str
    value: float
    series_id: str
    source: str = "FRED_HTML_SCRAPE"
    retrieved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class FREDSeriesResult:
    """Complete result for a FRED series query."""
    series_id: str
    title: str
    observations: List[FREDDataPoint]
    cache_hit: bool = False
    retrieval_method: str = "html_scrape"
    error: Optional[str] = None


class FREDScraper:
    """
    Scrapes FRED public data pages using stealth HTTP patterns.

    Anti-IP-Ban Measures (aligned with Qualitative Blueprint):
    - Randomized delays between 2.0s and 5.0s per request
    - Rotating user-agent pool (5 agents)
    - Local filesystem cache (24h TTL) to minimize request volume
    - Circuit breaker: backs off exponentially on consecutive failures
    """

    # Stealth timing parameters
    MIN_DELAY = 2.0   # seconds
    MAX_DELAY = 5.0   # seconds
    CACHE_TTL_HOURS = 24
    MAX_CONSECUTIVE_FAILURES = 3
    BACKOFF_BASE = 10.0  # seconds

    def __init__(self, cache_dir: Optional[Path] = None):
        self._cache_dir = cache_dir or CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._consecutive_failures = 0
        self._last_request_time = 0.0

    def _get_headers(self) -> Dict[str, str]:
        """Generate request headers with a random user-agent."""
        return {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
        }

    def _throttle(self) -> None:
        """Enforce randomized delay between requests to prevent rate-limiting."""
        elapsed = time.time() - self._last_request_time
        min_wait = self.MIN_DELAY + random.uniform(0, self.MAX_DELAY - self.MIN_DELAY)
        if elapsed < min_wait:
            sleep_time = min_wait - elapsed
            logger.debug(f"Throttling: sleeping {sleep_time:.2f}s before next FRED request")
            time.sleep(sleep_time)
        self._last_request_time = time.time()

    def _check_circuit_breaker(self) -> bool:
        """Check if we should back off due to consecutive failures."""
        if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            backoff = self.BACKOFF_BASE * (2 ** (self._consecutive_failures - self.MAX_CONSECUTIVE_FAILURES))
            backoff = min(backoff, 300.0)  # cap at 5 minutes
            logger.warning(
                f"Circuit breaker active: {self._consecutive_failures} consecutive failures. "
                f"Backing off {backoff:.1f}s"
            )
            time.sleep(backoff)
            return True
        return False

    def _get_cache_path(self, series_id: str) -> Path:
        """Get the cache file path for a FRED series."""
        return self._cache_dir / f"{series_id}.json"

    def _read_cache(self, series_id: str) -> Optional[FREDSeriesResult]:
        """Read cached data if it exists and is fresh."""
        cache_path = self._get_cache_path(series_id)
        if not cache_path.exists():
            return None

        try:
            data = json.loads(cache_path.read_text())
            cached_at = datetime.fromisoformat(data.get("cached_at", ""))
            age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600

            if age_hours > self.CACHE_TTL_HOURS:
                logger.debug(f"Cache expired for {series_id} (age: {age_hours:.1f}h)")
                return None

            observations = [
                FREDDataPoint(
                    date=obs["date"],
                    value=obs["value"],
                    series_id=series_id,
                    source="FRED_CACHE",
                    retrieved_at=obs.get("retrieved_at", data["cached_at"]),
                )
                for obs in data["observations"]
            ]

            logger.info(f"Cache hit for FRED series {series_id} ({len(observations)} observations)")
            return FREDSeriesResult(
                series_id=series_id,
                title=data.get("title", FRED_SERIES_REGISTRY.get(series_id, series_id)),
                observations=observations,
                cache_hit=True,
                retrieval_method="cache",
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Cache read error for {series_id}: {e}")
            return None

    def _write_cache(self, result: FREDSeriesResult) -> None:
        """Write scraped data to local cache."""
        cache_path = self._get_cache_path(result.series_id)
        data = {
            "series_id": result.series_id,
            "title": result.title,
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "observations": [
                {"date": obs.date, "value": obs.value, "retrieved_at": obs.retrieved_at}
                for obs in result.observations
            ],
        }
        cache_path.write_text(json.dumps(data, indent=2))
        logger.debug(f"Cached {len(result.observations)} observations for {result.series_id}")

    def _scrape_fred_download(self, series_id: str) -> FREDSeriesResult:
        """
        Primary scraping method: Download CSV from FRED's public download endpoint.

        URL pattern: https://fred.stlouisfed.org/graph/fredgraph.csv?id=SERIES_ID
        This endpoint serves raw CSV data without requiring authentication.
        """
        self._check_circuit_breaker()
        self._throttle()

        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        title = FRED_SERIES_REGISTRY.get(series_id, series_id)

        try:
            logger.info(f"Scraping FRED CSV for {series_id}: {url}")
            response = self._session.get(url, headers=self._get_headers(), timeout=30)
            response.raise_for_status()

            # Parse CSV response
            observations = []
            reader = csv.DictReader(io.StringIO(response.text))
            for row in reader:
                date_str = row.get("DATE", "")
                value_str = row.get(series_id, "")

                # FRED uses "." for missing values
                if not value_str or value_str.strip() == ".":
                    continue

                try:
                    value = float(value_str)
                    observations.append(FREDDataPoint(
                        date=date_str,
                        value=value,
                        series_id=series_id,
                    ))
                except ValueError:
                    logger.debug(f"Skipping non-numeric value for {series_id} on {date_str}: {value_str}")

            self._consecutive_failures = 0
            logger.info(f"Successfully scraped {len(observations)} observations for {series_id}")

            return FREDSeriesResult(
                series_id=series_id,
                title=title,
                observations=observations,
                retrieval_method="fred_csv_download",
            )

        except requests.RequestException as e:
            self._consecutive_failures += 1
            logger.error(f"FRED CSV download failed for {series_id}: {e}")
            return FREDSeriesResult(
                series_id=series_id,
                title=title,
                observations=[],
                error=str(e),
                retrieval_method="fred_csv_download",
            )

    def _scrape_fred_html(self, series_id: str) -> FREDSeriesResult:
        """
        Fallback scraping method: Parse FRED series HTML page.

        Used when CSV download endpoint is blocked or rate-limited.
        """
        self._check_circuit_breaker()
        self._throttle()

        url = f"https://fred.stlouisfed.org/series/{series_id}"
        title = FRED_SERIES_REGISTRY.get(series_id, series_id)

        try:
            logger.info(f"Scraping FRED HTML page for {series_id}: {url}")
            response = self._session.get(url, headers=self._get_headers(), timeout=30)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            # Extract title from page if available
            title_el = soup.find("span", class_="series-title")
            if title_el:
                title = title_el.get_text(strip=True)

            # Extract the latest observation value from the page header
            observations = []
            obs_value_el = soup.find("span", class_="series-meta-observation-value")
            obs_date_el = soup.find("span", class_="series-meta-observation-date")

            if obs_value_el and obs_date_el:
                try:
                    value = float(obs_value_el.get_text(strip=True).replace(",", ""))
                    date_str = obs_date_el.get_text(strip=True)
                    observations.append(FREDDataPoint(
                        date=date_str,
                        value=value,
                        series_id=series_id,
                        source="FRED_HTML_PARSE",
                    ))
                except ValueError:
                    pass

            self._consecutive_failures = 0
            return FREDSeriesResult(
                series_id=series_id,
                title=title,
                observations=observations,
                retrieval_method="fred_html_parse",
            )

        except requests.RequestException as e:
            self._consecutive_failures += 1
            logger.error(f"FRED HTML scrape failed for {series_id}: {e}")
            return FREDSeriesResult(
                series_id=series_id,
                title=title,
                observations=[],
                error=str(e),
                retrieval_method="fred_html_parse",
            )

    def _fallback_pandas_datareader(self, series_id: str) -> FREDSeriesResult:
        """
        Last-resort fallback: Use pandas_datareader to fetch FRED data.

        This still hits FRED's servers but through a different HTTP pathway,
        potentially bypassing IP-level blocks on the main domain.
        """
        title = FRED_SERIES_REGISTRY.get(series_id, series_id)
        try:
            import pandas_datareader.data as web
            from datetime import timedelta

            end_date = datetime.now()
            start_date = end_date - timedelta(days=365 * 5)  # 5 years

            logger.info(f"Attempting pandas_datareader fallback for {series_id}")
            df = web.DataReader(series_id, "fred", start_date, end_date)

            observations = []
            for date_idx, row in df.iterrows():
                value = row.iloc[0]
                if not (value is None or (isinstance(value, float) and value != value)):  # NaN check
                    observations.append(FREDDataPoint(
                        date=date_idx.strftime("%Y-%m-%d"),
                        value=float(value),
                        series_id=series_id,
                        source="PANDAS_DATAREADER",
                    ))

            self._consecutive_failures = 0
            logger.info(f"pandas_datareader returned {len(observations)} observations for {series_id}")
            return FREDSeriesResult(
                series_id=series_id,
                title=title,
                observations=observations,
                retrieval_method="pandas_datareader",
            )

        except ImportError:
            logger.warning("pandas_datareader not installed — final fallback unavailable")
            return FREDSeriesResult(
                series_id=series_id, title=title, observations=[],
                error="pandas_datareader not installed",
                retrieval_method="pandas_datareader",
            )
        except Exception as e:
            self._consecutive_failures += 1
            logger.error(f"pandas_datareader fallback failed for {series_id}: {e}")
            return FREDSeriesResult(
                series_id=series_id, title=title, observations=[],
                error=str(e), retrieval_method="pandas_datareader",
            )

    def fetch_series(self, series_id: str, force_refresh: bool = False) -> FREDSeriesResult:
        """
        Fetch a FRED series using the 3-tier fallback chain:
        1. Local cache (24h TTL)
        2. FRED CSV download endpoint (primary scrape)
        3. FRED HTML page parse (secondary scrape)
        4. pandas_datareader (last resort)

        Args:
            series_id: FRED series identifier (e.g., "GOLDPMGBD228NLBM")
            force_refresh: If True, skip cache and scrape fresh data

        Returns:
            FREDSeriesResult with observations and metadata
        """
        if series_id not in FRED_SERIES_REGISTRY:
            logger.warning(f"Unknown FRED series: {series_id}. Proceeding anyway.")

        # Tier 1: Cache
        if not force_refresh:
            cached = self._read_cache(series_id)
            if cached:
                return cached

        # Tier 2: CSV download (primary)
        result = self._scrape_fred_download(series_id)
        if result.observations:
            self._write_cache(result)
            return result

        # Tier 3: HTML page parse (secondary)
        logger.info(f"CSV download failed for {series_id}, trying HTML parse...")
        result = self._scrape_fred_html(series_id)
        if result.observations:
            self._write_cache(result)
            return result

        # Tier 4: pandas_datareader (last resort)
        logger.info(f"HTML parse failed for {series_id}, trying pandas_datareader...")
        result = self._fallback_pandas_datareader(series_id)
        if result.observations:
            self._write_cache(result)
            return result

        logger.error(f"All 3 FRED retrieval methods failed for {series_id}")
        return result

    def fetch_multiple(self, series_ids: List[str], force_refresh: bool = False) -> Dict[str, FREDSeriesResult]:
        """
        Fetch multiple FRED series with inter-request throttling.

        Args:
            series_ids: List of FRED series identifiers
            force_refresh: If True, skip cache for all series

        Returns:
            Dictionary mapping series_id → FREDSeriesResult
        """
        results = {}
        for i, series_id in enumerate(series_ids):
            logger.info(f"Fetching FRED series {i+1}/{len(series_ids)}: {series_id}")
            results[series_id] = self.fetch_series(series_id, force_refresh=force_refresh)
        return results

    def get_latest_value(self, series_id: str) -> Optional[Tuple[str, float]]:
        """
        Convenience method: Get the most recent (date, value) tuple for a series.

        Returns:
            Tuple of (date_string, value) or None if unavailable
        """
        result = self.fetch_series(series_id)
        if result.observations:
            latest = result.observations[-1]
            return (latest.date, latest.value)
        return None
