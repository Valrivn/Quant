import pandas as pd
import numpy as np
from typing import List, Dict, Any
from datetime import datetime
from collections import defaultdict
from scraper.fintech_clients.base import FintechMessage


class DataFusionEngine:
    """
    Fuses signals from multiple sources with provenance tracking.
    """

    def __init__(self, source_weights: Dict[str, float] = None):
        self.source_weights = source_weights or {
            "stocktwits": 0.5,
            "apewisdom": 0.3,
            "reddit": 0.2
        }
        self.recency_half_life_hours = 24

    def fuse(self, messages: List[FintechMessage]) -> List[Dict]:
        """Fuse messages into per-ticker daily signals with provenance."""
        if not messages:
            return []

        grouped = defaultdict(lambda: defaultdict(list))
        for msg in messages:
            date_key = msg.created_at.strftime("%Y-%m-%d")
            grouped[msg.ticker][date_key].append(msg)

        fused_signals = []
        for ticker, dates in grouped.items():
            for date_str, msgs in dates.items():
                signal = self._fuse_ticker_day(ticker, date_str, msgs)
                fused_signals.append(signal)

        return fused_signals

    def _fuse_ticker_day(self, ticker: str, date_str: str, messages: List[FintechMessage]) -> Dict:
        """Fuse all messages for a ticker on a given day."""
        weighted_sentiments = []
        provenance = []
        total_weight = 0.0

        for msg in messages:
            src_weight = self.source_weights.get(msg.source, 0.1)

            engagement = sum(msg.engagement.values())
            eng_weight = np.log1p(engagement) / 10.0

            hours_old = (datetime.utcnow() - msg.created_at).total_seconds() / 3600
            recency_weight = np.exp(-hours_old / self.recency_half_life_hours)

            conf_weight = msg.metadata.get("extraction_confidence", 0.5)

            combined_weight = src_weight * (1 + eng_weight) * recency_weight * conf_weight

            sentiment = msg.sentiment_score if msg.sentiment_score is not None else 0.0

            weighted_sentiments.append(sentiment * combined_weight)
            total_weight += combined_weight

            provenance.append({
                "source": msg.source,
                "source_id": msg.source_id,
                "weight": combined_weight,
                "sentiment": sentiment,
                "engagement": engagement,
                "author": msg.author,
                "url": msg.url
            })

        composite_sentiment = sum(weighted_sentiments) / total_weight if total_weight > 0 else 0.0

        category_breakdown = self._compute_category_breakdown(messages)

        return {
            "ticker": ticker,
            "date": date_str,
            "composite_sentiment": composite_sentiment,
            "total_weight": total_weight,
            "message_count": len(messages),
            "sources": list(set(m.source for m in messages)),
            "provenance": provenance,
            "category_breakdown": category_breakdown,
            "created_at": datetime.utcnow().isoformat()
        }

    def _compute_category_breakdown(self, messages: List[FintechMessage]) -> Dict:
        """Break down sentiment by category if available."""
        breakdown = defaultdict(lambda: {"sentiment": 0.0, "weight": 0.0, "count": 0})

        for msg in messages:
            category = msg.metadata.get("category", "unknown")
            src_weight = self.source_weights.get(msg.source, 0.1)
            breakdown[category]["sentiment"] += (msg.sentiment_score or 0) * src_weight
            breakdown[category]["weight"] += src_weight
            breakdown[category]["count"] += 1

        return {
            cat: {
                "weighted_sentiment": data["sentiment"] / data["weight"] if data["weight"] > 0 else 0,
                "total_weight": data["weight"],
                "message_count": data["count"]
            }
            for cat, data in breakdown.items()
        }