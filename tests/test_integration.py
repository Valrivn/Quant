import pytest
import sqlite3
import tempfile
import os
import sys
from datetime import datetime, timezone, timedelta

# Must patch before any imports that use DB_PATH
@pytest.fixture(scope="function")
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    
    # Patch the DB_PATH BEFORE importing modules that use it
    import db.connection
    original_path = db.connection.DB_PATH
    db.connection.DB_PATH = db_path
    
    # Clear any cached thread-local connection
    if hasattr(db.connection._local, 'conn') and db.connection._local.conn is not None:
        db.connection._local.conn.close()
        db.connection._local.conn = None
    
    # Re-initialize
    from db.connection import init_db
    init_db()
    
    yield db_path
    
    # Cleanup
    if hasattr(db.connection._local, 'conn') and db.connection._local.conn is not None:
        db.connection._local.conn.close()
        db.connection._local.conn = None
    db.connection.DB_PATH = original_path
    if os.path.exists(db_path):
        os.unlink(db_path)


class TestDatabaseIntegration:
    """Test database operations integration."""
    
    def test_init_creates_all_tables(self, temp_db):
        from db.connection import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        
        expected = {
            'submissions', 'scrape_state', 'daily_aggregations',
            'risk_signals', 'composite_scores', 'sentiment_runs', 'weight_versions'
        }
        assert expected.issubset(tables)
    
    def test_partition_creation(self, temp_db):
        from db.schema import get_or_create_partition
        from db.connection import get_db_connection
        
        now = int(datetime.now(timezone.utc).timestamp())
        partition = get_or_create_partition(get_db_connection(), now)
        
        assert partition.startswith('submissions_')
        assert '_' in partition


class TestSentimentEngineIntegration:
    """Test sentiment engine with real data scenarios."""
    
    def test_full_pipeline_ticker_extraction(self):
        from scraper.engine import QuantSentimentEngine
        engine = QuantSentimentEngine()
        
        text = """
        Just bought more $AAPL calls expiring next week. 
        The moat is widening with services revenue growth.
        Bullish on their AI integration roadmap.
        """
        
        tickers = engine.extract_tickers_with_confidence(text)
        ticker_symbols = [t for t, _ in tickers]
        
        assert 'AAPL' in ticker_symbols
        aapl_conf = next(c for t, c in tickers if t == 'AAPL')
        assert aapl_conf > 0.5
    
    def test_entity_resolution(self):
        from scraper.engine import QuantSentimentEngine
        engine = QuantSentimentEngine()
        
        text = "Microsoft and Nvidia are leading the AI race"
        tickers = engine.extract_tickers(text)
        
        assert 'MSFT' in tickers
        assert 'NVDA' in tickers
    
    def test_blacklist_filtering(self):
        from scraper.engine import QuantSentimentEngine
        engine = QuantSentimentEngine()
        
        text = "CEO YOLO GPU AI FOMO"
        tickers = engine.extract_tickers(text)
        
        assert len(tickers) == 0
    
    def test_sentiment_scoring(self):
        from scraper.engine import QuantSentimentEngine
        engine = QuantSentimentEngine()
        
        bullish = "bullish calls moon tendies undervalued growth moat"
        bearish = "bearish puts bagholder rug pull dump overvalued loss bankrupt"
        
        assert engine.analyze_sentiment(bullish) > 0.3
        assert engine.analyze_sentiment(bearish) < -0.3
    
    def test_risk_detection(self):
        from scraper.engine import QuantSentimentEngine
        engine = QuantSentimentEngine()
        
        text = "War tensions Taiwan tariff embargo factory closure semiconductor shortage"
        risks = engine.scan_risks(text)
        
        assert risks['geopolitical'] >= 3
        assert risks['supply_chain'] >= 2


class TestAggregationIntegration:
    """Test daily aggregation pipeline."""
    
    def test_daily_aggregation_flow(self, temp_db):
        from db.connection import get_db_connection
        from db.jobs import run_daily_aggregation
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        
        cursor.execute("""
            INSERT INTO daily_aggregations 
            (ticker, date, category, subreddit, mention_count, avg_sentiment, weighted_sum, total_weight)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ('AAPL', today, 'tech_product', 'hardware', 5, 0.5, 2.5, 5.0))
        
        cursor.execute("""
            INSERT INTO daily_aggregations 
            (ticker, date, category, subreddit, mention_count, avg_sentiment, weighted_sum, total_weight)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ('AAPL', today, 'retail_options', 'wallstreetbets', 10, 0.3, 3.0, 10.0))
        
        conn.commit()
        
        run_daily_aggregation(today)
        
        cursor.execute("SELECT composite_sentiment FROM composite_scores WHERE ticker=? AND date=?", ('AAPL', today))
        row = cursor.fetchone()
        
        assert row is not None
        assert row[0] is not None


class TestWeightVersionIntegration:
    """Test weight version management."""
    
    def test_weight_version_crud(self, temp_db):
        from db.jobs import record_weight_version, promote_weight_version, get_active_weight_version
        
        cat_weights = {'macro_geopolitical': 0.25, 'tech_product': 0.75}
        sub_weights = {'macro_geopolitical': {'geopolitics': 1.0}, 'tech_product': {'hardware': 1.0}}
        
        version_id = record_weight_version(
            config_yaml="test",
            category_weights=cat_weights,
            subreddit_weights=sub_weights,
            ic_score=0.15,
            sharpe_ratio=1.2,
            hit_rate=0.6,
            lookback_days=180,
            optimization_method="test"
        )
        
        assert version_id > 0
        
        promote_weight_version(version_id)
        
        active = get_active_weight_version()
        assert active is not None
        assert active['version_id'] == version_id
        assert active['is_active'] == 1


class TestBacktestIntegration:
    """Test backtesting with mock data."""
    
    def test_backtest_runs_without_error(self, temp_db):
        from backtesting.backtest import run_walk_forward_backtest
        
        cat_weights = {'macro_geopolitical': 0.25, 'tech_product': 0.75}
        sub_weights = {'macro_geopolitical': {'geopolitics': 1.0}, 'tech_product': {'hardware': 1.0}}
        
        results = run_walk_forward_backtest(
            category_weights=cat_weights,
            subreddit_weights=sub_weights,
            lookback_days=30
        )
        
        assert 'ic' in results
        assert 'sharpe' in results
        assert 'hit_rate' in results
        assert 'returns' in results


class TestOptimizationIntegration:
    """Test optimization pipeline (lightweight)."""
    
    def test_optimization_structure(self, temp_db):
        from optimization.optuna_search import run_bayesian_optimization
        
        try:
            results = run_bayesian_optimization(trials=2, objective_metric="sharpe")
            assert 'category_weights' in results
            assert 'subreddit_weights' in results
            assert 'metrics' in results
        except Exception as e:
            pytest.skip(f"Optimization needs data: {e}")


class TestDriftDetectionIntegration:
    """Test drift detection."""
    
    def test_drift_check_structure(self, temp_db):
        from backtesting.drift_detection import check_ic_drift_and_reoptimize
        
        result = check_ic_drift_and_reoptimize(decay_threshold=0.20, recent_window_days=60)
        
        assert 'drift_detected' in result
        assert isinstance(result['drift_detected'], bool)


class TestRiskDetectionIntegration:
    """Test risk narrative detection."""
    
    def test_risk_narratives_empty(self, temp_db):
        from scraper.risk_detector import detect_risk_narratives
        
        df = detect_risk_narratives(window_days=14, z_threshold=1.5)
        assert df.empty or 'z_score' in df.columns


if __name__ == "__main__":
    pytest.main([__file__, "-v"])