import sqlite3
from datetime import datetime, timezone

def get_or_create_partition(conn: sqlite3.Connection, created_utc: int) -> str:
    dt = datetime.fromtimestamp(created_utc, tz=timezone.utc)
    partition_name = f"submissions_{dt.year}_{dt.month:02d}"
    
    cursor = conn.cursor()
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {partition_name} (
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
    cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{partition_name}_subreddit_created ON {partition_name}(subreddit, created_utc)")
    conn.commit()
    
    return partition_name

def recreate_submissions_view(cursor: sqlite3.Cursor) -> None:
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'submissions_%'")
    partitions = [row[0] for row in cursor.fetchall()]
    
    if partitions:
        union_sql = " UNION ALL ".join([f"SELECT * FROM {p}" for p in sorted(partitions)])
        cursor.execute(f"DROP VIEW IF EXISTS submissions")
        cursor.execute(f"CREATE VIEW submissions AS {union_sql}")

def create_tables(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    
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
        CREATE TABLE IF NOT EXISTS scrape_state (
            subreddit TEXT,
            sort TEXT,
            last_cursor TEXT,
            last_run INTEGER,
            PRIMARY KEY (subreddit, sort)
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
            source TEXT DEFAULT 'reddit',
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
            source_breakdown TEXT,
            PRIMARY KEY (ticker, date)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sentiment_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            model_version TEXT,
            lexicon_hash TEXT,
            nltk_version TEXT,
            analyzer_config TEXT,
            created_at INTEGER
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weight_versions (
            version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_yaml TEXT,
            category_weights TEXT,
            subreddit_weights TEXT,
            ic_score REAL,
            sharpe_ratio REAL,
            hit_rate REAL,
            lookback_days INTEGER,
            optimization_method TEXT,
            promoted_at INTEGER,
            is_active BOOLEAN DEFAULT 0,
            created_at INTEGER
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            ticker TEXT PRIMARY KEY,
            added_at INTEGER NOT NULL,
            active BOOLEAN DEFAULT 1,
            notes TEXT
        )
    """)
    
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

def create_indexes(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_agg_ticker_date ON daily_aggregations(ticker, date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_agg_source ON daily_aggregations(source)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_risk_signals_ticker_date ON risk_signals(ticker, date, category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_submissions_subreddit_created ON submissions(subreddit, created_utc)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_submissions_date ON submissions(date(scraped_at/1000))")
    conn.commit()

def create_psychological_tables(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS psychological_vectors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            source_provenance TEXT NOT NULL,
            raw_text TEXT,
            compound_vader REAL,
            bull_bear_ratio REAL,
            bullish_count INTEGER,
            bearish_count INTEGER,
            mention_velocity REAL,
            comment_volume_sigma REAL,
            acceleration REAL,
            employee_sentiment_proxy REAL,
            dev_fork_acceleration REAL,
            metadata_json TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_psych_ticker_time ON psychological_vectors(ticker, timestamp)")
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS psychological_regimes (
            ticker TEXT,
            date TEXT,
            active_regime TEXT,
            contrarian_buy_authorized BOOLEAN,
            confidence_score REAL,
            bull_bear_ratio REAL,
            velocity_sigma REAL,
            employee_sentiment_proxy REAL,
            dev_velocity REAL,
            fintech_confirmation_score REAL,
            quantitative_value_signal REAL,
            PRIMARY KEY (ticker, date)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS velocity_snapshots (
            ticker TEXT,
            window_start INTEGER,
            window_end INTEGER,
            window_type TEXT,
            mention_count INTEGER,
            comment_volume INTEGER,
            unique_authors INTEGER,
            PRIMARY KEY (ticker, window_start, window_type)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS adzuna_job_snapshots (
            ticker TEXT,
            company_name TEXT,
            date TEXT,
            job_count INTEGER,
            job_count_7d_ago INTEGER,
            job_count_30d_ago INTEGER,
            delta_7d_pct REAL,
            delta_30d_pct REAL,
            PRIMARY KEY (ticker, date)
        )
    """)
    
    conn.commit()


def create_phase1_tables(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS validation_gate_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            normalized_glassdoor REAL,
            normalized_comparably REAL,
            divergence REAL,
            penalty_multiplier REAL,
            override_triggered BOOLEAN DEFAULT 0,
            confidence_floor REAL DEFAULT 0.40,
            kappa REAL DEFAULT 5.0,
            created_at INTEGER NOT NULL,
            UNIQUE(ticker, date)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_validation_gate_ticker_date ON validation_gate_results(ticker, date)")
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS glassdoor_comparably_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            glassdoor_slug TEXT,
            glassdoor_raw_score REAL,
            glassdoor_normalized REAL,
            comparably_badge_score REAL,
            comparably_normalized REAL,
            divergence REAL,
            penalty_multiplier REAL,
            override_triggered BOOLEAN DEFAULT 0,
            created_at INTEGER NOT NULL,
            UNIQUE(ticker, date)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_glassdoor_audit_ticker_date ON glassdoor_comparably_audit(ticker, date)")
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobspy_velocity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            source TEXT NOT NULL,
            job_count INTEGER,
            job_count_8_runs_ago INTEGER,
            delta_30d REAL,
            mean_252_runs REAL,
            std_252_runs REAL,
            zscore_1y REAL,
            ghost_job_flag BOOLEAN DEFAULT 0,
            operational_fracture_flag BOOLEAN DEFAULT 0,
            created_at INTEGER NOT NULL,
            UNIQUE(ticker, date, source)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_jobspy_velocity_ticker_date ON jobspy_velocity(ticker, date)")
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS g2_capterra_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            platform TEXT NOT NULL,
            product_name TEXT,
            rating REAL,
            review_text TEXT,
            review_date TEXT,
            keywords_detected TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_g2_capterra_ticker_date ON g2_capterra_reviews(ticker, date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_g2_capterra_platform ON g2_capterra_reviews(platform)")
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_store_feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            platform TEXT NOT NULL,
            app_id TEXT,
            app_name TEXT,
            rating REAL,
            review_text TEXT,
            review_date TEXT,
            vader_compound REAL,
            created_at INTEGER NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_app_store_ticker_date ON app_store_feeds(ticker, date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_app_store_platform ON app_store_feeds(platform)")
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quantitative_dcf_floor (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            intrinsic_floor REAL,
            intrinsic_ceiling REAL,
            margin_of_safety REAL,
            current_price REAL,
            wacc REAL,
            fcf_projection REAL,
            terminal_value REAL,
            model_version TEXT DEFAULT 'stub_v1',
            created_at INTEGER NOT NULL,
            UNIQUE(ticker, date)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_dcf_floor_ticker_date ON quantitative_dcf_floor(ticker, date)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hiring_velocity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            job_count INTEGER,
            delta_30d REAL,
            zscore_1y REAL,
            created_at INTEGER NOT NULL,
            UNIQUE(ticker, date)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_hiring_velocity_ticker_date ON hiring_velocity_snapshots(ticker, date)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS product_intel_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            platform TEXT NOT NULL,
            rating REAL,
            review_text TEXT,
            vader_compound REAL,
            created_at INTEGER NOT NULL
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_product_intel_ticker_date ON product_intel_reviews(ticker, date)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS glassdoor_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            rating REAL,
            created_at INTEGER NOT NULL,
            UNIQUE(ticker, date)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_glassdoor_snap_ticker_date ON glassdoor_snapshots(ticker, date)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS comparably_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            rating REAL,
            created_at INTEGER NOT NULL,
            UNIQUE(ticker, date)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_comparably_snap_ticker_date ON comparably_snapshots(ticker, date)")
    
    conn.commit()


def migrate_existing_schema(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    
    cursor.execute("PRAGMA table_info(daily_aggregations)")
    columns = [row[1] for row in cursor.fetchall()]
    if "source" not in columns:
        cursor.execute("ALTER TABLE daily_aggregations ADD COLUMN source TEXT DEFAULT 'reddit'")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_daily_agg_source ON daily_aggregations(source)")
    
    cursor.execute("PRAGMA table_info(composite_scores)")
    columns = [row[1] for row in cursor.fetchall()]
    if "source_breakdown" not in columns:
        cursor.execute("ALTER TABLE composite_scores ADD COLUMN source_breakdown TEXT")
    
    conn.commit()


def migrate_psychological_schema(conn: sqlite3.Connection) -> None:
    create_psychological_tables(conn)
    create_phase1_tables(conn)