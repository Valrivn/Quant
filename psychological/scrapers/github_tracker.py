import asyncio
import logging
import time
import statistics
import re
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
import aiohttp
from bs4 import BeautifulSoup
from config import load_hybrid_config

logger = logging.getLogger(__name__)


@dataclass
class RepoMetrics:
    repo: str
    stars: int
    forks: int
    commit_count_7d: int
    commit_count_30d: int
    contributor_count: int
    open_issues: int
    updated_at: Optional[str]
    fetched_at: str
    ticker: str
    velocity_history: List[Dict] = field(default_factory=list)


@dataclass
class StructuralBreak:
    ticker: str
    metric: str
    timestamp: str
    z_score: float
    direction: str
    severity: str


class GitHubTracker:
    def __init__(self, config_dict: dict = None):
        hybrid_config = load_hybrid_config()
        self.config = config_dict or hybrid_config.get("psychological", {})
        self.github_config = self.config.get("github", {})
        self.github_mappings = hybrid_config.get("github_mappings", {})
        self.token = self.github_config.get("token")
        self.base_url = "https://api.github.com"
        self.cache_duration = 3600
        self._cache = {}
        self._historical_metrics: Dict[str, List[RepoMetrics]] = {}
        self.structural_breaks: List[StructuralBreak] = []
        self.z_score_threshold = self.github_config.get("structural_break_z_threshold", 3.0)
        
    def _get_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "quant-psychological/1.0"
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        return headers
        
    async def _fetch(self, session: aiohttp.ClientSession, url: str) -> Optional[Dict]:
        try:
            async with session.get(url, headers=self._get_headers()) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 403:
                    logger.warning("GitHub API rate limit hit")
                    return None
                else:
                    logger.warning(f"GitHub API error {response.status}: {url}")
                    return None
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return None
    
    async def discover_repos_for_ticker(self, ticker: str, session: aiohttp.ClientSession) -> List[str]:
        """Dynamically discover GitHub repos for a ticker using search API."""
        query = f"{ticker} language:python,typescript,javascript,go,rust stars:>100"
        search_url = f"{self.base_url}/search/repositories"
        params = {"q": query, "sort": "stars", "order": "desc", "per_page": 10}
        
        try:
            async with session.get(search_url, headers=self._get_headers(), params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    repos = [item["full_name"] for item in data.get("items", [])]
                    logger.info(f"Discovered {len(repos)} repos for {ticker}: {repos[:3]}")
                    return repos
        except Exception as e:
            logger.error(f"Error discovering repos for {ticker}: {e}")
        return []
    
    async def get_repo_metrics(self, repo_full_name: str) -> Dict:
        cache_key = f"repo_{repo_full_name}"
        if cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if time.time() - cached_time < self.cache_duration:
                return cached_data
                
        async with aiohttp.ClientSession() as session:
            repo_url = f"{self.base_url}/repos/{repo_full_name}"
            repo_data = await self._fetch(session, repo_url)
            
            if not repo_data:
                return {}
                
            commits_url = f"{self.base_url}/repos/{repo_full_name}/commits?per_page=100"
            commits_data = await self._fetch(session, commits_url)
            
            contributors_url = f"{self.base_url}/repos/{repo_full_name}/contributors?per_page=100"
            contributors_data = await self._fetch(session, contributors_url)
            
            now = datetime.now(timezone.utc)
            week_ago = now - timedelta(days=7)
            month_ago = now - timedelta(days=30)
            
            commit_count_7d = 0
            commit_count_30d = 0
            if commits_data:
                for commit in commits_data:
                    commit_date_str = commit.get("commit", {}).get("committer", {}).get("date")
                    if commit_date_str:
                        commit_date = datetime.fromisoformat(commit_date_str.replace("Z", "+00:00"))
                        if commit_date >= week_ago:
                            commit_count_7d += 1
                        if commit_date >= month_ago:
                            commit_count_30d += 1
                            
            contributor_count = len(contributors_data) if contributors_data else 0
            
            result = {
                "repo": repo_full_name,
                "stars": repo_data.get("stargazers_count", 0),
                "forks": repo_data.get("forks_count", 0),
                "commit_count_7d": commit_count_7d,
                "commit_count_30d": commit_count_30d,
                "contributor_count": contributor_count,
                "open_issues": repo_data.get("open_issues_count", 0),
                "updated_at": repo_data.get("updated_at"),
                "fetched_at": now.isoformat()
            }
            
            self._cache[cache_key] = (time.time(), result)
            return result

    async def _fetch_web_ui_fallback(self, repo_full_name: str) -> Optional[Dict]:
        """Fallback: GitHub Web UI scraping when API rate limited"""
        try:
            logger.info(f"Attempting GitHub Web UI fallback for {repo_full_name}")
            url = f"https://github.com/{repo_full_name}"
            
            async with aiohttp.ClientSession() as session:
                headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        return None
                    html = await response.text()
            
            soup = BeautifulSoup(html, "html.parser")
            
            stars = 0
            forks = 0
            
            for elem in soup.select("a[href*='stargazers'], a[href*='forks']"):
                text = elem.get_text(strip=True)
                if "star" in text.lower() or "stargazer" in text.lower():
                    match = re.search(r'([\d,]+)', text)
                    if match:
                        stars = int(match.group(1).replace(',', ''))
                elif "fork" in text.lower():
                    match = re.search(r'([\d,]+)', text)
                    if match:
                        forks = int(match.group(1).replace(',', ''))
            
            if stars == 0:
                for elem in soup.find_all(text=re.compile(r'\d+.*star', re.I)):
                    match = re.search(r'([\d,]+)\s*star', elem, re.IGNORECASE)
                    if match:
                        stars = int(match.group(1).replace(',', ''))
                        break
            
            if forks == 0:
                for elem in soup.find_all(text=re.compile(r'\d+.*fork', re.I)):
                    match = re.search(r'([\d,]+)\s*fork', elem, re.IGNORECASE)
                    if match:
                        forks = int(match.group(1).replace(',', ''))
                        break
            
            if stars > 0 or forks > 0:
                logger.info(f"GitHub Web UI fallback success for {repo_full_name}: stars={stars}, forks={forks}")
                return {
                    "repo": repo_full_name,
                    "stars": stars,
                    "forks": forks,
                    "commit_count_7d": 0,
                    "commit_count_30d": 0,
                    "contributor_count": 0,
                    "open_issues": 0,
                    "updated_at": None,
                    "fetched_at": datetime.now(timezone.utc).isoformat()
                }
        except Exception as e:
            logger.warning(f"GitHub Web UI fallback failed for {repo_full_name}: {e}")
        return None

    async def get_repo_metrics_with_fallback(self, repo_full_name: str) -> Dict:
        """Get repo metrics with fallback to web UI on rate limit"""
        result = await self.get_repo_metrics(repo_full_name)
        if result:
            return result
        
        fallback_result = await self._fetch_web_ui_fallback(repo_full_name)
        if fallback_result:
            return fallback_result
        
        return {}
             
    async def get_all_metrics(self) -> Dict[str, Dict]:
        results = {}
        async with aiohttp.ClientSession() as session:
            for ticker, repo in self.github_mappings.items():
                logger.info(f"Fetching GitHub metrics for {ticker} -> {repo}")
                metrics = await self.get_repo_metrics_with_fallback(repo)
                if metrics:
                    metrics["ticker"] = ticker
                    results[ticker] = metrics
                    self._update_historical(ticker, metrics)
                await asyncio.sleep(0.1)
        return results
    
    def _update_historical(self, ticker: str, metrics: Dict):
        if ticker not in self._historical_metrics:
            self._historical_metrics[ticker] = []
        
        repo_metrics = RepoMetrics(
            repo=metrics["repo"],
            stars=metrics["stars"],
            forks=metrics["forks"],
            commit_count_7d=metrics["commit_count_7d"],
            commit_count_30d=metrics["commit_count_30d"],
            contributor_count=metrics["contributor_count"],
            open_issues=metrics["open_issues"],
            updated_at=metrics.get("updated_at"),
            fetched_at=metrics["fetched_at"],
            ticker=ticker
        )
        self._historical_metrics[ticker].append(repo_metrics)
        
        if len(self._historical_metrics[ticker]) > 100:
            self._historical_metrics[ticker] = self._historical_metrics[ticker][-100:]
    
    def detect_structural_breaks(self, ticker: str) -> List[StructuralBreak]:
        """Detect structural breaks in GitHub metrics using z-score analysis."""
        breaks = []
        history = self._historical_metrics.get(ticker, [])
        
        if len(history) < 10:
            return breaks
        
        metrics_to_check = ["stars", "forks", "commit_count_7d", "commit_count_30d"]
        
        for metric in metrics_to_check:
            values = [getattr(h, metric) for h in history]
            if len(values) < 10:
                continue
                
            mean_val = statistics.mean(values[:-1])
            stdev_val = statistics.stdev(values[:-1]) if len(values) > 1 else 0
            
            if stdev_val > 0:
                latest = values[-1]
                z_score = abs(latest - mean_val) / stdev_val
                
                if z_score > self.z_score_threshold:
                    direction = "spike" if latest > mean_val else "drop"
                    severity = "extreme" if z_score > 5 else "moderate"
                    
                    break_obj = StructuralBreak(
                        ticker=ticker,
                        metric=metric,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        z_score=z_score,
                        direction=direction,
                        severity=severity
                    )
                    breaks.append(break_obj)
                    self.structural_breaks.append(break_obj)
                    logger.warning(f"Structural break detected for {ticker}: {metric} {direction} (z={z_score:.2f})")
        
        return breaks
    
    def get_velocity_history(self, ticker: str, metric: str, lookback: int = 20) -> List[float]:
        """Get historical velocity for a specific metric."""
        history = self._historical_metrics.get(ticker, [])
        if len(history) < 2:
            return []
        
        velocities = []
        for i in range(1, min(len(history), lookback + 1)):
            prev_val = getattr(history[-(i+1)], metric)
            curr_val = getattr(history[-i], metric)
            velocities.append(curr_val - prev_val)
        
        return list(reversed(velocities))
    
    def calculate_velocities(self, current: Dict, previous: Dict = None) -> Dict:
        if not previous:
            return {
                "dev_fork_acceleration": 0.0,
                "star_velocity": 0.0,
                "commit_velocity": 0.0
            }
            
        time_diff_hours = 1.0
        
        star_velocity = (current.get("stars", 0) - previous.get("stars", 0)) / time_diff_hours
        fork_velocity = (current.get("forks", 0) - previous.get("forks", 0)) / time_diff_hours
        commit_velocity = (current.get("commit_count_7d", 0) - previous.get("commit_count_7d", 0)) / time_diff_hours
        
        fork_acceleration = fork_velocity - (previous.get("fork_velocity", 0) if "fork_velocity" in previous else 0)
        
        return {
            "dev_fork_acceleration": fork_acceleration,
            "star_velocity": star_velocity,
            "commit_velocity": commit_velocity
        }
    
    def get_structural_breaks(self, ticker: str = None, since: datetime = None) -> List[StructuralBreak]:
        """Get detected structural breaks, optionally filtered by ticker and time."""
        breaks = self.structural_breaks
        if ticker:
            breaks = [b for b in breaks if b.ticker == ticker]
        if since:
            breaks = [b for b in breaks if datetime.fromisoformat(b.timestamp.replace("Z", "+00:00")) >= since]
        return breaks


async def create_github_tracker(config_dict: dict = None) -> GitHubTracker:
    return GitHubTracker(config_dict)


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    async def test():
        tracker = await create_github_tracker()
        metrics = await tracker.get_all_metrics()
        for ticker, data in metrics.items():
            print(f"{ticker}: {data}")
            
    asyncio.run(test())