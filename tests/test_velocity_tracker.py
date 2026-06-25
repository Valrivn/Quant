import pytest
import tempfile
import os
import sqlite3
from datetime import datetime, timezone
from psychological.velocity_tracker import VelocityTracker, create_velocity_tracker
from psychological.interfaces import VelocitySnapshot
from db.schema import create_psychological_tables


class TestVelocityTracker:
    @pytest.fixture
    def temp_db(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        # Create tables
        conn = sqlite3.connect(db_path)
        create_psychological_tables(conn)
        conn.close()
        
        yield db_path
        if os.path.exists(db_path):
            os.unlink(db_path)

    @pytest.fixture
    def tracker(self, temp_db):
        return create_velocity_tracker(db_path=temp_db)

    def test_record_snapshot(self, tracker):
        snapshot = VelocitySnapshot(
            ticker="AAPL",
            window_start=int(datetime.now(timezone.utc).timestamp()),
            window_end=int(datetime.now(timezone.utc).timestamp()) + 3600,
            window_type="1h",
            mention_count=10,
            comment_volume=50,
            unique_authors=8
        )
        tracker.record_snapshot(snapshot)
        
        history = tracker.get_velocity_history("AAPL", 1)
        assert len(history) == 1
        assert history[0]["mention_count"] == 10
        assert history[0]["comment_volume"] == 50

    def test_record_provenance(self, tracker):
        tracker.record_provenance("AAPL", 
            int(datetime.now(timezone.utc).timestamp()), 
            "1h", "reddit", 1.0, 10)
        
        provenance = tracker.get_provenance_history("AAPL", 1)
        assert len(provenance) == 1
        assert provenance[0]["source"] == "reddit"
        assert provenance[0]["source_weight"] == 1.0

    def test_calculate_velocity_metrics_insufficient_data(self, tracker):
        metrics = tracker.calculate_velocity_metrics("AAPL", 24)
        
        assert metrics["mention_velocity"] == 0.0
        assert metrics["comment_volume_sigma"] == 0.0
        assert metrics["acceleration"] == 0.0

    def test_calculate_velocity_metrics_with_data(self, tracker):
        base_time = int(datetime.now(timezone.utc).timestamp())
        
        for i in range(5):
            snapshot = VelocitySnapshot(
                ticker="AAPL",
                window_start=base_time - i * 3600,
                window_end=base_time - i * 3600 + 3600,
                window_type="1h",
                mention_count=10 + i * 2,
                comment_volume=50 + i * 5,
                unique_authors=8 + i
            )
            tracker.record_snapshot(snapshot)
        
        metrics = tracker.calculate_velocity_metrics("AAPL", 1)
        
        assert metrics["mention_velocity"] > 0
        assert "comment_volume_sigma" in metrics
        assert "acceleration" in metrics
        assert metrics["velocity_1h"] > 0

    def test_sigma_calculation(self, tracker):
        base_time = int(datetime.now(timezone.utc).timestamp())
        
        for i in range(10):
            snapshot = VelocitySnapshot(
                ticker="AAPL",
                window_start=base_time - i * 3600,
                window_end=base_time - i * 3600 + 3600,
                window_type="1h",
                mention_count=10,
                comment_volume=50 + i,
                unique_authors=8
            )
            tracker.record_snapshot(snapshot)
        
        sigma = tracker.calculate_sigma("AAPL", 24)
        assert isinstance(sigma, float)

    def test_acceleration_calculation(self, tracker):
        base_time = int(datetime.now(timezone.utc).timestamp())
        
        snapshot1 = VelocitySnapshot(
            ticker="AAPL",
            window_start=base_time - 3600,
            window_end=base_time,
            window_type="1h",
            mention_count=10,
            comment_volume=50,
            unique_authors=8
        )
        snapshot2 = VelocitySnapshot(
            ticker="AAPL",
            window_start=base_time,
            window_end=base_time + 3600,
            window_type="1h",
            mention_count=20,
            comment_volume=100,
            unique_authors=15
        )
        tracker.record_snapshot(snapshot1)
        tracker.record_snapshot(snapshot2)
        
        acceleration = tracker.calculate_acceleration("AAPL", 1)
        assert acceleration > 0

    def test_multiple_window_types(self, tracker):
        base_time = int(datetime.now(timezone.utc).timestamp())
        
        for window_type, hours in [("1h", 1), ("4h", 4), ("24h", 24)]:
            snapshot = VelocitySnapshot(
                ticker="AAPL",
                window_start=base_time,
                window_end=base_time + hours * 3600,
                window_type=window_type,
                mention_count=10 * hours,
                comment_volume=50 * hours,
                unique_authors=8
            )
            tracker.record_snapshot(snapshot)
        
        history_1h = tracker.get_velocity_history("AAPL", 1)
        history_4h = tracker.get_velocity_history("AAPL", 4)
        history_24h = tracker.get_velocity_history("AAPL", 24)
        
        assert len(history_1h) == 1
        assert len(history_4h) == 1
        assert len(history_24h) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])