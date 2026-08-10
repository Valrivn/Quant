"""Alt-data lane — G3 gates: Reddit z-score and GitHub star-growth velocity.

Read-only against the shared ``reddit_quant.db`` (daily aggregations) and the
sentinel GitHub snapshot table. Never hits the network; a coverage floor drops
tickers with no signal in any source (0 + 0 => drop).
"""

import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple


def _ts(days_back: int) -> int:
    return int((datetime.now(tz=timezone.utc) - timedelta(days=days_back)).timestamp())


def record_github_snapshot(conn: sqlite3.Connection, ticker: str, repo: str, stars: int) -> None:
    conn.execute(
        """INSERT INTO sentinel_github_snapshots (ticker, repo_name, stars, fetched_at)
           VALUES (?, ?, ?, ?)""",
        (ticker, repo, int(stars), _ts(0)),
    )
    conn.commit()


def reddit_z(
    reddit_conn: sqlite3.Connection, ticker: str, window_days: int,
    min_observations: int = 5, as_of: Optional[int] = None,
) -> Optional[float]:
    """Recent-7d mean vs window baseline, as a z-score.

    Returns None when there are not enough daily observations (coverage gap).
    Uses the daily_aggregations ``date`` column (YYYY-MM-DD). Mentions are
    summed across subreddit/category rows per date (the table stores one row
    per ticker/date/subreddit).
    """
    now = as_of or _ts(0)
    start_dt = datetime.fromtimestamp(now, tz=timezone.utc) - timedelta(days=window_days)
    rows = reddit_conn.execute(
        """SELECT date, mention_count FROM daily_aggregations WHERE ticker = ?""",
        (ticker,),
    ).fetchall()
    by_date: Dict[str, float] = {}
    for r in rows:
        date = str(r["date"])[:10]
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            continue
        by_date[date] = by_date.get(date, 0.0) + float(r["mention_count"])
    dated = []
    for date, count in by_date.items():
        d = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if start_dt <= d <= datetime.fromtimestamp(now, tz=timezone.utc):
            dated.append((int(d.timestamp()), count))
    if len(dated) < min_observations:
        return None
    dated.sort()
    cutoff = int(dated[-1][0] - 7 * 86400)
    baseline = [v for ts, v in dated if ts <= cutoff]
    recent = [v for ts, v in dated if ts > cutoff]
    if not recent:
        recent = [dated[-1][1]]
    if not baseline:
        baseline = [v for _, v in dated[: max(1, len(dated) // 2)]]
    mu = statistics.mean(baseline)
    sd = statistics.pstdev(baseline)
    if sd == 0:
        return 0.0
    return (statistics.mean(recent) - mu) / sd


def github_growth(
    conn: sqlite3.Connection, ticker: str, lookback_days: int = 180,
    as_of: Optional[int] = None,
) -> Optional[float]:
    """Fractional star growth over the lookback window across all repos.

    Returns None when there is no snapshot history (coverage gap).
    """
    now = as_of or _ts(0)
    start = now - lookback_days * 86400
    rows = conn.execute(
        """SELECT repo_name, stars, fetched_at FROM sentinel_github_snapshots
           WHERE ticker = ? AND fetched_at >= ? AND fetched_at <= ?
           ORDER BY fetched_at ASC""",
        (ticker, start, now),
    ).fetchall()
    if not rows:
        return None
    first = rows[0]["stars"]
    last = rows[-1]["stars"]
    if first is None or last is None or first <= 0:
        return None
    return (last - first) / first


def g3_altdata(
    reddit_conn: sqlite3.Connection, conn: sqlite3.Connection, ticker: str, cfg: Dict,
    as_of: Optional[int] = None,
) -> Tuple[bool, str, Dict]:
    """Fail-closed alt-data gate.

    Coverage = number of sources (reddit, github) with usable data. A ticker
    with fewer than ``min_coverage_sources`` sources (default 1) is dropped.
    Pass requires at least one present source to clear its floor.
    """
    g3 = cfg["gates"]["g3_altdata"]
    rdz = reddit_z(reddit_conn, ticker, g3["reddit_window_days"],
                   g3.get("min_observations", 5), as_of)
    ghg = github_growth(conn, ticker, g3["github_lookback_days"], as_of)

    reddit_ok = rdz is not None and rdz >= g3["reddit_z_floor"]
    github_ok = ghg is not None and ghg >= g3["github_star_growth_floor"]

    coverage = sum(1 for v in (rdz, ghg) if v is not None)
    metrics = {
        "reddit_z": round(rdz, 3) if rdz is not None else None,
        "github_growth": round(ghg, 4) if ghg is not None else None,
        "coverage_sources": coverage,
    }

    if coverage < g3["min_coverage_sources"]:
        return False, "g3:no_altdata_coverage", metrics
    if not (reddit_ok or github_ok):
        legs = []
        if rdz is not None and not reddit_ok:
            legs.append(f"reddit_z<{g3['reddit_z_floor']}")
        if ghg is not None and not github_ok:
            legs.append(f"github_growth<{g3['github_star_growth_floor']}")
        return False, "g3:" + (";".join(legs) or "no_signal"), metrics
    return True, "", metrics
