import sqlite3
from datetime import datetime, timezone


def create_fintech_tables(conn: sqlite3.Connection) -> None:
    """Create tables for fintech API data and hybrid orchestration."""
    cursor = conn.cursor()

    # Fintech raw messages (unified schema)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fintech_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            text TEXT,
            sentiment_score REAL,
            author TEXT,
            created_utc INTEGER NOT NULL,
            scraped_at INTEGER NOT NULL,
            engagement_likes INTEGER DEFAULT 0,
            engagement_comments INTEGER DEFAULT 0,
            engagement_shares INTEGER DEFAULT 0,
            url TEXT,
            metadata_json TEXT,
            UNIQUE(source, source_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fintech_ticker_date ON fintech_messages(ticker, created_utc)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fintech_source_date ON fintech_messages(source, created_utc)")

    # API health monitoring
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_health_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            checked_at INTEGER NOT NULL,
            is_healthy BOOLEAN NOT NULL,
            latency_ms INTEGER,
            rate_limit_remaining INTEGER,
            rate_limit_reset INTEGER,
            error_message TEXT,
            consecutive_failures INTEGER DEFAULT 0
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_health_source_time ON api_health_log(source, checked_at)")

    # Circuit breaker state persistence
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS circuit_breaker_state (
            source TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            failure_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            last_failure_at INTEGER,
            last_state_change_at INTEGER,
            updated_at INTEGER
        )
    """)

    # Source provenance for fused signals
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS signal_provenance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            source TEXT NOT NULL,
            source_weight REAL NOT NULL,
            message_count INTEGER NOT NULL,
            weighted_sentiment REAL NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(ticker, date, source)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_provenance_ticker_date ON signal_provenance(ticker, date)")

    # Hybrid scrape runs log
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hybrid_scrape_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at INTEGER NOT NULL,
            completed_at INTEGER,
            fintech_sources_attempted TEXT,
            fintech_sources_succeeded TEXT,
            reddit_fallback_triggered BOOLEAN DEFAULT 0,
            total_messages_fintech INTEGER DEFAULT 0,
            total_messages_reddit INTEGER DEFAULT 0,
            total_tickers INTEGER DEFAULT 0,
            status TEXT,
            error_message TEXT
        )
    """)

    conn.commit()


def migrate_existing_schema(conn: sqlite3.Connection) -> None:
    """Add columns to existing tables for hybrid support."""
    cursor = conn.cursor()

    # Add source column to daily_aggregations if not exists
    cursor.execute("PRAGMA table_info(daily_aggregations)")
    columns = [row[1] for row in cursor.fetchall()]
    if "source" not in columns:
        cursor.execute("ALTER TABLE daily_aggregations ADD COLUMN source TEXT DEFAULT 'reddit'")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_agg_source ON daily_aggregations(source)")

    # Add source to composite_scores
    cursor.execute("PRAGMA table_info(composite_scores)")
    columns = [row[1] for row in cursor.fetchall()]
    if "source_breakdown" not in columns:
        cursor.execute("ALTER TABLE composite_scores ADD COLUMN source_breakdown TEXT")

    conn.commit()