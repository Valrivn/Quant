"""Wikidata SPARQL client and temporal probe (ruling D-20260820-001).

Structural Firewall: Research-only data layer. This code and its outputs must
never feed or be integrated into backtest-agent.
"""

import json
import urllib.parse
import urllib.request

WDQS_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "Quant Research (contact@example.com)"


def _sparql_query(query: str, timeout_s: int = 90) -> list[dict]:
    """POST to WDQS_ENDPOINT and return bindings as plain dictionaries.

    Raises on HTTP/connection error. Must perform no network at import time.
    """
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
    with urllib.request.urlopen(req, timeout=timeout_s) as response:
        res_data = json.loads(response.read().decode("utf-8"))
        bindings = res_data.get("results", {}).get("bindings", [])
        return [{k: v["value"] for k, v in b.items()} for b in bindings]


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
