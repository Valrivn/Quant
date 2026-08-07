"""DEGRADED-ledger per source for the discovery feed (D-20260804-002 pattern).

When a source is unavailable it is tagged DEGRADED: its contribution is zeroed
and a ledger entry is recorded. This is never a hard stop for one missing API.

Fail-closed rule (SEC 3.1): cross-source agreement requires >=2 LIVE independent
sources; if fewer, that cycle emits no candidates (logged). A degraded source
can never be the second agreeing source alone.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Minimum number of LIVE independent sources required for a cycle to emit
# candidates (SEC 3.1 fail-closed rule).
MIN_LIVE_SOURCES_FOR_AGREEMENT = 2

LIVE = "LIVE"
DEGRADED = "DEGRADED"


@dataclass
class SourceStatus:
    """Per-source status in the degraded registry."""

    source_id: str
    status: str  # LIVE | DEGRADED
    last_checked_at: Optional[int] = None
    reason: Optional[str] = None


@dataclass
class DegradedLedgerEntry:
    """A ledger entry recording that a source's contribution was zeroed."""

    source_id: str
    cycle_ts: int
    reason: str
    contribution_zeroed: bool = True


class DegradedRegistry:
    """Tracks per-source LIVE/DEGRADED status and the degraded ledger.

    Deterministic: no RNG. Status transitions are explicit calls.
    """

    def __init__(self, min_live_sources: int = MIN_LIVE_SOURCES_FOR_AGREEMENT):
        self.min_live_sources = min_live_sources
        self._status: Dict[str, SourceStatus] = {}
        self._ledger: List[DegradedLedgerEntry] = []

    def mark_live(self, source_id: str, checked_at: int = None) -> None:
        """Mark a source LIVE (available)."""
        self._status[source_id] = SourceStatus(source_id, LIVE, checked_at)

    def mark_degraded(self, source_id: str, reason: str, cycle_ts: int = None) -> None:
        """Mark a source DEGRADED, zeroing its contribution and logging it."""
        self._status[source_id] = SourceStatus(source_id, DEGRADED, cycle_ts, reason)
        self._ledger.append(
            DegradedLedgerEntry(source_id=source_id, cycle_ts=cycle_ts, reason=reason)
        )

    def is_live(self, source_id: str) -> bool:
        """True if the source is currently LIVE (non-degraded)."""
        st = self._status.get(source_id)
        return st is not None and st.status == LIVE

    def status_of(self, source_id: str) -> Optional[SourceStatus]:
        """Return the SourceStatus for a source, or None if never seen."""
        return self._status.get(source_id)

    def live_sources(self) -> List[str]:
        """Source ids currently LIVE, in insertion order."""
        return [sid for sid, st in self._status.items() if st.status == LIVE]

    def degraded_sources(self) -> List[str]:
        """Source ids currently DEGRADED, in insertion order."""
        return [sid for sid, st in self._status.items() if st.status == DEGRADED]

    def agreement_ok(self) -> bool:
        """Fail-closed: True only if >= min_live_sources LIVE independent sources.

        If False, the cycle must emit no candidates (SEC 3.1).
        """
        return len(self.live_sources()) >= self.min_live_sources

    def ledger_entries(self) -> List[DegradedLedgerEntry]:
        """All degraded-ledger entries, in insertion order."""
        return list(self._ledger)