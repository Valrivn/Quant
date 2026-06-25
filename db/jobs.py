import json
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from .connection import connection_context

logger = logging.getLogger(__name__)

def purge_old_submissions(retention_days: int = 30) -> int:
    """Delete raw submissions older than retention_days. Returns count deleted."""
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=retention_days)).timestamp())
    with connection_context() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM submissions WHERE scraped_at < ?", (cutoff,))
        deleted = cursor.rowcount
        logger.info(f"Purged {deleted} submissions older than {retention_days} days")
        return deleted

def run_daily_aggregation(date_str: Optional[str] = None) -> None:
    """Compute composite scores for a specific date."""
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    from config import CATEGORY_WEIGHTS, SUBREDDIT_TAXONOMY
    
    logger.debug(f"Running daily aggregation for {date_str}")
    with connection_context() as conn:
        cursor = conn.cursor()
        
        cursor.execute("SELECT DISTINCT ticker FROM daily_aggregations WHERE date=?", (date_str,))
        tickers = [row[0] for row in cursor.fetchall()]
        
        for ticker in tickers:
            cursor.execute("""
                SELECT category, subreddit, 
                       CASE WHEN total_weight > 0 THEN weighted_sum / total_weight ELSE 0 END as weighted_sentiment
                FROM daily_aggregations 
                WHERE ticker=? AND date=?
            """, (ticker, date_str))
            cat_data = cursor.fetchall()
            
            composite = 0.0
            total_weight = 0.0
            
            for category, subreddit, sentiment in cat_data:
                cat_w = CATEGORY_WEIGHTS.get(category, 0.0)
                sub_w = SUBREDDIT_TAXONOMY.get(category, {}).get(subreddit, 0.0)
                combined_weight = cat_w * sub_w
                composite += sentiment * combined_weight
                total_weight += combined_weight
                
            if total_weight > 0:
                composite = composite / total_weight
                cursor.execute("""
                    INSERT OR REPLACE INTO composite_scores (ticker, date, composite_sentiment)
                    VALUES (?, ?, ?)
                """, (ticker, date_str, composite))
    logger.info(f"Daily aggregation complete for {date_str}: {len(tickers)} tickers processed")

def get_active_weight_version() -> Optional[Dict[str, Any]]:
    """Get the currently active weight version."""
    with connection_context() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM weight_versions WHERE is_active = 1 ORDER BY promoted_at DESC LIMIT 1
        """)
        row = cursor.fetchone()
        return dict(row) if row else None

def record_weight_version(
    config_yaml: str,
    category_weights: Dict[str, float],
    subreddit_weights: Dict[str, Dict[str, float]],
    ic_score: float,
    sharpe_ratio: float,
    hit_rate: float,
    lookback_days: int,
    optimization_method: str
) -> int:
    """Record a new weight version. Returns version_id."""
    with connection_context() as conn:
        cursor = conn.cursor()
        now = int(time.time())
        cursor.execute("""
            INSERT INTO weight_versions 
            (config_yaml, category_weights, subreddit_weights, ic_score, sharpe_ratio, hit_rate,
             lookback_days, optimization_method, promoted_at, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (config_yaml, json.dumps(category_weights), json.dumps(subreddit_weights),
              ic_score, sharpe_ratio, hit_rate, lookback_days, optimization_method,
              None, 0, now))
        version_id = cursor.lastrowid
        logger.info(f"Recorded weight version {version_id} (IC={ic_score:.4f}, Sharpe={sharpe_ratio:.4f})")
        return version_id

def promote_weight_version(version_id: int) -> None:
    """Promote a weight version to active (deactivates current)."""
    now = int(time.time())
    with connection_context() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE weight_versions SET is_active = 0 WHERE is_active = 1")
        cursor.execute("UPDATE weight_versions SET is_active = 1, promoted_at = ? WHERE version_id = ?", 
                       (now, version_id))
    logger.info(f"Promoted weight version {version_id} to champion")

def record_sentiment_run(date_str: str, model_info: Dict[str, str]) -> int:
    """Record a sentiment analysis run with model version info."""
    with connection_context() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sentiment_runs (date, model_version, lexicon_hash, nltk_version, analyzer_config, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (date_str, model_info["model_version"], model_info["lexicon_hash"],
              model_info["nltk_version"], model_info["analyzer_config"], int(time.time())))
        return cursor.lastrowid