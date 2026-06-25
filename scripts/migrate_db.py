#!/usr/bin/env python3
"""
Database migration script for schema updates.

This script handles versioned database migrations for the quant pipeline.
Each migration is a function that transforms the database from version N to N+1.

Usage:
    python scripts/migrate_db.py              # Run all pending migrations
    python scripts/migrate_db.py --version 3  # Migrate to specific version
    python scripts/migrate_db.py --status     # Show current migration status
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.connection import get_db_connection, init_db

# Migration functions - each takes a connection and applies changes
MIGRATIONS = []


def migration_001_initial_schema(conn: sqlite3.Connection) -> None:
    """Initial schema creation - handled by init_db()."""
    from db.schema import create_tables, create_indexes
    create_tables(conn)
    create_indexes(conn)
    print("Applied migration 001: Initial schema")


def migration_002_add_model_versioning(conn: sqlite3.Connection) -> None:
    """Add model_version column to daily_aggregations for reproducibility."""
    cursor = conn.cursor()
    
    # Check if column exists
    cursor.execute("PRAGMA table_info(daily_aggregations)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if 'model_version' not in columns:
        cursor.execute("ALTER TABLE daily_aggregations ADD COLUMN model_version TEXT DEFAULT 'unknown'")
        print("Applied migration 002: Added model_version column")
    else:
        print("Migration 002 already applied")


def migration_003_add_risk_signal_details(conn: sqlite3.Connection) -> None:
    """Add z_score and severity columns to risk_signals."""
    cursor = conn.cursor()
    
    cursor.execute("PRAGMA table_info(risk_signals)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if 'z_score' not in columns:
        cursor.execute("ALTER TABLE risk_signals ADD COLUMN z_score REAL DEFAULT 0.0")
    if 'severity' not in columns:
        cursor.execute("ALTER TABLE risk_signals ADD COLUMN severity TEXT DEFAULT 'low'")
    print("Applied migration 003: Added risk signal details")


def migration_004_add_regime_tracking(conn: sqlite3.Connection) -> None:
    """Add regime tracking table for Markov chain states."""
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS regime_states (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            regime INTEGER NOT NULL,  -- 0:Apathy, 1:Grassroots, 2:Euphoria, 3:Panic
            confidence REAL NOT NULL,
            transition_from INTEGER,
            transition_prob REAL,
            created_at INTEGER NOT NULL,
            UNIQUE(date)
        )
    """)
    
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_regime_date ON regime_states(date)")
    print("Applied migration 004: Added regime tracking table")


def migration_005_add_bayes_tracking(conn: sqlite3.Connection) -> None:
    """Add Bayesian posterior tracking for micro-caps."""
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bayes_posteriors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            event_type TEXT NOT NULL,  -- 'form4', 'patent', 'github_fork'
            event_date TEXT NOT NULL,
            prior_prob REAL NOT NULL,
            likelihood REAL NOT NULL,
            posterior_prob REAL NOT NULL,
            confidence_interval_low REAL,
            confidence_interval_high REAL,
            confidence_pct REAL,
            manual_override BOOLEAN DEFAULT 0,
            created_at INTEGER NOT NULL,
            UNIQUE(ticker, event_type, event_date)
        )
    """)
    
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bayes_ticker_date ON bayes_posteriors(ticker, event_date)")
    print("Applied migration 005: Added Bayesian posterior tracking")


# Register migrations in order
MIGRATIONS = [
    (1, "initial_schema", migration_001_initial_schema),
    (2, "model_versioning", migration_002_add_model_versioning),
    (3, "risk_signal_details", migration_003_add_risk_signal_details),
    (4, "regime_tracking", migration_004_add_regime_tracking),
    (5, "bayes_tracking", migration_005_add_bayes_tracking),
]


def ensure_migration_table(conn: sqlite3.Connection) -> None:
    """Ensure the migrations tracking table exists."""
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at INTEGER NOT NULL
        )
    """)


def get_current_version(conn: sqlite3.Connection) -> int:
    """Get the current migration version."""
    ensure_migration_table(conn)
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(version) FROM schema_migrations")
    row = cursor.fetchone()
    return row[0] if row[0] is not None else 0


def get_pending_migrations(conn: sqlite3.Connection, target_version: int = None) -> list:
    """Get list of pending migrations."""
    current = get_current_version(conn)
    max_version = max(m[0] for m in MIGRATIONS)
    
    if target_version is None:
        target_version = max_version
    
    pending = []
    for version, name, func in MIGRATIONS:
        if current < version <= target_version:
            pending.append((version, name, func))
    
    return pending


def run_migration(conn: sqlite3.Connection, version: int, name: str, func) -> bool:
    """Run a single migration."""
    try:
        print(f"Running migration {version}: {name}...")
        func(conn)
        conn.commit()
        
        # Record migration
        cursor = conn.cursor()
        import time
        cursor.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (version, name, int(time.time()))
        )
        conn.commit()
        print(f"  ✓ Migration {version} completed")
        return True
    except Exception as e:
        conn.rollback()
        print(f"  ✗ Migration {version} failed: {e}")
        return False


def show_status(conn: sqlite3.Connection) -> None:
    """Show migration status."""
    ensure_migration_table(conn)
    cursor = conn.cursor()
    cursor.execute("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
    applied = cursor.fetchall()
    
    print("\n=== Migration Status ===")
    print(f"Current version: {get_current_version(conn)}")
    print(f"Latest available: {max(m[0] for m in MIGRATIONS)}")
    print("\nApplied migrations:")
    for v, name, ts in applied:
        from datetime import datetime
        dt = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
        print(f"  v{v:03d} - {name} (applied: {dt})")
    
    pending = get_pending_migrations(conn)
    if pending:
        print("\nPending migrations:")
        for v, name, _ in pending:
            print(f"  v{v:03d} - {name}")
    else:
        print("\n✓ All migrations applied")


def main():
    parser = argparse.ArgumentParser(description="Database migration tool")
    parser.add_argument("--version", type=int, help="Migrate to specific version")
    parser.add_argument("--status", action="store_true", help="Show migration status")
    parser.add_argument("--reset", action="store_true", help="Reset migration tracking (DANGEROUS)")
    args = parser.parse_args()
    
    # Initialize DB first
    init_db()
    conn = get_db_connection()
    
    try:
        if args.reset:
            confirm = input("This will delete migration history. Continue? (yes/no): ")
            if confirm.lower() == "yes":
                cursor = conn.cursor()
                cursor.execute("DELETE FROM schema_migrations")
                conn.commit()
                print("Migration history reset")
            return 0
        
        if args.status:
            show_status(conn)
            return 0
        
        pending = get_pending_migrations(conn, args.version)
        
        if not pending:
            print("No pending migrations")
            return 0
        
        print(f"Found {len(pending)} pending migration(s)")
        
        for version, name, func in pending:
            success = run_migration(conn, version, name, func)
            if not success:
                print(f"Migration failed at version {version}. Stopping.")
                return 1
        
        print(f"\n✓ All migrations completed. Current version: {get_current_version(conn)}")
        return 0
        
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())