import re
from typing import Dict, List, Tuple, Optional, Pattern
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from config import load_hybrid_config


class NLPEngine:
    def __init__(self, config_dict: dict = None):
        self.config = config_dict or load_hybrid_config().get("psychological", {})
        self.analyzer = SentimentIntensityAnalyzer()
        self._load_financial_lexicon()
        self._compile_patterns()
        
    def _load_financial_lexicon(self) -> None:
        bullish_terms = self.config.get("bullish_terms", {})
        bearish_terms = self.config.get("bearish_terms", {})
        
        financial_lexicon = {}
        for term, weight in bullish_terms.items():
            financial_lexicon[term.lower()] = weight
        for term, weight in bearish_terms.items():
            financial_lexicon[term.lower()] = -weight
            
        self.analyzer.lexicon.update(financial_lexicon)
        self.bullish_terms = {k.lower(): v for k, v in bullish_terms.items()}
        self.bearish_terms = {k.lower(): v for k, v in bearish_terms.items()}
        
    def _compile_patterns(self) -> None:
        self.ticker_pattern = re.compile(r'\$?([A-Z]{1,5})\b')
        self.options_pattern = re.compile(r'\b(calls?|puts?)\b', re.IGNORECASE)
        self.position_pattern = re.compile(r'\b(long|short)\b', re.IGNORECASE)
        self.slang_bullish = re.compile(
            r'\b(diamond\s+hands?|moon|tendies|yolo|rip|green|bullish|undervalued|rocket|🚀|💎|🙌)\b', 
            re.IGNORECASE
        )
        self.slang_bearish = re.compile(
            r'\b(paper\s+hands?|bagholder|dump|tank|crash|red|bearish|overvalued)\b', 
            re.IGNORECASE
        )
        self.emoji_bullish = re.compile(r'[🚀💎🙌📈💰🤑🔥]')
        self.emoji_bearish = re.compile(r'[📉💸😭🐻🩸💀]')
        
    def analyze(self, text: str) -> Dict:
        if not text or not text.strip():
            return {
                "compound_vader": 0.0,
                "bull_bear_ratio": 1.0,
                "bullish_count": 0,
                "bearish_count": 0,
                "options_signal": 0.0,
                "position_signal": 0.0,
                "emoji_bullish": 0,
                "emoji_bearish": 0,
                "slang_bullish": 0,
                "slang_bearish": 0,
                "ticker_mentions": []
            }
            
        scores = self.analyzer.polarity_scores(text)
        compound = scores["compound"]
        
        bullish_count = 0
        bearish_count = 0
        text_lower = text.lower()
        
        for term, weight in self.bullish_terms.items():
            count = len(re.findall(rf"\b{re.escape(term)}\b", text_lower))
            bullish_count += count * weight
            
        for term, weight in self.bearish_terms.items():
            count = len(re.findall(rf"\b{re.escape(term)}\b", text_lower))
            bearish_count += count * weight
            
        options_matches = self.options_pattern.findall(text_lower)
        options_signal = sum(1 if m == 'call' or m == 'calls' else -1 for m in options_matches)
        
        position_matches = self.position_pattern.findall(text_lower)
        position_signal = sum(1 if m == 'long' else -1 for m in position_matches)
        
        slang_bullish = len(self.slang_bullish.findall(text_lower))
        slang_bearish = len(self.slang_bearish.findall(text_lower))
        
        emoji_bullish = len(self.emoji_bullish.findall(text))
        emoji_bearish = len(self.emoji_bearish.findall(text))
        
        ticker_mentions = list(set(self.ticker_pattern.findall(text.upper())))
        
        try:
            ratio = bullish_count / bearish_count if bearish_count > 0 else 99.0
        except ZeroDivisionError:
            ratio = 99.0
            
        return {
            "compound_vader": compound,
            "bull_bear_ratio": ratio,
            "bullish_count": int(bullish_count),
            "bearish_count": int(bearish_count),
            "options_signal": float(options_signal),
            "position_signal": float(position_signal),
            "emoji_bullish": emoji_bullish,
            "emoji_bearish": emoji_bearish,
            "slang_bullish": slang_bullish,
            "slang_bearish": slang_bearish,
            "ticker_mentions": ticker_mentions
        }
        
    def analyze_batch(self, texts: List[str]) -> List[Dict]:
        return [self.analyze(text) for text in texts]


def create_nlp_engine(config_dict: dict = None) -> NLPEngine:
    return NLPEngine(config_dict)


if __name__ == "__main__":
    engine = create_nlp_engine()
    test_texts = [
        "AAPL calls to the moon! Diamond hands 💎🙌",
        "TSLA puts printing, paper hands everywhere",
        "NVDA undervalued, long calls",
        "Market crash coming, short everything",
        "$SPY $QQQ long calls 🚀 diamond hands",
        "SPY puts bearish 📉 paper hands 😭"
    ]
    for text in test_texts:
        result = engine.analyze(text)
        print(f"Text: {text}")
        print(f"Result: {result}\n")