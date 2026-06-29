import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from psychological.scrapers.corp_audit import (
    GlassdoorScraper, G2EmployerScraper, CorpAuditEngine,
    create_corp_audit_engine, create_glassdoor_scraper, create_g2_employer_scraper,
    GlassdoorScore, G2EmployerScore, ComparablyScraper, create_comparably_scraper,
    ComparablyBadges
)
from curl_cffi import AsyncSession


class MockCurlResponse:
    def __init__(self, status_code=200, text="<html></html>"):
        self.status_code = status_code
        self.text = text


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
            # Mock nodriver primary to prevent real browser launch
            mock_nodriver = AsyncMock()
            mock_nodriver.scrape_company.return_value = GlassdoorScore(
                "NVDA", "", None, None, None, None, None,
                datetime.now(timezone.utc).isoformat(), {}, []
            )
            scraper._nodriver_primary = mock_nodriver
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
        glassdoor_html = """
        <html>
            <div data-test="overallRating">4.5</div>
            <p>1,000 reviews</p>
            <p>90% approve CEO</p>
            <p>85% would recommend</p>
        </html>
        """
        scraper._curl_session.set_response(
            'https://www.glassdoor.com/Reviews/nvidia-reviews-SRCH_KE0,6.htm',
            status_code=200,
            text=glassdoor_html
        )
        
        result = await scraper.scrape_company("NVDA")
        
        assert result.ticker == "NVDA"
        assert result.slug == "nvidia"
        assert result.raw_score == 4.5
        assert result.normalized_score == 0.9

    @pytest.mark.asyncio
    async def test_scrape_company_category_and_awards(self, scraper):
        glassdoor_html = """
        <html>
            <div data-test="overallRating">4.5</div>
            <p>1,000 reviews</p>
            <h3>Ratings by category</h3>
            <div>Culture & values 4.4</div>
            <div>Work/Life balance 4.2</div>
            <div>Senior management 4.1</div>
            <h3>Arm awards & accolades</h3>
            <ul>
                <li>Glassdoor Best Places to Work UK 2024</li>
                <li>The Times Top 100 Graduate Employers, #99</li>
            </ul>
        </html>
        """
        scraper._curl_session.set_response(
            'https://www.glassdoor.com/Reviews/nvidia-reviews-SRCH_KE0,6.htm',
            status_code=200,
            text=glassdoor_html
        )
        
        result = await scraper.scrape_company("NVDA")
        
        assert result.ticker == "NVDA"
        assert result.category_ratings.get("Culture & values") == 4.4
        assert result.category_ratings.get("Senior management") == 4.1
    @pytest.mark.asyncio
    async def test_scrape_company_exact_snapshots(self, scraper):
        tesla_html = """
        <html>
            <div>Tesla Snapshot</div>
            <div>3.5★ based on 11,980 ratings</div>
            <div>58% would recommend to a friend</div>
            <div>Elon Musk 59% approve of CEO</div>
        </html>
        """
        scraper._curl_session.set_response(
            'https://www.glassdoor.com/Reviews/nvidia-reviews-SRCH_KE0,6.htm',
            status_code=200,
            text=tesla_html
        )
        result = await scraper.scrape_company("NVDA")
        assert result.raw_score == 3.5
        assert result.review_count == 11980
        assert result.recommend_to_friend == 58
        assert result.ceo_approval == 59

        intel_html = """
        <html>
            <div>Intel Corporation Snapshot</div>
            <div>3.9★ based on 31,942 ratings</div>
            <div>68% would recommend to a friend</div>
            <div>Lip-Bu Tan 66% approve of CEO</div>
        </html>
        """
        scraper._curl_session.set_response(
            'https://www.glassdoor.com/Reviews/nvidia-reviews-SRCH_KE0,6.htm',
            status_code=200,
            text=intel_html
        )
        result_intel = await scraper.scrape_company("NVDA")
        assert result_intel.raw_score == 3.9
        assert result_intel.review_count == 31942
        assert result_intel.recommend_to_friend == 68
        assert result_intel.ceo_approval == 66

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
            "glassdoor": {
                "company_slugs": {
                    "AAPL": "apple",
                    "GOOGL": "google"
                }
            },
            "g2_capterra": {
                "company_slugs": {
                    "AAPL": "apple",
                    "GOOGL": "google"
                }
            }
        }

    @pytest.fixture
    def scraper(self, mock_config):
        with patch('psychological.scrapers.corp_audit.load_hybrid_config') as mock_load:
            mock_load.return_value = {
                "glassdoor": mock_config["glassdoor"],
                "g2_capterra": mock_config["g2_capterra"]
            }
            scraper = G2EmployerScraper(config_dict=mock_config)
            scraper._curl_session = MockCurlSession()
            yield scraper

    def test_init(self, scraper):
        assert scraper is not None
        assert "AAPL" in scraper.company_slugs

    def test_parse_rating(self, scraper):
        assert scraper._parse_rating("4.5 out of 5") == 4.5
        assert scraper._parse_rating("4.5/5") == 4.5
        assert scraper._parse_rating("invalid") is None

    @pytest.mark.asyncio
    async def test_scrape_company_success(self, scraper):
        g2_html = """
        <html>
            <div data-testid="rating-value">4.5</div>
            <p>1,000 reviews</p>
            <p>85% would recommend</p>
        </html>
        """
        scraper._curl_session.set_response(
            'https://www.g2.com/products/apple/reviews',
            status_code=200,
            text=g2_html
        )
        
        result = await scraper.scrape_company("AAPL")
        
        assert result.ticker == "AAPL"
        assert result.slug == "apple"
        assert result.overall_rating == 4.5

    @pytest.mark.asyncio
    async def test_scrape_company_no_slug(self, scraper):
        result = await scraper.scrape_company("NONEXISTENT")
        assert result.ticker == "NONEXISTENT"
        assert result.overall_rating is None


class TestDedicatedComparablyScraper:
    @pytest.fixture
    def scraper(self):
        from psychological.scrapers.corp_audit import ComparablyScraper
        return ComparablyScraper()

    def test_extract_comparably_data(self, scraper):
        html = """
        <html>
            <div>Overall Company Culture B</div>
            <div>Tesla Culture 4.1/5</div>
            <div>CEO Elon Musk 76/100</div>
            <div>CEO Rating A-</div>
            <div>Gender C+</div>
            <div>Diversity B</div>
            <div>Happiness B</div>
            <div>eNPS 12</div>
            <div>81% positive</div>
            <img alt="Best Company Perks & Benefits 2024" />
        </html>
        """
        data = scraper._extract_comparably_data(html)
        assert data["overall_rating"] == 4.1
        assert data["culture_grade"] == "B"
        assert data["ceo_score"] == 76
        assert data["category_grades"].get("Gender") == "C+"
        assert data["category_grades"].get("Diversity") == "B"
        assert data["recommend_pct"] == 81
        assert "Best Company Perks & Benefits 2024" in data["awards"]


class TestCorpAuditEngine:
    @pytest.fixture
    def mock_config(self):
        return {
            "glassdoor": {"company_slugs": {"AAPL": "Apple"}},
            "g2_capterra": {"company_slugs": {"AAPL": "apple"}},
            "validation_gate": {"kappa": 5.0, "divergence_threshold": 0.20, "confidence_floor": 0.40}
        }

    @pytest.fixture
    def engine(self, mock_config):
        with patch('psychological.scrapers.corp_audit.load_hybrid_config') as mock_load:
            mock_load.return_value = {
                "glassdoor": mock_config["glassdoor"],
                "g2_capterra": mock_config["g2_capterra"],
                "validation_gate": mock_config["validation_gate"]
            }
            engine = CorpAuditEngine(config_dict=mock_config)
            engine.glassdoor_scraper = Mock()
            engine.indeed_scraper = Mock()
            engine.g2_scraper = Mock()
            engine.validation_gate = Mock()
            yield engine

    def test_init(self, engine):
        assert engine is not None
        assert engine.glassdoor_scraper is not None
        assert engine.indeed_scraper is not None
        assert engine.g2_scraper is not None
        assert engine.validation_gate is not None

    @pytest.mark.asyncio
    async def test_audit_ticker(self, engine):
        from psychological.scrapers.corp_audit import IndeedScore
        mock_gd = GlassdoorScore("AAPL", "Apple", 4.5, 0.9, 1000, 90, 85, datetime.now(timezone.utc).isoformat())
        mock_ind = IndeedScore("AAPL", "Apple", 4.2, 81, 86, "Tim Cook", 319, {"Happiness": "High"}, datetime.now(timezone.utc).isoformat())
        mock_g2 = G2EmployerScore("AAPL", "apple", 4.25, 1000, 85.0, {"Culture": 4.5}, datetime.now(timezone.utc).isoformat())
        
        engine.glassdoor_scraper.scrape_company = AsyncMock(return_value=mock_gd)
        engine.indeed_scraper.scrape_company = AsyncMock(return_value=mock_ind)
        engine.g2_scraper.scrape_company = AsyncMock(return_value=mock_g2)
        
        mock_validation = Mock()
        mock_validation.normalized_glassdoor = 0.9
        mock_validation.normalized_comparably = 0.84
        mock_validation.weighted_score = 0.885
        mock_validation.divergence = 0.05
        mock_validation.penalty_multiplier = 1.0
        mock_validation.override_triggered = False
        mock_validation.confidence_floor = 0.4
        mock_validation.kappa = 5.0
        engine.validation_gate.evaluate = Mock(return_value=mock_validation)
        
        result = await engine.audit_ticker("AAPL")
        
        assert result["ticker"] == "AAPL"
        assert result["glassdoor"]["raw_score"] == 4.5
        assert result["indeed"]["overall_rating"] == 4.2
        assert result["g2"]["overall_rating"] == 4.25
        assert result["validation_gate"]["override_triggered"] is False

    @pytest.mark.asyncio
    async def test_audit_all(self, engine):
        engine.audit_ticker = AsyncMock(side_effect=[
            {"ticker": "AAPL", "glassdoor": {"raw_score": 4.5}, "indeed": {"overall_rating": 4.2}, "g2": {"overall_rating": 4.25}, "validation_gate": {"override_triggered": False}},
            {"ticker": "MSFT", "glassdoor": {"raw_score": 4.3}, "indeed": {"overall_rating": 4.1}, "g2": {"overall_rating": 4.00}, "validation_gate": {"override_triggered": False}}
        ])
        
        results = await engine.audit_all(["AAPL", "MSFT"])
        
        assert "AAPL" in results
        assert "MSFT" in results


class TestCreateFunctions:
    @pytest.mark.asyncio
    async def test_create_corp_audit_engine(self):
        with patch('psychological.scrapers.corp_audit.load_hybrid_config') as mock_load:
            mock_load.return_value = {"glassdoor": {}, "validation_gate": {}}
            with patch('psychological.scrapers.corp_audit.GlassdoorScraper') as mock_gd, \
                 patch('psychological.scrapers.corp_audit.IndeedScraper') as mock_ind, \
                 patch('psychological.scrapers.corp_audit.G2EmployerScraper') as mock_g2:
                mock_gd.return_value.initialize = AsyncMock()
                mock_ind.return_value.initialize = AsyncMock()
                mock_g2.return_value.initialize = AsyncMock()
                
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
            mock_load.return_value = {"glassdoor": {"company_slugs": {}}}
            with patch('psychological.scrapers.corp_audit.AsyncSession') as mock_session_class:
                mock_instance = AsyncMock()
                mock_instance.close = AsyncMock()
                mock_session_class.return_value = mock_instance
                
                scraper = await create_comparably_scraper()
                assert isinstance(scraper, ComparablyScraper)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])