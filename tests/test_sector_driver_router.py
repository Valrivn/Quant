"""Unit and integration tests for Qualitative/routing/sector_driver_router.py."""

import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone

from Qualitative.routing.sector_driver_router import (
    SectorDriverRouter,
    SectorCategory,
    DriverSpec,
    PITValidationError,
    validate_pit_timestamp,
    filter_pit_records,
    SECTOR_DRIVER_SPECS,
)
from ingestion.huggingface_loader import HuggingFaceLoader, ORG_TO_TICKER, TICKER_TO_ORG


class TestSectorDriverRouter:
    @pytest.fixture
    def router(self):
        return SectorDriverRouter()

    def test_sector_classification(self, router):
        """Verify GICS sector classification routes to correct categories."""
        assert router.classify_sector("Information Technology") == SectorCategory.TECH_COMM
        assert router.classify_sector("Software & Services") == SectorCategory.TECH_COMM
        assert router.classify_sector("Communication Services") == SectorCategory.TECH_COMM
        assert router.classify_sector("Health Care") == SectorCategory.HEALTHCARE
        assert router.classify_sector("Biotechnology") == SectorCategory.HEALTHCARE
        assert router.classify_sector("Financials") == SectorCategory.FINANCIALS
        assert router.classify_sector("Commercial Banking") == SectorCategory.FINANCIALS
        assert router.classify_sector("Industrials") == SectorCategory.INDUSTRIALS_DEFENSE
        assert router.classify_sector("Aerospace & Defense") == SectorCategory.INDUSTRIALS_DEFENSE
        assert router.classify_sector("Consumer Discretionary") == SectorCategory.OTHER_CONSUMER
        assert router.classify_sector("Consumer Staples") == SectorCategory.OTHER_CONSUMER
        assert router.classify_sector("Energy") == SectorCategory.OTHER_CONSUMER
        assert router.classify_sector("Utilities") == SectorCategory.OTHER_CONSUMER

    def test_tech_comm_traction_formula(self, router):
        """Verify Tech/Comm traction formula: T_i = 0.35*Z_dev + 0.35*Z_ai + 0.30*Z_patent."""
        metrics = {
            "z_dev": 1.0,
            "z_ai": 0.8,
            "z_patent_quality": 0.5,
            "retrieval_timestamp": "2026-09-01T00:00:00Z",
        }
        res = router.route("NVDA", metrics, sector="Information Technology")
        assert res["sector_category"] == SectorCategory.TECH_COMM.value
        # Expected: 0.35 * 1.0 + 0.35 * 0.8 + 0.30 * 0.5 = 0.35 + 0.28 + 0.15 = 0.78
        expected_ti = 0.35 * 1.0 + 0.35 * 0.8 + 0.30 * 0.5
        assert np.isclose(res["traction_score"], expected_ti, atol=1e-5)
        assert res["driver_breakdown"]["z_dev"] == 1.0
        assert res["driver_breakdown"]["z_ai"] == 0.8
        assert res["driver_breakdown"]["z_patent_quality"] == 0.5

    def test_healthcare_traction_formula(self, router):
        """Verify Healthcare traction: T_i = 0.50*Z_clinical + 0.30*Z_fda + 0.20*Z_patent."""
        metrics = {
            "z_clinical_trials": 0.6,
            "z_fda_exclusivity": 0.4,
            "z_patent_quality": 0.2,
            "event_date": "2026-09-01",
        }
        res = router.route("PFE", metrics, sector="Health Care")
        assert res["sector_category"] == SectorCategory.HEALTHCARE.value
        expected_ti = 0.50 * 0.6 + 0.30 * 0.4 + 0.20 * 0.2  # 0.30 + 0.12 + 0.04 = 0.46
        assert np.isclose(res["traction_score"], expected_ti, atol=1e-5)

    def test_financials_traction_formula(self, router):
        """Verify Financials traction: T_i = 0.50*Z_cfpb_safety + 0.50*Z_fdic_margins."""
        metrics = {
            "z_cfpb_safety": -0.2,
            "z_fdic_margins": 0.8,
            "filed_date": "2026-09-01",
        }
        res = router.route("JPM", metrics, sector="Financials")
        assert res["sector_category"] == SectorCategory.FINANCIALS.value
        expected_ti = 0.50 * (-0.2) + 0.50 * 0.8  # 0.30
        assert np.isclose(res["traction_score"], expected_ti, atol=1e-5)

    def test_industrials_traction_formula(self, router):
        """Verify Industrials traction: T_i = 0.50*Z_usaspending + 0.30*Z_patent + 0.20*Z_nhtsa_safety."""
        metrics = {
            "z_usaspending_backlog": 0.7,
            "z_patent_quality": 0.5,
            "z_nhtsa_safety": 0.9,
            "grant_date": "2026-09-01",
        }
        res = router.route("LMT", metrics, sector="Industrials")
        assert res["sector_category"] == SectorCategory.INDUSTRIALS_DEFENSE.value
        expected_ti = 0.50 * 0.7 + 0.30 * 0.5 + 0.20 * 0.9  # 0.35 + 0.15 + 0.18 = 0.68
        assert np.isclose(res["traction_score"], expected_ti, atol=1e-5)

    def test_other_consumer_traction_formula(self, router):
        """Verify Other/Consumer traction: T_i = 0.50*Z_wiki + 0.50*Z_rpo."""
        metrics = {
            "z_wiki_mindshare": 0.4,
            "z_rpo_backlog": 0.6,
            "retrieval_timestamp": "2026-09-01T12:00:00Z",
        }
        res = router.route("AMZN", metrics, sector="Consumer Discretionary")
        assert res["sector_category"] == SectorCategory.OTHER_CONSUMER.value
        expected_ti = 0.50 * 0.4 + 0.50 * 0.6  # 0.50
        assert np.isclose(res["traction_score"], expected_ti, atol=1e-5)

    def test_metric_alias_normalization(self, router):
        """Verify that raw driver aliases map correctly to canonical z-scores."""
        metrics = {
            "github_prs": 0.9,
            "huggingface_downloads": 0.7,
            "uspto_citations": 0.4,
            "retrieval_timestamp": "2026-09-01T00:00:00Z",
        }
        res = router.route("MSFT", metrics)
        expected_ti = 0.35 * 0.9 + 0.35 * 0.7 + 0.30 * 0.4
        assert np.isclose(res["traction_score"], expected_ti, atol=1e-5)

    def test_pit_timestamp_missing_raises_error(self, router):
        """Strict PIT enforcement: Record with no PIT timestamp must raise PITValidationError."""
        invalid_record = {
            "z_dev": 0.5,
            "z_ai": 0.5,
            # No filed_date, grant_date, event_date, or retrieval_timestamp
        }
        with pytest.raises(PITValidationError, match="lacks unalterable timestamp column"):
            router.route("AAPL", invalid_record)

    def test_pit_lookahead_bias_rejected(self, router):
        """Strict PIT enforcement: Future timestamp past as_of_date must raise error."""
        future_record = {
            "z_dev": 0.5,
            "z_ai": 0.5,
            "z_patent_quality": 0.5,
            "filed_date": "2026-10-15",
        }
        as_of_date = "2026-10-01"
        with pytest.raises(PITValidationError, match="Lookahead bias detected"):
            router.route("AAPL", future_record, as_of_date=as_of_date)

    def test_filter_pit_records_dataframe(self):
        """Test filtering panel data to strictly <= as_of_date."""
        df = pd.DataFrame([
            {"ticker": "NVDA", "retrieval_timestamp": "2026-08-01T00:00:00Z", "val": 1},
            {"ticker": "NVDA", "retrieval_timestamp": "2026-09-01T00:00:00Z", "val": 2},
            {"ticker": "NVDA", "retrieval_timestamp": "2026-10-01T00:00:00Z", "val": 3},
        ])
        filtered = filter_pit_records(df, as_of_date="2026-09-15T00:00:00Z")
        assert len(filtered) == 2
        assert list(filtered["val"]) == [1, 2]

    def test_route_dataframe(self, router):
        """Verify routing a panel DataFrame of tickers across multiple sectors."""
        panel_df = pd.DataFrame([
            {"ticker": "NVDA", "sector": "Information Technology", "z_dev": 1.0, "z_ai": 0.5, "z_patent_quality": 0.2, "retrieval_timestamp": "2026-09-01T00:00:00Z"},
            {"ticker": "PFE", "sector": "Health Care", "z_clinical_trials": 0.8, "z_fda_exclusivity": 0.6, "z_patent_quality": 0.4, "event_date": "2026-09-01"},
            {"ticker": "JPM", "sector": "Financials", "z_cfpb_safety": 0.2, "z_fdic_margins": 0.4, "filed_date": "2026-09-01"},
        ])
        routed_df = router.route_dataframe(panel_df, as_of_date="2026-09-05T00:00:00Z")
        assert len(routed_df) == 3
        assert "traction_score" in routed_df.columns
        assert "sector_category" in routed_df.columns
        assert list(routed_df["sector_category"]) == [
            SectorCategory.TECH_COMM.value,
            SectorCategory.HEALTHCARE.value,
            SectorCategory.FINANCIALS.value,
        ]

    def test_huggingface_loader_org_mapping(self):
        """Verify Hugging Face org-to-ticker mapping requirements."""
        assert ORG_TO_TICKER["meta-llama"] == "META"
        assert ORG_TO_TICKER["google"] == "GOOGL"
        assert ORG_TO_TICKER["microsoft"] == "MSFT"
        assert ORG_TO_TICKER["snowflake"] == "SNOW"
        assert ORG_TO_TICKER["apple"] == "AAPL"
        assert ORG_TO_TICKER["nvidia"] == "NVDA"
        assert TICKER_TO_ORG["META"] == "meta-llama"

    def test_huggingface_loader_fallback_and_pit(self):
        """Verify Hugging Face loader works offline via calibrated fallback with PIT timestamp."""
        loader = HuggingFaceLoader(timeout=0.01)  # tiny timeout triggers fallback
        rec = loader.fetch_org_metrics("meta-llama")
        assert rec["ticker"] == "META"
        assert rec["model_count"] > 0
        assert rec["total_downloads"] > 0
        assert "retrieval_timestamp" in rec
        assert rec["source"] == "huggingface"
        assert rec["mapping_confidence"] == "high"
