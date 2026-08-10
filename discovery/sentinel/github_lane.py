"""GitHub star snapshot lane — second G3 altdata source.

Fetches the stargazer count of each roster ticker's flagship public
repository and stores a time series in ``sentinel_github_snapshots``.
``altdata_lane.github_growth`` consumes that series (first -> last snapshot
over the lookback window) as the G3 growth signal.

Fail-closed: an unmapped org, network error, or rate limit leaves the source
absent (coverage stays None) rather than inventing a signal. A wrong repo is
worse than no signal, so only well-known orgs are mapped; tickers without a
confident org are skipped.
"""

import logging
from typing import Dict, List, Optional

import requests

from discovery.sentinel import governor, queue as q

logger = logging.getLogger(__name__)

# ticker -> GitHub org whose top-starred public repo stands in for the
# company's developer ecosystem. Unknowns are deliberately omitted.
ORG_MAP: Dict[str, str] = {
    "AAPL": "apple",
    "MSFT": "microsoft",
    "META": "facebook",
    "GOOGL": "google",
    "AMZN": "aws",
    "NVDA": "NVIDIA",
    "INTC": "intel",
    "ADBE": "adobe",
    "CRM": "salesforce",
    "IBM": "IBM",
    "ORCL": "oracle",
    "TSLA": "teslamotors",
    "JPM": "jpmorganchase",
    "SNOW": "snowflakedb",
    "INTU": "intuit",
    "AMD": "ROCm",
    "QCOM": "qualcomm",
}

_SEARCH_URL = "https://api.github.com/search/repositories"


def top_starred_repo(
    org: str, token: Optional[str] = None,
    user_agent: str = "Quant Research (contact@example.com)",
) -> Optional[Dict]:
    """Return ``{"full_name": ..., "stargazers_count": ...}`` for an org's
    top-starred repo, or None when the org has no public repositories."""
    headers = {"User-Agent": user_agent}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = requests.get(
        _SEARCH_URL,
        params={"q": f"org:{org}", "sort": "stars", "order": "desc", "per_page": 1},
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    items = (resp.json() or {}).get("items") or []
    if not items:
        return None
    return {
        "full_name": items[0].get("full_name"),
        "stargazers_count": items[0].get("stargazers_count"),
    }


def sync_github_snapshots(
    conn, tickers: List[str], cfg: Dict, token: Optional[str] = None,
) -> Dict:
    """Snapshot the top repo of every mapped ticker into the snapshots table.

    ``cfg`` is the ``sentinel`` sub-config. Returns counts by outcome.
    """
    gh = cfg["lanes"].get("github", {})
    user_agent = gh.get("user_agent") or "Quant Research (contact@example.com)"
    rate = float(gh.get("rate_per_second", 0.5))
    burst = int(gh.get("burst", 2))

    counted = {"mapped": 0, "snapshotted": 0, "missing": 0, "error": 0}
    for ticker in tickers:
        org = ORG_MAP.get(ticker)
        if not org:
            continue
        counted["mapped"] += 1
        if q.github_snapshot_exists_today(conn, ticker):
            continue
        if not governor.circuit_allow(
            conn, "github",
            cfg["governor"]["circuit_failure_threshold"],
            cfg["governor"]["circuit_success_threshold"],
            cfg["governor"]["circuit_timeout_seconds"],
        ):
            break
        if not governor.throttle(conn, "github", rate, burst):
            break
        try:
            repo = top_starred_repo(org, token=token, user_agent=user_agent)
            if not repo or repo.get("full_name") is None or repo.get("stargazers_count") is None:
                counted["missing"] += 1
                continue
            q.upsert_github_snapshot(
                conn, ticker, repo["full_name"], int(repo["stargazers_count"]))
            counted["snapshotted"] += 1
            governor.record_success(conn, "github")
        except requests.HTTPError as exc:
            governor.record_failure(conn, "github")
            counted["error"] += 1
            if exc.response is not None and exc.response.status_code == 403:
                logger.warning("github rate limit hit for %s; stopping", ticker)
                break
        except Exception as exc:
            governor.record_failure(conn, "github")
            counted["error"] += 1
            logger.warning("github snapshot failed for %s: %s", ticker, exc)
    return counted
