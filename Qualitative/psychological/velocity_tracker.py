import sqlite3
import time
from typing import List, Dict, Optional, Tuple
from statistics import mean, stdev
from datetime import datetime, timezone
from config import load_hybrid_config
from psychological.interfaces import VelocitySnapshot


class VelocityTracker:
    def __init__(self, db_path: str = "reddit_quant.db", config_dict: dict = None):
        self.db_path = db_path
        self.config = config_dict or load_hybrid_config().get("psychological", {})
        self.velocity_windows = self.config.get("velocity_windows", {
            "short_hours": 1,
            "medium_hours": 4,
            "long_hours": 24
        })
        self._ensure_provenance_table()
        
    def _ensure_provenance_table(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS velocity_provenance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    window_start INTEGER NOT NULL,
                    window_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_weight REAL NOT NULL,
                    mention_count INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(ticker, window_start, window_type, source)
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_velocity_provenance_ticker ON velocity_provenance(ticker, window_start)")
            conn.commit()
        
    def _get_window_type(self, hours: int) -> str:
        if hours <= 1:
            return "1h"
        elif hours <= 4:
            return "4h"
        else:
            return "24h"
            
    def record_snapshot(self, snapshot: VelocitySnapshot) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO velocity_snapshots 
                (ticker, window_start, window_end, window_type, mention_count, comment_volume, unique_authors)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot["ticker"],
                snapshot["window_start"],
                snapshot["window_end"],
                snapshot["window_type"],
                snapshot["mention_count"],
                snapshot["comment_volume"],
                snapshot["unique_authors"]
            ))
            conn.commit()
            
    def record_provenance(self, ticker: str, window_start: int, window_type: str, 
                          source: str, source_weight: float, mention_count: int) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO velocity_provenance 
                (ticker, window_start, window_type, source, source_weight, mention_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                ticker, window_start, window_type, source, source_weight, mention_count,
                int(datetime.now(timezone.utc).timestamp())
            ))
            conn.commit()
            
    def get_velocity_history(self, ticker: str, window_hours: int, limit: int = 100) -> List[VelocitySnapshot]:
        window_type = self._get_window_type(window_hours)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM velocity_snapshots
                WHERE ticker = ? AND window_type = ?
                ORDER BY window_start DESC
                LIMIT ?
            """, (ticker, window_type, limit))
            return [dict(row) for row in cursor.fetchall()]
            
    def get_provenance_history(self, ticker: str, window_hours: int, limit: int = 100) -> List[Dict]:
        window_type = self._get_window_type(window_hours)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM velocity_provenance
                WHERE ticker = ? AND window_type = ?
                ORDER BY window_start DESC
                LIMIT ?
            """, (ticker, window_type, limit))
            return [dict(row) for row in cursor.fetchall()]
            
    def calculate_velocity_metrics(self, ticker: str, window_hours: int = 24) -> Dict:
        snapshots = self.get_velocity_history(ticker, window_hours)
        if len(snapshots) < 2:
            return {
                "mention_velocity": 0.0,
                "comment_volume_sigma": 0.0,
                "acceleration": 0.0,
                "velocity_1h": 0.0,
                "velocity_4h": 0.0,
                "velocity_24h": 0.0,
                "sigma_1h": 0.0,
                "sigma_4h": 0.0,
                "sigma_24h": 0.0,
                "cross_source_provenance": []
            }
            
        mention_counts = [s["mention_count"] for s in snapshots]
        comment_volumes = [s["comment_volume"] for s in snapshots]
        
        current_mentions = mention_counts[0]
        current_volume = comment_volumes[0]
        
        mean_mentions = mean(mention_counts)
        std_mentions = stdev(mention_counts) if len(mention_counts) > 1 else 1.0
        mean_volume = mean(comment_volumes)
        std_volume = stdev(comment_volumes) if len(comment_volumes) > 1 else 1.0
        
        mention_velocity = current_mentions / window_hours if window_hours > 0 else 0.0
        comment_volume_sigma = (current_volume - mean_volume) / std_volume if std_volume > 0 else 0.0
        
        if len(mention_counts) >= 2:
            prev_mentions = mention_counts[1]
            time_diff_hours = (snapshots[0]["window_start"] - snapshots[1]["window_start"]) / 3600
            acceleration = (current_mentions - prev_mentions) / time_diff_hours if time_diff_hours > 0 else 0.0
        else:
            acceleration = 0.0
        
        velocity_1h = self._calc_velocity(ticker, 1)
        velocity_4h = self._calc_velocity(ticker, 4)
        velocity_24h = self._calc_velocity(ticker, 24)
        
        sigma_1h = self._calc_sigma(ticker, 1)
        sigma_4h = self._calc_sigma(ticker, 4)
        sigma_24h = self._calc_sigma(ticker, 24)
        
        provenance = self.get_provenance_history(ticker, window_hours, 20)
        
        return {
            "mention_velocity": mention_velocity,
            "comment_volume_sigma": comment_volume_sigma,
            "acceleration": acceleration,
            "velocity_1h": velocity_1h,
            "velocity_4h": velocity_4h,
            "velocity_24h": velocity_24h,
            "sigma_1h": sigma_1h,
            "sigma_4h": sigma_4h,
            "sigma_24h": sigma_24h,
            "cross_source_provenance": provenance
        }
        
    def _calc_velocity(self, ticker: str, hours: int) -> float:
        snapshots = self.get_velocity_history(ticker, hours)
        if not snapshots:
            return 0.0
        return snapshots[0]["mention_count"] / hours
    
    def _calc_sigma(self, ticker: str, hours: int) -> float:
        snapshots = self.get_velocity_history(ticker, hours)
        if len(snapshots) < 2:
            return 0.0
        volumes = [s["comment_volume"] for s in snapshots]
        mean_v = mean(volumes)
        std_v = stdev(volumes) if len(volumes) > 1 else 1.0
        return (volumes[0] - mean_v) / std_v if std_v > 0 else 0.0
        
    def calculate_sigma(self, ticker: str, window_hours: int = 24) -> float:
        metrics = self.calculate_velocity_metrics(ticker, window_hours)
        return metrics.get("comment_volume_sigma", 0.0)
        
    def calculate_acceleration(self, ticker: str, window_hours: int = 24) -> float:
        metrics = self.calculate_velocity_metrics(ticker, window_hours)
        return metrics.get("acceleration", 0.0)


def create_velocity_tracker(db_path: str = "reddit_quant.db", config_dict: dict = None) -> VelocityTracker:
    return VelocityTracker(db_path, config_dict)


if __name__ == "__main__":
    tracker = create_velocity_tracker()
    print("Velocity tracker initialized")