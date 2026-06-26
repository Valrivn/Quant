import pytest
import json
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from psychological.scrapers.product_intel import (
    G2Scraper, CapterraScraper, AppStoreScraper, ProductIntelEngine,
    create_product_intel_engine, G2Review, CapterraReview, AppStoreReview
)


class MockCurlResponse:
    def __init__(self, status_code=200, text="<html></html>"):
        self.status_code = status_code
        self.text = text


class MockCurlSession:
    def __init__(self):
        self._responses = {}
    
    async def get(self, url, headers=None, timeout=30):
        response = self._responses.get(url)
        if response:
            return response
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.text = "<html></html>"
        return mock_resp
    
    async def close(self):
        pass
    
    def set_response(self, url, status_code=200, text=""):
        mock_resp = Mock()
        mock_resp.status_code = status_code
        mock_resp.text = text
        self._responses[url] = mock_resp


class TestG2Scraper:
    @pytest.fixture
    def mock_config(self):
        return {
            "g2_capterra": {
                "keywords": ["bug", "crash", "slow", "fast", "great", "terrible", "love", "hate"],
                "date_filter_days": 90
            }
        }

    @pytest.fixture
    def scraper(self, mock_config):
        with patch('psychological.scrapers.product_intel.load_hybrid_config') as mock_load:
            mock_load.return_value = {"g2_capterra": mock_config["g2_capterra"]}
            scraper = G2Scraper(config_dict=mock_config)
            scraper._curl_session = MockCurlSession()
            yield scraper

    def test_init(self, scraper):
        assert scraper is not None
        assert "NVDA" in scraper.company_mappings
        assert "bug" in scraper.keywords

    def test_detect_keywords(self, scraper):
        text = "This software has a bug and is slow but great"
        keywords = scraper._detect_keywords(text)
        assert "bug" in keywords
        assert "slow" in keywords
        assert "great" in keywords
        assert len(keywords) == 3

    def test_detect_keywords_empty(self, scraper):
        keywords = scraper._detect_keywords("")
        assert keywords == []
        
        keywords = scraper._detect_keywords(None)
        assert keywords == []

    @pytest.mark.asyncio
    async def test_scrape_company_success(self, scraper):
        bing_html = """
        <html>
            <li class="b_algo">
                <p>NVIDIA Reviews - Rating: 4.5/5 - Great product, very fast!</p>
            </li>
            <li class="b_algo">
                <p>NVIDIA Reviews - Rating: 3.0/5 - Has some bugs but okay</p>
            </li>
        </html>
        """
        scraper._curl_session.set_response(
            "https://www.bing.com/search?q=site:g2.com/products/nvidia+reviews+rating",
            status_code=200,
            text=bing_html
        )
        
        reviews = await scraper.scrape_company("NVDA")
        
        assert len(reviews) == 2
        assert reviews[0].ticker == "NVDA"
        assert reviews[0].rating == 4.5
        assert "fast" in reviews[0].keywords_detected
        assert reviews[1].rating == 3.0
        assert "bug" in reviews[1].keywords_detected

    @pytest.mark.asyncio
    async def test_scrape_company_not_found(self, scraper):
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status = 404
        
        class MockCM:
            async def __aenter__(self):
                return mock_response
            async def __aexit__(self, *args):
                return None
        
        mock_session.get = Mock(return_value=MockCM())
        
        reviews = await scraper.scrape_company("NVDA", mock_session)
        assert reviews == []

    @pytest.mark.asyncio
    async def test_scrape_company_no_slug(self, scraper):
        mock_session = Mock()
        reviews = await scraper.scrape_company("NONEXISTENT", mock_session)
        assert reviews == []


class TestCapterraScraper:
    @pytest.fixture
    def mock_config(self):
        return {
            "g2_capterra": {
                "keywords": ["bug", "crash", "slow", "fast", "great", "terrible"],
                "date_filter_days": 90
            }
        }

    @pytest.fixture
    def scraper(self, mock_config):
        with patch('psychological.scrapers.product_intel.load_hybrid_config') as mock_load:
            mock_load.return_value = {"g2_capterra": mock_config["g2_capterra"]}
            scraper = CapterraScraper(config_dict=mock_config)
            scraper._curl_session = MockCurlSession()
            yield scraper

    def test_init(self, scraper):
        assert scraper is not None
        assert "AAPL" in scraper.company_mappings

    def test_detect_keywords(self, scraper):
        text = "Terrible experience, crashes often"
        keywords = scraper._detect_keywords(text)
        assert "terrible" in keywords
        assert "crash" in keywords

    @pytest.mark.asyncio
    async def test_scrape_company_success(self, scraper):
        bing_html = """
        <html>
            <li class="b_algo">
                <div class="b_caption">
                    <p>Apple Capterra - Rating: 5.0/5 - Excellent software, very fast</p>
                </div>
            </li>
            <li class="b_algo">
                <div class="b_caption">
                    <p>Apple Capterra - Rating: 2.0/5 - Buggy and slow</p>
                </div>
            </li>
        </html>
        """
        scraper._curl_session.set_response(
            "https://www.bing.com/search?q=site:capterra.com/p/apple+reviews+rating",
            status_code=200,
            text=bing_html
        )
        
        reviews = await scraper.scrape_company("AAPL")
        
        assert len(reviews) == 2
        assert reviews[0].ticker == "AAPL"
        assert reviews[0].rating == 5.0
        assert "fast" in reviews[0].keywords_detected
        assert reviews[1].rating == 2.0
        assert "bug" in reviews[1].keywords_detected


class TestAppStoreScraper:
    @pytest.fixture
    def mock_config(self):
        return {
            "app_store": {
                "flagship_apps": {
                    "AAPL": "apple-store",
                    "GOOGL": "google-maps"
                },
                "date_filter_days": 90
            }
        }

    @pytest.fixture
    def scraper(self, mock_config):
        with patch('psychological.scrapers.product_intel.load_hybrid_config') as mock_load:
            mock_load.return_value = {"app_store": mock_config["app_store"]}
            scraper = AppStoreScraper(config_dict=mock_config)
            yield scraper

    def test_init(self, scraper):
        assert scraper is not None
        assert scraper.flagship_apps["AAPL"] == "apple-store"

    def test_vader_score(self, scraper):
        score = scraper._vader_score("I love this app!")
        assert score is not None
        assert -1.0 <= score <= 1.0
        
        score = scraper._vader_score("")
        assert score is not None
        assert score == 0.0

    @pytest.mark.asyncio
    async def test_scrape_company_success(self, scraper):
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value=json.dumps({
            "feed": {
                "entry": [
                    {"im:rating": {"label": "5"}, "content": {"label": "Amazing app!"}, "updated": {"label": "2024-01-15T10:00:00Z"}},
                    {"im:rating": {"label": "3"}, "content": {"label": "Okay but has bugs"}, "updated": {"label": "2024-01-10T10:00:00Z"}}
                ]
            }
        }))
        
        class MockCM:
            async def __aenter__(self):
                return mock_response
            async def __aexit__(self, *args):
                return None
        
        mock_session.get = Mock(return_value=MockCM())
        
        reviews = await scraper.scrape_company("AAPL", mock_session)
        
        assert len(reviews) == 2
        assert reviews[0].ticker == "AAPL"
        assert reviews[0].rating == 5.0
        assert reviews[0].vader_compound is not None
        assert reviews[1].rating == 3.0

    @pytest.mark.asyncio
    async def test_scrape_company_no_app(self, scraper):
        mock_session = Mock()
        reviews = await scraper.scrape_company("NONEXISTENT", mock_session)
        assert reviews == []


class TestProductIntelEngine:
    @pytest.fixture
    def mock_config(self):
        return {
            "g2_capterra": {"keywords": ["bug", "great"], "date_filter_days": 90},
            "app_store": {"flagship_apps": {"AAPL": "apple-store"}, "date_filter_days": 90}
        }

    @pytest.fixture
    def engine(self, mock_config):
        with patch('psychological.scrapers.product_intel.load_hybrid_config') as mock_load:
            mock_load.return_value = {
                "g2_capterra": mock_config["g2_capterra"],
                "app_store": mock_config["app_store"]
            }
            engine = ProductIntelEngine(config_dict=mock_config)
            engine.g2_scraper = Mock()
            engine.capterra_scraper = Mock()
            engine.app_store_scraper = Mock()
            yield engine

    def test_init(self, engine):
        assert engine is not None
        assert "NVDA" in engine.company_mappings

    @pytest.mark.asyncio
    async def test_gather_intel(self, engine):
        mock_g2 = [G2Review("AAPL", "g2", "apple", 4.5, "Great!", "2024-01-15", ["great"], datetime.now(timezone.utc).isoformat())]
        mock_cap = [CapterraReview("AAPL", "capterra", "apple", 4.0, "Good", "2024-01-15", ["great"], datetime.now(timezone.utc).isoformat())]
        mock_app = [AppStoreReview("AAPL", "apple", "apple-store", "apple-store", 5.0, "Love it", "2024-01-15", 0.8, datetime.now(timezone.utc).isoformat())]
        
        engine.g2_scraper.scrape_company = AsyncMock(return_value=mock_g2)
        engine.capterra_scraper.scrape_company = AsyncMock(return_value=mock_cap)
        engine.app_store_scraper.scrape_company = AsyncMock(return_value=mock_app)
        
        results = await engine.gather_intel(["AAPL"])
        
        assert "AAPL" in results
        assert len(results["AAPL"]["g2"]) == 1
        assert len(results["AAPL"]["capterra"]) == 1
        assert len(results["AAPL"]["app_store"]) == 1

    def test_compute_product_sentiment_vader_only(self, engine):
        reviews = [
            {"vader_compound": 0.8, "rating": None},
            {"vader_compound": 0.5, "rating": None},
            {"vader_compound": -0.3, "rating": None}
        ]
        
        sentiment = engine.compute_product_sentiment(reviews)
        assert -1.0 <= sentiment <= 1.0

    def test_compute_product_sentiment_rating_only(self, engine):
        reviews = [
            {"rating": 5, "vader_compound": None},
            {"rating": 4, "vader_compound": None},
            {"rating": 2, "vader_compound": None}
        ]
        
        sentiment = engine.compute_product_sentiment(reviews)
        assert -1.0 <= sentiment <= 1.0

    def test_compute_product_sentiment_both(self, engine):
        reviews = [
            {"vader_compound": 0.8, "rating": 5},
            {"vader_compound": -0.2, "rating": 3}
        ]
        
        sentiment = engine.compute_product_sentiment(reviews)
        assert -1.0 <= sentiment <= 1.0

    def test_compute_product_sentiment_empty(self, engine):
        sentiment = engine.compute_product_sentiment([])
        assert sentiment == 0.0
        
        sentiment = engine.compute_product_sentiment([{}])
        assert sentiment == 0.0


class TestCreateProductIntelEngine:
    @pytest.mark.asyncio
    async def test_create_product_intel_engine(self):
        with patch('psychological.scrapers.product_intel.load_hybrid_config') as mock_load:
            mock_load.return_value = {"g2_capterra": {}, "app_store": {}}
            engine = await create_product_intel_engine()
            assert isinstance(engine, ProductIntelEngine)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])