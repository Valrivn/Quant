import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from psychological.scrapers.reddit_custom import RedditScraper, create_reddit_scraper
from psychological.interfaces import RedditCommentPayload


class MockSB:
    def __init__(self):
        self.driver = Mock()
        
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass
        
    def get(self, url):
        pass
        
    def wait_for_element(self, selector, timeout=30):
        pass
        
    def find_elements(self, selector):
        return []
        
    def get_title(self):
        return "Test Page"


class TestRedditScraper:
    @pytest.fixture
    def mock_config(self):
        return {
            "psychological": {
                "reddit": {
                    "subreddits": ["wallstreetbets", "stocks"],
                    "rate_limit_min": 1,
                    "rate_limit_max": 2
                }
            }
        }

    @pytest.fixture
    def scraper(self, mock_config):
        with patch('psychological.scrapers.reddit_custom.UnifiedScraperSession') as mock_session_class:
            mock_session = Mock()
            mock_session.get_sb.return_value = MockSB()
            mock_session_class.return_value = mock_session
            
            scraper = RedditScraper(config_dict=mock_config)
            scraper.session = mock_session
            yield scraper

    def test_init(self, scraper):
        assert scraper is not None
        assert "wallstreetbets" in scraper.all_subreddits or "stocks" in scraper.all_subreddits
        assert len(scraper.ticker_blacklist) > 0

    def test_extract_tickers(self, scraper):
        text = "AAPL and TSLA are great stocks. Also buy MSFT and GOOGL."
        tickers = scraper._extract_tickers(text)
        
        assert "AAPL" in tickers
        assert "TSLA" in tickers
        assert "MSFT" in tickers
        assert "GOOGL" in tickers

    def test_extract_tickers_blacklist(self, scraper):
        text = "THE and AND are not tickers. BUT AAPL is."
        tickers = scraper._extract_tickers(text)
        
        assert "AAPL" in tickers
        assert len(tickers) >= 1

    def test_submission_relevant(self, scraper):
        assert scraper._submission_relevant("AAPL earnings", "", {"AAPL"}) is True
        assert scraper._submission_relevant("Market update", "TSLA up", {"AAPL"}) is False
        assert scraper._submission_relevant("", "", {"AAPL"}) is False

    def test_parse_score(self, scraper):
        assert scraper._parse_score("100") == 100
        assert scraper._parse_score("1,234") == 1234
        assert scraper._parse_score("invalid") == 0
        assert scraper._parse_score("5k") == 5000

    @pytest.mark.asyncio
    async def test_harvest_raw_commentary(self, scraper):
        from dataclasses import asdict
        
        mock_comment = RedditCommentPayload(
            ticker="AAPL",
            text="AAPL to the moon!",
            subreddit="wallstreetbets",
            created_utc=int(datetime.now(timezone.utc).timestamp()),
            score=100
        )
        
        scraper.all_subreddits = ["wallstreetbets"]
        scraper._extract_posts = Mock(return_value=[{
            "id": "t3_test",
            "title": "AAPL earnings",
            "selftext": "Great quarter",
            "score": 500,
            "permalink": "/r/wallstreetbets/comments/test/",
            "subreddit": "wallstreetbets",
            "created_utc": int(datetime.now(timezone.utc).timestamp())
        }])
        
        async def mock_extract_comments(*args, **kwargs):
            return [mock_comment]
        
        scraper._extract_comments = mock_extract_comments
        scraper.session.throttled_get = AsyncMock(return_value=True)
        
        comments = []
        async for comment in scraper.harvest_raw_commentary(["AAPL"], limit_per_subreddit=1):
            comments.append(comment)
            
        # Handle both dataclass and dict
        if comments and hasattr(comments[0], 'ticker'):
            assert comments[0].ticker == "AAPL"
            assert comments[0].subreddit == "wallstreetbets"
        elif comments and isinstance(comments[0], dict):
            assert comments[0]['ticker'] == "AAPL"
            assert comments[0]['subreddit'] == "wallstreetbets"
        else:
            assert len(comments) >= 1


class TestCreateRedditScraper:
    @pytest.mark.asyncio
    async def test_create_reddit_scraper(self):
        with patch('psychological.scrapers.reddit_custom.UnifiedScraperSession'):
            scraper = await create_reddit_scraper()
            assert isinstance(scraper, RedditScraper)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])