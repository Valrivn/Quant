"""Unit tests for FRED/ALFRED automated scheduler, health check, and data refresh pipeline."""

import gc
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from ingestion.data_lake import DataLakeStore
from ingestion.fred_alfred import EXPANDED_SERIES_CATALOG, FredAlfredClient
from scripts.fred_scheduler import (
    DEFAULT_CONFIG_PATH,
    FredScheduler,
    compute_next_scheduled_times,
    load_schedule_config,
    setup_structured_logging,
)


class TestFredSchedulerConfigAndHealth(unittest.TestCase):
    def test_load_schedule_config(self):
        config = load_schedule_config()
        self.assertIn("schedule", config)
        self.assertIn("daily_update", config["schedule"])
        self.assertIn("weekly_refresh", config["schedule"])
        self.assertIn("monthly_revalidation", config["schedule"])
        self.assertEqual(config["schedule"]["daily_update"]["max_business_day_gap"], 5)

    def test_compute_next_scheduled_times(self):
        sched = compute_next_scheduled_times()
        self.assertIn("daily_update", sched)
        self.assertIn("weekly_refresh", sched)
        self.assertIn("monthly_revalidation", sched)


class TestFredSchedulerPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_lake = DataLakeStore(base_dir=self.temp_dir.name)
        self.client = FredAlfredClient(api_key="test_dummy_key", rate_limit_per_min=1000)
        
        # Override health file and log path to temp dir
        self.health_path = Path(self.temp_dir.name) / "fred_health_check.json"
        self.log_path = Path(self.temp_dir.name) / "fred_pipeline.log"
        
        self.scheduler = FredScheduler(data_lake=self.data_lake, client=self.client)
        self.scheduler.health_file = self.health_path
        self.scheduler.log_file = self.log_path

    def tearDown(self):
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    def test_catalog_series_count(self):
        all_series = self.scheduler.get_all_catalog_series()
        self.assertEqual(len(all_series), 69)

        daily_series = self.scheduler.get_daily_series()
        self.assertGreater(len(daily_series), 0)

        macro_series = self.scheduler.get_macro_series()
        self.assertGreater(len(macro_series), 0)

    def test_daily_gap_validation(self):
        # Create a dataframe with a 10 business day gap
        df_gap = pd.DataFrame({
            "date": ["2025-01-01", "2025-01-20"],
            "value": [4.5, 4.6],
        })
        max_gap, missing_pct, alerts = self.scheduler.check_daily_series_gaps(df_gap, max_allowed_gap_bd=5)
        self.assertGreater(max_gap, 5)
        self.assertGreater(len(alerts), 0)

    @patch.object(FredAlfredClient, "get_series_metadata")
    @patch.object(FredAlfredClient, "get_series_observations")
    def test_run_daily_update_incremental(self, mock_obs, mock_meta):
        mock_meta.return_value = {"id": "DGS10", "title": "10-Year Treasury"}
        
        initial_df = pd.DataFrame({
            "date": ["2025-01-01", "2025-01-02"],
            "value": [4.0, 4.1],
            "realtime_start": ["2025-01-01", "2025-01-02"],
            "realtime_end": ["2025-01-01", "2025-01-02"],
        })
        new_df = pd.DataFrame({
            "date": ["2025-01-02", "2025-01-03"],
            "value": [4.1, 4.2],
            "realtime_start": ["2025-01-02", "2025-01-03"],
            "realtime_end": ["2025-01-02", "2025-01-03"],
        })
        mock_obs.return_value = (new_df, {"count": 2})

        # Save initial dataset
        self.data_lake.save_dataframe(domain="fred", series_id="DGS10", df=initial_df)

        res = self.scheduler.run_daily_update(series_subset=["DGS10"])
        
        self.assertEqual(res["task"], "daily_update")
        self.assertEqual(res["rows_added"], 1)  # Only 2025-01-03 is new
        self.assertEqual(res["total_rows_stored"], 3)
        
        # Verify stored data in Data Lake
        stored = self.data_lake.load_dataframe("fred", "DGS10")
        self.assertEqual(len(stored), 3)

    @patch.object(FredAlfredClient, "get_series_metadata")
    @patch.object(FredAlfredClient, "get_vintage_dates")
    @patch.object(FredAlfredClient, "get_series_observations")
    def test_run_weekly_refresh(self, mock_obs, mock_vintages, mock_meta):
        mock_meta.return_value = {"id": "GDP", "title": "Gross Domestic Product"}
        mock_vintages.return_value = ["2025-01-01", "2025-02-01"]
        
        old_df = pd.DataFrame({"date": ["2024-01-01"], "value": [100.0]})
        new_df = pd.DataFrame({"date": ["2024-01-01"], "value": [102.5]})  # 2.5 revision
        mock_obs.return_value = (new_df, {"count": 1})

        self.data_lake.save_dataframe(domain="alfred", series_id="GDP", df=old_df)

        res = self.scheduler.run_weekly_refresh(series_subset=["GDP"])

        self.assertEqual(res["task"], "weekly_refresh")
        self.assertEqual(res["vintages_fetched"], 2)
        self.assertAlmostEqual(res["overall_mean_abs_revision"], 2.5)

    @patch.object(FredAlfredClient, "get_series_metadata")
    @patch.object(FredAlfredClient, "get_series_observations")
    def test_run_monthly_revalidation(self, mock_obs, mock_meta):
        mock_meta.return_value = {"id": "UNRATE", "title": "Unemployment Rate"}
        
        valid_df = pd.DataFrame({"date": ["2025-01-01"], "value": [3.7]})
        mock_obs.return_value = (valid_df, {"count": 1})

        # Save corrupted or empty dataframe to trigger rebuild
        self.data_lake.save_dataframe(domain="alfred", series_id="UNRATE", df=pd.DataFrame())

        res = self.scheduler.run_monthly_revalidation(series_subset=["UNRATE"])

        self.assertEqual(res["task"], "monthly_revalidation")
        self.assertEqual(res["rebuilt_count"], 1)

    def test_health_check_status(self):
        # Run sample update to populate health state
        with patch.object(FredAlfredClient, "get_series_metadata") as m_meta, \
             patch.object(FredAlfredClient, "get_series_observations") as m_obs:
            m_meta.return_value = {"id": "DGS10", "title": "10-Year Treasury"}
            m_obs.return_value = (pd.DataFrame({"date": ["2025-01-01"], "value": [4.0]}), {"count": 1})
            self.scheduler.run_daily_update(series_subset=["DGS10"])

        health = self.scheduler.get_health_status()
        self.assertIn("status", health)
        self.assertIn("tasks", health)
        self.assertIn("daily_update", health["tasks"])
        self.assertEqual(health["tasks"]["daily_update"]["status"], "HEALTHY")


if __name__ == "__main__":
    unittest.main()
