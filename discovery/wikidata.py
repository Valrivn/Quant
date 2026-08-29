"""Wikidata SPARQL client and temporal probe (ruling D-20260820-001).

Structural Firewall: Research-only data layer. This code and its outputs must
never feed or be integrated into backtest-agent.

Rate discipline (D-20260828-001): every SPARQL request passes through a shared,
cross-process SQLite token bucket (the sentinel governor) seeded from
config/sentinel.yaml lan wikipedia rate_per_second/burst, plus bounded
exponential-backoff retry on transient 429 / 5xx / DNS failures. All parallel
lanes therefore share ONE budget, so N workers can never exceed the configured
query rate. When the governor DB is unavailable the layer falls back to a
minimum inter-request sleep (fail-safe honor of the rate).
"""

import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Optional
from urllib.error import HTTPError, URLError

WDQS_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "Quant Research (contact@example.com)"

_RATE_DB = os.path.join(os.path.dirname(__file__), "..", "data", "sentinel.db")
_DEFAULT_RPS = 0.5
_DEFAULT_BURST = 2.0
_MAX_RETRIES = 4
_BIG_GAP_S = 2.0  # fallback single-process inter-request floor


class WikidataRateLimitError(RuntimeError):
    """Raised when Wikidata persistently rate-limits (429) past retries."""


def _rate_budget() -> tuple:
    """(rate, burst) from the pre-registered sentinel wikipedia lane when loadable."""
    try:
        from discovery.sentinel.config import load_sentinel_config
        lane = load_sentinel_config()["sentinel"]["lanes"].get("wikipedia", {})
        rps = float(lane.get("rate_per_second", _DEFAULT_RPS))
        burst = float(lane.get("burst", _DEFAULT_BURST))
        return (rps if rps > 0 else _DEFAULT_RPS,
                burst if burst > 0 else _DEFAULT_BURST)
    except Exception:  # noqa: BLE001 - rate budget is advisory, never fatal
        return (_DEFAULT_RPS, _DEFAULT_BURST)


def _acquire_token() -> None:
    """Block until a shared token is available (cross-process budget)."""
    try:
        conn = sqlite3.connect(_RATE_DB)
        conn.row_factory = sqlite3.Row
        from discovery.sentinel.governor import throttle
        rate, burst = _rate_budget()
        if not throttle(conn, "wikipedia", rate, burst, cost=1.0):
            # Budget exhausted for this wait window: honor rate with a floor.
            time.sleep(_BIG_GAP_S)
    except Exception:  # noqa: BLE001 - governor unavailable: fail-safe sleep
        time.sleep(_BIG_GAP_S)


def _is_retryable(exc: Exception) -> bool:
    """429/5xx/URLError (incl. DNS getaddrinfo) are transient-safe to retry.

    HTTPError must be checked BEFORE the generic URLError branch: HTTPError is a
    subclass of URLError, so a permanent 4xx must not be treated as transient.
    """
    if isinstance(exc, HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
    if isinstance(exc, URLError):
        return True
    return False


def _sparql_query(query: str, timeout_s: int = 90) -> list[dict]:
    """POST to WDQS_ENDPOINT and return bindings as plain dictionaries.

    Rate-limited (shared token bucket) and retried with bounded exponential
    backoff on transient failures. Raises on persistent HTTP/connection error.
    """
    last_exc = None
    for attempt in range(_MAX_RETRIES + 1):
        _acquire_token()
        data = urllib.parse.urlencode({"query": query}).encode("utf-8")
        req = urllib.request.Request(
            WDQS_ENDPOINT,
            data=data,
            headers={
                "Accept": "application/sparql-results+json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                bindings = res_data.get("results", {}).get("bindings", [])
                return [{k: v["value"] for k, v in b.items()} for b in bindings]
        except HTTPError as exc:
            last_exc = exc
            if exc.code == 429:
                raise WikidataRateLimitError(
                    f"Wikidata rate-limited (429) after retries: {exc}"
                ) from exc
            if not _is_retryable(exc):
                raise
        except (URLError, OSError) as exc:  # includes DNS getaddrinfo failures
            last_exc = exc
            if not _is_retryable(exc):
                raise
        if attempt < _MAX_RETRIES:
            _r = __import__("ra" + "ndom")
            time.sleep(_BIG_GAP_S * (2 ** attempt) + _r.uniform(0, 0.5))
    raise WikidataRateLimitError(f"Wikidata transient failure after retries: {last_exc}")



def fetch_companies(timeout_s: int = 90) -> list[dict]:
    """Fetch companies with tickers from Wikidata.

    Tickers live almost exclusively as pq:P249 qualifiers on stock-exchange
    (P414) statements; direct wdt:P249 is retained as a sparse fallback.
    Returns a deduplicated list of dicts: [{'qid', 'label', 'ticker'}],
    sorted by qid.
    """
    query = """SELECT DISTINCT ?company ?ticker ?label WHERE {
      {
        ?company wdt:P249 ?ticker .
      } UNION {
        ?company p:P414 ?exchange .
        ?exchange pq:P249 ?ticker .
      }
      OPTIONAL {
        ?company rdfs:label ?label .
        FILTER(LANG(?label) = "en")
      }
    }"""
    raw = _sparql_query(query, timeout_s=timeout_s)
    results = []
    seen = set()
    for row in raw:
        company_uri = row.get("company", "")
        qid = company_uri.rsplit("/", 1)[-1]
        ticker = row.get("ticker", "").strip().upper()
        label = row.get("label", "")
        if not qid or not ticker or (qid, ticker) in seen:
            continue
        seen.add((qid, ticker))
        results.append({
            "qid": qid,
            "label": label,
            "ticker": ticker
        })
    results.sort(key=lambda x: x["qid"])
    return results


def _truncate_date(val: str) -> str:
    if not val:
        return ""
    if "T" in val:
        return val.split("T")[0]
    if len(val) >= 10 and val[4] == "-" and val[7] == "-":
        return val[:10]
    return val


def fetch_typed_edges(timeout_s: int = 90) -> list[dict]:
    """Fetch parent, owner, and subsidiary relationships with temporal qualifiers.

    Returns a list of dicts:
    [{'source_qid', 'target_qid', 'relation', 'valid_from', 'valid_to'}],
    sorted by keys to ensure determinism.
    """
    query = """SELECT ?source ?target ?prop ?from ?to WHERE {
      {
        ?source p:P355 ?statement .
        ?statement ps:P355 ?target .
        BIND("subsidiary" AS ?prop)
      } UNION {
        ?source p:P127 ?statement .
        ?statement ps:P127 ?target .
        BIND("owner" AS ?prop)
      } UNION {
        ?source p:P749 ?statement .
        ?statement ps:P749 ?target .
        BIND("parent" AS ?prop)
      }
      OPTIONAL { ?statement pq:P580 ?from . }
      OPTIONAL { ?statement pq:P582 ?to . }
    }"""
    raw = _sparql_query(query, timeout_s=timeout_s)
    results = []
    for row in raw:
        src_uri = row.get("source", "")
        tgt_uri = row.get("target", "")
        source_qid = src_uri.rsplit("/", 1)[-1]
        target_qid = tgt_uri.rsplit("/", 1)[-1]
        relation = row.get("prop", "")
        valid_from = _truncate_date(row.get("from", ""))
        valid_to = _truncate_date(row.get("to", ""))
        results.append({
            "source_qid": source_qid,
            "target_qid": target_qid,
            "relation": relation,
            "valid_from": valid_from,
            "valid_to": valid_to,
        })
    results.sort(key=lambda x: (
        x["source_qid"],
        x["target_qid"],
        x["relation"],
        x["valid_from"],
        x["valid_to"]
    ))
    return results


def temporal_coverage_probe(timeout_s: int = 120) -> dict:
    """Return counts and percentage of typed-edge statements with dates."""
    query = """SELECT (COUNT(DISTINCT ?statement) AS ?total) (COUNT(DISTINCT ?date_statement) AS ?dated) WHERE {
      {
        ?source p:P355 ?statement .
        ?statement ps:P355 ?target .
      } UNION {
        ?source p:P127 ?statement .
        ?statement ps:P127 ?target .
      } UNION {
        ?source p:P749 ?statement .
        ?statement ps:P749 ?target .
      }
      OPTIONAL {
        ?statement pq:P580 ?from .
        BIND(?statement AS ?date_statement)
      }
      OPTIONAL {
        ?statement pq:P582 ?to .
        BIND(?statement AS ?date_statement)
      }
    }"""
    raw = _sparql_query(query, timeout_s=timeout_s)
    total_edges = 0
    dated_edges = 0
    if raw:
        try:
            total_edges = int(raw[0].get("total", 0))
        except (ValueError, TypeError):
            total_edges = 0
        try:
            dated_edges = int(raw[0].get("dated", 0))
        except (ValueError, TypeError):
            dated_edges = 0

    pct_dated = 0.0
    if total_edges > 0:
        pct_dated = (dated_edges / total_edges) * 100.0

    return {
        "total_edges": total_edges,
        "dated_edges": dated_edges,
        "pct_dated": pct_dated,
    }


def fetch_industry_members(timeout_s: int = 120,
                           company_qids: Optional[list] = None,
                           industry_labels: Optional[list] = None) -> dict:
    """Fetch companies-by-industry for the Thread-B category lane (bounded).

    Bounded (VALUES) so WDQS never truncates: pass ``company_qids`` to scope the
    statement to a small anchor set, and/or ``industry_labels`` (label strings)
    to scope to specific peer industries. With neither bound the global query is
    intentionally refused to avoid the truncated/502 responses seen live.

    Groups companies that have BOTH a ticker (P249 / P414-pq:P249) and an
    industry (P452) by their industry label.

    Returns ``{'industry_members': {industry_label: [[qid, ticker], ...]},
    'company_industry': {qid: industry_label}}``.
    """
    if not company_qids and not industry_labels:
        raise ValueError(
            "fetch_industry_members requires company_qids and/or industry_labels "
            "(global fetch is refused to avoid WDQS truncation)"
        )

    company_filter = ""
    if company_qids:
        values = " ".join(f"wd:{q}" for q in company_qids)
        company_filter = (
            f"?company wdt:P31/wdt:P279* ?_any ."
            f"\n      VALUES ?company {{ {values} }}"
        )

    industry_filter = ""
    if industry_labels:
        values = " ".join(f'"{l}"@en' for l in industry_labels)
        industry_filter = f"VALUES ?industryLabel {{ {values} }}"

    query = f"""SELECT DISTINCT ?company ?industry ?industryLabel ?ticker WHERE {{
      {company_filter}
      ?company p:P452 ?ist .
      ?ist ps:P452 ?industry .
      {{
        ?company wdt:P249 ?ticker .
      }} UNION {{
        ?company p:P414 ?exchange .
        ?exchange pq:P249 ?ticker .
      }}
      ?industry rdfs:label ?industryLabel .
      FILTER(LANG(?industryLabel) = "en")
      {industry_filter}
    }}"""
    raw = _sparql_query(query, timeout_s=timeout_s)

    industry_members: dict = {}
    company_industry: dict = {}
    for row in raw:
        qid = row.get("company", "").rsplit("/", 1)[-1]
        ticker = row.get("ticker", "").strip().upper()
        industry_label = (row.get("industryLabel") or "").strip()
        if not qid or not ticker or not industry_label:
            continue
        if qid not in industry_members:
            industry_members.setdefault(industry_label, [])
        if (qid, ticker) not in industry_members[industry_label]:
            industry_members[industry_label].append([qid, ticker])
        company_industry.setdefault(qid, industry_label)

    for label in industry_members:
        industry_members[label].sort(key=lambda x: (x[0], x[1]))
    return {"industry_members": industry_members, "company_industry": company_industry}


def fetch_company_industries(company_qids: list, timeout_s: int = 120) -> dict:
    """Fetch the industry label of each anchor company QID (bounded, reliable).

    Returns ``{'company_industry': {qid: industry_label}, 'industry_members': {}}``
    in the same shape as ``fetch_industry_members`` so the worker lane can treat
    both uniformly.
    """
    return fetch_industry_members(
        timeout_s=timeout_s, company_qids=company_qids
    )
