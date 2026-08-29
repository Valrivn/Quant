import sqlite3
import threading
from contextlib import contextmanager
from typing import Generator

DB_PATH = "reddit_quant.db"
_local = threading.local()

def _open_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn

def get_connection() -> sqlite3.Connection:
    """Get thread-local database connection with WAL mode.

    Reuses the thread-local connection while it is open. ``sqlite3.Connection``
    has no reliable ``closed`` attribute across Python versions, so liveness is
    probed with a trivial query instead of ``getattr(conn, 'closed', ...)``
    (which leaked a fresh connection on every call on Python 3.13).
    """
    conn = getattr(_local, "conn", None)
    if conn is not None and _is_closed(conn):
        conn = None
    if conn is None:
        conn = _open_conn()
        _local.conn = conn
    return conn

def _is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1")
        return False
    except sqlite3.ProgrammingError:
        return True

@contextmanager
def connection_context() -> Generator[sqlite3.Connection, None, None]:
    """Context manager for database connections."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        if not _is_closed(conn):
            conn.rollback()
        raise
    finally:
        if _is_closed(conn):
            _local.conn = None

def close_connection() -> None:
    """Close thread-local connection."""
    if hasattr(_local, "conn") and _local.conn is not None:
        _local.conn.close()
        _local.conn = None

def init_db() -> None:
    """Initialize database with all tables and indexes."""
    from .schema import create_tables, create_indexes, migrate_psychological_schema, migrate_existing_schema, create_lane_gamma_tables
    from .schema_discovery import create_discovery_tables
    from .schema_consensus import create_consensus_tables
    with connection_context() as conn:
        create_tables(conn)
        migrate_existing_schema(conn)
        create_indexes(conn)
        migrate_psychological_schema(conn)
        create_lane_gamma_tables(conn)
        create_discovery_tables(conn)
        create_consensus_tables(conn)

# Alias for backward compatibility
get_db_connection = get_connection