"""P1 live probe runner for B-20260820-001 (ruling D-20260820-001).

Wave-based crawl: expands outward from the SEC frontier seeds through Wikidata
typed edges (P355/P127/P749 with P580/P582 validity windows) using bounded
per-batch VALUES queries — no monolithic dumps. Persists to the research
tables in data/sentinel.db, diffs against persisted SEC edges, checks the
>=50% PIT unlock bar.

Research-only. Never wired into run-all. Structural firewall intact.
sim-guardian audit 2026-08-20: live-screener mode only until replay
hardening (temporal edge filtering + historical ticker mapping) lands.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.schema_discovery import create_discovery_tables
from discovery.wikidata import _sparql_query, fetch_companies, temporal_coverage_probe
from discovery.wiki_frontier import expand_wiki_frontier
from discovery.wiki_sec_diff import diff_wiki_sec
from discovery.wiki_census import pit_unlock_check

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sentinel.db"
SEED_TICKERS = [
    "NVDA", "AMD", "INTC", "AVGO", "MSFT",
    "GOOGL", "META", "AMZN", "AAPL", "TSM", "ASML",
]
MAJOR_TICKERS = {
    "NVDA", "AMD", "INTC", "AVGO", "MSFT",
    "GOOGL", "META", "AMZN", "AAPL",
}
WAVE_BATCH = 48
MAX_DEPTH = 3
MAX_NODES = 2500
QUERY_GAP_S = 1.0

_WAVE_QUERY = """SELECT ?src ?rel ?tgt ?from ?to WHERE {
  VALUES ?seed { %SEEDS% }
  {
    ?seed p:P355 ?st . ?st ps:P355 ?tgt .
    BIND(?seed AS ?src) BIND("subsidiary" AS ?rel)
  } UNION {
    ?seed p:P127 ?st . ?st ps:P127 ?tgt .
    BIND(?seed AS ?src) BIND("owner" AS ?rel)
  } UNION {
    ?seed p:P749 ?st . ?st ps:P749 ?tgt .
    BIND(?seed AS ?src) BIND("parent" AS ?rel)
  } UNION {
    ?x p:P355 ?st . ?st ps:P355 ?seed .
    BIND(?x AS ?src) BIND(?seed AS ?tgt) BIND("owner" AS ?rel)
  } UNION {
    ?x p:P127 ?st . ?st ps:P127 ?seed .
    BIND(?x AS ?src) BIND(?seed AS ?tgt) BIND("owned_by_rev" AS ?rel)
  } UNION {
    ?x p:P749 ?st . ?st ps:P749 ?seed .
    BIND(?x AS ?src) BIND(?seed AS ?tgt) BIND("parent_rev" AS ?rel)
  }
  OPTIONAL { ?st pq:P580 ?from . }
  OPTIONAL { ?st pq:P582 ?to . }
}"""


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _wave_edges(qids: list, timeout_s: int = 90) -> tuple:
    """All typed edges touching the given QIDs (both directions). -> (edges, neighbor_qids)"""
    values = " ".join(f"wd:{q}" for q in qids)
    query = _WAVE_QUERY.replace("%SEEDS%", values)
    raw = _sparql_query(query, timeout_s=timeout_s)
    edges, neighbors = [], set()
    for row in raw:
        src, tgt = _qid(row.get("src", "")), _qid(row.get("tgt", ""))
        rel = row.get("rel", "")
        if not src or not tgt or src == tgt:
            continue
        edges.append({
            "source_qid": src,
            "target_qid": tgt,
            "relation": rel,
            "valid_from": (row.get("from", "") or "")[:10],
            "valid_to": (row.get("to", "") or "")[:10],
        })
        neighbors.update((src, tgt))
    return edges, neighbors


def crawl_waves(seed_qids: list) -> tuple:
    """BFS waves outward from seeds; returns (all_edges, visited_qids, wave_log)."""
    visited = set(seed_qids)
    frontier = list(seed_qids)
    all_edges: dict = {}
    wave_log = []
    for depth in range(1, MAX_DEPTH + 1):
        if not frontier or len(visited) >= MAX_NODES:
            break
        nxt = []
        for i in range(0, len(frontier), WAVE_BATCH):
            batch = frontier[i:i + WAVE_BATCH]
            try:
                edges, neighbors = _wave_edges(batch)
            except Exception as exc:  # noqa: BLE001 - one bad batch never kills the crawl
                wave_log.append({"depth": depth, "batch": len(batch),
                                 "error": str(exc)[:120]})
                continue
            new_edges = 0
            for e in edges:
                key = (e["source_qid"], e["target_qid"], e["relation"],
                       e["valid_from"], e["valid_to"])
                if key not in all_edges:
                    all_edges[key] = e
                    new_edges += 1
            fresh = sorted(neighbors - visited)[:WAVE_BATCH * 2]
            visited.update(fresh)
            nxt.extend(fresh)
            wave_log.append({"depth": depth, "batch": len(batch),
                             "edges": new_edges, "fresh": len(fresh)})
            time.sleep(QUERY_GAP_S)
            if len(visited) >= MAX_NODES:
                break
        frontier = [q for q in sorted(set(nxt)) if q not in seed_qids][:MAX_NODES]
    return list(all_edges.values()), visited, wave_log


def main() -> None:
    if os.environ.get("DISCOVERY_LIVE") != "1":
        print(json.dumps({"status": "DEGRADED", "reason": "DISCOVERY_LIVE!=1"}))
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    create_discovery_tables(conn)
    started = int(time.time())
    stats = {"mode": "live_wave"}

    print("[1/5] fetching companies (P249)...", flush=True)
    companies_raw = fetch_companies(timeout_s=120)
    companies, labels = {}, {}
    for c in companies_raw:
        t = (c.get("ticker") or "").strip().upper()
        if t and len(t) <= 10:
            companies[c["qid"]] = t
            labels[c["qid"]] = c.get("label", "")
    stats["companies_fetched"] = len(companies_raw)
    stats["companies_valid_ticker"] = len(companies)

    seed_qids = sorted(q for q, t in companies.items() if t in SEED_TICKERS)
    stats["seed_qids"] = {q: companies[q] for q in seed_qids}
    if not seed_qids:
        stats["status"] = "DEGRADED"
        stats["reason"] = "no seed QIDs resolved from P249 map"
        print(json.dumps(stats, indent=2))
        return

    print("[2/5] crawling waves from seeds...", flush=True)
    edges, visited, wave_log = crawl_waves(seed_qids)
    stats["waves"] = wave_log
    stats["edges_crawled"] = len(edges)
    stats["nodes_visited"] = len(visited)

    print("[3/5] temporal coverage probe...", flush=True)
    try:
        coverage = temporal_coverage_probe(timeout_s=150)
        stats["coverage_global"] = coverage
    except Exception as exc:  # noqa: BLE001 - global aggregate is advisory only
        stats["coverage_global"] = {"status": "DEGRADED", "reason": str(exc)[:120]}
    dated_local = sum(1 for e in edges if e["valid_from"] or e["valid_to"])
    stats["coverage_local"] = {
        "total_edges": len(edges),
        "dated_edges": dated_local,
        "pct_dated": round(100.0 * dated_local / len(edges), 2) if edges else 0.0,
    }
    stats["pit_unlock"] = pit_unlock_check(stats["coverage_local"]["pct_dated"])

    print("[4/5] expanding wiki frontier...", flush=True)
    result = expand_wiki_frontier(
        seed_qids=seed_qids,
        companies=companies,
        edges=edges,
        major_tickers=MAJOR_TICKERS,
    )
    stats["frontier_summary"] = result.summary
    wiki_names = sorted({
        companies[n.qid] for n in result.nodes if n.kind == "company"
    })
    stats["wiki_names"] = wiki_names

    print("[5/5] diffing vs SEC edges + persisting...", flush=True)
    sec_names = set()
    try:
        rows = conn.execute(
            "SELECT DISTINCT target FROM ecosystem_graph_edges"
        ).fetchall()
        sec_names = {r["target"].strip().upper() for r in rows if r["target"]}
    except sqlite3.OperationalError:
        pass
    diff = diff_wiki_sec(set(wiki_names), sec_names)
    stats["diff_summary"] = diff["summary"]
    stats["diff_buckets"] = {k: diff[k] for k in ("sec_only", "wiki_only", "both")}

    now = int(time.time())
    provenance = f"wdqs_wave:{started}"
    conn.executemany(
        "INSERT OR REPLACE INTO wikidata_companies (qid, label, ticker, fetched_at)"
        " VALUES (?, ?, ?, ?)",
        [
            (c["qid"], c.get("label", ""), c["ticker"], now)
            for c in companies_raw
            if (c.get("ticker") or "").strip().upper()
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO wiki_edges (source_qid, target_qid, relation,"
        " valid_from, valid_to, provenance, discovered_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                e["source_qid"], e["target_qid"], e["relation"],
                e["valid_from"], e["valid_to"], provenance, now,
            )
            for e in edges
        ],
    )
    conn.execute(
        "INSERT INTO wiki_runs (kind, started, finished, stats_json)"
        " VALUES (?, ?, ?, ?)",
        ("p1_probe_wave", started, int(time.time()), json.dumps(stats, default=str)),
    )
    conn.commit()

    print(json.dumps(stats, indent=2, default=str))


if __name__ == "__main__":
    main()
