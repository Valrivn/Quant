import pytest
import tempfile
import os
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from psychological.orchestrator import PsychologicalOrchestrator, create_psychological_orchestrator
from psychological.interfaces import PsychologicalFeatureVector, RegimeOutput, CorporateAffinity, RedditCommentPayload, VelocitySnapshot
from db.schema import create_psychological_tables, create_phase1_tables


class TestPsychologicalOrchestrator:
    @pytest.fixture
    def temp_db(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        import sqlite3
        conn = sqlite3.connect(db_path)
        create_psychological_tables(conn)
        create_phase1_tables(conn)
        conn.close()
        
        yield db_path
        
        os.unlink(db_path)

    @pytest.fixture
    def mock_config(self):
        return {
            "psychological": {
                "reddit": {"subreddits": ["wallstreetbets"]},
                "github": {"token": "test"},
                "corp_anonymous": {},
                "fusion_weights": {
                    "psychological_regime": 0.60,
                    "fintech_confirmation": 0.25,
                    "quantitative_value": 0.15
                },
                "regime_thresholds": {
                    "panic_ratio": 0.25,
                    "panic_velocity_sigma": 2.0,
                    "euphoria_ratio": 4.0,
                    "euphoria_velocity_sigma": 2.5,
                    "apathy_ratio_min": 0.8,
                    "apathy_ratio_max": 1.5,
                    "apathy_velocity_sigma": 0.5,
                    "asymmetric_employee_sigma": -1.5,
                    "asymmetric_git_velocity_sigma": 1.0,
                    "glassdoor_divergence_threshold": 0.3
                }
            }
        }

    @pytest.fixture
    def orchestrator(self, temp_db, mock_config):
        with patch('psychological.orchestrator.load_hybrid_config') as mock_load:
            mock_load.return_value = {
                "psychological": mock_config["psychological"],
                "github_mappings": {"AAPL": "apple/swift"},
                "adzuna": {"app_id": "test", "app_key": "test"},
                "glassdoor": {},
                "comparably": {},
                "product_intel": {}
            }
            
            orch = PsychologicalOrchestrator(db_path=temp_db, config_dict=mock_config["psychological"])
            
            # Mock all scrapers
            orch.nlp_engine = Mock()
            orch.velocity_tracker = Mock()
            orch.state_machine = Mock()
            orch.feature_store = Mock()
            orch.validation_gate = Mock()
            orch.cross_validation_engine = Mock()
            orch.signal_matrix = Mock()
            
            yield orch

    def test_init(self, orchestrator):
        assert orchestrator is not None
        assert orchestrator.db_path is not None
        assert orchestrator.fusion_weights["psychological_regime"] == 0.60

    @pytest.mark.asyncio
    async def test_initialize_scrapers(self, orchestrator):
        with patch('psychological.orchestrator.create_old_reddit_scraper', new_callable=AsyncMock) as mock_reddit, \
             patch('psychological.orchestrator.create_github_tracker', new_callable=AsyncMock) as mock_github, \
             patch('psychological.orchestrator.create_corp_anonymous_scraper', new_callable=AsyncMock) as mock_corp, \
             patch('psychological.orchestrator.create_glassdoor_scraper', new_callable=AsyncMock) as mock_gd, \
             patch('psychological.orchestrator.create_comparably_scraper', new_callable=AsyncMock) as mock_comp, \
             patch('psychological.orchestrator.create_product_intel_engine', new_callable=AsyncMock) as mock_prod:
            
            await orchestrator.initialize_scrapers()
            
            assert orchestrator.reddit_scraper is not None
            assert orchestrator.github_tracker is not None
            assert orchestrator.corp_scraper is not None
            assert orchestrator.glassdoor_scraper is not None
            assert orchestrator.comparably_scraper is not None
            assert orchestrator.product_intel_engine is not None

    @pytest.mark.asyncio
    async def test_run_primary_pipeline(self, orchestrator):
        mock_comment = RedditCommentPayload(
            ticker="AAPL",
            text="AAPL to the moon!",
            subreddit="wallstreetbets",
            created_utc=int(datetime.now(timezone.utc).timestamp()),
            score=100
        )
        
        async def mock_harvest(*args, **kwargs):
            yield mock_comment
        
        orchestrator.reddit_scraper = Mock()
        orchestrator.reddit_scraper.harvest_raw_commentary = mock_harvest
        
        orchestrator.nlp_engine.analyze = Mock(return_value={
            "compound_vader": 0.8,
            "bull_bear_ratio": 4.0,
            "bullish_count": 1,
            "bearish_count": 0
        })
        
        orchestrator.velocity_tracker.record_snapshot = Mock()
        orchestrator.velocity_tracker.calculate_velocity_metrics = Mock(return_value={
            "mention_velocity": 1.0,
            "comment_volume_sigma": 0.5,
            "acceleration": 0.1
        })
        
        orchestrator.state_machine.evaluate = Mock(return_value={
            "regime": "NEUTRAL",
            "contrarian_buy_authorized": False,
            "confidence": 0.5
        })
        
        orchestrator.feature_store.commit_vector = Mock(return_value=1)
        orchestrator.feature_store.commit_regime = Mock()
        
        orchestrator.glassdoor_scraper = Mock()
        orchestrator.glassdoor_scraper.get_all_snapshots = AsyncMock(return_value={})
        orchestrator.comparably_scraper = Mock()
        orchestrator.comparably_scraper.get_all_snapshots = AsyncMock(return_value={})
        
        result = await orchestrator.run_primary_pipeline(["AAPL"])
        
        assert result.source == "reddit_custom"
        assert "AAPL" in result.tickers_processed
        assert result.vectors_committed == 1
        assert result.regimes_committed == 1

    @pytest.mark.asyncio
    async def test_run_secondary_pipeline(self, orchestrator):
        orchestrator.github_tracker = Mock()
        orchestrator.github_tracker.get_all_metrics = AsyncMock(return_value={
            "AAPL": {"repo": "apple/swift", "stars": 1000, "forks": 200}
        })
        orchestrator.github_tracker.calculate_velocities = Mock(return_value={
            "dev_fork_acceleration": 0.5,
            "star_velocity": 10.0,
            "commit_velocity": 2.0
        })
        
        orchestrator.corp_scraper = Mock()
        orchestrator.corp_scraper.get_all_snapshots = AsyncMock(return_value={
            "AAPL": {"ticker": "AAPL", "job_count": 500}
        })
        orchestrator.corp_scraper.calculate_sentiment_proxy = Mock(return_value=0.2)
        
        orchestrator.product_intel_engine = Mock()
        orchestrator.product_intel_engine.get_all_snapshots = AsyncMock(return_value={
            "AAPL": {"g2": [], "capterra": [], "app_store": []}
        })
        orchestrator.product_intel_engine.compute_product_sentiment = Mock(return_value=0.3)
        
        affinities = await orchestrator.run_secondary_pipeline(["AAPL"])
        
        assert "AAPL" in affinities
        assert "dev_fork_acceleration" in affinities["AAPL"]
        assert "employee_sentiment_proxy" in affinities["AAPL"]
        assert "product_sentiment_proxy" in affinities["AAPL"]

    def test_get_quantitative_value_signal(self, orchestrator):
        import sqlite3
        conn = sqlite3.connect(orchestrator.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO quantitative_dcf_floor 
            (ticker, date, intrinsic_floor, intrinsic_ceiling, current_price, margin_of_safety, wacc, fcf_projection, terminal_value, model_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("AAPL", datetime.now(timezone.utc).strftime("%Y-%m-%d"), 150.0, 200.0, 175.0, 0.2, 0.1, 10.0, 500.0, "stub_v1", int(datetime.now(timezone.utc).timestamp())))
        conn.commit()
        conn.close()
        
        signal = orchestrator.get_quantitative_value_signal("AAPL")
        assert 0.0 <= signal <= 1.0

    def test_get_quantitative_value_signal_missing(self, orchestrator):
        signal = orchestrator.get_quantitative_value_signal("NONEXISTENT")
        assert signal == 0.5

    def test_get_dcf_floor_data(self, orchestrator):
        import sqlite3
        conn = sqlite3.connect(orchestrator.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO quantitative_dcf_floor 
            (ticker, date, intrinsic_floor, intrinsic_ceiling, current_price, margin_of_safety, wacc, fcf_projection, terminal_value, model_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("AAPL", datetime.now(timezone.utc).strftime("%Y-%m-%d"), 150.0, 200.0, 175.0, 0.2, 0.1, 10.0, 500.0, "stub_v1", int(datetime.now(timezone.utc).timestamp())))
        conn.commit()
        conn.close()
        
        data = orchestrator.get_dcf_floor_data("AAPL")
        assert data is not None
        assert data["intrinsic_floor"] == 150.0
        assert data["current_price"] == 175.0

    def test_get_dcf_floor_data_missing(self, orchestrator):
        data = orchestrator.get_dcf_floor_data("NONEXISTENT")
        assert data is None

    def test_compute_cross_validation_penalty(self, orchestrator):
        penalty = orchestrator.compute_cross_validation_penalty("AAPL", 4.0, 0.5, 0.1)
        assert 0.0 <= penalty <= 1.0
        
        penalty = orchestrator.compute_cross_validation_penalty("AAPL", 1.0, 0.5, 0.1)
        assert 0.0 <= penalty <= 1.0

    def test_compute_fused_confidence(self, orchestrator):
        regime = RegimeOutput(regime="NEUTRAL", contrarian_buy_authorized=False, confidence=0.5)
        fused = orchestrator.compute_fused_confidence(regime, 0.5, 0.5)
        assert 0.0 <= fused <= 1.0
        
        regime = RegimeOutput(regime="PANIC_CAPITULATION", contrarian_buy_authorized=True, confidence=0.9)
        fused = orchestrator.compute_fused_confidence(regime, 0.8, 0.7)
        assert 0.0 <= fused <= 1.0

    @pytest.mark.asyncio
    async def test_run_full_pipeline(self, orchestrator):
        mock_comment = RedditCommentPayload(
            ticker="AAPL",
            text="AAPL to the moon!",
            subreddit="wallstreetbets",
            created_utc=int(datetime.now(timezone.utc).timestamp()),
            score=100
        )
        
        orchestrator.initialize_scrapers = AsyncMock()
        
        orchestrator.run_primary_pipeline = AsyncMock(return_value=Mock(
            source="reddit_custom",
            tickers_processed=["AAPL"],
            vectors_committed=1,
            regimes_committed=1,
            errors=[]
        ))
        
        orchestrator.run_secondary_pipeline = AsyncMock(return_value={
            "AAPL": CorporateAffinity(employee_sentiment_proxy=0.2, dev_fork_acceleration=0.5, product_sentiment_proxy=0.3)
        })
        
        orchestrator.fetch_fintech_confirmation = AsyncMock(return_value={"AAPL": 0.6})
        
        orchestrator.feature_store.get_regimes = Mock(return_value=[{
            "active_regime": "NEUTRAL",
            "contrarian_buy_authorized": 0,
            "confidence_score": 0.5,
            "bull_bear_ratio": 1.2,
            "velocity_sigma": 0.3,
            "fintech_confirmation_score": 0.5,
            "quantitative_value_signal": 0.5
        }])
        
        orchestrator.cross_validation_engine.evaluate_all_layers = Mock(return_value={
            "final_override": False,
            "combined_penalty": 1.0,
            "layers": {}
        })
        
        orchestrator.signal_matrix.evaluate = Mock(return_value=Mock(
            execution_directive="HOLD",
            contrarian_buy_authorized=False,
            dcf_floor_signal=0.5,
            validation_passed=True,
            rationale="Test"
        ))
        
        results = await orchestrator.run_full_pipeline(["AAPL"])
        
        assert "primary_result" in results
        assert "corporate_affinities" in results
        assert "fused_results" in results
        assert "AAPL" in results["fused_results"]

    def test_get_regime_status(self, orchestrator):
        orchestrator.feature_store.get_regimes = Mock(return_value=[{
            "active_regime": "PANIC_CAPITULATION",
            "contrarian_buy_authorized": 1,
            "confidence_score": 0.8,
            "bull_bear_ratio": 0.2,
            "velocity_sigma": 2.5,
            "fintech_confirmation_score": 0.6,
            "quantitative_value_signal": 0.7
        }])
        
        status = orchestrator.get_regime_status("AAPL")
        assert status is not None
        assert status["active_regime"] == "PANIC_CAPITULATION"
        assert "fused_confidence" in status

    def test_get_regime_status_empty(self, orchestrator):
        orchestrator.feature_store.get_regimes = Mock(return_value=[])
        status = orchestrator.get_regime_status("AAPL")
        assert status is None


class TestCreatePsychologicalOrchestrator:
    @pytest.mark.asyncio
    async def test_create_psychological_orchestrator(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            with patch('psychological.orchestrator.load_hybrid_config') as mock_load:
                mock_load.return_value = {
                    "psychological": {"fusion_weights": {}},
                    "github_mappings": {},
                    "adzuna": {}
                }
                orch = await create_psychological_orchestrator(db_path)
                assert isinstance(orch, PsychologicalOrchestrator)
        finally:
            os.unlink(db_path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])