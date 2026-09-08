"""Ingestion package containing data lake abstraction, FRED/ALFRED client, and Google Trends scraper."""

from ingestion.data_lake import DataLakeStore
from ingestion.fred_alfred import FredAlfredClient, FredIngestionPipeline, KEY_ECONOMIC_SERIES
from ingestion.google_trends import (
    CompanySpec,
    KeywordGenerator,
    ProxyTracker,
    ProxyPool,
    CheckpointStore,
    GoogleTrendsScraper,
    GoogleTrendsIngestionPipeline,
    ParallelGoogleTrendsPipeline,
    chunk_keywords,
)

__all__ = [
    "DataLakeStore",
    "FredAlfredClient",
    "FredIngestionPipeline",
    "KEY_ECONOMIC_SERIES",
    "CompanySpec",
    "KeywordGenerator",
    "ProxyTracker",
    "ProxyPool",
    "CheckpointStore",
    "GoogleTrendsScraper",
    "GoogleTrendsIngestionPipeline",
    "ParallelGoogleTrendsPipeline",
    "chunk_keywords",
]
