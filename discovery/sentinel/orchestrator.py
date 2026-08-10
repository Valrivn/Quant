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


def _save_raw_mention(conn: sqlite3.Connection, row: Dict) -> None:
    from datetime import datetime, timezone
    conn.execute(
        """INSERT OR IGNORE INTO instagram_raw_mentions
           (id, ticker, shortcode, caption, sentiment, views, comments, followers,
            verified, fetch_ts, external_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row.get("id"), row.get("entity"), row.get("shortcode") or "",
            row.get("caption"), row.get("sentiment"), row.get("views"),
            row.get("comments"), row.get("followers"), row.get("verified"),
            row.get("fetch_ts"), row.get("external_id"),
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
        _save_raw_mention(conn, row)
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
