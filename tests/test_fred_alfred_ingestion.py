"""Unit tests for FRED/ALFRED ingestion pipeline and Data Lake storage."""

import gc
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import requests

from ingestion.data_lake import DataLakeStore
from ingestion.fred_alfred import (
    EXPANDED_SERIES_CATALOG,
    KEY_ECONOMIC_SERIES,
    SERIES_ALIASES,
    FredAlfredClient,
    FredIngestionPipeline,
    resolve_series_id,
)


class TestExpandedFredCatalog(unittest.TestCase):
    def test_catalog_structure_and_categories(self):
        expected_categories = [
            "Yield Curve & Rates",
            "Fed & Money",
            "Labor Market",
            "Inflation",
            "Real Economy",
            "Financial Conditions",
            "International",
        ]
        for cat in expected_categories:
            self.assertIn(cat, EXPANDED_SERIES_CATALOG)
            self.assertGreaterEqual(len(EXPANDED_SERIES_CATALOG[cat]), 5)

    def test_alias_resolution(self):
        self.assertEqual(resolve_series_id("CLAIMSx"), "ICSA")
        self.assertEqual(resolve_series_id("CONTINUE"), "CCSA")
        self.assertEqual(resolve_series_id("JTSQUIT"), "JTSQUL")
        self.assertEqual(resolve_series_id("LNS14000000"), "UNEMPLOY")
        self.assertEqual(resolve_series_id("CPIMFSSL"), "CPIUFDSL")
        self.assertEqual(resolve_series_id("MOVE"), "VXOCLS")
        self.assertEqual(resolve_series_id("T10YIFR"), "T5YIFR")

    def test_key_series_present_in_expanded_catalog(self):
        # Verify key series IDs are present either directly or via alias
        all_catalog_ids = set()
        for cat_list in EXPANDED_SERIES_CATALOG.values():
            for s in cat_list:
                all_catalog_ids.add(s["series_id"])

        for k_sid in KEY_ECONOMIC_SERIES:
            self.assertIn(k_sid, all_catalog_ids)


class TestFredIngestionPipelineExpanded(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_lake = DataLakeStore(base_dir=self.temp_dir.name)
        self.client = FredAlfredClient(api_key="test_dummy_key", rate_limit_per_min=1000)
        self.pipeline = FredIngestionPipeline(data_lake=self.data_lake, client=self.client)

    def tearDown(self):
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    @patch.object(FredAlfredClient, "get_series_metadata")
    @patch.object(FredAlfredClient, "get_vintage_dates")
    @patch.object(FredAlfredClient, "get_series_observations")
    def test_ingest_expanded_catalog(self, mock_obs, mock_vintages, mock_meta):
        mock_meta.return_value = {"id": "TEST", "title": "Test Series"}
        mock_vintages.return_value = ["2024-01-01", "2024-02-01"]

        df_dummy = pd.DataFrame(
            {"date": ["2025-01-01"], "value": [100.0], "realtime_start": ["2025-01-01"], "realtime_end": ["2025-01-01"]}
        )
        mock_obs.return_value = (df_dummy, {"count": 1})

        # Test ingesting single category
        report = self.pipeline.ingest_expanded_catalog(categories=["Financial Conditions"])

        self.assertIn("total_series", report)
        self.assertIn("total_rows", report)
        self.assertIn("total_vintages", report)
        self.assertIn("execution_time_sec", report)

        self.assertEqual(report["total_series"], len(EXPANDED_SERIES_CATALOG["Financial Conditions"]))
        self.assertGreater(report["total_rows"], 0)
        self.assertIn("Financial Conditions", report["categories"])

    @patch.object(FredAlfredClient, "_request")
    def test_vintage_date_batching_x10(self, mock_request):
        mock_request.return_value = {
            "count": 1,
            "observations": [{"date": "2024-01-01", "value": "5.0", "realtime_start": "2024-01-01", "realtime_end": "2024-01-01"}],
        }

        # 25 vintage dates should result in 3 batch API requests (10, 10, 5)
        vintage_dates = [f"2024-01-{i:02d}" for i in range(1, 26)]
        df, meta = self.client.get_series_observations("GDP", vintage_dates=vintage_dates)

        self.assertFalse(df.empty)
        self.assertEqual(mock_request.call_count, 3)


if __name__ == "__main__":
    unittest.main()
