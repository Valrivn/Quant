"""Lane Gamma ingestion — SEC XBRL, GitHub org, Glassdoor and Reddit coverage.

Flat data structures, deterministic bounds, temporal alignment. Guards every
value that crosses a boundary and clamps z-scores into [-1, 1].
"""
import json
import logging
import math
import re
import sqlite3
import time
import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = str(PROJECT_ROOT / "reddit_quant.db")
CF_MEMORY_PATH = PROJECT_ROOT / "config" / "cloudflare_strategy_memory.json"
CONFIG_PATH = PROJECT_ROOT / "config" / "hybrid_config.yaml"

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

XBRL_FACT_NAMES = [
    "RevenueFromContractWithCustomer",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenue",
    "Revenues",
    "CostOfRevenue",
    "GrossProfit",
    "OperatingIncome",
    "NetIncomeLoss",
]

_RATING_STAR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:★|out of 5)", re.IGNORECASE)
_RATING_NUM_CLASS_RE = re.compile(r'class="ratingNum">\s*(\d+(?:\.\d+)?)')
_RATING_JSON_RE = re.compile(r"overallRating\s*:\s*(\d+(?:\.\d+)?)")


def tanh_clamp(z: float) -> float:
    """Clamp a raw z-score into [-1, 1] deterministically."""
    return math.tanh(float(z))


def tanh_clamp_unit(z: float) -> float:
    """Map a z-score into [0, 1] via tanh."""
    return (math.tanh(float(z)) + 1.0) / 2.0


def guard_float(value: Any, default: float = 0.0) -> float:
    """Coerce to float, falling back to default on None/NaN/Inf/parse failure."""
    if value is None:
        return default
    try:
        f = float(value)
    except (ValueError, TypeError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def guard_int(value: Any, default: int = 0) -> int:
    """Coerce to int, falling back to default on None/NaN/parse failure."""
    if value is None:
        return default
    try:
        i = int(value)
    except (ValueError, TypeError):
        return default
    return i


def bounded_z_score(value: float, mean: float, std: float) -> float:
    """z-score with std guard, clamped into [-1, 1]."""
    v = guard_float(value)
    m = guard_float(mean)
    s = guard_float(std)
    if s <= 1e-9:
        return 0.0
    return tanh_clamp((v - m) / s)


def _extract_glassdoor_rating(html: str) -> Optional[float]:
    """Extract a Glassdoor rating (0 < r <= 5) from raw HTML/JSON snippets."""
    if not html:
        return None
    for pattern in (_RATING_STAR_RE, _RATING_NUM_CLASS_RE, _RATING_JSON_RE):
        m = pattern.search(html)
        if m:
            try:
                rating = float(m.group(1))
            except ValueError:
                return None
            if 0.0 < rating <= 5.0:
                return rating
    return None


def get_conn() -> sqlite3.Connection:
    """Open a connection to DB_PATH."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn


def create_lane_gamma_tables(conn: sqlite3.Connection) -> None:
    """Create the Lane Gamma table set if not present."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sec_xbrl_facts (
            ticker TEXT NOT NULL,
            cik TEXT,
            accession_number TEXT,
            filing_date TEXT,
            fact_name TEXT,
            fact_value REAL,
            unit TEXT,
            segment TEXT,
            fiscal_year INTEGER,
            fiscal_period TEXT,
            source_url TEXT,
            fetched_at INTEGER,
            UNIQUE(ticker, accession_number, fact_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS github_org_metrics (
            ticker TEXT NOT NULL,
            org_name TEXT,
            repo_name TEXT,
            stars INTEGER,
            forks INTEGER,
            open_issues INTEGER,
            watchers INTEGER,
            language TEXT,
            description TEXT,
            topics TEXT,
            created_at_api TEXT,
            updated_at_api TEXT,
            fetched_at INTEGER,
            UNIQUE(ticker, repo_name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS glassdoor_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            rating REAL,
            created_at INTEGER NOT NULL,
            UNIQUE(ticker, date)
        )
    """)
    conn.execute("""
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
    conn.commit()


def load_config() -> Dict[str, Any]:
    """Load hybrid_config.yaml."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_company_config(cfg: Dict[str, Any], ticker: str) -> Dict[str, Any]:
    """Return the per-company config block for a ticker."""
    return cfg.get("companies", {}).get(ticker, {})


def get_tickers() -> List[str]:
    """Return the configured company tickers."""
    cfg = load_config()
    return list(cfg.get("companies", {}).keys())


class PublicationLagMatrix:
    """Temporal alignment for sources whose data publishes late."""

    DEFAULTS: Dict[str, int] = {
        "reddit_velocity": 0,
        "employee_sentiment": 3,
    }

    def __init__(self, lag_map: Optional[Dict[str, int]] = None):
        self.lag_map = dict(self.DEFAULTS)
        if lag_map:
            self.lag_map.update({k: guard_int(v) for k, v in lag_map.items()})

    def lag_for(self, source: str) -> int:
        return self.lag_map.get(source, 0)

    def adjust_timestamp(self, source: str, dt: datetime) -> datetime:
        return dt + timedelta(days=self.lag_for(source))

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "PublicationLagMatrix":
        lag_map = cfg.get("publication_lags") or {}
        return cls(lag_map)


def build_coverage_matrix(
    tickers: List[str],
    sec_days: int,
    github_days: int,
    reddit_report: Dict[str, Dict[str, Any]],
    glassdoor_results: Dict[str, Optional[float]],
) -> Dict[str, Dict[str, Any]]:
    """Build the per-ticker source coverage matrix from DB + reports."""
    matrix: Dict[str, Dict[str, Any]] = {}
    conn = get_conn()
    try:
        for t in tickers:
            sec_count = guard_int(conn.execute(
                "SELECT COUNT(*) FROM sec_xbrl_facts WHERE ticker=?", (t,)
            ).fetchone()[0])
            github_count = guard_int(conn.execute(
                "SELECT COUNT(*) FROM github_org_metrics WHERE ticker=?", (t,)
            ).fetchone()[0])
            gd_count = guard_int(conn.execute(
                "SELECT COUNT(*) FROM glassdoor_snapshots WHERE ticker=?", (t,)
            ).fetchone()[0])
            report = reddit_report.get(t, {})
            rating = glassdoor_results.get(t)
            matrix[t] = {
                "sec_xbrl_records": sec_count,
                "sec_status": "OK" if sec_count > 0 else "MISSING",
                "github_repos": github_count,
                "github_status": "OK" if github_count > 0 else "MISSING",
                "glassdoor_records": gd_count,
                "glassdoor_current": "OK" if rating is not None else "BLOCKED_403",
                "glassdoor_current_rating": rating,
                "reddit_agg_records": guard_int(report.get("total_records", 0)),
                "reddit_status": "OK" if report.get("status") == "OK" else "MISSING",
                "reddit_latest_date": report.get("latest_date") or "N/A",
            }
    finally:
        conn.close()
    return matrix


def _coverage_string(matrix: Dict[str, Any]) -> str:
    sources_ok = sum(
        1
        for key in ("sec_status", "github_status", "glassdoor_current", "reddit_status")
        if matrix.get(key) == "OK"
    )
    return f"{sources_ok}/4 sources"


def write_audit_md(
    tickers: List[str],
    matrix: Dict[str, Dict[str, Any]],
    sec_days: int,
    github_days: int,
    elapsed: float,
) -> None:
    """Write the data-completeness audit markdown into center/."""
    lines = [
        "# Lane Gamma Data Completeness Audit",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"SEC lookback: {sec_days}d | GitHub lookback: {github_days}d | Elapsed: {elapsed:.1f}s",
        "",
        "| Ticker | SEC | GitHub | Glassdoor | Reddit | Coverage | Rating |",
        "|--------|-----|--------|-----------|--------|----------|--------|",
    ]
    for t in tickers:
        m = matrix.get(t, {})
        lines.append(
            "| {t} | {sec} | {git} | {gd} | {rd} | {cov} | {rating} |".format(
                t=t,
                sec=m.get("sec_status", "MISSING"),
                git=m.get("github_status", "MISSING"),
                gd=m.get("glassdoor_current", "BLOCKED_403"),
                rd=m.get("reddit_status", "MISSING"),
                cov=_coverage_string(m),
                rating=m.get("glassdoor_current_rating") if m.get("glassdoor_current_rating") is not None else "N/A",
            )
        )
    lines += [
        "",
        "## Guard & Alignment Notes",
        "",
        "- `tanh_clamp` keeps every z-score inside [-1, 1].",
        "- `PublicationLagMatrix` shifts late-reporting sources before alignment.",
        "- Glassdoor fetch uses the CF Bypass Strategy 3 (distributed SERP API).",
    ]
    audit_path = PROJECT_ROOT / "center" / "data_completeness_audit.md"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info(f"Wrote audit to {audit_path}")


def log_cf_strategy_performance(
    sec_errors: List[str],
    github_errors: List[str],
    glassdoor_results: Dict[str, Optional[float]],
    elapsed: float,
) -> None:
    """Persist Cloudflare-bypass strategy performance to JSON memory."""
    total = len(glassdoor_results)
    successes = sum(1 for r in glassdoor_results.values() if r is not None)
    success_rate = (successes / total) if total > 0 else 0.0
    memory: Dict[str, Any] = {}
    if CF_MEMORY_PATH.exists():
        try:
            memory = json.loads(CF_MEMORY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            memory = {}
    entry = memory.setdefault("distributed_serp_api", {})
    pm = entry.setdefault("performance_metrics", {})
    pm["glassdoor_success_rate"] = round(success_rate, 3)
    pm["last_run_elapsed_seconds"] = round(float(elapsed), 1)
    pm["last_run_timestamp"] = datetime.now(timezone.utc).isoformat()
    entry["last_run_errors"] = {"sec": sec_errors, "github": github_errors}
    CF_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    CF_MEMORY_PATH.write_text(json.dumps(memory, indent=2), encoding="utf-8")


async def main_mock_only() -> Dict[str, Any]:
    """Deterministic mock ingestion for offline pipeline validation."""
    tickers = get_tickers()
    conn = get_conn()
    try:
        create_lane_gamma_tables(conn)
        now = int(time.time())
        sec_total = 0
        for t in tickers:
            for i in range(3):
                conn.execute(
                    """INSERT OR REPLACE INTO sec_xbrl_facts
                       (ticker, cik, accession_number, filing_date, fact_name, fact_value,
                        unit, segment, fiscal_year, fiscal_period, source_url, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (t, "CIK", f"{t}-ACC-{i}", "2025-01-15", XBRL_FACT_NAMES[i % len(XBRL_FACT_NAMES)],
                     1000.0, "USD", "", 2024, "FY", "mock://sec", now),
                )
                sec_total += 1
        github_total = 0
        for t in tickers:
            for i in range(2):
                conn.execute(
                    """INSERT OR REPLACE INTO github_org_metrics
                       (ticker, org_name, repo_name, stars, forks, open_issues, watchers, language,
                        description, topics, created_at_api, updated_at_api, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (t, t, f"{t}/repo{i}", 100, 50, 5, 200, "Python", "desc", "[]",
                     "2024-01-01T00:00:00Z", "2025-01-01T00:00:00Z", now),
                )
                github_total += 1
        for t in tickers:
            conn.execute(
                "INSERT OR REPLACE INTO glassdoor_snapshots (ticker, date, rating, created_at) VALUES (?,?,?,?)",
                (t, "2026-06-29", 4.2, now),
            )
        conn.commit()
    finally:
        conn.close()
    reddit_report = {
        t: {"total_records": 100, "latest_date": "2026-06-28", "categories": 4,
            "subreddits": 3, "status": "OK"}
        for t in tickers
    }
    glassdoor_results: Dict[str, Optional[float]] = {t: 4.2 for t in tickers}
    return {
        "tickers": tickers,
        "sec_total": sec_total,
        "github_total": github_total,
        "reddit_report": reddit_report,
        "glassdoor_results": glassdoor_results,
    }
