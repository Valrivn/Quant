import sqlite3
import logging
from typing import List, Dict, Optional
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
from psychological.interfaces import PsychologicalFeatureVector

logger = logging.getLogger(__name__)


class BehavioralFeatureStore:
    def __init__(self, db_path: str = "reddit_quant.db"):
        self.db_path = db_path
        
    def commit_vector(self, vector: PsychologicalFeatureVector) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO psychological_vectors 
                (ticker, timestamp, source_provenance, raw_text, compound_vader, 
                 bull_bear_ratio, bullish_count, bearish_count, mention_velocity, 
                 comment_volume_sigma, acceleration, employee_sentiment_proxy, 
                 dev_fork_acceleration, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                vector["ticker"],
                vector["timestamp"],
                vector["source_provenance"],
                vector.get("raw_text"),
                vector.get("compound_vader"),
                vector.get("bull_bear_ratio"),
                vector.get("bullish_count", 0),
                vector.get("bearish_count", 0),
                vector.get("mention_velocity"),
                vector.get("comment_volume_sigma"),
                vector.get("acceleration"),
                vector.get("employee_sentiment_proxy"),
                vector.get("dev_fork_acceleration"),
                vector.get("metadata_json")
            ))
            conn.commit()
            return cursor.lastrowid
            
    def commit_regime(self, ticker: str, date: str, regime: str, 
                      contrarian_buy: bool, confidence: float,
                      bull_bear_ratio: float, velocity_sigma: float,
                      employee_sentiment_proxy: float = None,
                      dev_velocity: float = None,
                      fintech_confirmation_score: float = None,
                      quantitative_value_signal: float = None) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO psychological_regimes
                (ticker, date, active_regime, contrarian_buy_authorized, confidence_score,
                 bull_bear_ratio, velocity_sigma, employee_sentiment_proxy, dev_velocity,
                 fintech_confirmation_score, quantitative_value_signal)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ticker,
                date,
                regime,
                contrarian_buy,
                confidence,
                bull_bear_ratio,
                velocity_sigma,
                employee_sentiment_proxy,
                dev_velocity,
                fintech_confirmation_score,
                quantitative_value_signal
            ))
            conn.commit()
            
    def get_velocity_history(self, ticker: str, window_hours: int = 24, limit: int = 100) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cutoff = int((datetime.now(timezone.utc).timestamp() - window_hours * 3600))
            cursor.execute("""
                SELECT * FROM velocity_snapshots
                WHERE ticker = ? AND window_start >= ?
                ORDER BY window_start DESC
                LIMIT ?
            """, (ticker, cutoff, limit))
            return [dict(row) for row in cursor.fetchall()]
            
    def get_psychological_vectors(self, ticker: str, start_date: str = None, 
                                   end_date: str = None) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            query = "SELECT * FROM psychological_vectors WHERE ticker = ?"
            params = [ticker]
            
            if start_date:
                start_ts = int(datetime.fromisoformat(start_date).timestamp())
                query += " AND timestamp >= ?"
                params.append(start_ts)
                
            if end_date:
                end_ts = int(datetime.fromisoformat(end_date).timestamp())
                query += " AND timestamp <= ?"
                params.append(end_ts)
                
            query += " ORDER BY timestamp DESC"
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
            
    def get_regimes(self, ticker: str, start_date: str = None, end_date: str = None) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            query = "SELECT * FROM psychological_regimes WHERE ticker = ?"
            params = [ticker]
            
            if start_date:
                query += " AND date >= ?"
                params.append(start_date)
                
            if end_date:
                query += " AND date <= ?"
                params.append(end_date)
                
            query += " ORDER BY date DESC"
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
            
    def export_parquet(self, start_date: str, end_date: str, output_dir: str = "data") -> str:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        
        with sqlite3.connect(self.db_path) as conn:
            query = """
                SELECT * FROM psychological_vectors
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY timestamp
            """
            start_ts = int(datetime.fromisoformat(start_date).timestamp())
            end_ts = int(datetime.fromisoformat(end_date).timestamp())
            
            df = pd.read_sql_query(query, conn, params=(start_ts, end_ts))
            
        output_file = Path(output_dir) / f"psychological_vectors_{start_date}_to_{end_date}.parquet"
        df.to_parquet(output_file, index=False)
        logger.info(f"Exported {len(df)} rows to {output_file}")
        return str(output_file)
        
    def export_regimes_parquet(self, start_date: str, end_date: str, output_dir: str = "data") -> str:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        
        with sqlite3.connect(self.db_path) as conn:
            query = """
                SELECT * FROM psychological_regimes
                WHERE date >= ? AND date <= ?
                ORDER BY date
            """
            df = pd.read_sql_query(query, conn, params=(start_date, end_date))
            
        output_file = Path(output_dir) / f"psychological_regimes_{start_date}_to_{end_date}.parquet"
        df.to_parquet(output_file, index=False)
        logger.info(f"Exported {len(df)} regime rows to {output_file}")
        return str(output_file)


def create_behavioral_feature_store(db_path: str = "reddit_quant.db") -> BehavioralFeatureStore:
    return BehavioralFeatureStore(db_path)


if __name__ == "__main__":
    store = create_behavioral_feature_store()
    print("Behavioral feature store initialized")