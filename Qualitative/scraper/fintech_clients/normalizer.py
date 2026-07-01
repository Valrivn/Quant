from typing import List, Set, Dict, Any
from dataclasses import dataclass
from datetime import datetime
import re
from scraper.fintech_clients.base import FintechMessage


@dataclass
class NormalizerConfig:
    ticker_blacklist: Set[str]
    validation_keywords: Set[str]
    min_text_length: int = 10
    max_text_length: int = 5000


class FintechNormalizer:
    """Normalizes and deduplicates fintech messages across sources."""

    def __init__(self, ticker_blacklist: Set[str], validation_keywords: Set[str]):
        self.config = NormalizerConfig(ticker_blacklist, validation_keywords)
        self._seen_hashes: Set[str] = set()

    def normalize_batch(self, messages: List[FintechMessage]) -> List[FintechMessage]:
        """Normalize a batch of messages."""
        normalized = []
        for msg in messages:
            norm_msg = self._normalize_single(msg)
            if norm_msg and self._is_valid(norm_msg):
                normalized.append(norm_msg)
        return self.deduplicate(normalized)

    def _normalize_single(self, msg: FintechMessage) -> FintechMessage:
        """Normalize a single message."""
        text = self._clean_text(msg.text)
        tickers = self._extract_tickers(text, msg.source)

        return FintechMessage(
            source=msg.source,
            source_id=msg.source_id,
            ticker=tickers[0] if tickers else msg.ticker,
            text=text,
            sentiment_score=msg.sentiment_score,
            author=msg.author,
            created_at=msg.created_at,
            engagement=msg.engagement,
            url=msg.url,
            metadata={**msg.metadata, "extraction_confidence": self._calc_confidence(text, tickers)}
        )

    def _clean_text(self, text: str) -> str:
        """Clean and normalize text content."""
        if not text:
            return ""
        text = re.sub(r'\s+', ' ', text)
        text = re.sub(r'[^\x00-\x7F]+', '', text)  # Remove non-ASCII
        return text.strip()[:self.config.max_text_length]

    def _extract_tickers(self, text: str, source: str) -> List[str]:
        """Extract tickers with source-specific patterns."""
        if source == "stocktwits":
            pattern = r"\$([A-Z]{1,5})\b"
        elif source == "apewisdom":
            pattern = r"\b([A-Z]{1,5})\b"
        else:
            pattern = r"\b([A-Z]{1,5})\b"

        found = re.findall(pattern, text)
        valid = [t for t in found if t not in self.config.ticker_blacklist and len(t) >= 2]
        return list(set(valid))

    def _calc_confidence(self, text: str, tickers: List[str]) -> float:
        """Calculate extraction confidence."""
        if not tickers:
            return 0.1
        text_lower = text.lower()
        context_count = sum(1 for kw in self.config.validation_keywords if kw in text_lower)
        context_strength = min(context_count / 5.0, 1.0)
        return 0.4 + (context_strength * 0.4) + min(len(tickers[0]) / 5.0, 0.2)

    def _is_valid(self, msg: FintechMessage) -> bool:
        """Validate normalized message."""
        return (
            len(msg.text) >= self.config.min_text_length and
            msg.ticker not in self.config.ticker_blacklist and
            len(msg.ticker) >= 2
        )

    def deduplicate(self, messages: List[FintechMessage]) -> List[FintechMessage]:
        """Remove duplicate messages based on source and source_id."""
        unique = []
        seen_ids = set()
        for msg in messages:
            msg_id = f"{msg.source}:{msg.source_id}"
            if msg_id not in seen_ids:
                seen_ids.add(msg_id)
                unique.append(msg)
        return unique

    def clear_cache(self):
        """Clear deduplication cache."""
        self._seen_hashes.clear()

    def _canonicalize_ticker(self, ticker: str) -> str:
        """Normalize ticker format (e.g., $AAPL -> AAPL)."""
        if not ticker:
            return ""
        ticker = ticker.strip().upper()
        if ticker.startswith("$"):
            ticker = ticker[1:]
        return ticker

    def _compute_confidence(self, msg: FintechMessage) -> float:
        """Compute confidence score for a message."""
        base_confidence = {"stocktwits": 0.9, "apewisdom": 0.8, "reddit": 0.6}.get(msg.source, 0.5)
        source_bonus = {"stocktwits": 0.15, "apewisdom": 0.1, "reddit": 0.0}.get(msg.source, 0.0)
        engagement = msg.engagement or {}
        likes = engagement.get("likes", 0) + engagement.get("upvotes", 0)
        engagement_bonus = min(likes / 1000.0, 0.1) if likes > 0 else 0.0
        text_confidence = self._calc_confidence(msg.text, [msg.ticker])
        return min(base_confidence + source_bonus + engagement_bonus + (text_confidence - 0.5) * 0.3, 1.0)