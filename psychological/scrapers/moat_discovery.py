import asyncio
import logging
import math
import re
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from curl_cffi import AsyncSession
from config import load_hybrid_config

logger = logging.getLogger(__name__)


@dataclass
class MoatNode:
    name: str
    source: str
    ticker: str
    node_type: str
    description: Optional[str] = None
    stars: Optional[int] = None
    forks: Optional[int] = None
    url: Optional[str] = None
    overall_rating: Optional[object] = None
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __hash__(self) -> int:
        return hash((self.name.lower(), self.ticker))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MoatNode):
            return NotImplemented
        return self.name.lower() == other.name.lower() and self.ticker == other.ticker


def process_node_ratings(node):
    # Enforce strict type checking to handle single-object schema variations safely
    ratings_list = node.overall_rating if isinstance(node.overall_rating, list) else [node.overall_rating]
    processed_scores = []
    for r in ratings_list:
        if hasattr(r, 'rating_value'):
            processed_scores.append(float(r.rating_value))
        else:
            processed_scores.append(0.0)
    return processed_scores



@dataclass
class MoatTree:
    ticker: str
    company_name: str
    nodes: List[MoatNode] = field(default_factory=list)
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def add_node(self, node: MoatNode) -> None:
        if node not in self.nodes:
            self.nodes.append(node)

    @property
    def count(self) -> int:
        return len(self.nodes)

    @property
    def wikipedia_nodes(self) -> List[MoatNode]:
        return [n for n in self.nodes if n.source == "wikipedia"]

    @property
    def github_nodes(self) -> List[MoatNode]:
        return [n for n in self.nodes if n.source == "github"]


class MoatDiscoveryEngine:
    COMPANY_NAMES: Dict[str, str] = {
        "NVDA": "NVIDIA",
        "AMD": "AMD",
        "MSFT": "Microsoft",
        "GOOGL": "Google",
        "META": "Meta",
        "TSLA": "Tesla",
        "AAPL": "Apple",
        "AMZN": "Amazon",
        "AVGO": "Broadcom",
        "INTC": "Intel",
        "ARM": "ARM",
    }

    GITHUB_ORGS: Dict[str, str] = {
        "NVDA": "NVIDIA",
        "AMD": "AMD",
        "MSFT": "Microsoft",
        "GOOGL": "Google",
        "META": "facebook",
        "TSLA": "teslamotors",
        "AAPL": "apple",
        "AMZN": "aws",
        "AVGO": "Broadcom",
        "INTC": "intel",
    }

    WIKIPEDIA_SLUGS: Dict[str, str] = {
        "NVDA": "Nvidia",
        "AMD": "Advanced_Micro_Devices",
        "MSFT": "Microsoft",
        "GOOGL": "Google",
        "META": "Meta_Platforms",
        "TSLA": "Tesla,_Inc.",
        "AAPL": "Apple_Inc.",
        "AMZN": "Amazon_(company)",
        "AVGO": "Broadcom_Inc.",
        "INTC": "Intel",
        "ARM": "Arm_Holdings",
    }

    MAX_NODES_PER_TICKER: int = 8

    def __init__(self, config_dict: Optional[dict] = None):
        self.config = config_dict or load_hybrid_config()
        self._curl_session: Optional[AsyncSession] = None

    async def initialize(self) -> None:
        if self._curl_session is None:
            self._curl_session = AsyncSession(
                impersonate="chrome120",
                timeout=30,
            )
            logger.info("MoatDiscoveryEngine initialized with curl_cffi")

    async def close(self) -> None:
        if self._curl_session:
            await self._curl_session.close()
            self._curl_session = None

    async def __aenter__(self) -> "MoatDiscoveryEngine":
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    @staticmethod
    def _guard_namespace(name: str, company_name: str) -> str:
        cleaned = name.strip()
        if " " not in cleaned:
            return f"{company_name} {cleaned}"
        return cleaned

    async def discover_wikipedia_nodes(self, ticker: str) -> List[MoatNode]:
        slug = self.WIKIPEDIA_SLUGS.get(ticker)
        if not slug:
            logger.warning("No Wikipedia slug for %s", ticker)
            return []

        company_name = self.COMPANY_NAMES.get(ticker, ticker)
        nodes: List[MoatNode] = []
        url = f"https://en.wikipedia.org/wiki/{slug}"

        try:
            await self.initialize()
            response = await self._curl_session.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
            if response.status_code != 200:
                logger.warning("Wikipedia fetch failed for %s: status %s", slug, response.status_code)
                return []

            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            infobox = soup.find("table", class_="infobox")
            if not infobox:
                logger.warning("No infobox found for %s", slug)
                return []

            for row in infobox.find_all("tr"):
                header = row.find("th")
                data = row.find("td")
                if not header or not data:
                    continue

                header_text = header.get_text(strip=True).lower()
                if header_text not in ("products", "product", "platforms", "services", "brands", "divisions"):
                    continue

                for link in data.find_all("a"):
                    name = link.get_text(strip=True)
                    if not name or len(name) < 2:
                        continue
                    guarded = self._guard_namespace(name, company_name)
                    href = link.get("href", "")
                    full_url = f"https://en.wikipedia.org{href}" if href.startswith("/") else href
                    nodes.append(MoatNode(
                        name=guarded,
                        source="wikipedia",
                        ticker=ticker,
                        node_type="platform",
                        description=None,
                        url=full_url if full_url.startswith("http") else None,
                    ))

                if not nodes:
                    text_items = data.get_text(separator=",", strip=True)
                    for item in text_items.split(","):
                        item = item.strip()
                        if item and len(item) > 2:
                            guarded = self._guard_namespace(item, company_name)
                            nodes.append(MoatNode(
                                name=guarded,
                                source="wikipedia",
                                ticker=ticker,
                                node_type="platform",
                                description=None,
                            ))

            logger.info("Wikipedia discovery for %s: %d nodes", ticker, len(nodes))
            return nodes

        except Exception as e:
            logger.error("Wikipedia discovery error for %s: %s", ticker, e)
            return []

    async def discover_github_nodes(self, ticker: str) -> List[MoatNode]:
        org = self.GITHUB_ORGS.get(ticker)
        if not org:
            logger.warning("No GitHub org for %s", ticker)
            return []

        company_name = self.COMPANY_NAMES.get(ticker, ticker)
        nodes: List[MoatNode] = []

        try:
            await self.initialize()
            url = f"https://api.github.com/orgs/{org}/repos?per_page=100&sort=stars&direction=desc"
            response = await self._curl_session.get(
                url,
                headers={
                    "User-Agent": "quant-psychological/1.0",
                    "Accept": "application/vnd.github.v3+json",
                },
            )

            if response.status_code == 403:
                logger.warning("GitHub API rate limited for org %s", org)
                return []
            if response.status_code != 200:
                logger.warning("GitHub API error for org %s: status %s", org, response.status_code)
                return []

            import json as _json
            repos = _json.loads(response.text)

            for repo in repos:
                name = repo.get("name", "")
                stars = repo.get("stargazers_count", 0) or 0
                if stars <= 100:
                    continue

                repo_name = repo.get("full_name", f"{org}/{name}")
                guarded = self._guard_namespace(name, company_name)
                nodes.append(MoatNode(
                    name=guarded,
                    source="github",
                    ticker=ticker,
                    node_type="repository",
                    description=repo.get("description"),
                    stars=stars,
                    forks=repo.get("forks_count", 0) or 0,
                    url=repo.get("html_url", f"https://github.com/{org}/{name}"),
                ))

            logger.info("GitHub discovery for %s (%s): %d nodes", ticker, org, len(nodes))
            return nodes

        except Exception as e:
            logger.error("GitHub discovery error for %s: %s", ticker, e)
            return []

    @staticmethod
    def _deduplicate(nodes: List[MoatNode]) -> List[MoatNode]:
        seen: Set[str] = set()
        result: List[MoatNode] = []
        for node in nodes:
            key = node.name.lower()
            if key not in seen:
                seen.add(key)
                result.append(node)
        return result

    async def discover(self, ticker: str) -> MoatTree:
        company_name = self.COMPANY_NAMES.get(ticker, ticker)

        wiki_nodes = await self.discover_wikipedia_nodes(ticker)
        gh_nodes = await self.discover_github_nodes(ticker)

        combined = self._deduplicate(wiki_nodes + gh_nodes)

        capped = combined[:self.MAX_NODES_PER_TICKER]

        tree = MoatTree(ticker=ticker, company_name=company_name)
        for node in capped:
            tree.add_node(node)

        logger.info(
            "Moat discovery for %s: %d wiki + %d gh = %d unique, capped to %d",
            ticker, len(wiki_nodes), len(gh_nodes), len(combined), tree.count,
        )
        return tree


async def create_moat_discovery_engine(config_dict: Optional[dict] = None) -> MoatDiscoveryEngine:
    return MoatDiscoveryEngine(config_dict)


class MoatWeightingLayer:
    """Weight nodes by GitHub star velocity decay + SEC 10-K revenue segment cross-referencing + moat_overrides."""

    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        moat_config = self.config if isinstance(self.config, dict) else {}
        self.star_decay_factor = moat_config.get("star_decay_factor", 0.15)
        self.max_nodes = moat_config.get("max_nodes_per_ticker", 8)
        self.sec_revenue_segments = moat_config.get("sec_revenue_segments", {})
        self.moat_overrides = moat_config.get("moat_overrides", {})

    def compute_star_velocity(self, node: MoatNode) -> float:
        if node.stars is None:
            return 0.0
        return max(0.0, min(1.0, node.stars / 10000.0))

    def apply_star_velocity_decay(self, node: MoatNode) -> float:
        raw = self.compute_star_velocity(node)
        decayed = raw * math.exp(-self.star_decay_factor)
        return max(0.0, min(1.0, decayed))

    def cross_reference_revenue_segments(self, node: MoatNode, ticker: str) -> float:
        segments = self._fetch_revenue_segments(ticker)
        if not segments or not node.description:
            return 0.0
        desc_lower = node.description.lower()
        matches = sum(1 for seg in segments if seg.lower() in desc_lower)
        return min(1.0, matches / max(len(segments), 1))

    def _fetch_revenue_segments(self, ticker: str) -> List[str]:
        return self.sec_revenue_segments.get(ticker, [])

    def node_override(self, node: MoatNode) -> Optional[float]:
        node_key = f"{node.ticker}:{node.name}"
        return self.moat_overrides.get(node_key)

    def compute_node_weight(self, node: MoatNode, ticker: str) -> float:
        override = self.node_override(node)
        if override is not None:
            return override
        star_score = self.apply_star_velocity_decay(node)
        revenue_score = self.cross_reference_revenue_segments(node, ticker)
        return max(0.0, min(1.0, 0.6 * star_score + 0.4 * revenue_score))

    def rank_nodes(self, tree: MoatTree) -> MoatTree:
        for node in tree.nodes:
            weight = self.compute_node_weight(node, tree.ticker)
            node.stars = int(weight * 10000) if node.stars is None or weight > (node.stars / 10000.0) else node.stars
        tree.nodes.sort(key=lambda n: -(n.stars or 0))
        tree.nodes = tree.nodes[:self.max_nodes]
        return tree


class MoatScoringEngine:
    """Budget-constrained node scoring loops (<=20 SERP queries per ticker) across G2, Reddit, Capterra, App Store RSS."""

    MAX_SERP_QUERIES = 20

    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config()
        self.product_intel = None
        self.reddit_scraper = None
        self._serp_counts: Dict[str, int] = {}

    async def initialize(self, product_intel=None, reddit_scraper=None) -> None:
        self.product_intel = product_intel
        self.reddit_scraper = reddit_scraper

    def _consume_budget(self, ticker: str, count: int = 1) -> bool:
        used = self._serp_counts.get(ticker, 0)
        if used + count > self.MAX_SERP_QUERIES:
            return False
        self._serp_counts[ticker] = used + count
        return True

    def budget_remaining(self, ticker: str) -> int:
        return max(0, self.MAX_SERP_QUERIES - self._serp_counts.get(ticker, 0))

    async def score_node_g2(self, node: MoatNode, ticker: str) -> Optional[float]:
        if not self._consume_budget(ticker):
            return None
        if self.product_intel and hasattr(self.product_intel, "g2_scraper"):
            try:
                res = await self.product_intel.g2_scraper.scrape_company(ticker)
                if res is None:
                    return None
                node.overall_rating = res
                ratings = process_node_ratings(node)
                if ratings:
                    return sum(ratings) / len(ratings)
            except Exception as e:
                logger.warning("G2 scoring failed for %s: %s", node.name, e)
        return None

    async def score_node_reddit(self, node: MoatNode, ticker: str) -> Optional[int]:
        if not self._consume_budget(ticker):
            return None
        if self.reddit_scraper:
            try:
                mentions = 0
                async for payload in self.reddit_scraper.harvest_raw_commentary([ticker]):
                    if node.name.lower() in payload["text"].lower():
                        mentions += 1
                return mentions
            except Exception as e:
                logger.warning("Reddit scoring failed for %s: %s", node.name, e)
        return None

    async def score_node_capterra(self, node: MoatNode, ticker: str) -> Optional[float]:
        if not self._consume_budget(ticker):
            return None
        if self.product_intel and hasattr(self.product_intel, "capterra_scraper"):
            try:
                res = await self.product_intel.capterra_scraper.scrape_company(ticker)
                if res is None:
                    return None
                node.overall_rating = res
                ratings = process_node_ratings(node)
                if ratings:
                    return sum(ratings) / len(ratings)
            except Exception as e:
                logger.warning("Capterra scoring failed for %s: %s", node.name, e)
        return None

    async def score_node_app_store(self, node: MoatNode, ticker: str) -> Optional[float]:
        if not self._consume_budget(ticker):
            return None
        if self.product_intel and hasattr(self.product_intel, "app_store_scraper"):
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    res = await self.product_intel.app_store_scraper.scrape_company(ticker, session)
                    if res is None:
                        return None
                    node.overall_rating = res
                    ratings = process_node_ratings(node)
                    if ratings:
                        return sum(ratings) / len(ratings)
            except Exception as e:
                logger.warning("App Store scoring failed for %s: %s", node.name, e)
        return None

    async def score_node(self, node: MoatNode, ticker: str) -> MoatNode:
        budget = self.budget_remaining(ticker)
        if budget <= 0:
            return node

        g2_rating = await self.score_node_g2(node, ticker)
        capterra_rating = await self.score_node_capterra(node, ticker)
        app_store_rating = await self.score_node_app_store(node, ticker)
        reddit_mentions = await self.score_node_reddit(node, ticker)

        scores = []
        if g2_rating is not None:
            scores.append(g2_rating / 5.0)
        if capterra_rating is not None:
            scores.append(capterra_rating / 5.0)
        if app_store_rating is not None:
            scores.append(app_store_rating / 5.0)
        if reddit_mentions is not None and reddit_mentions > 0:
            scores.append(min(1.0, reddit_mentions / 100.0))

        if scores:
            node.stars = int((sum(scores) / len(scores)) * 10000)

        return node

    async def score_tree(self, tree: MoatTree) -> MoatTree:
        self._serp_counts[tree.ticker] = 0
        for node in tree.nodes:
            node = await self.score_node(node, tree.ticker)
        tree.nodes.sort(key=lambda n: -(n.stars or 0))
        return tree


async def create_moat_weighting_layer(config_dict: dict = None) -> MoatWeightingLayer:
    return MoatWeightingLayer(config_dict)


async def create_moat_scoring_engine(config_dict: dict = None) -> MoatScoringEngine:
    return MoatScoringEngine(config_dict)
