"""Tests for GDELT news sentiment fetcher."""

import os
import pytest
from unittest.mock import patch, MagicMock
from discovery.gdelt import (
    fetch_gdelt_news,
    run_pipeline,
)


@pytest.fixture
def mock_gdelt_response():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {
      "articles": [
        {
          "url": "https://example.com/article1",
          "title": "Microsoft does things",
          "seendate": "20260822T120000Z",
          "domain": "example.com",
          "sourcecountry": "United States",
          "tone": -1.5
        }
      ]
    }
    return mock


@patch("discovery.gdelt.requests.get")
def test_fetch_gdelt_news(mock_get, mock_gdelt_response):
    mock_get.return_value = mock_gdelt_response
    news = fetch_gdelt_news("MSFT", "Microsoft")
    assert len(news) == 1
    assert news[0]["ticker"] == "MSFT"
    assert news[0]["company"] == "Microsoft"
    assert news[0]["url"] == "https://example.com/article1"
    assert news[0]["title"] == "Microsoft does things"
    assert news[0]["seendate"] == "20260822T120000Z"
    assert news[0]["domain"] == "example.com"
    assert news[0]["sourcecountry"] == "United States"
    assert news[0]["sentiment_placeholder"] == -1.5


@patch("discovery.gdelt.requests.get")
def test_run_pipeline(mock_get, mock_gdelt_response, tmp_path):
    mock_get.return_value = mock_gdelt_response
    test_csv = tmp_path / "test_gdelt.csv"
    
    with patch("discovery.gdelt.CSV_FILE", str(test_csv)):
        tickers = [("MSFT", "Microsoft")]
        new_count = run_pipeline(tickers)
        
        assert new_count == 1
        assert os.path.exists(test_csv)
        
        # Verify deduplication / append-safe logic
        # Run again with same data
        new_count_dup = run_pipeline(tickers)
        assert new_count_dup == 0
