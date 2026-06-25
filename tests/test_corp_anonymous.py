import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from psychological.scrapers.corp_anonymous import CorpAnonymousScraper, create_corp_anonymous_scraper


class TestCorpAnonymousScraper:
    @pytest.fixture
    def mock_config(self):
        return {
            "psychological": {
                "corp_anonymous": {
                    "cache_duration": 86400
                }
            }
        }

    @pytest.fixture
    def scraper(self, mock_config):
        with patch('psychological.scrapers.corp_anonymous.load_hybrid_config') as mock_load:
            mock_load.return_value = {
                "psychological": mock_config["psychological"],
                "adzuna": {
                    "app_id": "test_app_id",
                    "app_key": "test_app_key",
                    "base_url": "https://api.adzuna.com/v1/api/jobs",
                    "country": "us"
                }
            }
            scraper = CorpAnonymousScraper(config_dict=mock_config["psychological"])
            yield scraper

    def test_init(self, scraper):
        assert scraper is not None
        assert "NVDA" in scraper.company_mappings
        assert "AAPL" in scraper.company_mappings
        assert scraper.app_id == "test_app_id"
        assert scraper.app_key == "test_app_key"
        assert scraper.base_url == "https://api.adzuna.com/v1/api/jobs"

    def test_get_company_mappings(self, scraper):
        mappings = scraper._get_company_mappings()
        assert mappings["NVDA"] == "NVIDIA"
        assert mappings["MSFT"] == "Microsoft"
        assert len(mappings) >= 8

    def test_get_cache_key(self, scraper):
        cache_key = scraper._get_cache_key("AAPL")
        assert cache_key.startswith("adzuna_AAPL_")
        assert datetime.now(timezone.utc).strftime('%Y-%m-%d') in cache_key

    @pytest.mark.asyncio
    async def test_fetch_adzuna_success(self, scraper):
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"count": 500, "results": []})
        
        class MockCM:
            async def __aenter__(self):
                return mock_response
            async def __aexit__(self, *args):
                return None
        
        mock_session.get = Mock(return_value=MockCM())
        
        result = await scraper._fetch_adzuna(mock_session, "Apple")
        assert result == {"count": 500, "results": []}

    @pytest.mark.asyncio
    async def test_fetch_adzuna_rate_limit(self, scraper):
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status = 429
        
        class MockCM:
            async def __aenter__(self):
                return mock_response
            async def __aexit__(self, *args):
                return None
        
        mock_session.get = Mock(return_value=MockCM())
        
        result = await scraper._fetch_adzuna(mock_session, "Apple")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_adzuna_no_credentials(self, scraper):
        scraper.app_id = None
        scraper.app_key = None
        
        mock_session = Mock()
        result = await scraper._fetch_adzuna(mock_session, "Apple")
        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_career_index_fallback(self, scraper):
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value="<html>1,234 jobs found</html>")
        
        class MockCM:
            async def __aenter__(self):
                return mock_response
            async def __aexit__(self, *args):
                return None
        
        mock_session.get = Mock(return_value=MockCM())
        
        result = await scraper._fetch_career_index_fallback(mock_session, "Apple")
        assert result == 1234

    @pytest.mark.asyncio
    async def test_get_job_count_from_adzuna(self, scraper):
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"count": 1000, "results": []})
        
        class MockCM:
            async def __aenter__(self):
                return mock_response
            async def __aexit__(self, *args):
                return None
        
        mock_session.get = Mock(return_value=MockCM())
        
        class MockClientSession:
            async def __aenter__(self):
                return mock_session
            async def __aexit__(self, *args):
                return None
        
        with patch('aiohttp.ClientSession', return_value=MockClientSession()):
            count = await scraper.get_job_count("AAPL")
        
        assert count == 1000

    @pytest.mark.asyncio
    async def test_get_job_count_fallback(self, scraper):
        scraper._fetch_adzuna = AsyncMock(return_value=None)
        scraper._fetch_adzuna_web_ui_fallback = AsyncMock(return_value=None)
        scraper._fetch_jobspy_fallback = AsyncMock(return_value=None)
        scraper._fetch_career_index_fallback = AsyncMock(return_value=500)
        
        count = await scraper.get_job_count("AAPL")
        
        assert count == 500

    @pytest.mark.asyncio
    async def test_get_historical_snapshots(self, scraper):
        scraper.get_job_count = AsyncMock(return_value=2000)
        
        snapshot = await scraper.get_historical_snapshots("AAPL")
        
        assert snapshot["ticker"] == "AAPL"
        assert snapshot["company_name"] == "Apple"
        assert snapshot["job_count"] == 2000
        assert snapshot["date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert "fetched_at" in snapshot

    @pytest.mark.asyncio
    async def test_get_historical_snapshots_failed(self, scraper):
        scraper.get_job_count = AsyncMock(return_value=None)
        
        snapshot = await scraper.get_historical_snapshots("AAPL")
        
        assert snapshot == {}

    def test_calculate_sentiment_proxy(self, scraper):
        current = {"job_count": 1000}
        previous_7d = {"job_count": 800}
        previous_30d = {"job_count": 700}
        
        sentiment = scraper.calculate_sentiment_proxy(current, previous_7d, previous_30d)
        
        assert sentiment > 0
        assert sentiment <= 1.0

    def test_calculate_sentiment_proxy_negative(self, scraper):
        current = {"job_count": 500}
        previous_7d = {"job_count": 1000}
        
        sentiment = scraper.calculate_sentiment_proxy(current, previous_7d)
        
        assert sentiment < 0
        assert sentiment >= -1.0

    def test_calculate_sentiment_proxy_none(self, scraper):
        current = {"job_count": None}
        sentiment = scraper.calculate_sentiment_proxy(current)
        assert sentiment == 0.0

    def test_calculate_sentiment_proxy_empty(self, scraper):
        current = {}
        sentiment = scraper.calculate_sentiment_proxy(current)
        assert sentiment == 0.0

    @pytest.mark.asyncio
    async def test_get_all_snapshots(self, scraper):
        scraper.get_historical_snapshots = AsyncMock(side_effect=lambda t: {
            "ticker": t,
            "company_name": scraper.company_mappings.get(t, t),
            "job_count": 1000
        } if t == "AAPL" else {})
        
        results = await scraper.get_all_snapshots()
        
        assert "AAPL" in results
        assert results["AAPL"]["job_count"] == 1000


class TestCreateCorpAnonymousScraper:
    @pytest.mark.asyncio
    async def test_create_corp_anonymous_scraper(self):
        with patch('psychological.scrapers.corp_anonymous.load_hybrid_config') as mock_load:
            mock_load.return_value = {
                "psychological": {"corp_anonymous": {}},
                "adzuna": {"app_id": "test", "app_key": "test"}
            }
            scraper = await create_corp_anonymous_scraper()
            assert isinstance(scraper, CorpAnonymousScraper)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])