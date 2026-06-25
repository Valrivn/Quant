import pytest
import sqlite3
import os
import yaml

@pytest.fixture
def mock_db_conn(tmp_path):
    """Provides a temporary SQLite database connection with full test schema."""
    db_file = tmp_path / "test_reddit_quant.db"
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    
    # Create tables
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id TEXT PRIMARY KEY,
            subreddit TEXT,
            category TEXT,
            title TEXT,
            selftext TEXT,
            score INTEGER,
            upvote_ratio REAL,
            num_comments INTEGER,
            url TEXT,
            created_utc INTEGER,
            scraped_at INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_aggregations (
            ticker TEXT,
            date TEXT,
            category TEXT,
            subreddit TEXT,
            mention_count INTEGER,
            avg_sentiment REAL,
            weighted_sum REAL,
            total_weight REAL,
            PRIMARY KEY (ticker, date, category, subreddit)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS risk_signals (
            ticker TEXT,
            date TEXT,
            risk_type TEXT,
            category TEXT,
            frequency INTEGER,
            PRIMARY KEY (ticker, date, risk_type, category)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS composite_scores (
            ticker TEXT,
            date TEXT,
            composite_sentiment REAL,
            PRIMARY KEY (ticker, date)
        )
    """)
    
    conn.commit()
    yield conn
    conn.close()
