"""Structured source fetchers for the discovery feed.

Wraps official sources (such as SEC EDGAR new-filers) and compliant alternative data
feeds in the DEGRADED registry pattern (``deg_registry.py``): an unavailable source is
DEGRADED-tagged with a zeroed contribution and a ledger entry, never a hard stop.
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
    """Live SEC EDGAR new-filers fetcher (reuses cik_resolver for CIK)."""
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


class SecEdgarNewFilersSource(StructuredSource):
    def __init__(self, registry: DegradedRegistry, fetcher: Optional[Callable] = None):
        super().__init__("sec_edgar_new_filers", fetcher or _sec_edgar_default_fetcher, registry)
