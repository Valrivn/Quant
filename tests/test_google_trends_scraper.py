"""Unit tests for Google Trends scraper and ingestion pipeline."""

import gc
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd
import requests

from ingestion.data_lake import DataLakeStore
from ingestion.google_trends import (
    GoogleTrendsIngestionPipeline,
    GoogleTrendsScraper,
    chunk_keywords,
)


class TestGoogleTrendsScraper(unittest.TestCase):
    def setUp(self):
        self.scraper = GoogleTrendsScraper(min_delay=0.01, max_delay=0.02, max_retries=2)

    def test_chunk_keywords(self):
        keywords = ["k1", "k2", "k3", "k4", "k5", "k6", "k7"]
        chunks = chunk_keywords(keywords, max_size=5)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0], ["k1", "k2", "k3", "k4", "k5"])
        self.assertEqual(chunks[1], ["k6", "k7"])

    def test_clean_json_response(self):
        text = ")]}'\n{\"status\": \"ok\", \"value\": 100}"
        cleaned = self.scraper._clean_json_response(text)
        self.assertEqual(cleaned["status"], "ok")

    def test_check_captcha_or_rate_limit(self):
        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429
        self.assertTrue(self.scraper._check_captcha_or_rate_limit(mock_resp_429))

        mock_resp_captcha = MagicMock()
        mock_resp_captcha.status_code = 200
        mock_resp_captcha.text = "<html>Please solve CAPTCHA to continue</html>"
        self.assertTrue(self.scraper._check_captcha_or_rate_limit(mock_resp_captcha))

        mock_resp_ok = MagicMock()
        mock_resp_ok.status_code = 200
        mock_resp_ok.text = ")]}'\n{\"widgets\": []}"
        self.assertFalse(self.scraper._check_captcha_or_rate_limit(mock_resp_ok))

    def test_max_keywords_validation(self):
        keywords = ["k1", "k2", "k3", "k4", "k5", "k6"]
        with self.assertRaises(ValueError):
            self.scraper.get_interest_over_time(keywords)

    @patch.object(GoogleTrendsScraper, "_http_get")
    def test_get_interest_over_time(self, mock_http_get):
        # Mock explore response (widgets)
        widgets_response = {
            "widgets": [
                {
                    "id": "TIMESERIES",
                    "token": "test_token_123",
                    "request": {"comparisonItem": [{"keyword": "stock"}]},
                }
            ]
        }

        # Mock multiline response
        multiline_response = {
            "default": {
                "timelineData": [
                    {"time": "1700000000", "formattedTime": "Nov 2023", "value": [85]},
                    {"time": "1700086400", "formattedTime": "Nov 2023", "value": [90]},
                ]
            }
        }

        mock_http_get.side_effect = [widgets_response, multiline_response]

        df = self.scraper.get_interest_over_time(["stock"])
        self.assertEqual(len(df), 2)
        self.assertIn("stock", df.columns)
        self.assertEqual(df["stock"].iloc[0], 85)

    @patch.object(GoogleTrendsScraper, "_http_get")
    def test_get_interest_by_region(self, mock_http_get):
        widgets_response = {
            "widgets": [
                {
                    "id": "GEO_MAP",
                    "token": "geo_token_123",
                    "request": {"comparisonItem": [{"keyword": "earnings"}]},
                }
            ]
        }

        geo_response = {
            "default": {
                "geoMapData": [
                    {"geoCode": "US-CA", "geoName": "California", "value": [100]},
                    {"geoCode": "US-NY", "geoName": "New York", "value": [95]},
                ]
            }
        }

        mock_http_get.side_effect = [widgets_response, geo_response]

        df = self.scraper.get_interest_by_region(["earnings"])
        self.assertEqual(len(df), 2)
        self.assertIn("geo_code", df.columns)
        self.assertIn("earnings", df.columns)


class TestGoogleTrendsIngestionPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_lake = DataLakeStore(base_dir=self.temp_dir.name)
        self.scraper = GoogleTrendsScraper(min_delay=0.01, max_delay=0.02, max_retries=2)
        self.pipeline = GoogleTrendsIngestionPipeline(data_lake=self.data_lake, scraper=self.scraper)

    def tearDown(self):
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    @patch.object(GoogleTrendsScraper, "get_interest_over_time")
    @patch.object(GoogleTrendsScraper, "get_interest_by_region")
    @patch.object(GoogleTrendsScraper, "get_related_queries")
    def test_ingest_keywords_pipeline(self, mock_related, mock_region, mock_time):
        df_time = pd.DataFrame(
            {"stock": [50, 60], "earnings": [70, 80]},
            index=pd.to_datetime(["2025-01-01", "2025-01-02"]),
        )
        mock_time.return_value = df_time

        df_region = pd.DataFrame(
            {"geo_code": ["US-NY"], "geo_name": ["New York"], "stock": [90], "earnings": [85]}
        )
        mock_region.return_value = df_region

        mock_related.return_value = {
            "stock": {"top": [{"query": "stock price", "value": 100}], "rising": []}
        }

        keywords = ["stock", "earnings"]
        report = self.pipeline.ingest_keywords(keywords, batch_id="test_batch_1")

        self.assertEqual(report["status"], "SUCCESS")
        self.assertIn("interest_over_time", report["saved_paths"])
        self.assertIn("interest_by_region", report["saved_paths"])
        self.assertIn("related_queries", report["saved_paths"])

        # Check loaded data lake entries
        time_df = self.data_lake.load_dataframe("google_trends", "test_batch_1_time")
        self.assertEqual(len(time_df), 2)


if __name__ == "__main__":
    unittest.main()
