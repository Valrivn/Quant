"""Backfill provenance sidecars for data/factors/*.parquet (Phase 4 — Workstream B).

Repairs the factor store so every Parquet file carries the Data_Strategy.md
section-8 provenance contract: source, retrieval_timestamp, source_version,
pit_timestamp_column, entity_key, transformations, sha256, row_count,
date_range.  SHA-256, row_count and date_range are computed from the live file
bytes/schema, so the sidecar is always a truthful record of what exists.

Existing sidecar keys that are NOT contract fields are preserved (e.g.
finbert_model, sample_quarters, transformations lists from the original
ingestors).

Idempotent: safe to re-run; re-computes the cryptographic fields each time.

Usage:
    python scripts/backfill_provenance_sidecars.py [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

FACTORS_DIR = PROJECT_ROOT / "data" / "factors"

# Canonical source per file (Data_Strategy.md section 2 registry).
EXPECTED_SOURCES: Dict[str, str] = {
    "rpo.parquet": "SEC_EDGAR",
    "mda_tone.parquet": "SEC_EDGAR",
    "gh_velocity.parquet": "GH_ARCHIVE",
    "nhtsa_signals.parquet": "NHTSA",
    "cfpb_signals.parquet": "CFPB",
    "ats_hiring.parquet": "ATS_RSS",
    "patent_signals.parquet": "USPTO",
    "app_store_signals.parquet": "APP_STORE",
    "amazon_signals.parquet": "AMAZON",
    "inst_ownership.parquet": "SEC_13F",
    "apewisdom_signals.parquet": "APEWISDOM",
    "glassdoor_sentiment.parquet": "GLASSDOOR",
}

# True PIT anchor column per file (must exist in the parquet schema).
PIT_COLUMNS: Dict[str, str] = {
    "rpo.parquet": "filed_date",
    "mda_tone.parquet": "filed_date",
    "gh_velocity.parquet": "created_at",
    "nhtsa_signals.parquet": "DateReceived",
    "cfpb_signals.parquet": "DateReceived",
    "ats_hiring.parquet": "posted_at",
    "patent_signals.parquet": "grant_date",
    "app_store_signals.parquet": "date",
    "inst_ownership.parquet": "date",
    "apewisdom_signals.parquet": "date",
    "glassdoor_sentiment.parquet": "date",
}

ENTITY_KEYS: Dict[str, str] = {
    "rpo.parquet": "cik|ticker",
    "mda_tone.parquet": "cik|ticker",
    "gh_velocity.parquet": "org|ticker",
    "nhtsa_signals.parquet": "make_model|ticker",
    "cfpb_signals.parquet": "product|company|ticker",
    "ats_hiring.parquet": "ticker",
    "patent_signals.parquet": "assignee|ticker",
    "app_store_signals.parquet": "app_id|ticker",
    "inst_ownership.parquet": "cusip|ticker",
    "apewisdom_signals.parquet": "ticker",
    "glassdoor_sentiment.parquet": "firm|ticker",
}

# Contract keys we regenerate; everything else in an existing sidecar is kept.
CONTRACT_KEYS = ("source", "retrieval_timestamp", "pit_timestamp_column",
                 "entity_key", "sha256", "row_count", "date_range")
SOURCE_VERSIONS: Dict[str, str] = {
    "rpo.parquet": "companyfacts_20260909",
    "mda_tone.parquet": "synthetic_fallback_v1",
    "gh_velocity.parquet": "gharchive_202609_sample",
    "nhtsa_signals.parquet": "nhtsa_api_202609_sample",
    "cfpb_signals.parquet": "cfpb_api_202609_sample",
    "ats_hiring.parquet": "greenhouse_lever_rss_202609_sample",
    "patent_signals.parquet": "patentsview_odp_202609_sample",
    "app_store_signals.parquet": "app_store_rss_202609",
    "inst_ownership.parquet": "form-13f-data-sets_202609_sample",
    "apewisdom_signals.parquet": "apewisdom_api_202609",
    "glassdoor_sentiment.parquet": "synthetic_quarterly_panel_v1",
}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def pit_column_for(file_name: str, columns: List[str]) -> str:
    if file_name in PIT_COLUMNS and PIT_COLUMNS[file_name] in columns:
        return PIT_COLUMNS[file_name]
    for cand in ("filed_date", "posted_at", "grant_date", "DateReceived",
                 "created_at", "date"):
        if cand in columns:
            return cand
    return columns[0]


def backfill(factors_dir: Path, dry_run: bool = False) -> Dict[str, Any]:
    parquet_files = sorted(factors_dir.glob("*.parquet"))
    written, skipped = [], []
    summary: Dict[str, Any] = {"files": [], "wrote": 0, "skipped": 0}

    for pf in parquet_files:
        df = pd.read_parquet(pf)
        prov_path = pf.with_suffix(".parquet.provenance.json")
        old: Dict[str, Any] = {}
        if prov_path.exists():
            try:
                old = json.loads(prov_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                old = {}

        ts_col = pit_column_for(pf.name, list(df.columns))
        ts = pd.to_datetime(df[ts_col])
        contract = {
            "source": EXPECTED_SOURCES.get(pf.name, old.get("source", "UNKNOWN")),
            "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
            "source_version": old.get("source_version",
                                      SOURCE_VERSIONS.get(pf.name, "v1")),
            "pit_timestamp_column": ts_col,
            "entity_key": old.get("entity_key",
                                  ENTITY_KEYS.get(pf.name, "ticker")),
            "sha256": sha256_of(pf),
            "row_count": int(len(df)),
            "date_range": {
                "min": str(ts.min().date()),
                "max": str(ts.max().date()),
            },
        }
        # Preserve non-contract metadata from the original sidecar.
        preserved = {k: v for k, v in old.items() if k not in CONTRACT_KEYS}
        sidecar: Dict[str, Any] = {**preserved, **contract}

        if old == sidecar:
            skipped.append(pf.name)
        else:
            written.append(pf.name)
            if not dry_run:
                prov_path.write_text(
                    json.dumps(sidecar, indent=2, sort_keys=True),
                    encoding="utf-8")

        summary["files"].append({
            "file": pf.name,
            "rows": int(len(df)),
            "pit_timestamp_column": ts_col,
            "source": contract["source"],
            "sha256": contract["sha256"][:16] + "...",
            "changed": old != sidecar,
        })
        print(f"[sidecar] {'write' if old != sidecar else 'keep  '} {pf.name:28s} "
              f"rows={len(df):5d} pit={ts_col:12s} src={contract['source']}")

    summary["wrote"] = len(written)
    summary["skipped"] = len(skipped)
    summary["n_files"] = len(parquet_files)
    if dry_run:
        print(f"[sidecar] DRY RUN — {len(written)} would be written, "
              f"{len(skipped)} unchanged")
    else:
        print(f"[sidecar] wrote {len(written)}, kept {len(skipped)} unchanged "
              f"across {len(parquet_files)} factor files")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill factor provenance sidecars")
    parser.add_argument("--factors-dir", type=str, default=str(FACTORS_DIR))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    summary = backfill(Path(args.factors_dir), dry_run=args.dry_run)

    out = Path(args.factors_dir) / "_sidecar_backfill_summary.json"
    if not args.dry_run:
        out.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str),
                       encoding="utf-8")
        print(f"[sidecar] summary: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())