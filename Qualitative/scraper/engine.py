import re
import json
import hashlib
import nltk
import logging
from nltk.sentiment.vader import SentimentIntensityAnalyzer
from typing import List, Dict, Tuple
from config import TICKER_BLACKLIST, VALIDATION_KEYWORDS, FINANCIAL_LEXICON, ENTITY_RESOLUTION, RISK_KEYWORDS

logger = logging.getLogger(__name__)

nltk.download('vader_lexicon', quiet=True)


class QuantSentimentEngine:
    """Financial sentiment engine with VADER + custom lexicon and ticker extraction."""

    def __init__(self):
        self.sia = SentimentIntensityAnalyzer()
        self.sia.lexicon.update(FINANCIAL_LEXICON)
        self.ticker_pattern = re.compile(r"\b[A-Z]{1,5}\b")
        self._model_version = self._compute_model_version()

    def _compute_model_version(self) -> Dict[str, str]:
        lexicon_str = json.dumps(self.sia.lexicon, sort_keys=True)
        lexicon_hash = hashlib.sha256(lexicon_str.encode()).hexdigest()[:16]
        return {
            "model_version": f"vader-financial-v1-{lexicon_hash}",
            "lexicon_hash": lexicon_hash,
            "nltk_version": nltk.__version__,
            "analyzer_config": json.dumps({
                "custom_terms_count": len(FINANCIAL_LEXICON),
                "blacklist_count": len(TICKER_BLACKLIST),
                "entity_count": len(ENTITY_RESOLUTION)
            })
        }

    def extract_tickers(self, text: str) -> List[str]:
        result = self.extract_tickers_with_confidence(text)
        return [t for t, _ in result]

    def extract_tickers_with_confidence(self, text: str) -> List[Tuple[str, float]]:
        logger.debug(f"Extracting tickers from text (length: {len(text)})")
        found = self.ticker_pattern.findall(text)
        text_lower = text.lower()

        context_count = sum(1 for kw in VALIDATION_KEYWORDS if kw in text_lower)
        context_strength = min(context_count / 5.0, 1.0)
        has_context = context_count > 0

        ticker_confidence = {}

        for t in found:
            if t in TICKER_BLACKLIST or len(t) < 1:
                continue
            if len(t) <= 2 and not has_context:
                continue

            length_bonus = min(len(t) / 5.0, 0.3)
            confidence = 0.4 + (context_strength * 0.4) + length_bonus
            ticker_confidence[t] = max(ticker_confidence.get(t, 0), confidence)

        for company, ticker in ENTITY_RESOLUTION.items():
            if ticker in TICKER_BLACKLIST:
                continue
            pattern = r'\b' + re.escape(company) + r'\b'
            if re.search(pattern, text_lower):
                confidence = 0.35 + (context_strength * 0.4)
                ticker_confidence[ticker] = max(ticker_confidence.get(ticker, 0), confidence)

        result = sorted(ticker_confidence.items(), key=lambda x: x[1], reverse=True)
        logger.debug(f"Found {len(result)} tickers: {result[:5]}...")
        return result

    def analyze_sentiment(self, text: str) -> float:
        if not text.strip():
            return 0.0
        score = self.sia.polarity_scores(text)["compound"]
        logger.debug(f"Sentiment score: {score:.4f}")
        return score

    def analyze_sentiment_detailed(self, text: str) -> Dict:
        if not text.strip():
            return {"compound": 0.0, "pos": 0.0, "neu": 0.0, "neg": 0.0}
        return self.sia.polarity_scores(text)

    def scan_risks(self, text: str) -> Dict[str, int]:
        text_lower = text.lower()
        risk_counts = {}

        for risk_type, keywords in RISK_KEYWORDS.items():
            count = 0
            for kw in keywords:
                if kw in text_lower:
                    count += 1
            if count > 0:
                risk_counts[risk_type] = count

        if risk_counts:
            logger.debug(f"Risk signals detected: {risk_counts}")
        return risk_counts

    def get_model_version(self) -> Dict[str, str]:
        return self._model_version

    def process_fintech_message(self, text: str, source_sentiment: float = None, source: str = "unknown") -> Dict:
        """Process a fintech message with optional pre-computed sentiment."""
        tickers = self.extract_tickers(text)
        sentiment = source_sentiment if source_sentiment is not None else self.analyze_sentiment(text)
        risks = self.scan_risks(text)
        return {
            "tickers": tickers,
            "sentiment": sentiment,
            "risks": risks,
            "text": text,
            "source": source
        }

    def extract_tickers_from_fintech_message(self, text: str, source: str) -> List[str]:
        """Extract tickers with source-specific patterns."""
        if source == "stocktwits":
            pattern = r"\$([A-Z]{1,5})\b"
        elif source == "apewisdom":
            pattern = r"\b([A-Z]{1,5})\b"
        else:
            pattern = r"\b([A-Z]{1,5})\b"
        
        found = re.findall(pattern, text)
        valid_tickers = [t for t in found if t not in TICKER_BLACKLIST and len(t) >= 2]
        return list(set(valid_tickers))