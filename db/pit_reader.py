"""Clock-stepped PIT reader — the only sanctioned read path over sandbox tables.

Creates TEMP VIEWs (`v_<table>`) filtered to `available_as_of <= :as_of`.
Every query — including JOINs — must go through the views, so temporal
filtering is enforced centrally at the access layer (sim-guardian's fix for
Position B's static-clock hole) while storage stays auditable per-row
(Position A's schema). The frozen-hash replay test in tests/test_pit_sandbox.py
proves the mechanism empirically: mutating post-date rows must not change any
as-of output.
"""
import re

from db.schema_pit import SANDBOX_TABLES


_VIEW_PATTERN = re.compile(r"\bv_(\w+)\b")
_AS_OF_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}:\d{2})?$")


def open_pit_views(conn, as_of: str) -> list[str]:
    """(Re)create filtered temp views for every sandbox table. Returns names."""
    if not _AS_OF_PATTERN.match(as_of or ""):
        raise ValueError(f"as_of must be ISO date/datetime, got {as_of!r}")
    literal = as_of.replace("T", " ")
    names = []
    for table in SANDBOX_TABLES:
        view = f"v_{table}"
        conn.execute(f"DROP VIEW IF EXISTS {view}")
        conn.execute(
            f"CREATE TEMP VIEW {view} AS "
            f"SELECT * FROM {table} WHERE available_as_of <= '{literal}'"
        )
        names.append(view)
    return names


def pit_query(conn, as_of: str, sql: str, params: tuple = ()) -> list[tuple]:
    """Run a read query as of `as_of`. Rejects raw-table references."""
    stripped = _VIEW_PATTERN.sub("", sql)
    for table in SANDBOX_TABLES:
        if re.search(rf"\b{table}\b", stripped):
            raise PermissionError(
                f"raw table '{table}' is not readable; use v_{table} via open_pit_views"
            )
    if re.search(r"\b(insert|update|delete|drop|alter|create)\b", sql, re.I):
        raise PermissionError("pit_query is read-only")
    open_pit_views(conn, as_of)
    return conn.execute(sql, params).fetchall()
