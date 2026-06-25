import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from collections import deque
from psychological.scrapers.hiring_velocity import HiringVelocityEngine, create_hiring_velocity, JobSpySnapshot


class TestHiringVelocityEngine:
    @pytest.fixture
    def mock_config(self):
        return {
            "hiring_velocity": {
                "lookback_runs_30d": 8,
                "baseline_runs_1y": 10,
                "min_baseline_count": 1,
                "sources": ["linkedin", "indeed"],
                "throttle_per_source": 5,
                "ghost_job_threshold": 0.0,
                "operational_fracture": {
                    "delta_threshold": 0.9,
                    "zscore_threshold": 3.0,
                    "sentiment_threshold": 0.0
                }
            }
        }

    @pytest.fixture
    def engine(self, mock_config):
        with patch('psychological.scrapers.hiring_velocity.load_hybrid_config') as mock_load:
            mock_load.return_value = mock_config
            engine = HiringVelocityEngine(config_dict=mock_config)
            yield engine

    def test_init(self, engine):
        assert engine is not None
        assert "NVDA" in engine.company_mappings
        assert engine.lookback_runs_30d == 8
        assert engine.baseline_runs_1y == 10
        assert "linkedin" in engine.sources

    def test_get_cache_key(self, engine):
        cache_key = engine._get_cache_key("AAPL", "linkedin")
        assert cache_key.startswith("jobspy_AAPL_linkedin_")
        assert datetime.now(timezone.utc).strftime('%Y-%m-%d') in cache_key

    def test_calculate_velocity_metrics_empty(self, engine):
        metrics = engine.calculate_velocity_metrics({})
        assert metrics == {}

    def test_calculate_velocity_metrics_with_data(self, engine):
        snapshots = {
            "linkedin": JobSpySnapshot(
                ticker="AAPL",
                company_name="Apple",
                date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                source="linkedin",
                job_count=100,
                job_count_8_runs_ago=80,
                delta_30d=0.25,
                mean_252_runs=90,
                std_252_runs=10,
                zscore_1y=1.0,
                ghost_job_flag=False,
                operational_fracture_flag=False,
                fetched_at=datetime.now(timezone.utc).isoformat()
            ),
            "indeed": JobSpySnapshot(
                ticker="AAPL",
                company_name="Apple",
                date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                source="indeed",
                job_count=150,
                job_count_8_runs_ago=120,
                delta_30d=0.25,
                mean_252_runs=130,
                std_252_runs=15,
                zscore_1y=1.33,
                ghost_job_flag=False,
                operational_fracture_flag=False,
                fetched_at=datetime.now(timezone.utc).isoformat()
            )
        }
        
        metrics = engine.calculate_velocity_metrics(snapshots)
        
        assert metrics["total_job_count"] == 250
        assert metrics["avg_delta_30d"] == 0.25
        assert metrics["avg_zscore_1y"] == pytest.approx(1.165, rel=0.1)
        assert metrics["ghost_job_detected"] is False
        assert metrics["operational_fracture_detected"] is False
        assert "linkedin" in metrics["sources"]
        assert "indeed" in metrics["sources"]

    def test_calculate_velocity_metrics_ghost_job(self, engine):
        snapshots = {
            "linkedin": JobSpySnapshot(
                ticker="AAPL",
                company_name="Apple",
                date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                source="linkedin",
                job_count=50,
                job_count_8_runs_ago=100,
                delta_30d=-0.5,
                mean_252_runs=80,
                std_252_runs=10,
                zscore_1y=-3.0,
                ghost_job_flag=True,
                operational_fracture_flag=False,
                fetched_at=datetime.now(timezone.utc).isoformat()
            )
        }
        
        metrics = engine.calculate_velocity_metrics(snapshots)
        
        assert metrics["ghost_job_detected"] is True
        assert metrics["operational_fracture_detected"] is False

    def test_calculate_velocity_metrics_operational_fracture(self, engine):
        snapshots = {
            "linkedin": JobSpySnapshot(
                ticker="AAPL",
                company_name="Apple",
                date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                source="linkedin",
                job_count=200,
                job_count_8_runs_ago=80,
                delta_30d=1.5,
                mean_252_runs=90,
                std_252_runs=20,
                zscore_1y=5.5,
                ghost_job_flag=False,
                operational_fracture_flag=True,
                fetched_at=datetime.now(timezone.utc).isoformat()
            )
        }
        
        metrics = engine.calculate_velocity_metrics(snapshots)
        
        assert metrics["ghost_job_detected"] is False
        assert metrics["operational_fracture_detected"] is True

    @pytest.mark.asyncio
    async def test_fetch_job_counts_no_jobspy(self, engine):
        # Test when JobSpy is not available
        with patch('psychological.scrapers.hiring_velocity.JOBSPY_AVAILABLE', False):
            result = await engine.fetch_job_counts("AAPL")
            assert result == {}

    @pytest.mark.asyncio
    async def test_fetch_job_counts_with_history(self, engine):
        # Add some history to test delta and zscore calculations
        # Need at least 8+ items to test the 8-runs-ago logic
        engine._history["linkedin"] = deque([
            {"ticker": "AAPL", "count": 80, "timestamp": "2024-01-01T00:00:00Z"},
            {"ticker": "AAPL", "count": 85, "timestamp": "2024-01-15T00:00:00Z"},
            {"ticker": "AAPL", "count": 90, "timestamp": "2024-02-01T00:00:00Z"},
            {"ticker": "AAPL", "count": 95, "timestamp": "2024-02-15T00:00:00Z"},
            {"ticker": "AAPL", "count": 100, "timestamp": "2024-03-01T00:00:00Z"},
            {"ticker": "AAPL", "count": 105, "timestamp": "2024-03-15T00:00:00Z"},
            {"ticker": "AAPL", "count": 110, "timestamp": "2024-04-01T00:00:00Z"},
            {"ticker": "AAPL", "count": 115, "timestamp": "2024-04-15T00:00:00Z"},
            {"ticker": "AAPL", "count": 120, "timestamp": "2024-05-01T00:00:00Z"},
            {"ticker": "AAPL", "count": 125, "timestamp": "2024-05-15T00:00:00Z"},
            {"ticker": "AAPL", "count": 130, "timestamp": "2024-06-01T00:00:00Z"},
            {"ticker": "AAPL", "count": 135, "timestamp": "2024-06-15T00:00:00Z"},
        ], maxlen=12)
        
        with patch('psychological.scrapers.hiring_velocity.JOBSPY_AVAILABLE', True), \
             patch('psychological.scrapers.hiring_velocity.scrape_jobs', return_value=[{"id": i} for i in range(150)]):
            
            result = await engine.fetch_job_counts("AAPL")
            
            assert "linkedin" in result
            assert result["linkedin"].job_count == 150
            # 8 runs ago from 12 items = index -8 = 4th item (0-indexed) = 100
            assert result["linkedin"].job_count_8_runs_ago == 100
            assert result["linkedin"].delta_30d == pytest.approx(0.5, rel=0.1)

    def test_calculate_velocity_metrics_mixed_sources(self, engine):
        snapshots = {
            "linkedin": JobSpySnapshot(
                ticker="AAPL",
                company_name="Apple",
                date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                source="linkedin",
                job_count=100,
                job_count_8_runs_ago=80,
                delta_30d=0.25,
                mean_252_runs=90,
                std_252_runs=10,
                zscore_1y=1.0,
                ghost_job_flag=False,
                operational_fracture_flag=False,
                fetched_at=datetime.now(timezone.utc).isoformat()
            ),
            "indeed": JobSpySnapshot(
                ticker="AAPL",
                company_name="Apple",
                date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                source="indeed",
                job_count=150,
                job_count_8_runs_ago=None,
                delta_30d=None,
                mean_252_runs=None,
                std_252_runs=None,
                zscore_1y=None,
                ghost_job_flag=False,
                operational_fracture_flag=False,
                fetched_at=datetime.now(timezone.utc).isoformat()
            )
        }
        
        metrics = engine.calculate_velocity_metrics(snapshots)
        
        assert metrics["total_job_count"] == 250
        assert metrics["avg_delta_30d"] == 0.25
        assert metrics["avg_zscore_1y"] == 1.0
        assert metrics["sources"]["linkedin"]["count"] == 100
        assert metrics["sources"]["indeed"]["count"] == 150
        assert metrics["sources"]["indeed"]["delta_30d"] is None


class TestCreateHiringVelocity:
    @pytest.mark.asyncio
    async def test_create_hiring_velocity(self):
        with patch('psychological.scrapers.hiring_velocity.load_hybrid_config') as mock_load:
            mock_load.return_value = {"hiring_velocity": {}}
            engine = await create_hiring_velocity()
            assert isinstance(engine, HiringVelocityEngine)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])