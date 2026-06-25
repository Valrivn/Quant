import sqlite3
import threading
from contextlib import contextmanager
from typing import Generator

DB_PATH = "reddit_quant.db"
_local = threading.local()

def get_connection() -> sqlite3.Connection:
    """Get thread-local database connection with WAL mode."""
    if not hasattr(_local, "conn") or _local.conn is None or getattr(_local.conn, 'closed', True):
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.execute("PRAGMA journal_mode=WAL;")
        _local.conn.execute("PRAGMA busy_timeout=5000;")
        _local.conn.row_factory = sqlite3.Row
    return _local.conn

@contextmanager
def connection_context() -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database connections."""
    conn = get_connection()
    try:
        yield conn
        if not getattr(conn, 'closed', False):
            conn.commit()
    except Exception:
        if not getattr(conn, 'closed', False):
            conn.rollback()
        raise
    finally:
        if getattr(conn, 'closed', False):
            _local.conn = None

def close_connection() -> None:
    """Close thread-local connection."""
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None

def init_db() -> None:
    """Initialize database with all tables and indexes."""
    from .schema import create_tables, create_indexes, migrate_psychological_schema, migrate_existing_schema
    with connection_context() as conn:
        create_tables(conn)
        migrate_existing_schema(conn)
        create_indexes(conn)
        migrate_psychological_schema(conn)

# Alias for backward compatibility
get_db_connection = get_connection