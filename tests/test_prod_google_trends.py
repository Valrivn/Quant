"""Unit tests for production-grade Google Trends scraper and company pipeline."""

import gc
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from ingestion.data_lake import DataLakeStore
from ingestion.google_trends import (
    CheckpointStore,
    CompanySpec,
    GoogleTrendsScraper,
    KeywordGenerator,
    ParallelGoogleTrendsPipeline,
    ProxyPool,
    ProxyTracker,
)


class TestKeywordGenerator(unittest.TestCase):
    def test_auto_generate_keywords_semiconductors(self):
        comp = CompanySpec(
            ticker="NVDA",
            name="Nvidia",
            sector="Semiconductors",
            ceo="Jensen Huang",
            products=["Blackwell GPU", "H100", "CUDA"],
            competitors=["AMD", "Intel"],
        )
        tiers = KeywordGenerator.generate_keywords(comp)

        self.assertIn("macro", tiers)
        self.assertIn("company", tiers)
        self.assertIn("product", tiers)
        self.assertIn("competitive", tiers)

        # Check macro keywords contain semiconductor themes
        self.assertTrue(any("chip" in kw.lower() or "gpu" in kw.lower() or "semiconductor" in kw.lower() for kw in tiers["macro"]))

        # Check company keywords
        self.assertIn("NVDA", tiers["company"])
        self.assertIn("Nvidia", tiers["company"])
        self.assertIn("Nvidia CEO", tiers["company"])

        # Check product keywords
        self.assertIn("Blackwell GPU", tiers["product"])
        self.assertIn("H100", tiers["product"])

        # Check competitive keywords
        self.assertTrue(any("vs" in kw.lower() for kw in tiers["competitive"]))

    def test_custom_keyword_overrides(self):
        comp = CompanySpec(
            ticker="AAPL",
            name="Apple",
            sector="Consumer Electronics",
            custom_keywords={
                "product": ["iPhone 16", "Vision Pro"],
                "macro": ["tech spending"],
            },
        )
        tiers = KeywordGenerator.generate_keywords(comp)
        self.assertEqual(tiers["product"], ["iPhone 16", "Vision Pro"])
        self.assertEqual(tiers["macro"], ["tech spending"])


class TestProxyAndAntiScrape(unittest.TestCase):
    def test_proxy_tracker_budget_and_cooldown(self):
        tracker = ProxyTracker(proxy_url="http://user:pass@1.2.3.4:8080", max_requests_per_hour=2)

        # Initially available
        self.assertTrue(tracker.is_available())

        # Record requests up to budget
        tracker.record_request()
        self.assertTrue(tracker.is_available())

        tracker.record_request()
        # Budget exhausted
        self.assertFalse(tracker.is_available())

        # CAPTCHA trigger cooldown
        tracker.record_captcha_or_block(cooldown_seconds=10.0)
        self.assertFalse(tracker.is_available())
        self.assertEqual(tracker.captcha_count, 1)

    def test_proxy_pool_checkout(self):
        pool = ProxyPool(
            proxy_urls=["http://proxy1:8080", "http://proxy2:8080"],
            max_requests_per_hour_per_proxy=10,
        )
        t1 = pool.checkout_proxy()
        t2 = pool.checkout_proxy()
        self.assertIsNotNone(t1)
        self.assertIsNotNone(t2)
        self.assertNotEqual(t1.proxy_url, t2.proxy_url)


class TestCheckpointStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = f"{self.temp_dir.name}/test_checkpoint.db"
        self.store = CheckpointStore(db_path=self.db_path)

    def tearDown(self):
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    def test_checkpoint_lifecycle(self):
        job_id = "job_test_123"
        self.assertFalse(self.store.is_completed(job_id))

        self.store.mark_completed(
            job_id=job_id,
            ticker="NVDA",
            tier="product",
            module="interest_over_time",
            geo="US",
            timeframe="today 5-y",
            rows_written=50,
        )
        self.assertTrue(self.store.is_completed(job_id))

        # Test failure marking and reset
        fail_id = "job_test_failed"
        self.store.mark_failed(
            job_id=fail_id,
            ticker="AAPL",
            tier="macro",
            module="related_queries",
            geo="US",
            timeframe="today 5-y",
            error_msg="HTTP 429",
        )
        self.assertFalse(self.store.is_completed(fail_id))

        reset_count = self.store.reset_failed()
        self.assertEqual(reset_count, 1)


class TestParallelGoogleTrendsPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_lake = DataLakeStore(base_dir=self.temp_dir.name)
        self.checkpoint_db = f"{self.temp_dir.name}/checkpoints.db"
        self.checkpoint_store = CheckpointStore(db_path=self.checkpoint_db)
        self.pipeline = ParallelGoogleTrendsPipeline(
            data_lake=self.data_lake,
            checkpoint_store=self.checkpoint_store,
            max_workers=2,
            min_delay=0.01,
            max_delay=0.02,
        )

    def tearDown(self):
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    @patch.object(GoogleTrendsScraper, "get_interest_over_time")
    @patch.object(GoogleTrendsScraper, "get_interest_by_region")
    @patch.object(GoogleTrendsScraper, "get_related_queries")
    @patch.object(GoogleTrendsScraper, "get_related_topics")
    def test_run_company_pipeline(self, mock_topics, mock_queries, mock_region, mock_time):
        df_time = pd.DataFrame(
            {"H100": [80, 85], "Blackwell GPU": [90, 95]},
            index=pd.to_datetime(["2025-01-01", "2025-01-02"]),
        )
        mock_time.return_value = df_time

        df_region = pd.DataFrame(
            {"geo_code": ["US-CA"], "geo_name": ["California"], "H100": [100], "Blackwell GPU": [90]}
        )
        mock_region.return_value = df_region

        mock_queries.return_value = {
            "H100": {"top": [{"query": "h100 price", "value": 100}], "rising": []}
        }

        mock_topics.return_value = {
            "H100": {
                "top": [{"topic_title": "Graphics Processing Unit", "topic_type": "Hardware", "value": 100}],
                "rising": [],
            }
        }

        company = CompanySpec(
            ticker="NVDA",
            name="Nvidia",
            sector="Semiconductors",
            products=["H100", "Blackwell GPU"],
        )

        report = self.pipeline.run_company_pipeline(
            companies=[company],
            geos=["US"],
            timeframes=["today 5-y"],
            tier_limits={"macro": 1, "company": 1, "product": 2, "competitive": 1},
        )

        self.assertEqual(report["status"], "SUCCESS")
        self.assertGreater(report["completed_tasks"], 0)
        self.assertGreater(len(report["saved_paths"]), 0)

        # Inspect Saved Parquet data lake table for company/product dimensions
        catalog = self.data_lake.list_series("google_trends")
        self.assertGreater(len(catalog), 0)

        # Load one of the saved dataframes and check dimensions
        sample_series = catalog[0]["series_id"]
        if "time" in sample_series or "region" in sample_series or "queries" in sample_series or "topics" in sample_series:
            saved_df = self.data_lake.load_dataframe("google_trends", sample_series)
            self.assertIn("ticker", saved_df.columns)
            self.assertIn("company_name", saved_df.columns)
            self.assertIn("sector", saved_df.columns)
            self.assertIn("tier", saved_df.columns)
            self.assertEqual(saved_df["ticker"].iloc[0], "NVDA")


if __name__ == "__main__":
    unittest.main()
