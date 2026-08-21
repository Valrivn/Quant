"""Track 1 prospective freeze for D-20260820-001 (B-20260820-001).

Exports the discovery state as-of the latest wiki_runs entry into an
immutable freeze file (JSON + SHA-256). The freeze is the pre-registration
anchor: evaluation bars live in
.agents/project/org/research/wiki-track1-prospective-freeze.md and are scored
ONLY at the pre-registered evaluation dates against this exact cohort.
"""

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sentinel.db"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "wiki_track1_freeze_20260820.json"

SEEDS = ["NVDA", "AMD", "AVGO", "MSFT", "GOOGL", "META", "AMZN", "AAPL", "TSM", "ASML"]


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    run = conn.execute(
        "SELECT run_id, started, finished, stats_json FROM wiki_runs"
        " WHERE kind='p1_probe_wave' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if run is None:
        print("no p1_probe_wave run found; nothing to freeze")
        return

    companies = {
        r["qid"]: {"label": r["label"], "ticker": r["ticker"], "fetched_at": r["fetched_at"]}
        for r in conn.execute("SELECT * FROM wikidata_companies")
    }
    edges = [
        dict(r)
        for r in conn.execute(
            "SELECT source_qid, target_qid, relation, valid_from, valid_to,"
            " provenance, discovered_at FROM wiki_edges"
        )
    ]

    stats = json.loads(run["stats_json"])
    cohort = sorted(set(stats.get("wiki_names") or []))
    seed_set = set(SEEDS)

    freeze = {
        "freeze_id": "WIKI-T1-20260820",
        "rule": "D-20260820-001 Track 1 prospective validation",
        "discovered_at_utc": run["finished"],
        "crawl_run_id": run["run_id"],
        "seeds": SEEDS,
        "cohort_all": cohort,
        "cohort_nonseed": [t for t in cohort if t not in seed_set],
        "cohort_seeds": [t for t in cohort if t in seed_set],
        "edges_count": len(edges),
        "companies_in_db": len(companies),
        "pit_coverage_pct": (stats.get("coverage_local") or {}).get("pct_dated"),
        "known_stale_from_prior_audit": ["ATY", "BRCM", "MLNX", "VMW", "WFM", "TIT"],
        "note": "stale flags are informational; tradability is re-derived mechanically at each evaluation date from price availability",
        "edges": edges,
    }

    payload = json.dumps(freeze, indent=2, sort_keys=True)
    OUT_PATH.write_text(payload, encoding="utf-8")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    print(f"frozen: {OUT_PATH}")
    print(f"sha256: {digest}")
    print(f"cohort_all ({len(cohort)}): {cohort}")
    print(f"non-seed ({len(freeze['cohort_nonseed'])}): {freeze['cohort_nonseed']}")


if __name__ == "__main__":
    main()
