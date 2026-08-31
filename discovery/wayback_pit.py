"""Point-in-time (PIT) rating harvesting from Wayback Machine archives.

Glassdoor and Capterra only expose the current aggregate rating on their live
pages, so there is no historical API to scrape. The Wayback Machine, however,
has archived their company/product pages since 2008-2009, and each archived
snapshot is a true as-of observation of what a visitor saw on the capture
date. That makes the archive a legitimate PIT dataset for backtests: a rating
recorded on 2010-03-12 is usable only for returns from that date forward.

Pipeline per ticker:
  1. CDX lookup -> one fetch target per calendar month (collapse=timestamp:6).
  2. Fetch ``web.archive.org/web/{ts}id_/{original}`` (id_ suppresses the
     Wayback toolbar so the page markup is the original server output).
  3. Parse the aggregate rating from the page (legacy divs, schema.org JSON-LD,
     or embedded JSON) and store it with full provenance.

Stored rows never mutate (INSERT OR IGNORE on UNIQUE(ticker, source,
valid_date)); re-running a harvest is a cheap incremental backfill.
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

CDX_API = "https://web.archive.org/cdx/search/cdx"
WEB_BASE = "https://web.archive.org/web/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SOURCE_GLASSDOOR = "glassdoor"

# Resolved via Wayback CDX (verified 2026-08-30). `GLASSDOOR_PATH` is the
# case-sensitive archived URL-key prefix that scopes each company's page
# family; `GLASSDOOR_EIDS` is the stable Glassdoor company identifier.
GLASSDOOR_PATH = {
    "NVDA": "glassdoor.com/Reviews/NVIDIA",
    "AVGO": "glassdoor.com/Reviews/Broadcom",
    "INTC": "glassdoor.com/Reviews/Intel-Corporation",
    "AMD": "glassdoor.com/Reviews/AMD",
    "MSFT": "glassdoor.com/Reviews/Microsoft",
    "GOOGL": "glassdoor.com/Reviews/Google",
    "META": "glassdoor.com/Reviews/Meta",
    "TSLA": "glassdoor.com/Reviews/Tesla",
    "AAPL": "glassdoor.com/Reviews/Apple",
    "AMZN": "glassdoor.com/Reviews/Amazon",
    "QCOM": "glassdoor.com/Reviews/Qualcomm",
    "MU": "glassdoor.com/Reviews/Micron-Technology",
    "TSM": "glassdoor.com/Reviews/TSMC",
    "CRM": "glassdoor.com/Reviews/Salesforce",
    "ADBE": "glassdoor.com/Reviews/Adobe",
    "DELL": "glassdoor.com/Reviews/Dell-Technologies",
    "SMCI": "glassdoor.com/Reviews/Super-Micro-Computer",
    "IBM": "glassdoor.com/Reviews/IBM",
}

GLASSDOOR_EIDS = {
    "NVDA": 7633, "AVGO": 6926, "INTC": 1519, "AMD": 15, "MSFT": 1651,
    "GOOGL": 9079, "META": 40772, "TSLA": 43129, "AAPL": 1138, "AMZN": 6036,
    "QCOM": 640, "MU": 1648, "TSM": 4130, "CRM": 11159, "ADBE": 1090,
    "DELL": 1327, "SMCI": 7993, "IBM": 354,
}

_RATING_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("jsonld_ratingValue", re.compile(r'"ratingValue"\s*:\s*"?(\d+(?:\.\d+)?)"?', re.I)),
    ("meta_ratingValue", re.compile(r'<meta[^>]+itemprop="ratingValue"[^>]+content="(\d+(?:\.\d+)?)"', re.I)),
    ("data_test_ratingNumber", re.compile(r'data-test="ratingNumber"[^>]*>\s*([\d.]+)\s*<', re.I)),
    ("class_ratingNum", re.compile(r'class="[^"]*ratingNum[^"]*"[^>]*>\s*([\d.]+)\s*<', re.I)),
    ("overallCompanyRating", re.compile(r'"overallCompanyRating"\s*:\s*(\d+(?:\.\d+)?)', re.I)),
    ("bigRating", re.compile(r'class="[^"]*bigRating[^"]*"[^>]*>\s*([\d.]+)\s*<', re.I)),
]

_REVIEW_COUNT_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("jsonld_reviewCount", re.compile(r'"reviewCount"\s*:\s*"?(\d+)"?', re.I)),
    ("class_reviewCount", re.compile(r'class="[^"]*reviewCount[^"]*"[^>]*>\s*([\d,]+)\s*<', re.I)),
    ("class_reviewsCount", re.compile(r'class="[^"]*reviewsCount[^"]*"[^>]*>\s*([\d,]+)\s*<', re.I)),
]

_MIN_HTML_LEN = 3000

_SUBPAGE_RE = re.compile(r"_K[HO]|_IL|_IS|_IN\d")


def _default_db_path() -> str:
    return os.environ.get("QUANT_DB_PATH", "reddit_quant.db")


def _open(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or _default_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def cdx_rows(params: dict, tries: int = 3) -> List[List[str]]:
    """Query the Wayback CDX API, returning the JSON rows (header excluded)."""
    for attempt in range(tries):
        try:
            r = requests.get(CDX_API, params=params, timeout=60)
            if r.status_code in (200, 404):
                text = r.text.strip()
                if text.startswith("["):
                    return r.json()[1:]
                return []
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    return []


def list_tickers(include: Optional[List[str]] = None) -> List[str]:
    """Tickers with a resolved archived Glassdoor path, filtered by ``include``."""
    tickers = list(GLASSDOOR_PATH.keys())
    if include:
        tickers = [t for t in tickers if t in include]
    return tickers


def month_snapshots(ticker: str, since: Optional[str] = None,
                    until: Optional[str] = None, sleep: float = 0.3) -> Dict[str, Tuple[str, str]]:
    """Map calendar months (YYYYMM) to the first archived snapshot in that month.

    The canonical company review page (``glassdoor.com/Reviews/{Name}-Reviews-
    E{id}.htm``) has a stable URL since 2008 and always renders the aggregate
    rating, so it is the primary target (collapse=timestamp:6 -> one capture
    per month). Tickers whose canonical page was sparsely archived fall back
    to the whole page family, preferring company-level pages over job/location
    subpages.
    """
    canonical = f"{GLASSDOOR_PATH[ticker]}-Reviews-E{eid_for(ticker)}"
    months = _scan_prefix(ticker, canonical, since, until, sleep)
    if len(months) >= 12:
        return months
    family = _scan_prefix(ticker, GLASSDOOR_PATH[ticker], since, until, 0.0)
    merged = dict(family)
    for mm, target in months.items():
        if mm not in merged or _subpage_weight(target[1]) >= _subpage_weight(merged[mm][1]):
            merged[mm] = target
    return dict(sorted(merged.items()))


def _scan_prefix(ticker: str, url_prefix: str, since: Optional[str],
                 until: Optional[str], sleep: float) -> Dict[str, Tuple[str, str]]:
    rows = cdx_rows({
        "url": url_prefix,
        "matchType": "prefix",
        "output": "json",
        "fl": "timestamp,original",
        "filter": "statuscode:200",
        "collapse": "timestamp:6",
        "limit": "100000",
    })
    months: Dict[str, Tuple[str, str]] = {}
    for row in rows:
        ts, original = row[0], row[1]
        mm = ts[:6]
        if since and mm < since:
            continue
        if until and mm > until:
            continue
        if mm not in months or _subpage_weight(original) > _subpage_weight(months[mm][1]):
            months[mm] = (ts, original)
    if sleep:
        time.sleep(sleep)
    return months


def eid_for(ticker: str) -> int:
    return GLASSDOOR_EIDS[ticker]


def _subpage_weight(url: str) -> int:
    """0 = job/location subpage (job-titled or filtered), 1 = company-level page."""
    return 0 if _SUBPAGE_RE.search(url) else 1


def fetch_snapshot(ts: str, original: str, timeout: int = 45) -> Optional[str]:
    """Fetch an archived page body with the id_ modifier (no Wayback toolbar).

    The archive rate-limits aggressively (503 "Temporarily Offline"), so
    transient failures are retried with backoff before the month is marked
    unparseable.
    """
    url = f"{WEB_BASE}{ts}id_/{original}"
    attempt = 0
    while True:
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        except requests.RequestException:
            attempt += 1
            if attempt >= 3:
                return None
            time.sleep(6 * attempt)
            continue
        if r.status_code in (503, 429):
            attempt += 1
            if attempt >= 3:
                return None
            time.sleep(6 * attempt)
            continue
        if r.status_code != 200:
            return None
        if r.encoding and r.encoding.lower() not in ("utf-8", "utf8", "iso-8859-1", "latin-1"):
            r.encoding = "utf-8"
        body = r.text
        if len(body) < _MIN_HTML_LEN:
            return None
        return body


def parse_glassdoor_rating(html: str) -> Optional[Dict]:
    """Extract the aggregate company rating (1-5) plus review count.

    Tries every known markup generation in order (2008-era divs through
    current schema.org JSON-LD). Returns None when nothing validates in range.
    """
    if not html:
        return None
    for name, pattern in _RATING_PATTERNS:
        match = pattern.search(html)
        if match:
            try:
                value = float(match.group(1))
            except (ValueError, IndexError):
                continue
            if 1.0 <= value <= 5.0:
                start = max(0, match.start() - 80)
                snippet = html[start:match.end() + 80]
                review_count = None
                for _, rc_pat in _REVIEW_COUNT_PATTERNS:
                    rc = rc_pat.search(html)
                    if rc:
                        try:
                            review_count = int(rc.group(1).replace(",", ""))
                        except ValueError:
                            review_count = None
                        break
                return {
                    "rating": round(value, 2),
                    "pattern": name,
                    "snippet": snippet,
                    "review_count": review_count,
                }
    return None


def _valid_date_from_ts(ts: str) -> str:
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"


class WaybackPitHarvester:
    """Harvests PIT ratings into ``pit_rating_snapshots`` with provenance."""

    def __init__(self, db_path: Optional[str] = None, dry_run: bool = False,
                 cdx_sleep: float = 0.3, fetch_sleep: float = 1.2):
        self.db_path = db_path or _default_db_path()
        self.dry_run = dry_run
        self.cdx_sleep = cdx_sleep
        self.fetch_sleep = fetch_sleep
        self._results: List[Dict] = []
        self._attempted: int = 0

    @classmethod
    def _create_tables(cls, conn: sqlite3.Connection) -> None:
        from db.schema import create_pit_rating_tables
        create_pit_rating_tables(conn)

    def existing_months(self, ticker: str, source: str) -> set:
        conn = _open(self.db_path)
        try:
            rows = conn.execute(
                "SELECT valid_date FROM pit_rating_snapshots WHERE ticker = ? AND source = ?",
                (ticker, source),
            ).fetchall()
        finally:
            conn.close()
        return {str(r["valid_date"]).replace("-", "")[:6] for r in rows}

    def store_rating(self, ticker: str, source: str, ts: str, original: str,
                     parsed: Optional[Dict]) -> str:
        """Persist one observation; returns 'ok' | 'skipped' | 'failed'."""
        valid_date = _valid_date_from_ts(ts)
        conn = _open(self.db_path)
        try:
            if parsed is None:
                cur = conn.execute(
                    "SELECT 1 FROM pit_rating_snapshots WHERE ticker = ? AND source = ? AND valid_date = ?",
                    (ticker, source, valid_date),
                )
                if cur.fetchone():
                    return "skipped"
                if self.dry_run:
                    return "failed"
                conn.execute(
                    "INSERT OR IGNORE INTO pit_rating_snapshots "
                    "(ticker, source, valid_date, rating, review_count, detail_json, "
                    " original_url, snapshot_ts, parse_pattern, created_at) "
                    "VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, NULL, ?)",
                    (ticker, source, valid_date,
                     json.dumps({"reason": "unparseable"}, default=str),
                     original, ts, int(time.time())),
                )
                conn.commit()
                return "failed"
            rating = parsed["rating"]
            detail = json.dumps({
                "pattern": parsed["pattern"],
                "snippet": parsed["snippet"],
                "review_count": parsed["review_count"],
            }, default=str)
            cur = conn.execute(
                "SELECT 1 FROM pit_rating_snapshots WHERE ticker = ? AND source = ? AND valid_date = ?",
                (ticker, source, valid_date),
            )
            if cur.fetchone():
                return "skipped"
            if self.dry_run:
                self._results.append({
                    "ticker": ticker, "valid_date": valid_date, "rating": rating,
                    "pattern": parsed["pattern"], "original": original,
                })
                return "ok"
            conn.execute(
                "INSERT OR IGNORE INTO pit_rating_snapshots "
                "(ticker, source, valid_date, rating, review_count, detail_json, "
                " original_url, snapshot_ts, parse_pattern, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ticker, source, valid_date, rating, parsed["review_count"], detail,
                 original, ts, parsed["pattern"], int(time.time())),
            )
            conn.commit()
            return "ok"
        finally:
            conn.close()

    def harvest_ticker(self, ticker: str, since: Optional[str] = None,
                       until: Optional[str] = None,
                       only_months: Optional[set] = None) -> Dict:
        months = month_snapshots(ticker, since=since, until=until, sleep=self.cdx_sleep)
        existing = self.existing_months(ticker, SOURCE_GLASSDOOR)
        counts = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "months": len(months)}
        for mm, (ts, original) in months.items():
            if only_months is not None and mm not in only_months:
                continue
            if mm in existing:
                counts["skipped"] += 1
                continue
            counts["attempted"] += 1
            self._attempted += 1
            body = fetch_snapshot(ts, original)
            parsed = parse_glassdoor_rating(body) if body else None
            outcome = self.store_rating(ticker, SOURCE_GLASSDOOR, ts, original, parsed)
            counts[outcome] += 1
            if self.fetch_sleep:
                time.sleep(self.fetch_sleep)
        return counts

    def harvest(self, tickers: Optional[List[str]] = None, since: Optional[str] = None,
                until: Optional[str] = None, only_months: Optional[set] = None) -> Dict:
        started = int(time.time())
        tickers = list_tickers(tickers)
        conn = _open(self.db_path)
        try:
            self._create_tables(conn)
        finally:
            conn.close()

        per = {}
        totals = {"ok": 0, "failed": 0, "skipped": 0}
        for ticker in tickers:
            try:
                counts = self.harvest_ticker(ticker, since=since, until=until, only_months=only_months)
            except Exception as exc:  # pragma: no cover - network guard
                counts = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "error": str(exc)}
            per[ticker] = counts
            for key in ("ok", "failed", "skipped"):
                totals[key] += counts.get(key, 0)

        summary = {
            "tickers": tickers,
            "per_ticker": per,
            "totals": totals,
            "dry_run": self.dry_run,
        }
        if not self.dry_run:
            self._log_run(started, tickers, totals)
        return summary

    def _log_run(self, started: int, tickers: List[str], totals: Dict) -> None:
        conn = _open(self.db_path)
        try:
            conn.execute(
                "INSERT INTO pit_harvest_runs "
                "(source, started_at, completed_at, tickers, attempted, succeeded, failed, skipped, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (SOURCE_GLASSDOOR, started, int(time.time()), ",".join(tickers),
                 self._attempted, totals["ok"], totals["failed"], totals["skipped"], "complete"),
            )
            conn.commit()
        finally:
            conn.close()


def load_pit_panel(tickers: Optional[List[str]] = None, source: str = SOURCE_GLASSDOOR,
                   start: Optional[str] = None, end: Optional[str] = None,
                   db_path: Optional[str] = None) -> "pd.DataFrame":
    """Return a wide monthly panel of PIT ratings (index=date, columns=ticker).

    Values carry as-of semantics: the row for date D is the rating the source
    showed on snapshot date D, so no future information leaks into a backtest.
    Empty DataFrames when nothing has been harvested yet.
    """
    import pandas as pd
    conn = _open(db_path)
    try:
        query = ("SELECT valid_date, ticker, rating FROM pit_rating_snapshots "
                 "WHERE source = ? AND rating IS NOT NULL")
        params: list = [source]
        if tickers:
            placeholders = ",".join("?" * len(tickers))
            query += f" AND ticker IN ({placeholders})"
            params.extend(tickers)
        if start:
            query += " AND valid_date >= ?"
            params.append(start)
        if end:
            query += " AND valid_date <= ?"
            params.append(end)
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    frame = pd.DataFrame(
        [(int(datetime.fromisoformat(r["valid_date"]).timestamp()) // 86400, r["ticker"], r["rating"])
         for r in rows],
        columns=["day", "ticker", "rating"],
    )
    if frame.empty:
        return pd.DataFrame()
    frame["day"] = pd.to_datetime(frame["day"], unit="D")
    return frame.pivot(index="day", columns="ticker", values="rating").sort_index()