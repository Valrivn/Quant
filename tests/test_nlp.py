from scraper.engine import QuantSentimentEngine

def test_ticker_extraction():
    engine = QuantSentimentEngine()
    
    # Direct uppercase ticker
    text1 = "Long positions in MSFT and AAPL are looking solid."
    tickers1 = engine.extract_tickers(text1)
    assert "MSFT" in tickers1
    assert "AAPL" in tickers1
    
    # Blacklisted word should be skipped
    text2 = "This is a great GPU for AI, yolo!"
    tickers2 = engine.extract_tickers(text2)
    assert "GPU" not in tickers2
    assert "YOLO" not in tickers2
    
    # Entity resolution: company name -> ticker
    text3 = "I am buying shares of microsoft and nvidia."
    tickers3 = engine.extract_tickers(text3)
    assert "MSFT" in tickers3
    assert "NVDA" in tickers3

def test_sentiment_scoring():
    engine = QuantSentimentEngine()
    
    # Bullish indicators (should have positive compound score)
    bullish_text = "I am extremely bullish on AMD, calls are printing moon tendies!"
    score_bull = engine.analyze_sentiment(bullish_text)
    assert score_bull > 0.3
    
    # Bearish indicators (should have negative compound score)
    bearish_text = "The stock is completely overvalued, bagholders are facing bankrupt dump."
    score_bear = engine.analyze_sentiment(bearish_text)
    assert score_bear < -0.3

def test_risk_detection():
    engine = QuantSentimentEngine()
    
    # Geopolitical and supply chain risk mentions
    text = "We expect factory closures and semiconductor shortages due to tensions and tariff wars in Taiwan."
    risks = engine.scan_risks(text)
    
    assert risks["geopolitical"] >= 2  # 'tensions', 'tariff'
    assert risks["supply_chain"] >= 2  # 'factory closure', 'semiconductor shortage'
