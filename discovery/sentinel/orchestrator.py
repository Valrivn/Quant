"""Sentinel orchestrator — drives the queue through the funnel lanes.

Single entry points:
  - ``run_pass``            — claims a queue batch and runs G1/G2/G3/G4.
  - ``run_ig_harvest``      — single-account IG scrape (fail-closed on
                              cooldown/circuit/challenge), enqueues tickers.
  - ``run_sec_sync``        — bulk quarterly + per-CIK fundamentals sync.
"""

import logging
import sqlite3
import time
from typing import Dict, List, Optional

import pandas as pd

from discovery.sentinel import (
    altdata_lane, enrich_lane, github_lane, governor, queue as q, sec_lane,
)
from discovery.sentinel.gates import pit_filter, run_gates

logger = logging.getLogger(__name__)


def as_of_date(cfg: Dict) -> str:
    drift = int(cfg.get("as_of_drift_days", 5))
    return (pd.Timestamp.utcnow() - pd.Timedelta(days=drift)).strftime("%Y-%m-%d")


def _ensure_fundamentals(conn, item, cfg, cik_resolver) -> int:
    """Fetch missing fundamentals for a ticker via per-CIK fallback. Returns rows added."""
    existing = q.get_fundamentals(conn, item["ticker"])
    if existing:
        return 0
    cik = cik_resolver(item["ticker"])
    if not cik:
        return 0
    sec = cfg["lanes"]["sec"]
    if not governor.circuit_allow(conn, "sec",
                                  cfg["governor"]["circuit_failure_threshold"],
                                  cfg["governor"]["circuit_success_threshold"],
                                  cfg["governor"]["circuit_timeout_seconds"]):
        return 0
    try:
        if not governor.throttle(conn, "sec", sec["rate_per_second"], sec["burst"]):
            return 0
        added = sec_lane.sync_per_cik_fallback(
            conn, [item["ticker"]], cik_resolver, cfg)
        governor.record_success(conn, "sec")
        return added
    except Exception as exc:  # fail closed on any lane error
        governor.record_failure(conn, "sec")
        logger.warning("sec lane failure for %s: %s", item["ticker"], exc)
        return 0


def process_item(conn, reddit_conn, item, cfg, cik_resolver) -> Dict:
    """Run the funnel for one queue item; returns {gate: verdict}.

    ``cfg`` is the ``sentinel`` sub-config.
    """
    ticker = item["ticker"]
    verdicts = {}

    _ensure_fundamentals(conn, item, cfg, cik_resolver)
    fund = q.get_fundamentals(conn, ticker)
    fund_df = pd.DataFrame([dict(r) for r in fund]) if fund else pd.DataFrame()

    for gv in run_gates(fund_df, as_of_date(cfg), cfg):
        q.record_funnel(conn, ticker, item["source"], gv["gate"],
                        gv["passed"], gv["reason"], gv["metrics"])
        verdicts[gv["gate"]] = gv

    g3 = altdata_lane.g3_altdata(reddit_conn, conn, ticker, cfg)
    g3_verdict = {"gate": "g3_altdata", "passed": g3[0], "reason": g3[1], "metrics": g3[2]}
    q.record_funnel(conn, ticker, item["source"], "g3_altdata",
                    g3[0], g3[1], g3[2])
    verdicts["g3_altdata"] = g3_verdict

    targets = enrich_lane.default_targets(ticker, _companies())
    if targets:
        g4_scores = enrich_lane.enrich_ticker(conn, ticker, targets, cfg)
        q.record_funnel(conn, ticker, item["source"], "g4_enrich",
                        True, "advisory", {"scores": g4_scores})
    return verdicts


_companies_cache: Optional[Dict] = None


def _companies() -> Dict:
    global _companies_cache
    if _companies_cache is None:
        from config import load_hybrid_config
        _companies_cache = load_hybrid_config().get("companies", {})
    return _companies_cache


def _item_passed(verdicts: Dict) -> Optional[str]:
    for gate in ("g1_survival", "g2_fundamentals", "g3_altdata"):
        gv = verdicts.get(gate)
        if gv is None:
            return f"{gate}:not_run"
        if not gv["passed"]:
            return gv.get("reason") or f"{gate}:failed"
    return None


def run_pass(conn, reddit_conn, cfg, cik_resolver, batch_limit: Optional[int] = None) -> Dict:
    """Process a batch of pending queue items. ``cfg`` is the sentinel sub-config."""
    qcfg = cfg["queue"]
    run_id = q.start_run(conn, "pass")
    processed = passed = failed = 0
    batches = batch_limit or 1
    try:
        for _ in range(batches):
            items = q.claim_batch(conn, qcfg["batch_size"], qcfg["max_attempts"])
            if not items:
                break
            for item in items:
                processed += 1
                verdicts = process_item(conn, reddit_conn, item, cfg, cik_resolver)
                reason = _item_passed(verdicts)
                if reason is None:
                    q.mark(conn, item["id"], q.STAGE_PASSED)
                    passed += 1
                else:
                    q.mark(conn, item["id"], q.STAGE_FAILED, error=reason)
                    failed += 1
        q.end_run(conn, run_id, processed, passed, failed, "done")
    except Exception as exc:
        q.end_run(conn, run_id, processed, passed, failed, "error", str(exc))
        raise
    return {"processed": processed, "passed": passed, "failed": failed}


def _derive_ig_record(row: Dict) -> tuple:
    """Stable unique id + shortcode for an IG mention row (dedupe-safe).

    ``to_mention_row`` emits ``id``/``shortcode`` directly, but this guard keeps
    any other producer from writing NULL ids: SQLite treats NULL as distinct in
    a PRIMARY KEY, so a NULL id silently defeats INSERT OR IGNORE and the same
    reel gets re-inserted on every harvest pass.
    """
    ticker = (row.get("entity") or "").strip() or "UNKNOWN"
    ext_id = row.get("external_id") or ""
    parts = [p for p in ext_id.split("/") if p]
    shortcode = parts[-1] if parts else (row.get("shortcode") or "")
    if shortcode:
        return "{}_".format(ticker) + str(shortcode), str(shortcode)
    row_id = row.get("id")
    if row_id:
        return "{}_".format(ticker) + str(row_id), str(row_id)
    import random as _random
    import time as _time
    return "{}_{}_{}".format(ticker, int(_time.time()), _random.randint(1000, 9999)), ""


def _save_raw_mention(conn: sqlite3.Connection, row: Dict) -> None:
    record_id, shortcode = _derive_ig_record(row)
    conn.execute(
        """INSERT OR IGNORE INTO instagram_raw_mentions
           (id, ticker, shortcode, caption, sentiment,
            finbert_label, finbert_sentiment, finbert_confidence,
            views, comments, followers, verified, fetch_ts, external_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row.get("id") or record_id, row.get("entity"), shortcode,
            row.get("caption"), row.get("sentiment"),
            row.get("finbert_label"), row.get("finbert_sentiment"),
            row.get("finbert_confidence"),
            row.get("views"), row.get("comments"), row.get("followers"),
            row.get("verified"), row.get("fetch_ts"), row.get("external_id"),
        ),
    )
    conn.commit()


def run_ig_harvest(conn, reddit_conn, cfg, limit: int = 30) -> Dict:
    """One IG harvest pass using the existing single cookie session (D-20260809-003)."""
    ig = cfg["lanes"]["ig"]
    gov = cfg["governor"]
    run_id = q.start_run(conn, "ig_harvest")
    account_key = "ig:single"
    if governor.cooldown_blocked(conn, account_key):
        q.end_run(conn, run_id, 0, 0, 0, "cooldown")
        return {"skipped": "cooldown"}

    if not governor.circuit_allow(conn, "ig",
                                  gov["circuit_failure_threshold"],
                                  gov["circuit_success_threshold"],
                                  gov["circuit_timeout_seconds"]):
        q.end_run(conn, run_id, 0, 0, 0, "circuit_open")
        return {"skipped": "circuit_open"}

    if not governor.throttle(conn, "ig", ig["rate_per_second"], ig["burst"]):
        q.end_run(conn, run_id, 0, 0, 0, "throttled")
        return {"skipped": "throttled"}

    try:
        from Qualitative.psychological.scrapers.instagram_primary import (
            InstagramChallengeDetected, InstagramConfig, fetch_instagram_mentions,
        )
        cfg_ig = InstagramConfig({
            "max_pages_per_session": int(ig.get("max_pages_per_session", 10)),
            "session_cool_down_seconds": float(ig.get("session_cool_down_seconds", 300)),
        })
        mentions = fetch_instagram_mentions(limit=limit, config=cfg_ig)
    except InstagramChallengeDetected:
        governor.record_failure(conn, "ig")
        governor.cooldown_set(conn, account_key,
                              int(time.time()) + int(ig.get("challenge_cooldown_seconds", 900)),
                              "challenge_detected")
        q.end_run(conn, run_id, 0, 0, 0, "challenge")
        return {"skipped": "challenge_detected"}
    except Exception as exc:
        governor.record_failure(conn, "ig")
        q.end_run(conn, run_id, 0, 0, 0, "error", str(exc))
        return {"skipped": "error", "error": str(exc)}

    governor.record_success(conn, "ig")
    enqueued = 0
    for row in mentions:
        _save_raw_mention(reddit_conn, row)
        ticker = row.get("entity")
        skey = row.get("external_id") or row.get("id") or ""
        if not ticker or not skey:
            continue
        if q.enqueue(conn, ticker, "ig", skey, raw_json=row):
            enqueued += 1
    q.end_run(conn, run_id, len(mentions), enqueued, 0, "done")
    return {"mentions": len(mentions), "enqueued": enqueued}


def run_sec_sync(conn, cfg, tickers: List[str], cik_resolver,
                 start_year: Optional[int] = None, years_back: int = 3) -> Dict:
    run_id = q.start_run(conn, "sec_sync")
    try:
        counts = sec_lane.sync_lane(conn, tickers, cik_resolver, cfg,
                                    start_year=start_year, years_back=years_back)
        q.end_run(conn, run_id, counts["bulk"] + counts["fallback"],
                  counts["bulk"], counts["fallback"], "done")
        return counts
    except Exception as exc:
        q.end_run(conn, run_id, 0, 0, 0, "error", str(exc))
        raise


def run_github_sync(conn, cfg, tickers: List[str], token: Optional[str] = None) -> Dict:
    """One GitHub star snapshot pass over the roster (G3 second source)."""
    run_id = q.start_run(conn, "github_sync")
    try:
        counts = github_lane.sync_github_snapshots(conn, tickers, cfg, token=token)
        q.end_run(conn, run_id, counts["mapped"], counts["snapshotted"], counts["error"], "done")
        return counts
    except Exception as exc:
        q.end_run(conn, run_id, 0, 0, 0, "error", str(exc))
        raise


def _load_edge_maps() -> Dict:
    """Load pre-registered edge maps from config/frontier_edges.yaml (optional).

    The file is optional: when absent or unreadable the frontier expands from
    whatever edges are already persisted in ecosystem_graph_edges. Never
    fabricates edges (honest empty graph > invented relationships).
    """
    import os
    import yaml

    path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "frontier_edges.yaml")
    if not os.path.exists(path):
        return {"customer_to_suppliers": {}, "supplier_to_suppliers": {}}
    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}
    return {
        "customer_to_suppliers": data.get("customer_to_suppliers") or {},
        "supplier_to_suppliers": data.get("supplier_to_suppliers") or {},
    }


def run_frontier_expansion(conn, cfg, cik_resolver, edge_maps: Optional[Dict] = None) -> Dict:
    """One overlap-graded frontier expansion (B-20260819-001).

    Slow by design: the engine runs deterministically over the supplied edge
    maps; newly discovered nodes are enqueued (source='frontier') so they flow
    through the G1-G4 funnel under the existing governor. Nothing here invents
    edges — ``edge_maps`` come from persisted graph rows + the optional
    pre-registered config file, both CIK-filtered before enqueue.
    """
    from discovery.frontier import expand_frontier, normalize_tickers

    fr = cfg["frontier"]
    maps = dict(edge_maps or _load_edge_maps())
    cust = maps.get("customer_to_suppliers") or {}
    supp = maps.get("supplier_to_suppliers") or {}

    # Merge any edges already persisted by prior runs.
    try:
        rows = conn.execute(
            "SELECT source, target FROM ecosystem_graph_edges WHERE relation='customer'"
        ).fetchall()
        for r in rows:
            cust.setdefault(r["source"], []).append(r["target"])
    except Exception:  # noqa: BLE001 - graph table may not exist yet
        pass

    # ETF weights feed the grading denominator (cached, quarterly).
    relevance: Dict[str, float] = {}
    try:
        from discovery.etf_weights import get_weights
        weights = get_weights(conn, fr.get("etf_denominator", "QQQ"))
        relevance = {t: float(w) for t, w in weights.items()}
    except Exception:  # noqa: BLE001 - no weights -> equal-weight grading
        pass

    run_id = q.start_run(conn, "frontier_expansion")
    try:
        result = expand_frontier(
            seed_tickers=fr["seed_tickers"],
            competitor_set=fr["competitor_set"],
            customer_to_suppliers=cust,
            supplier_to_suppliers=supp,
            major_set=fr.get("major_set"),
            relevance=relevance or None,
            max_depth=int(fr["max_depth"]),
            max_nodes=int(fr["max_nodes_per_seed"]),
            max_edges_per_node=int(fr["max_edges_per_node"]),
        )
    except Exception as exc:
        q.end_run(conn, run_id, 0, 0, 0, "error", str(exc))
        raise

    now_ts = int(time.time())
    node_count = edge_count = enqueued = 0
    for n in result.nodes:
        if n.depth < 1:  # seeds + competitors are the anchor, not discoveries
            continue
        cik = None
        try:
            cik = cik_resolver(n.ticker)
        except Exception:  # noqa: BLE001 - no CIK -> node still recorded without cik
            pass
        if cik is None:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO ecosystem_graph_nodes
               (ticker, cik, depth, grade, seed, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (n.ticker, cik, n.depth, n.grade, fr["seed_tickers"][0], now_ts, now_ts),
        )
        node_count += 1
        if q.enqueue(conn, n.ticker, "frontier", f"frontier:{n.ticker}"):
            enqueued += 1

    for e in result.edges:
        conn.execute(
            """INSERT OR REPLACE INTO ecosystem_graph_edges
               (source, target, relation, confidence, filed_date, provenance, discovered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (e.source, e.target, e.relation, e.confidence, e.filed_date,
             e.provenance, now_ts),
        )
        edge_count += 1
    conn.commit()
    q.end_run(conn, run_id, node_count, enqueued, 0, "done")
    return {
        "nodes": node_count,
        "edges": edge_count,
        "enqueued": enqueued,
        "summary": result.summary,
    }
