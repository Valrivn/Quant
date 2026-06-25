import asyncio
import logging
import time
import random
import re
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from statistics import mean, stdev
from collections import deque

try:
    from jobspy import scrape_jobs
    JOBSPY_AVAILABLE = True
except ImportError:
    JOBSPY_AVAILABLE = False
    scrape_jobs = None

from config import load_hybrid_config
from psychological.scrapers.nodriver_scraper import NodriverSession, NodriverConfig, scrape_with_nodriver
from psychological.engineering_guards import (
    guard_nan, guard_division, guard_bounds, guard_utc_timestamp, ensure_utc,
    RateLimiter, timed_operation, safe_float, safe_int, with_timeout
)

logger = logging.getLogger(__name__)


@dataclass
class JobSpySnapshot:
    ticker: str
    company_name: str
    date: str
    source: str
    job_count: int
    job_count_8_runs_ago: Optional[int] = None
    delta_30d: Optional[float] = None
    mean_252_runs: Optional[float] = None
    std_252_runs: Optional[float] = None
    zscore_1y: Optional[float] = None
    ghost_job_flag: bool = False
    operational_fracture_flag: bool = False
    fetched_at: str = ""


class HiringVelocityEngine:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.hv_config = self.config.get("hiring_velocity", {})
        self.lookback_runs_30d = self.hv_config.get("lookback_runs_30d", 8)
        self.baseline_runs_1y = self.hv_config.get("baseline_runs_1y", 252)
        self.min_baseline_count = self.hv_config.get("min_baseline_count", 1)
        self.sources = self.hv_config.get("sources", ["linkedin", "indeed", "themuse"])
        self.throttle_per_source = self.hv_config.get("throttle_per_source", 10)
        self.ghost_job_threshold = self.hv_config.get("ghost_job_threshold", 0.0)
        op_frac = self.hv_config.get("operational_fracture", {})
        self.op_delta_threshold = op_frac.get("delta_threshold", 0.9)
        self.op_zscore_threshold = op_frac.get("zscore_threshold", 3.0)
        self.op_sentiment_threshold = op_frac.get("sentiment_threshold", 0.0)

        self.company_mappings = {
            "NVDA": "NVIDIA",
            "AMD": "Advanced Micro Devices",
            "MSFT": "Microsoft",
            "GOOGL": "Google",
            "META": "Meta",
            "TSLA": "Tesla",
            "AAPL": "Apple",
            "AMZN": "Amazon",
        }

        self._history: Dict[str, deque] = {source: deque(maxlen=self.baseline_runs_1y) for source in self.sources}
        self._last_run: Dict[str, datetime] = {}

    def _get_cache_key(self, ticker: str, source: str) -> str:
        return f"jobspy_{ticker}_{source}_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

    async def fetch_job_counts(self, ticker: str) -> Dict[str, JobSpySnapshot]:
        if not JOBSPY_AVAILABLE:
            logger.warning("JobSpy not installed, returning empty results")
            return {}

        company = self.company_mappings.get(ticker, ticker)
        snapshots = {}

        for source in self.sources:
            await self._throttle(source)
            try:
                jobs = scrape_jobs(
                    site_name=source,
                    search_term=company,
                    results_wanted=self.throttle_per_source,
                    hours_old=720,
                    country_indeed="USA",
                )
                count = len(jobs) if jobs is not None else 0
            except Exception as e:
                logger.error(f"JobSpy {source} error for {company}: {e}")
                count = 0

            now = datetime.now(timezone.utc)
            date_str = now.strftime("%Y-%m-%d")

            historical_counts = [h["count"] for h in self._history[source] if h["ticker"] == ticker]
            job_count_8_runs_ago = historical_counts[-self.lookback_runs_30d] if len(historical_counts) >= self.lookback_runs_30d else None

            delta_30d = None
            if job_count_8_runs_ago and job_count_8_runs_ago > 0:
                delta_30d = (count - job_count_8_runs_ago) / job_count_8_runs_ago

            mean_252 = mean(historical_counts) if len(historical_counts) >= self.min_baseline_count else None
            std_252 = stdev(historical_counts) if len(historical_counts) > 1 else None

            zscore_1y = None
            if mean_252 is not None and std_252 and std_252 > 1e-6:
                zscore_1y = (count - mean_252) / std_252

            ghost_job_flag = delta_30d is not None and delta_30d <= self.ghost_job_threshold
            operational_fracture_flag = (
                delta_30d is not None and delta_30d >= self.op_delta_threshold and
                zscore_1y is not None and zscore_1y >= self.op_zscore_threshold
            )

            snapshot = JobSpySnapshot(
                ticker=ticker,
                company_name=company,
                date=date_str,
                source=source,
                job_count=count,
                job_count_8_runs_ago=job_count_8_runs_ago,
                delta_30d=delta_30d,
                mean_252_runs=mean_252,
                std_252_runs=std_252,
                zscore_1y=zscore_1y,
                ghost_job_flag=ghost_job_flag,
                operational_fracture_flag=operational_fracture_flag,
                fetched_at=now.isoformat()
            )

            self._history[source].append({
                "ticker": ticker,
                "count": count,
                "timestamp": now.isoformat()
            })
            self._last_run[source] = now

            snapshots[source] = snapshot

        return snapshots

    async def _nodriver_fallback(self, ticker: str, company: str) -> Dict[str, JobSpySnapshot]:
        """Fallback: Use Nodriver to scrape public job board search results"""
        snapshots = {}
        sources = ["indeed", "linkedin"]
        
        for source in sources:
            await self._throttle(source)
            try:
                if source == "indeed":
                    url = f"https://www.indeed.com/jobs?q={company.replace(' ', '+')}"
                    wait_for = "div.job_seen_beacon, div.result"
                else:
                    url = f"https://www.linkedin.com/jobs/search/?keywords={company.replace(' ', '%20')}"
                    wait_for = "div.job-card-container, li.job-card"
                
                config = NodriverConfig(headless=True)
                
                async def extract_jobs(session: NodriverSession):
                    await asyncio.sleep(random.uniform(3, 5))
                    await session.scroll_down(1500)
                    await asyncio.sleep(2)
                    
                    count = 0
                    if source == "indeed":
                        count_text = await session.evaluate("""
                            () => {
                                const elem = document.querySelector('div.jobsearch-JobCountAndSortPane-jobCount span, div[data-testid="job-count"]');
                                return elem ? elem.textContent : '';
                            }
                        """)
                        if count_text:
                            match = re.search(r'([\d,]+)', count_text.replace(',', ''))
                            if match:
                                count = int(match.group(1).replace(',', ''))
                    else:
                        elements = await session.find_elements("div.job-card-container, li.job-card")
                        count = len(elements)
                    
                    return count
                
                count = await scrape_with_nodriver(url, wait_for=wait_for, config=config, extract_fn=extract_jobs)
                count = count or 0
                
            except Exception as e:
                logger.error(f"Nodriver {source} fallback error for {company}: {e}")
                count = 0

            now = datetime.now(timezone.utc)
            date_str = now.strftime("%Y-%m-%d")

            historical_counts = [h["count"] for h in self._history[source] if h["ticker"] == ticker]
            job_count_8_runs_ago = historical_counts[-self.lookback_runs_30d] if len(historical_counts) >= self.lookback_runs_30d else None

            delta_30d = None
            if job_count_8_runs_ago and job_count_8_runs_ago > 0:
                delta_30d = (count - job_count_8_runs_ago) / job_count_8_runs_ago

            mean_252 = mean(historical_counts) if len(historical_counts) >= self.min_baseline_count else None
            std_252 = stdev(historical_counts) if len(historical_counts) > 1 else None

            zscore_1y = None
            if mean_252 is not None and std_252 and std_252 > 1e-6:
                zscore_1y = (count - mean_252) / std_252

            ghost_job_flag = delta_30d is not None and delta_30d <= self.ghost_job_threshold
            operational_fracture_flag = (
                delta_30d is not None and delta_30d >= self.op_delta_threshold and
                zscore_1y is not None and zscore_1y >= self.op_zscore_threshold
            )

            snapshot = JobSpySnapshot(
                ticker=ticker,
                company_name=company,
                date=date_str,
                source=f"{source}-nodriver-fallback",
                job_count=count,
                job_count_8_runs_ago=job_count_8_runs_ago,
                delta_30d=delta_30d,
                mean_252_runs=mean_252,
                std_252_runs=std_252,
                zscore_1y=zscore_1y,
                ghost_job_flag=ghost_job_flag,
                operational_fracture_flag=operational_fracture_flag,
                fetched_at=now.isoformat()
            )

            self._history[source].append({
                "ticker": ticker,
                "count": count,
                "timestamp": now.isoformat()
            })
            self._last_run[source] = now

            snapshots[source] = snapshot

        return snapshots

    async def fetch_job_counts_with_fallback(self, ticker: str) -> Dict[str, JobSpySnapshot]:
        """Fetch job counts with JobSpy primary and Nodriver fallback"""
        if JOBSPY_AVAILABLE:
            result = await self.fetch_job_counts(ticker)
            has_data = any(s.job_count > 0 for s in result.values())
            if has_data:
                return result
        
        logger.info(f"JobSpy unavailable or returned no data for {ticker}, trying Nodriver fallback")
        company = self.company_mappings.get(ticker, ticker)
        return await self._nodriver_fallback(ticker, company)

    async def _throttle(self, source: str) -> None:
        limiter = RateLimiter(min_delay=12.0, max_delay=25.0)
        await limiter.wait()

    async def get_all_snapshots(self) -> Dict[str, Dict[str, JobSpySnapshot]]:
        results = {}
        for ticker in self.company_mappings:
            logger.info(f"Fetching job data for {ticker}")
            results[ticker] = await with_timeout(
                self.fetch_job_counts_with_fallback(ticker), 
                timeout_seconds=120.0,
                default={}
            )
            await asyncio.sleep(0.5)
        return results

    def calculate_velocity_metrics(self, snapshots: Dict[str, JobSpySnapshot]) -> Dict:
        if not snapshots:
            return {}

        total_count = sum(safe_int(s.job_count) for s in snapshots.values())
        
        delta_values = [safe_float(s.delta_30d) for s in snapshots.values() if s.delta_30d is not None]
        avg_delta_30d = safe_float(mean(delta_values)) if delta_values else 0.0
        
        zscore_values = [safe_float(s.zscore_1y) for s in snapshots.values() if s.zscore_1y is not None]
        avg_zscore = safe_float(mean(zscore_values)) if zscore_values else 0.0

        any_ghost = any(s.ghost_job_flag for s in snapshots.values())
        any_fracture = any(s.operational_fracture_flag for s in snapshots.values())

        return {
            "total_job_count": total_count,
            "avg_delta_30d": guard_bounds(avg_delta_30d, -1.0, 1.0),
            "avg_zscore_1y": guard_bounds(avg_zscore, -5.0, 5.0),
            "ghost_job_detected": any_ghost,
            "operational_fracture_detected": any_fracture,
            "sources": {source: {
                "count": safe_int(s.job_count),
                "delta_30d": guard_bounds(safe_float(s.delta_30d), -1.0, 1.0) if s.delta_30d is not None else None,
                "zscore_1y": guard_bounds(safe_float(s.zscore_1y), -5.0, 5.0) if s.zscore_1y is not None else None,
                "ghost_job": s.ghost_job_flag,
                "operational_fracture": s.operational_fracture_flag
            } for source, s in snapshots.items()}
        }


async def create_hiring_velocity(config_dict: dict = None) -> HiringVelocityEngine:
    return HiringVelocityEngine(config_dict)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    async def test():
        engine = await create_hiring_velocity()
        results = await engine.get_all_snapshots()
        for ticker, sources in results.items():
            print(f"\n{ticker}:")
            for source, snap in sources.items():
                print(f"  {source}: count={snap.job_count}, delta_30d={snap.delta_30d}, zscore={snap.zscore_1y}")
            metrics = engine.calculate_velocity_metrics(sources)
            print(f"  Combined: {metrics}")

    asyncio.run(test())