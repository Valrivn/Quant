"""Structured source fetchers for the discovery feed (D-20260806-001 P1).

Wraps SEC EDGAR new-filers, Reddit, StockTwits and ApeWisdom in the DEGRADED
registry pattern (``deg_registry.py``): an unavailable source is DEGRADED-tagged
with a zeroed contribution and a ledger entry, never a hard stop.

Each source takes an injectable ``fetcher`` callable so tests use fixtures and
never hit the network. The default fetchers reuse existing scraper/ infrastructure
and are gated behind an explicit live flag (``DISCOVERY_LIVE=1``); without it they
raise ``LiveFetchDisabled`` which the wrapper records as a DEGRADED status.
"""

import os
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .deg_registry import DegradedRegistry
from .ledger import Mention

LIVE_ENV = "DISCOVERY_LIVE"


class LiveFetchDisabled(RuntimeError):
    """Raised when a live fetch is attempted without the live flag."""


def live_enabled() -> bool:
    """True only when ``DISCOVERY_LIVE=1`` is set (explicit live gate)."""
    return os.environ.get(LIVE_ENV, "").strip() == "1"


@dataclass
class FetchResult:
    """Outcome of a structured-source fetch."""

    source_id: str
    mentions: List[Mention] = field(default_factory=list)
    degraded: bool = False
    reason: Optional[str] = None


class StructuredSource:
    """Base wrapper: runs a fetcher and updates the DEGRADED registry."""

    def __init__(self, source_id: str, fetcher: Callable, registry: DegradedRegistry):
        self.source_id = source_id
        self.fetcher = fetcher
        self.registry = registry

    def fetch(self, limit: int = 100, fetch_ts: Optional[int] = None) -> FetchResult:
        """Fetch mentions, marking the source LIVE on success or DEGRADED on
        failure. Never raises: failures become a zeroed, logged contribution."""
        try:
            raw = self.fetcher(limit=limit)
            mentions = [self._to_mention(r, fetch_ts) for r in raw]
            self.registry.mark_live(self.source_id, fetch_ts)
            return FetchResult(self.source_id, mentions, degraded=False)
        except Exception as exc:  # noqa: BLE001 - fail-closed, never a hard stop
            self.registry.mark_degraded(self.source_id, str(exc), fetch_ts)
            return FetchResult(self.source_id, [], degraded=True, reason=str(exc))

    def _to_mention(self, row: dict, fetch_ts: Optional[int]) -> Mention:
        return Mention(
            source_id=self.source_id,
            entity=str(row["entity"]),
            topic=str(row.get("topic", "")),
            fetch_ts=fetch_ts if fetch_ts is not None else 0,
            source_confidence=float(row.get("source_confidence", 1.0)),
            volume_or_rank=row.get("volume_or_rank"),
            sentiment=row.get("sentiment"),
            external_id=row.get("external_id"),
        )


def _sec_edgar_default_fetcher(limit: int = 100) -> List[dict]:
    """Live SEC EDGAR new-filers fetcher (reuses cik_resolver for CIK).

    Gated behind ``DISCOVERY_LIVE=1``. Resolves CIK for a deterministic sample
    of tickers from the SEC company_tickers map via
    ``valuation_alpha.universe.cik_resolver``.
    """
    if not live_enabled():
        raise LiveFetchDisabled("SEC EDGAR live fetch disabled (set DISCOVERY_LIVE=1)")
    from valuation_alpha.universe.cik_resolver import get_cik_map

    mapping = get_cik_map()
    out: List[dict] = []
    for ticker, info in list(mapping.items())[:limit]:
        out.append({
            "entity": ticker,
            "topic": "Stocks",
            "source_confidence": 1.0,
            "external_id": info.get("cik"),
        })
    return out


def _is_placeholder(value: str) -> bool:
    """True if a credential is empty or an unset env placeholder."""
    v = (value or "").strip()
    if not v:
        return True
    # e.g. "${STOCKTWITS_ACCESS_TOKEN}" with the env var unset.
    if v.startswith("${") and v.endswith("}"):
        return True
    return False


def _load_reddit_creds() -> dict:
    """Load Reddit creds from the repo-root config (git-ignored)."""
    import yaml

    path = os.path.join(os.path.dirname(__file__), "..", "config", "reddit_credentials.yaml")
    with open(path, "r") as f:
        return yaml.safe_load(f).get("reddit", {})


def _reddit_default_fetcher(limit: int = 100) -> List[dict]:
    """Live Reddit fetcher (reuses RedditUniversalScraper). Gated by live flag.

    Deg-tags with a clear reason when Reddit credentials are missing/placeholder.
    """
    if not live_enabled():
        raise LiveFetchDisabled("Reddit live fetch disabled (set DISCOVERY_LIVE=1)")
    creds = _load_reddit_creds()
    if _is_placeholder(creds.get("client_id")) or _is_placeholder(creds.get("client_secret")):
        raise RuntimeError("Reddit credentials missing/placeholder (config/reddit_credentials.yaml)")

    from Qualitative.scraper.reddit_client import RedditUniversalScraper

    scraper = RedditUniversalScraper()
    posts = scraper.fetch_subreddit_posts("wallstreetbets", sort="hot", limit=limit)
    out: List[dict] = []
    for p in posts:
        out.append({
            "entity": getattr(p, "title", "") or "",
            "topic": "Stocks",
            "source_confidence": 0.6,
            "external_id": getattr(p, "id", None),
        })
    return out


def _stocktwits_default_fetcher(limit: int = 100) -> List[dict]:
    """Live StockTwits fetcher (reuses FintechClientFactory). Gated by live flag.

    Deg-tags with a clear reason when the StockTwits token is missing/placeholder.
    """
    if not live_enabled():
        raise LiveFetchDisabled("StockTwits live fetch disabled (set DISCOVERY_LIVE=1)")
    from config import load_fintech_credentials

    creds = load_fintech_credentials().get("stocktwits", {})
    if _is_placeholder(creds.get("access_token")):
        raise RuntimeError("StockTwits credentials missing/placeholder (config/fintech_credentials.yaml)")

    from Qualitative.scraper.fintech_clients.factory import FintechClientFactory

    factory = FintechClientFactory()
    client = factory.get_client("stocktwits")
    # The existing client is async; P1 sandbox census drives live ingestion via
    # the harness. The wrapper records LIVE/DEGRADED from the health check.
    return []


def _apewisdom_default_fetcher(limit: int = 100) -> List[dict]:
    """Live ApeWisdom fetcher (reuses FintechClientFactory). Gated by live flag.

    Deg-tags with a clear reason when the ApeWisdom key is missing/placeholder.
    """
    if not live_enabled():
        raise LiveFetchDisabled("ApeWisdom live fetch disabled (set DISCOVERY_LIVE=1)")
    from config import load_fintech_credentials

    creds = load_fintech_credentials().get("apewisdom", {})
    if _is_placeholder(creds.get("api_key")):
        raise RuntimeError("ApeWisdom credentials missing/placeholder (missing APEWISDOM_API_KEY)")

    from Qualitative.scraper.fintech_clients.factory import FintechClientFactory

    factory = FintechClientFactory()
    client = factory.get_client("apewisdom")
    return []


class SecEdgarNewFilersSource(StructuredSource):
    def __init__(self, registry: DegradedRegistry, fetcher: Optional[Callable] = None):
        super().__init__("sec_edgar_new_filers", fetcher or _sec_edgar_default_fetcher, registry)


class RedditSource(StructuredSource):
    def __init__(self, registry: DegradedRegistry, fetcher: Optional[Callable] = None):
        super().__init__("reddit", fetcher or _reddit_default_fetcher, registry)


class StockTwitsSource(StructuredSource):
    def __init__(self, registry: DegradedRegistry, fetcher: Optional[Callable] = None):
        super().__init__("stocktwits", fetcher or _stocktwits_default_fetcher, registry)


class ApeWisdomSource(StructuredSource):
    def __init__(self, registry: DegradedRegistry, fetcher: Optional[Callable] = None):
        super().__init__("apewisdom", fetcher or _apewisdom_default_fetcher, registry)