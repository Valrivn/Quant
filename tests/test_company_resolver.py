import pytest
from unittest.mock import Mock, patch
from psychological.scrapers.company_resolver import CompanyResolver, CompanyEntity


MOCK_COMPANIES = {
    "NVDA": {
        "legal_name": "NVIDIA Corporation",
        "common_name": "NVIDIA",
        "glassdoor_slug": "nvidia",
        "comparably_slug": "nvidia",
        "indeed_slug": "Nvidia",
        "g2_slug": "nvidia",
        "capterra_slug": "nvidia",
        "github_org": "NVIDIA",
        "sec_cik": "0001045810",
        "app_store_name": "NVIDIA",
    },
    "AAPL": {
        "legal_name": "Apple Inc.",
        "common_name": "Apple",
        "glassdoor_slug": "apple",
        "comparably_slug": "apple",
        "indeed_slug": "Apple",
        "g2_slug": "apple",
        "capterra_slug": "apple",
        "github_org": "apple",
        "sec_cik": "0000320193",
        "app_store_name": "Apple",
    },
    "MSFT": {
        "legal_name": "Microsoft Corporation",
        "common_name": "Microsoft",
        "glassdoor_slug": "microsoft",
        "comparably_slug": "microsoft",
        "indeed_slug": "Microsoft",
        "g2_slug": "microsoft",
        "capterra_slug": "microsoft",
        "github_org": "microsoft",
        "sec_cik": "0000789019",
        "app_store_name": "Microsoft",
    },
}


class TestCompanyResolver:
    @pytest.fixture
    def resolver(self):
        with patch("psychological.scrapers.company_resolver.load_hybrid_config") as mock_load:
            mock_load.return_value = {"companies": MOCK_COMPANIES}
            return CompanyResolver()

    def test_init(self, resolver):
        assert resolver is not None

    def test_resolve_existing(self, resolver):
        nvda = resolver.resolve("NVDA")
        assert nvda is not None
        assert nvda.ticker == "NVDA"
        assert nvda.legal_name == "NVIDIA Corporation"
        assert nvda.common_name == "NVIDIA"
        assert nvda.glassdoor_slug == "nvidia"
        assert nvda.comparably_slug == "nvidia"
        assert nvda.indeed_slug == "Nvidia"
        assert nvda.g2_slug == "nvidia"
        assert nvda.capterra_slug == "nvidia"
        assert nvda.github_org == "NVIDIA"
        assert nvda.sec_cik == "0001045810"
        assert nvda.app_store_name == "NVIDIA"

    def test_resolve_case_insensitive(self, resolver):
        nvda = resolver.resolve("nvda")
        assert nvda is not None
        assert nvda.ticker == "NVDA"

    def test_resolve_nonexistent(self, resolver):
        assert resolver.resolve("NONEXISTENT") is None

    def test_resolve_by_slug(self, resolver):
        nvda = resolver.resolve_by_slug("nvidia")
        assert nvda is not None
        assert nvda.ticker == "NVDA"

    def test_resolve_by_slug_nonexistent(self, resolver):
        assert resolver.resolve_by_slug("nonexistent-slug") is None

    def test_get_slug(self, resolver):
        assert resolver.get_slug("NVDA", "glassdoor") == "nvidia"
        assert resolver.get_slug("NVDA", "comparably") == "nvidia"
        assert resolver.get_slug("NVDA", "indeed") == "Nvidia"
        assert resolver.get_slug("NVDA", "g2") == "nvidia"
        assert resolver.get_slug("NVDA", "capterra") == "nvidia"

    def test_get_slug_nonexistent(self, resolver):
        assert resolver.get_slug("NONEXISTENT", "glassdoor") is None

    def test_get_all_tickers(self, resolver):
        tickers = resolver.get_all_tickers()
        assert "NVDA" in tickers
        assert "AAPL" in tickers
        assert "MSFT" in tickers
        assert len(tickers) == 3

    def test_get_all_entities(self, resolver):
        entities = resolver.get_all_entities()
        assert len(entities) == 3
        tickers = [e.ticker for e in entities]
        assert "NVDA" in tickers
        assert "AAPL" in tickers
        assert "MSFT" in tickers

    def test_company_entity_frozen(self, resolver):
        nvda = resolver.resolve("NVDA")
        with pytest.raises(AttributeError):
            nvda.ticker = "AMD"
