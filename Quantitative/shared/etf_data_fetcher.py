#!/usr/bin/env python3
"""
etf_data_fetcher.py — ETF Market Data Fetcher

Primary: yfinance for ADV, prices, returns.
Fallback: ETFdb.com BeautifulSoup scraping for NAV premium/discount and bid-ask spread.

Implements the same stealth patterns as the Qualitative Blueprint scrapers:
- Randomized delays (2.0s - 5.0s) for ETFdb requests
- Rotating user-agents
- Local cache with 12h TTL for market data
"""

import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = WORKSPACE_ROOT / "data" / "etf_cache"

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]


@dataclass
class ETFMetrics:
    """Complete metrics for a single ETF needed by the Liquidity Gatekeeper."""
    ticker: str
    avg_daily_volume: Optional[float] = None
    median_bid_ask_spread: Optional[float] = None  # as decimal (0.0002 = 0.02%)
    nav_premium_discount: Optional[float] = None   # as decimal (-0.001 = -0.10%)
    expense_ratio: Optional[float] = None           # as decimal (0.0025 = 0.25%)
    closing_price: Optional[float] = None
    total_assets_millions: Optional[float] = None
    data_source: str = "unknown"
    retrieved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    daily_returns: Optional[List[float]] = None     # for tracking error calculations
    daily_prices: Optional[List[float]] = None
    daily_dates: Optional[List[str]] = None


class ETFDataFetcher:
    """
    Fetches ETF market data with yfinance primary + ETFdb fallback.

    Caches results locally to minimize API/scraping requests during overnight runs.
    """

    CACHE_TTL_HOURS = 12
    MIN_DELAY = 2.0
    MAX_DELAY = 5.0

    def __init__(self, cache_dir: Optional[Path] = None):
        self._cache_dir = cache_dir or CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._last_request_time = 0.0

    def _throttle(self) -> None:
        """Enforce randomized delay for web scraping requests."""
        elapsed = time.time() - self._last_request_time
        min_wait = self.MIN_DELAY + random.uniform(0, self.MAX_DELAY - self.MIN_DELAY)
        if elapsed < min_wait:
            time.sleep(min_wait - elapsed)
        self._last_request_time = time.time()

    def _get_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _get_cache_path(self, ticker: str) -> Path:
        return self._cache_dir / f"{ticker}_metrics.json"

    def _read_cache(self, ticker: str) -> Optional[ETFMetrics]:
        """Read cached ETF metrics if fresh."""
        cache_path = self._get_cache_path(ticker)
        if not cache_path.exists():
            return None
        try:
            data = json.loads(cache_path.read_text())
            cached_at = datetime.fromisoformat(data["retrieved_at"])
            age_hours = (datetime.now(timezone.utc) - cached_at).total_seconds() / 3600
            if age_hours > self.CACHE_TTL_HOURS:
                return None

            return ETFMetrics(
                ticker=data["ticker"],
                avg_daily_volume=data.get("avg_daily_volume"),
                median_bid_ask_spread=data.get("median_bid_ask_spread"),
                nav_premium_discount=data.get("nav_premium_discount"),
                expense_ratio=data.get("expense_ratio"),
                closing_price=data.get("closing_price"),
                total_assets_millions=data.get("total_assets_millions"),
                data_source=data.get("data_source", "cache"),
                retrieved_at=data["retrieved_at"],
                daily_returns=data.get("daily_returns"),
                daily_prices=data.get("daily_prices"),
                daily_dates=data.get("daily_dates"),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Cache read error for {ticker}: {e}")
            return None

    def _write_cache(self, metrics: ETFMetrics) -> None:
        """Write ETF metrics to local cache."""
        cache_path = self._get_cache_path(metrics.ticker)
        data = {
            "ticker": metrics.ticker,
            "avg_daily_volume": metrics.avg_daily_volume,
            "median_bid_ask_spread": metrics.median_bid_ask_spread,
            "nav_premium_discount": metrics.nav_premium_discount,
            "expense_ratio": metrics.expense_ratio,
            "closing_price": metrics.closing_price,
            "total_assets_millions": metrics.total_assets_millions,
            "data_source": metrics.data_source,
            "retrieved_at": metrics.retrieved_at,
            "daily_returns": metrics.daily_returns,
            "daily_prices": metrics.daily_prices,
            "daily_dates": metrics.daily_dates,
        }
        cache_path.write_text(json.dumps(data, indent=2))

    def fetch_yfinance(self, ticker: str, period: str = "1y") -> ETFMetrics:
        """
        Primary data source: yfinance.

        Retrieves ADV, closing price, expense ratio, and historical daily returns.
        """
        try:
            import yfinance as yf

            logger.info(f"Fetching yfinance data for {ticker} (period={period})")
            etf = yf.Ticker(ticker)
            info = etf.info or {}

            # Historical data for returns calculation
            hist = etf.history(period=period)

            daily_returns = None
            daily_prices = None
            daily_dates = None
            if not hist.empty and "Close" in hist.columns:
                closes = hist["Close"].dropna()
                if len(closes) > 1:
                    log_returns = np.log(closes / closes.shift(1)).dropna()
                    daily_returns = log_returns.tolist()
                    daily_prices = closes.tolist()
                    daily_dates = [d.strftime("%Y-%m-%d") for d in closes.index]

            # Compute ADV from historical volume
            adv = None
            if not hist.empty and "Volume" in hist.columns:
                adv = float(hist["Volume"].mean())

            # Bid-ask spread approximation from yfinance
            # yfinance provides bid/ask for current quotes
            bid = info.get("bid", 0)
            ask = info.get("ask", 0)
            mid = (bid + ask) / 2 if bid and ask and (bid + ask) > 0 else None
            spread = ((ask - bid) / mid) if mid and mid > 0 else None

            metrics = ETFMetrics(
                ticker=ticker,
                avg_daily_volume=adv,
                median_bid_ask_spread=spread,
                nav_premium_discount=None,  # yfinance doesn't reliably provide this
                expense_ratio=info.get("annualReportExpenseRatio"),
                closing_price=float(closes.iloc[-1]) if daily_prices else info.get("previousClose"),
                total_assets_millions=(info.get("totalAssets", 0) or 0) / 1_000_000,
                data_source="yfinance",
                daily_returns=daily_returns,
                daily_prices=daily_prices,
                daily_dates=daily_dates,
            )

            logger.info(
                f"yfinance {ticker}: ADV={adv:.0f}, price={metrics.closing_price}, "
                f"ER={metrics.expense_ratio}, spread={spread}"
            )
            return metrics

        except ImportError:
            logger.error("yfinance not installed — cannot use primary data source")
            return ETFMetrics(ticker=ticker, data_source="yfinance_unavailable")
        except Exception as e:
            logger.error(f"yfinance fetch failed for {ticker}: {e}")
            return ETFMetrics(ticker=ticker, data_source="yfinance_error")

    def fetch_etfdb_fallback(self, ticker: str) -> ETFMetrics:
        """
        Fallback data source: ETFdb.com web scraping.

        Scrapes bid-ask spread, NAV premium/discount, and expense ratio from ETFdb.
        Uses stealth HTTP patterns (randomized delays, rotating user-agents).
        """
        self._throttle()

        url = f"https://etfdb.com/etf/{ticker}/"
        logger.info(f"Scraping ETFdb.com fallback for {ticker}: {url}")

        try:
            response = self._session.get(url, headers=self._get_headers(), timeout=30)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            # Parse expense ratio
            expense_ratio = None
            er_el = soup.find(string=lambda t: t and "Expense Ratio" in t if t else False)
            if er_el:
                parent = er_el.find_parent("tr") or er_el.find_parent("div")
                if parent:
                    val_el = parent.find("td", class_="text-right") or parent.find("span", class_="text-right")
                    if val_el:
                        try:
                            expense_ratio = float(val_el.get_text(strip=True).replace("%", "")) / 100
                        except ValueError:
                            pass

            # Parse bid-ask spread
            spread = None
            spread_el = soup.find(string=lambda t: t and "Bid-Ask Spread" in t if t else False)
            if spread_el:
                parent = spread_el.find_parent("tr") or spread_el.find_parent("div")
                if parent:
                    val_el = parent.find("td", class_="text-right") or parent.find("span", class_="text-right")
                    if val_el:
                        try:
                            spread = float(val_el.get_text(strip=True).replace("%", "").replace("$", "")) / 100
                        except ValueError:
                            pass

            # Parse NAV premium/discount
            nav_dev = None
            nav_el = soup.find(string=lambda t: t and "Premium/Discount" in t if t else False)
            if nav_el:
                parent = nav_el.find_parent("tr") or nav_el.find_parent("div")
                if parent:
                    val_el = parent.find("td", class_="text-right") or parent.find("span", class_="text-right")
                    if val_el:
                        try:
                            nav_dev = float(val_el.get_text(strip=True).replace("%", "")) / 100
                        except ValueError:
                            pass

            return ETFMetrics(
                ticker=ticker,
                median_bid_ask_spread=spread,
                nav_premium_discount=nav_dev,
                expense_ratio=expense_ratio,
                data_source="etfdb_scrape",
            )

        except requests.RequestException as e:
            logger.error(f"ETFdb scrape failed for {ticker}: {e}")
            return ETFMetrics(ticker=ticker, data_source="etfdb_error")

    def fetch(self, ticker: str, force_refresh: bool = False) -> ETFMetrics:
        """
        Fetch ETF metrics using the 2-tier fallback chain:
        1. Local cache
        2. yfinance (primary) merged with ETFdb (fallback for NAV/spread gaps)

        Args:
            ticker: ETF ticker symbol
            force_refresh: Skip cache if True

        Returns:
            ETFMetrics with all available data points
        """
        # Check cache first
        if not force_refresh:
            cached = self._read_cache(ticker)
            if cached:
                logger.info(f"Cache hit for {ticker}")
                return cached

        # Primary: yfinance
        metrics = self.fetch_yfinance(ticker)

        # If yfinance is missing NAV premium/discount or bid-ask spread,
        # fill gaps from ETFdb
        if metrics.nav_premium_discount is None or metrics.median_bid_ask_spread is None:
            logger.info(f"Filling data gaps for {ticker} from ETFdb.com...")
            etfdb = self.fetch_etfdb_fallback(ticker)

            if metrics.nav_premium_discount is None and etfdb.nav_premium_discount is not None:
                metrics.nav_premium_discount = etfdb.nav_premium_discount
            if metrics.median_bid_ask_spread is None and etfdb.median_bid_ask_spread is not None:
                metrics.median_bid_ask_spread = etfdb.median_bid_ask_spread
            if metrics.expense_ratio is None and etfdb.expense_ratio is not None:
                metrics.expense_ratio = etfdb.expense_ratio

            metrics.data_source = "yfinance+etfdb"

        # Cache the merged result
        self._write_cache(metrics)
        return metrics

    def fetch_multiple(self, tickers: List[str], force_refresh: bool = False) -> Dict[str, ETFMetrics]:
        """Fetch metrics for multiple ETFs with appropriate throttling."""
        results = {}
        for i, ticker in enumerate(tickers):
            logger.info(f"Fetching ETF data {i+1}/{len(tickers)}: {ticker}")
            results[ticker] = self.fetch(ticker, force_refresh=force_refresh)
        return results
