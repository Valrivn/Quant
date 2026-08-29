"""SEC EDGAR ticker -> CIK resolver with a WAL DB cache (B-20260803 P1).

Source: SEC EDGAR ``company_tickers.json`` (public, no auth). The map is
persisted in the ``sec_cik_map`` table so lookups are offline after the first
refresh. SEC rate limit is 10 req/s; this module issues one bulk GET, then
resolves locally.
"""

import time

import requests

from db.connection import get_connection

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_USER_AGENT = "Quant-research backtest/1.0 (data integrity work; contact hayden@quant.local)"
_MAX_AGE_SECONDS = 7 * 24 * 3600  # refresh weekly


def _table_exists() -> bool:
    cur = get_connection().execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sec_cik_map'"
    )
    return cur.fetchone() is not None


def _ensure_table() -> None:
    if _table_exists():
        return
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sec_cik_map (
            ticker TEXT PRIMARY KEY,
            cik TEXT NOT NULL,
            title TEXT,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.commit()


def fetch_cik_map(user_agent: str = _USER_AGENT, max_attempts: int = 3) -> dict:
    """Fetch the full SEC ticker->CIK map.

    Returns {ticker: {"cik": 10-digit CIK, "title": company name}}. Empty dict
    only after all attempts fail. Retries up to ``max_attempts`` times with
    exponential backoff (``time.sleep(2 ** attempt)`` between attempts) to
    tolerate SEC rate limiting / intermittent unreachability.
    """
    for attempt in range(max_attempts):
        try:
            resp = requests.get(
                _TICKERS_URL, headers={"User-Agent": user_agent}, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception:
            if attempt == max_attempts - 1:
                return {}
            time.sleep(2 ** attempt)
    out = {}
    for row in data.values():
        ticker = str(row.get("ticker", "")).upper()
        cik = str(row.get("cik_str", "")).zfill(10)
        if ticker and cik != "0000000000":
            out[ticker] = {"cik": cik, "title": row.get("title", "")}
    return out


def refresh_cik_map(user_agent: str = _USER_AGENT) -> dict:
    """Fetch from SEC and upsert the DB cache. Returns the fetched map."""
    mapping = fetch_cik_map(user_agent)
    if not mapping:
        return {}
    _ensure_table()
    now = int(time.time())
    conn = get_connection()
    conn.executemany(
        "INSERT OR REPLACE INTO sec_cik_map (ticker, cik, title, updated_at)"
        " VALUES (?, ?, ?, ?)",
        [(t, v["cik"], v["title"], now) for t, v in mapping.items()],
    )
    conn.commit()
    return mapping


def _is_fresh(max_age_seconds: int = _MAX_AGE_SECONDS) -> bool:
    if not _table_exists():
        return False
    cur = get_connection().execute("SELECT COUNT(*) AS n FROM sec_cik_map")
    n = cur.fetchone()["n"]
    if n < 5000:
        return False
    cur = get_connection().execute(
        "SELECT MAX(updated_at) AS m FROM sec_cik_map"
    )
    latest = cur.fetchone()["m"]
    return bool(latest) and (int(time.time()) - latest) < max_age_seconds


def get_cik_map(force_refresh: bool = False, user_agent: str = _USER_AGENT) -> dict:
    """Return {ticker: {"cik", "title"}} from the DB cache, refreshing when
    stale or empty. Never raises on network failure (returns the cache)."""
    if force_refresh:
        fetched = refresh_cik_map(user_agent)
        if fetched:
            return fetched
    if _is_fresh():
        cur = get_connection().execute("SELECT ticker, cik, title FROM sec_cik_map")
        return {r["ticker"]: {"cik": r["cik"], "title": r["title"]} for r in cur.fetchall()}
    fetched = refresh_cik_map(user_agent)
    if fetched:
        return fetched
    if _table_exists():
        cur = get_connection().execute("SELECT ticker, cik, title FROM sec_cik_map")
        return {r["ticker"]: {"cik": r["cik"], "title": r["title"]} for r in cur.fetchall()}
    return {}


def resolve_cik(ticker: str, force_refresh: bool = False) -> str | None:
    """Resolve one ticker to its 10-digit SEC CIK, or None if unknown."""
    ticker = str(ticker).upper().strip()
    if not ticker:
        return None
    mapping = get_cik_map(force_refresh=force_refresh)
    hit = mapping.get(ticker)
    return hit["cik"] if hit else None


def resolve_ciks(tickers: list, force_refresh: bool = False) -> dict:
    """Resolve many tickers at once: {ticker: cik or None}."""
    mapping = get_cik_map(force_refresh=force_refresh)
    out = {}
    for t in tickers:
        t = str(t).upper().strip()
        hit = mapping.get(t)
        out[t] = hit["cik"] if hit else None
    return out


def enrich_universe(rows: list) -> list:
    """Fill in ``sec_cik`` for roster rows lacking one, resolving from the map.

    Mutates a copy of the input rows and returns it. Rows already carrying a
    truthy ``sec_cik`` are left untouched.
    """
    missing = [r for r in rows if not r.get("sec_cik")]
    if not missing:
        return list(rows)
    resolved = resolve_ciks([r["ticker"] for r in missing])
    out = [dict(r) for r in rows]
    for r in out:
        if not r.get("sec_cik"):
            r["sec_cik"] = resolved.get(r["ticker"])
    return out
