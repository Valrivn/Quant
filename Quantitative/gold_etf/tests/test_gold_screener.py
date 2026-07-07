def test_gold_screener_syntax():
    try:
        import Quantitative.gold_etf.gold_etf_screener
        assert True
    except Exception as e:
        assert False, f"Failed to import gold_etf_screener: {e}"
