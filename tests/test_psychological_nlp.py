import pytest
from psychological.nlp_engine import NLPEngine, create_nlp_engine


class TestNLPEngine:
    @pytest.fixture
    def engine(self):
        return create_nlp_engine()

    def test_bullish_sentiment(self, engine):
        text = "AAPL calls to the moon! Diamond hands 💎🙌"
        result = engine.analyze(text)
        
        assert result["compound_vader"] > 0
        assert result["bull_bear_ratio"] > 1.0
        assert result["bullish_count"] > result["bearish_count"]
        assert "AAPL" in result["ticker_mentions"]

    def test_bearish_sentiment(self, engine):
        text = "TSLA puts printing, paper hands everywhere 📉😭"
        result = engine.analyze(text)
        
        assert result["compound_vader"] < 0
        assert result["bull_bear_ratio"] < 1.0
        assert result["bearish_count"] > result["bullish_count"]
        assert "TSLA" in result["ticker_mentions"]

    def test_zero_division_handling(self, engine):
        text = "NVDA undervalued, long calls"
        result = engine.analyze(text)
        
        assert result["bull_bear_ratio"] == 99.0
        assert result["bearish_count"] == 0

    def test_empty_text(self, engine):
        result = engine.analyze("")
        
        assert result["compound_vader"] == 0.0
        assert result["bull_bear_ratio"] == 1.0
        assert result["bullish_count"] == 0
        assert result["bearish_count"] == 0

    def test_options_signal(self, engine):
        text = "Buy $SPY call, sell $QQQ put"
        result = engine.analyze(text)
        
        assert result["options_signal"] == 0  # call=+1, put=-1 = 0
        
        text2 = "Buy $SPY calls, buy $QQQ calls"
        result2 = engine.analyze(text2)
        
        assert result2["options_signal"] == 2

    def test_position_signal(self, engine):
        text = "Long AAPL, short TSLA"
        result = engine.analyze(text)
        
        assert result["position_signal"] == 0

    def test_emoji_detection(self, engine):
        text = "To the moon 🚀💎🙌"
        result = engine.analyze(text)
        
        assert result["emoji_bullish"] >= 3

    def test_slang_detection(self, engine):
        text = "Diamond hands yolo tendies"
        result = engine.analyze(text)
        
        assert result["slang_bullish"] >= 3

    def test_batch_analysis(self, engine):
        texts = [
            "AAPL calls to the moon!",
            "TSLA puts printing",
            "NVDA undervalued"
        ]
        results = engine.analyze_batch(texts)
        
        assert len(results) == 3
        assert all("compound_vader" in r for r in results)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])