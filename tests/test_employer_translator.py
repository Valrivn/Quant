import pytest
from unittest.mock import patch, AsyncMock
from datetime import datetime, timezone

from psychological.scrapers.employer_translator import EmployerTranslator, EmployerSentimentResult
from psychological.scrapers.corp_audit import GlassdoorScore, IndeedScore, ComparablyScore


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
}


def _mock_gd_score(ticker, slug, raw):
    return GlassdoorScore(
        ticker=ticker, slug=slug, raw_score=raw,
        normalized_score=raw / 5.0 if raw else None,
        review_count=1000, ceo_approval=90, recommend_to_friend=85,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def _mock_indeed_score(ticker, slug, rating):
    return IndeedScore(
        ticker=ticker, slug=slug, overall_rating=rating,
        work_wellbeing_score=75, ceo_approval=80, ceo_name="CEO",
        review_count=500, wellbeing_breakdown={},
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def _mock_comparably_score(ticker, slug, rating):
    return ComparablyScore(
        ticker=ticker, slug=slug, overall_rating=rating,
        culture_grade="B", ceo_score=80, ceo_name="CEO",
        recommend_pct=85, category_grades={}, awards=[],
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


class TestEmployerTranslator:
    @pytest.fixture
    def translator(self):
        with patch("psychological.scrapers.employer_translator.load_hybrid_config") as mock_load:
            mock_load.return_value = {"companies": MOCK_COMPANIES}
            with patch("psychological.scrapers.employer_translator.GlassdoorScraper") as mock_gd, \
                 patch("psychological.scrapers.employer_translator.IndeedScraper") as mock_ind, \
                 patch("psychological.scrapers.employer_translator.ComparablyScraper") as mock_comp:
                mock_gd.return_value.scrape_company = AsyncMock()
                mock_ind.return_value.scrape_company = AsyncMock()
                mock_comp.return_value.scrape_company = AsyncMock()
                mock_gd.return_value.initialize = AsyncMock()
                mock_ind.return_value.initialize = AsyncMock()
                mock_comp.return_value.initialize = AsyncMock()
                mock_gd.return_value.close = AsyncMock()
                mock_ind.return_value.close = AsyncMock()
                mock_comp.return_value.close = AsyncMock()
                return EmployerTranslator()

    def test_init(self, translator):
        assert translator is not None

    def test_normalize_score(self, translator):
        assert translator._normalize_score(4.5) == 0.9
        assert translator._normalize_score(5.0) == 1.0
        assert translator._normalize_score(0.0) == 0.0
        assert translator._normalize_score(None) is None
        assert translator._normalize_score(2.5) == 0.5

    def test_compute_agreement_perfect(self, translator):
        assert translator._compute_agreement([0.9, 0.9, 0.9]) == 1.0

    def test_compute_agreement_partial(self, translator):
        agreement = translator._compute_agreement([0.9, 0.5])
        assert agreement == pytest.approx(0.6)

    def test_compute_agreement_single_source(self, translator):
        assert translator._compute_agreement([0.9]) == 1.0

    def test_compute_agreement_no_sources(self, translator):
        assert translator._compute_agreement([]) == 1.0

    def test_compute_agreement_large_divergence(self, translator):
        agreement = translator._compute_agreement([0.1, 0.9])
        assert agreement == pytest.approx(0.2)

    def test_compute_weighted_all_equal(self, translator):
        scores = {"glassdoor": 0.9, "indeed": 0.5, "comparably": 0.7}
        weighted = translator._compute_weighted(scores)
        expected = 0.9 * 0.40 + 0.5 * 0.30 + 0.7 * 0.30
        assert weighted == pytest.approx(expected)

    def test_compute_weighted_with_none(self, translator):
        scores = {"glassdoor": 0.9, "indeed": None, "comparably": 0.7}
        weighted = translator._compute_weighted(scores)
        expected = (0.9 * 0.40 + 0.7 * 0.30) / (0.40 + 0.30)
        assert weighted == pytest.approx(expected)

    def test_compute_weighted_empty(self, translator):
        assert translator._compute_weighted({}) == 0.0

    @pytest.mark.asyncio
    async def test_translate_all_sources(self, translator):
        translator._glassdoor.scrape_company.return_value = _mock_gd_score("NVDA", "nvidia", 4.5)
        translator._indeed.scrape_company.return_value = _mock_indeed_score("NVDA", "Nvidia", 4.2)
        translator._comparably.scrape_company.return_value = _mock_comparably_score("NVDA", "nvidia", 4.0)

        result = await translator.translate("NVDA")

        assert result.ticker == "NVDA"
        assert result.glassdoor_normalized == 0.9
        assert result.indeed_normalized == pytest.approx(0.84)
        assert result.comparably_normalized == 0.8
        assert result.source_count == 3
        assert 0.0 <= result.agreement_score <= 1.0
        assert 0.0 <= result.weighted_score <= 1.0

    @pytest.mark.asyncio
    async def test_translate_nonexistent_ticker(self, translator):
        result = await translator.translate("NONEXISTENT")

        assert result.ticker == "NONEXISTENT"
        assert result.glassdoor_normalized is None
        assert result.indeed_normalized is None
        assert result.comparably_normalized is None
        assert result.weighted_score == 0.0
        assert result.agreement_score == 0.0
        assert result.source_count == 0

    @pytest.mark.asyncio
    async def test_translate_all_multiple(self, translator):
        translator._glassdoor.scrape_company = AsyncMock(side_effect=[
            _mock_gd_score("NVDA", "nvidia", 4.5),
            _mock_gd_score("AAPL", "apple", 4.3),
        ])
        translator._indeed.scrape_company = AsyncMock(side_effect=[
            _mock_indeed_score("NVDA", "Nvidia", 4.2),
            _mock_indeed_score("AAPL", "Apple", 4.0),
        ])
        translator._comparably.scrape_company = AsyncMock(side_effect=[
            _mock_comparably_score("NVDA", "nvidia", 4.0),
            _mock_comparably_score("AAPL", "apple", 4.1),
        ])

        results = await translator.translate_all(["NVDA", "AAPL"])

        assert "NVDA" in results
        assert "AAPL" in results
        assert results["NVDA"].source_count == 3
        assert results["AAPL"].source_count == 3

    def test_employer_sentiment_result_dataclass(self):
        result = EmployerSentimentResult(
            ticker="NVDA",
            glassdoor_normalized=0.9,
            indeed_normalized=0.84,
            comparably_normalized=0.8,
            weighted_score=0.85,
            agreement_score=0.9,
            source_count=3,
            timestamp="2024-01-01T00:00:00",
        )
        assert result.ticker == "NVDA"
        assert result.glassdoor_normalized == 0.9
        assert result.indeed_normalized == 0.84
        assert result.comparably_normalized == 0.8
        assert result.weighted_score == 0.85
        assert result.agreement_score == 0.9
        assert result.source_count == 3

    def test_employer_sentiment_result_default_raw_scores(self):
        result = EmployerSentimentResult(
            ticker="NVDA",
            glassdoor_normalized=None,
            indeed_normalized=None,
            comparably_normalized=None,
            weighted_score=0.0,
            agreement_score=0.0,
            source_count=0,
            timestamp="2024-01-01T00:00:00",
        )
        assert result.raw_scores == {}
