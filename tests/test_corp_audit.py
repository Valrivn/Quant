import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from psychological.scrapers.corp_audit import (
    GlassdoorScraper, ComparablyScraper, CorpAuditEngine,
    create_corp_audit_engine, create_glassdoor_scraper, create_comparably_scraper,
    GlassdoorScore, ComparablyBadges
)
from curl_cffi import AsyncSession


class MockCurlResponse:
    def __init__(self, status_code=200, text="<html></html>"):
        self.status_code = status_code
        self.text = text


class MockCurlSession:
    def __init__(self):
        self._responses = {}
    
    async def get(self, url, headers=None, timeout=None):
        return self._responses.get(url, MockCurlResponse())
    
    async def close(self):
        pass
    
    def set_response(self, url, status_code=200, text="<html></html>"):
        self._responses[url] = MockCurlResponse(status_code, text)


class MockSession:
    def __init__(self):
        self._sb = Mock()
    
    def get_sb(self):
        return self._sb
    
    async def throttled_get(self, url, wait_for=None, timeout=30):
        return True
    
    def close(self):
        pass


class MockCurlSession:
    def __init__(self):
        self._responses = {}
    
    async def get(self, url, headers=None, timeout=30):
        response = self._responses.get(url)
        if response:
            return response
        # Default mock response
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


class TestGlassdoorScraper:
    @pytest.fixture
    def mock_config(self):
        return {
            "glassdoor": {
                "company_slugs": {
                    "NVDA": "nvidia",
                    "AAPL": "Apple",
                    "MSFT": "Microsoft"
                }
            }
        }

    @pytest.fixture
    def scraper(self, mock_config):
        with patch('psychological.scrapers.corp_audit.load_hybrid_config') as mock_load:
            mock_load.return_value = {"glassdoor": mock_config["glassdoor"]}
            scraper = GlassdoorScraper(config_dict=mock_config)
            scraper._curl_session = MockCurlSession()
            yield scraper

    def test_init(self, scraper):
        assert scraper is not None
        assert "AAPL" in scraper.company_slugs

    def test_parse_score(self, scraper):
        assert scraper._parse_score("4.5 out of 5") == 4.5
        assert scraper._parse_score("3.2/5") == 3.2
        assert scraper._parse_score("invalid") is None

    def test_parse_percentage(self, scraper):
        assert scraper._parse_percentage("85%") == 85
        assert scraper._parse_percentage("100% recommend") == 100
        assert scraper._parse_percentage("invalid") is None

    @pytest.mark.asyncio
    async def test_scrape_company_success(self, scraper):
        bing_html = """
        <html>
            <li class="b_algo">
                <div class="b_caption">
                    <p>NVIDIA Reviews - 4.5 out of 5 - 1000 reviews - 90% CEO Approval - 85% Recommend to Friend</p>
                </div>
            </li>
        </html>
        """
        scraper._curl_session.set_response(
            'https://www.bing.com/search?q=site:glassdoor.com+"NVIDIA"+reviews+rating',
            status_code=200,
            text=bing_html
        )
        
        result = await scraper.scrape_company("NVDA")
        
        assert result.ticker == "NVDA"
        assert result.slug == "nvidia"
        assert result.raw_score == 4.5
        assert result.normalized_score == 0.9

    @pytest.mark.asyncio
    async def test_scrape_company_no_slug(self, scraper):
        result = await scraper.scrape_company("NONEXISTENT")
        assert result.ticker == "NONEXISTENT"
        assert result.raw_score is None

    @pytest.mark.asyncio
    async def test_scrape_all(self, scraper):
        scraper.scrape_company = AsyncMock(side_effect=[
            GlassdoorScore("AAPL", "Apple", 4.5, 0.9, 1000, 90, 85, datetime.now(timezone.utc).isoformat()),
            GlassdoorScore("MSFT", "Microsoft", 4.3, 0.86, 2000, 88, 82, datetime.now(timezone.utc).isoformat())
        ])
        
        results = await scraper.scrape_all(["AAPL", "MSFT"])
        
        assert "AAPL" in results
        assert "MSFT" in results


class TestComparablyScraper:
    @pytest.fixture
    def mock_config(self):
        return {
            "comparably": {
                "company_slugs": {
                    "AAPL": "apple",
                    "GOOGL": "google"
                }
            }
        }

    @pytest.fixture
    def scraper(self, mock_config):
        with patch('psychological.scrapers.corp_audit.load_hybrid_config') as mock_load:
            mock_load.return_value = {"comparably": mock_config["comparably"]}
            scraper = ComparablyScraper(config_dict=mock_config)
            scraper._curl_session = MockCurlSession()
            yield scraper

    def test_init(self, scraper):
        assert scraper is not None
        assert "AAPL" in scraper.company_slugs

    def test_parse_badge_score(self, scraper):
        assert scraper._parse_badge_score("85 out of 100") == 85
        assert scraper._parse_badge_score("90/100") == 90
        assert scraper._parse_badge_score("invalid") is None

    @pytest.mark.asyncio
    async def test_scrape_company_success(self, scraper):
        bing_html = """
        <html>
            <li class="b_algo">
                <div class="b_caption">
                    <p>Apple Comparably - 85 out of 100 - 5 badges</p>
                </div>
            </li>
        </html>
        """
        scraper._curl_session.set_response(
            'https://www.bing.com/search?q=site:comparably.com+"Apple"+badges+score',
            status_code=200,
            text=bing_html
        )
        
        result = await scraper.scrape_company("AAPL")
        
        assert result.ticker == "AAPL"
        assert result.slug == "apple"
        assert result.badge_score == 85
        assert result.badge_count == 0  # Bing search doesn't always extract badge count

    @pytest.mark.asyncio
    async def test_scrape_company_no_slug(self, scraper):
        result = await scraper.scrape_company("NONEXISTENT")
        assert result.ticker == "NONEXISTENT"
        assert result.badge_score is None


class TestCorpAuditEngine:
    @pytest.fixture
    def mock_config(self):
        return {
            "glassdoor": {"company_slugs": {"AAPL": "Apple"}},
            "comparably": {"company_slugs": {"AAPL": "apple"}},
            "validation_gate": {"kappa": 5.0, "divergence_threshold": 0.20, "confidence_floor": 0.40}
        }

    @pytest.fixture
    def engine(self, mock_config):
        with patch('psychological.scrapers.corp_audit.load_hybrid_config') as mock_load:
            mock_load.return_value = {
                "glassdoor": mock_config["glassdoor"],
                "comparably": mock_config["comparably"],
                "validation_gate": mock_config["validation_gate"]
            }
            engine = CorpAuditEngine(config_dict=mock_config)
            engine.glassdoor_scraper = Mock()
            engine.comparably_scraper = Mock()
            engine.validation_gate = Mock()
            yield engine

    def test_init(self, engine):
        assert engine is not None
        assert engine.glassdoor_scraper is not None
        assert engine.comparably_scraper is not None
        assert engine.validation_gate is not None

    @pytest.mark.asyncio
    async def test_audit_ticker(self, engine):
        mock_gd = GlassdoorScore("AAPL", "Apple", 4.5, 0.9, 1000, 90, 85, datetime.now(timezone.utc).isoformat())
        mock_comp = ComparablyBadges("AAPL", "apple", 85.0, 5, {"Culture": 90}, datetime.now(timezone.utc).isoformat())
        
        engine.glassdoor_scraper.scrape_company = AsyncMock(return_value=mock_gd)
        engine.comparably_scraper.scrape_company = AsyncMock(return_value=mock_comp)
        
        mock_validation = Mock()
        mock_validation.normalized_glassdoor = 0.9
        mock_validation.normalized_comparably = 0.85
        mock_validation.divergence = 0.05
        mock_validation.penalty_multiplier = 1.0
        mock_validation.override_triggered = False
        mock_validation.confidence_floor = 0.4
        mock_validation.kappa = 5.0
        engine.validation_gate.evaluate = Mock(return_value=mock_validation)
        
        result = await engine.audit_ticker("AAPL")
        
        assert result["ticker"] == "AAPL"
        assert result["glassdoor"]["raw_score"] == 4.5
        assert result["comparably"]["badge_score"] == 85.0
        assert result["validation_gate"]["override_triggered"] is False

    @pytest.mark.asyncio
    async def test_audit_all(self, engine):
        engine.audit_ticker = AsyncMock(side_effect=[
            {"ticker": "AAPL", "glassdoor": {"raw_score": 4.5}, "comparably": {"badge_score": 85}, "validation_gate": {"override_triggered": False}},
            {"ticker": "MSFT", "glassdoor": {"raw_score": 4.3}, "comparably": {"badge_score": 80}, "validation_gate": {"override_triggered": False}}
        ])
        
        results = await engine.audit_all(["AAPL", "MSFT"])
        
        assert "AAPL" in results
        assert "MSFT" in results


class TestCreateFunctions:
    @pytest.mark.asyncio
    async def test_create_corp_audit_engine(self):
        with patch('psychological.scrapers.corp_audit.load_hybrid_config') as mock_load:
            mock_load.return_value = {"glassdoor": {}, "comparably": {}, "validation_gate": {}}
            with patch('psychological.scrapers.corp_audit.GlassdoorScraper') as mock_gd, \
                 patch('psychological.scrapers.corp_audit.ComparablyScraper') as mock_comp:
                mock_gd.return_value.initialize = AsyncMock()
                mock_comp.return_value.initialize = AsyncMock()
                
                engine = await create_corp_audit_engine()
                assert isinstance(engine, CorpAuditEngine)

    @pytest.mark.asyncio
    async def test_create_glassdoor_scraper(self):
        with patch('psychological.scrapers.corp_audit.load_hybrid_config') as mock_load:
            mock_load.return_value = {"glassdoor": {"company_slugs": {}}}
            with patch('psychological.scrapers.corp_audit.AsyncSession') as mock_session_class:
                mock_instance = AsyncMock()
                mock_instance.close = AsyncMock()
                mock_session_class.return_value = mock_instance
                
                scraper = await create_glassdoor_scraper()
                assert isinstance(scraper, GlassdoorScraper)

    @pytest.mark.asyncio
    async def test_create_comparably_scraper(self):
        with patch('psychological.scrapers.corp_audit.load_hybrid_config') as mock_load:
            mock_load.return_value = {"comparably": {"company_slugs": {}}}
            with patch('psychological.scrapers.corp_audit.AsyncSession') as mock_session_class:
                mock_instance = AsyncMock()
                mock_instance.close = AsyncMock()
                mock_session_class.return_value = mock_instance
                
                scraper = await create_comparably_scraper()
                assert isinstance(scraper, ComparablyScraper)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])