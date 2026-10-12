import sys
from pathlib import Path

# Add project root to sys.path to resolve absolute imports correctly
root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

# Add Qualitative/ (houses the psychological + scraper packages) so that the
# documented gate command `python -m pytest tests/ -q` works from repo root.
qual = root / "Qualitative"
if str(qual) not in sys.path:
    sys.path.insert(0, str(qual))

import pytest
import sqlite3
import os
import hashlib
import yaml

# ---------------------------------------------------------------------------
# Production data must survive a test run.
# ---------------------------------------------------------------------------
# `valuation_alpha.datastore.factors` resolves its cache from a MODULE-LEVEL
# absolute path (`Path("data/fred_cache") / "ff5_factors_daily.parquet"`), so a
# test that stubs the Ken French fetch used to write its fixture straight into
# the real data estate. It replaced the actual 15,876-row 1963-2026 FF5 panel
# with a 2-row 1946 placeholder, and `data/` is gitignored so the loss was
# invisible to `git diff`. Provenance R5 caught the hash drift only after the
# fact. Two defences below: redirect the known offender, and prove at session
# end that nothing under the guarded trees moved.

_GUARDED_SUBDIRS = ("data/fred_cache",)
_GUARDED_SUFFIXES = (".json", ".parquet")


def _fingerprint(subdirs=_GUARDED_SUBDIRS):
    out = {}
    for sub in subdirs:
        base = root / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.suffix in _GUARDED_SUFFIXES:
                try:
                    out[str(p.relative_to(root))] = hashlib.sha256(
                        p.read_bytes()).hexdigest()
                except OSError:
                    pass
    return out


_BASELINE_FINGERPRINT = _fingerprint()


@pytest.fixture(autouse=True)
def _isolate_shared_caches(tmp_path, monkeypatch):
    """Redirect module-level cache globals into tmp_path for every test.

    Any module that caches to a hard-coded path under data/ gets pinned to a
    per-test temporary location. Add new offenders here as they appear.
    """
    try:
        from valuation_alpha.datastore import factors as _factors
    except Exception:
        yield
        return

    monkeypatch.setattr(_factors, "_FF5_CACHE", tmp_path / "ff5_factors_daily.parquet",
                        raising=False)
    yield


def pytest_sessionfinish(session, exitstatus):
    after = _fingerprint()
    changed = sorted(
        k for k in set(_BASELINE_FINGERPRINT) | set(after)
        if _BASELINE_FINGERPRINT.get(k) != after.get(k))
    if changed:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter:
            reporter.write_line("")
            reporter.write_line(
                "=" * 72, red=True, bold=True)
            reporter.write_line(
                "PRODUCTION DATA MUTATED BY THE TEST RUN", red=True, bold=True)
            for name in changed:
                reporter.write_line(f"  changed: {name}", red=True)
            reporter.write_line(
                "Tests must not write to data/. See the _isolate_shared_caches "
                "fixture in tests/conftest.py.", red=True)
            reporter.write_line("=" * 72, red=True, bold=True)
        session.exitstatus = 1


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
