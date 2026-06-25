import sqlite3
import pandas as pd
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from .connection import connection_context
from config import CATEGORY_WEIGHTS, SUBREDDIT_TAXONOMY, HYBRID_SOURCE_WEIGHTS

class FeatureStore:
    """Feature store for per-category sentiment features."""
    
    @staticmethod
    def get_ticker_features(ticker: str, date_str: str) -> Dict:
        """Get all per-category features for a ticker on a date."""
        with connection_context() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT category, subreddit, mention_count, avg_sentiment,
                       CASE WHEN total_weight > 0 THEN weighted_sum / total_weight ELSE 0 END as weighted_sentiment,
                       source
                FROM daily_aggregations 
                WHERE ticker=? AND date=?
            """, (ticker, date_str))
            rows = cursor.fetchall()
            
            features = {
                "ticker": ticker,
                "date": date_str,
                "categories": {},
                "sources": {},
                "composite": 0.0
            }
            
            composite = 0.0
            total_weight = 0.0
            
            for row in rows:
                cat = row["category"]
                sub = row["subreddit"]
                source = row["source"] if "source" in row.keys() else "reddit"
                
                features["categories"].setdefault(cat, {"subreddits": {}, "weighted_sentiment": 0.0})
                features["sources"].setdefault(source, {"weighted_sentiment": 0.0, "message_count": 0})
                
                cat_w = CATEGORY_WEIGHTS.get(cat, 0.0)
                sub_w = SUBREDDIT_TAXONOMY.get(cat, {}).get(sub, 0.0)
                src_w = HYBRID_SOURCE_WEIGHTS.get(source, 0.1)
                combined_w = cat_w * sub_w * src_w
                
                features["categories"][cat]["subreddits"][sub] = {
                    "mention_count": row["mention_count"],
                    "avg_sentiment": row["avg_sentiment"],
                    "weighted_sentiment": row["weighted_sentiment"],
                    "weight": combined_w,
                    "source": source
                }
                
                features["sources"][source]["weighted_sentiment"] += row["weighted_sentiment"] * combined_w
                features["sources"][source]["message_count"] += row["mention_count"]
                
                composite += row["weighted_sentiment"] * combined_w
                total_weight += combined_w
            
            if total_weight > 0:
                features["composite"] = composite / total_weight
            
            return features

    @staticmethod
    def get_source_provenance(ticker: str, date_str: str) -> List[Dict]:
        """Get source provenance data for a ticker on a date."""
        with connection_context() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT source, source_weight, message_count, weighted_sentiment, created_at
                FROM signal_provenance
                WHERE ticker=? AND date=?
            """, (ticker, date_str))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    
    @staticmethod
    def get_ticker_history(ticker: str, days: int = 30) -> List[Dict]:
        """Get historical composite scores for a ticker."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        with connection_context() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT date, composite_sentiment FROM composite_scores
                WHERE ticker=? AND date >= ?
                ORDER BY date DESC
            """, (ticker, cutoff))
            return [dict(row) for row in cursor.fetchall()]
    
    @staticmethod
    def get_category_breakdown(ticker: str, date_str: str) -> Dict:
        """Get sentiment breakdown by category for a ticker."""
        features = FeatureStore.get_ticker_features(ticker, date_str)
        return {
            "ticker": ticker,
            "date": date_str,
            "categories": {
                cat: {
                    "weighted_sentiment": data.get("weighted_sentiment", 0.0),
                    "total_mentions": sum(s["mention_count"] for s in data["subreddits"].values()),
                    "subreddits": list(data["subreddits"].keys())
                }
                for cat, data in features["categories"].items()
            },
            "composite": features["composite"]
        }
    
    @staticmethod
    def get_risk_signals(ticker: str, date_str: str) -> Dict:
        """Get risk signals for a ticker by category."""
        with connection_context() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT risk_type, category, frequency FROM risk_signals
                WHERE ticker=? AND date=?
            """, (ticker, date_str))
            rows = cursor.fetchall()
            
            signals = {}
            for row in rows:
                signals.setdefault(row["category"], {})[row["risk_type"]] = row["frequency"]
            return signals
    
    @staticmethod
    def get_top_tickers_by_mentions(date_str: str, limit: int = 50) -> List[Dict]:
        """Get top tickers by total mentions across all categories."""
        with connection_context() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT ticker, SUM(mention_count) as total_mentions,
                       SUM(weighted_sum) / SUM(total_weight) as avg_weighted_sentiment
                FROM daily_aggregations
                WHERE date=?
                GROUP BY ticker
                ORDER BY total_mentions DESC
                LIMIT ?
            """, (date_str, limit))
            return [dict(row) for row in cursor.fetchall()]
    
    @staticmethod
    def get_weight_evolution(limit: int = 12) -> List[Dict]:
        """Get weight version history for charting."""
        with connection_context() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT version_id, category_weights, subreddit_weights, ic_score, sharpe_ratio, 
                       hit_rate, promoted_at, is_active, created_at
                FROM weight_versions
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]


def get_sentiment_features(ticker: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """
    Get pivoted sentiment features for a ticker with date index and categories as columns.
    Returns DataFrame with columns: macro_geopolitical, fundamental_institutional, tech_product, retail_options, composite
    """
    if start_date is None:
        start_date = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    if end_date is None:
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    with connection_context() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT date, category, 
                   CASE WHEN total_weight > 0 THEN weighted_sum / total_weight ELSE 0 END as weighted_sentiment,
                   total_weight
            FROM daily_aggregations 
            WHERE ticker=? AND date BETWEEN ? AND ?
            ORDER BY date
        """, (ticker, start_date, end_date))
        rows = cursor.fetchall()
    
    if not rows:
        return pd.DataFrame()
    
    df = pd.DataFrame(rows, columns=["date", "category", "weighted_sentiment", "total_weight"])
    df["date"] = pd.to_datetime(df["date"])
    
    # Pivot to get categories as columns
    pivot = df.pivot_table(index="date", columns="category", values="weighted_sentiment", aggfunc="mean").fillna(0.0)
    
    # Ensure all categories exist
    categories = ["macro_geopolitical", "fundamental_institutional", "tech_product", "retail_options"]
    for cat in categories:
        if cat not in pivot.columns:
            pivot[cat] = 0.0
    
    # Calculate composite
    pivot["composite"] = 0.0
    total_w = 0.0
    for cat in categories:
        cat_w = CATEGORY_WEIGHTS.get(cat, 0.0)
        pivot["composite"] += pivot[cat] * cat_w
        total_w += cat_w
    
    if total_w > 0:
        pivot["composite"] = pivot["composite"] / total_w
    
    return pivot[categories + ["composite"]]