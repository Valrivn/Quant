"""Sandbox-gated video source stub for IG/TikTok (D-20260806-001 P1).

Video sources are LOCKED until P1 sandbox evidence (>=1 candidate passing the
qualitative gate). This stub emits a placeholder ``gated`` status and raises if
called to produce candidates without that evidence. No actual scraping of
Instagram/TikTok happens anywhere in this module.
"""

from dataclasses import dataclass
from typing import List, Optional


class VideoSourceLockedError(RuntimeError):
    """Raised when a video source is asked to produce candidates without P1
    evidence. The video gate is locked until the P1 census shows >=1 qualitative
    gate pass."""


@dataclass
class VideoSourceStatus:
    """Placeholder status for a locked video source."""

    source_id: str
    status: str = "gated"
    locked: bool = True
    reason: str = "locked until P1 sandbox evidence (>=1 qualitative-gate pass)"


class VideoSourceStub:
    """Sandbox stub for Instagram/TikTok.

    ``status()`` returns a ``gated`` placeholder. ``produce_candidates`` raises
    ``VideoSourceLockedError`` because the video gate is locked until P1 evidence.
    """

    def __init__(self, source_id: str):
        if source_id not in ("instagram", "tiktok"):
            raise ValueError(f"video source stub only supports instagram/tiktok, got {source_id!r}")
        self.source_id = source_id

    def status(self) -> VideoSourceStatus:
        return VideoSourceStatus(
            source_id=self.source_id,
            status="gated",
        )

    def produce_candidates(self, limit: int = 200) -> List[dict]:
        """Raise: video sources are locked until P1 evidence. Never returns
        candidates."""
        raise VideoSourceLockedError(
            f"{self.source_id} is gated: no candidates until P1 sandbox evidence "
            "(>=1 qualitative-gate pass). No IG/TikTok scraping is performed."
        )