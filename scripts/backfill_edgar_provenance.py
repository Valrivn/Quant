"""Backfill SEC EDGAR/XBRL provenance metadata into ingested filing JSONs.

Scans ``data/edgar_xbrl_ingestion/`` and adds a ``provenance`` object to each
filing JSON (and provenance metadata to ``_ingestion_summary.json``) based on
the standard SEC EDGAR URL patterns. Used to repair files written before the
pipeline persisted provenance (Workstream B).

Idempotent: files that already carry a complete ``provenance`` object are
skipped, so re-running is safe.

Usage:
    python scripts/backfill_edgar_provenance.py [--dry-run]
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("backfill_edgar_provenance")

OUTPUT_DIR = PROJECT_ROOT / "data" / "edgar_xbrl_ingestion"
SUMMARY_FILENAME = "_ingestion_summary.json"
CONCEPT_MAP_FILENAME = "_concept_chain_map.json"

PARSER_VERSION = "sector_xbrl_chains_v2.1"
XBRL_TAXONOMY = "US-GAAP 2024"
SOURCE_NAME = "SEC_EDGAR"

# Keys that must all be present for a provenance object to count as complete.
REQUIRED_PROVENANCE_KEYS = (
    "source",
    "submissions_endpoint",
    "companyfacts_endpoint",
    "parser_version",
    "xbrl_taxonomy",
)


def submissions_endpoint(cik: str) -> str:
    """SEC EDGAR submissions endpoint URL for a CIK."""
    return f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"


def companyfacts_endpoint(cik: str) -> str:
    """SEC EDGAR companyfacts XBRL endpoint URL for a CIK."""
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json"


def filing_document_url(cik: str, accession: str) -> str:
    """Best-effort reconstruction of the filing document URL.

    The live pipeline stores the primary_document file URL, but that field was
    not persisted before provenance existed. The EDGAR filing index URL below
    is unambiguously reconstructable from CIK + accession and resolves to the
    filing's document landing page.
    """
    cik_padded = cik.zfill(10)
    accn_dashed = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_padded}/{accn_dashed}/"


def build_provenance(data: Dict[str, Any], fetched_at: str) -> Dict[str, Any]:
    """Build the provenance object for a filing document (matches pipeline output)."""
    cik = str(data.get("cik", "")).zfill(10)
    accession = data.get("accession", "")
    return {
        "source": SOURCE_NAME,
        "submissions_endpoint": submissions_endpoint(cik),
        "companyfacts_endpoint": companyfacts_endpoint(cik),
        "filing_document_url": filing_document_url(cik, accession),
        "fetched_at": fetched_at,
        "parser_version": PARSER_VERSION,
        "xbrl_taxonomy": XBRL_TAXONOMY,
        "cik": cik,
        "accession": accession,
        "form": data.get("form", ""),
        "filing_date": data.get("filed", ""),
    }


def has_complete_provenance(data: Dict[str, Any]) -> bool:
    """True if the filing JSON already carries a complete provenance object."""
    prov = data.get("provenance")
    if not isinstance(prov, dict):
        return False
    return all(k in prov and prov[k] for k in REQUIRED_PROVENANCE_KEYS)


def backfill_edgar_provenance(dry_run: bool = False) -> Dict[str, Any]:
    """Backfill provenance into filing JSONs and the ingestion summary.

    Returns a report dict (also used for the CLI printout). Never touches
    concept-chain map or any file outside data/edgar_xbrl_ingestion.
    """
    if not OUTPUT_DIR.is_dir():
        raise FileNotFoundError(f"EDGAR output directory not found: {OUTPUT_DIR}")

    # Reference fetch timestamp: prefer the run completion of the stored
    # ingestion summary (the moment the raw data was pulled from SEC).
    summary_path = OUTPUT_DIR / SUMMARY_FILENAME
    summary_fetched_at = ""
    if summary_path.exists():
        try:
            with open(summary_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            summary_fetched_at = existing.get("completed_at", "")
        except Exception as exc:
            logger.warning("Could not read %s: %s", SUMMARY_FILENAME, exc)

    if not summary_fetched_at:
        summary_fetched_at = datetime.now(timezone.utc).isoformat()

    filing_files: List[Path] = sorted(
        p for p in OUTPUT_DIR.glob("*.json")
        if p.name not in (SUMMARY_FILENAME, CONCEPT_MAP_FILENAME) and not p.name.startswith("_")
    )

    updated = 0
    skipped = 0
    failed: List[str] = []

    for filepath in filing_files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            if has_complete_provenance(data):
                skipped += 1
                continue

            data["provenance"] = build_provenance(data, summary_fetched_at)

            if not dry_run:
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            updated += 1
        except Exception as exc:
            failed.append(f"{filepath.name}: {exc}")
            logger.error("Failed to backfill %s: %s", filepath.name, exc)

    # -- Update _ingestion_summary.json with provenance metadata --------------
    summary_meta = {
        "source": SOURCE_NAME,
        "submissions_endpoint": "https://data.sec.gov/submissions/CIK{cik_padded}.json",
        "companyfacts_endpoint": "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json",
        "parser_version": PARSER_VERSION,
        "xbrl_taxonomy": XBRL_TAXONOMY,
        "files_processed": len(filing_files),
        "files_updated": updated,
        "files_already_had_provenance": skipped,
        "files_failed": len(failed),
        "backfilled_at": datetime.now(timezone.utc).isoformat(),
    }

    # Only (re)write the summary block when this run changed files, so
    # idempotent re-runs preserve the original backfill record instead of
    # overwriting it with a "0 updated" snapshot.
    preserve_existing = False
    if updated == 0 and summary_path.exists():
        with open(summary_path, "r", encoding="utf-8") as f:
            summary = json.load(f)
        if summary.get("provenance"):
            preserve_existing = True
            summary_meta = summary["provenance"]

    if not dry_run and not preserve_existing:
        if summary_path.exists():
            with open(summary_path, "r", encoding="utf-8") as f:
                summary = json.load(f)
        else:
            summary = {}
        summary["provenance"] = summary_meta
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

    logger.info(
        "Backfill complete: %d updated, %d already had provenance, %d failed, %d files scanned (dry_run=%s)",
        updated, skipped, len(failed), len(filing_files), dry_run,
    )
    return {
        "status": "SUCCESS" if not failed else "PARTIAL",
        "dry_run": dry_run,
        "files_scanned": len(filing_files),
        "files_updated": updated,
        "files_already_had_provenance": skipped,
        "files_failed": len(failed),
        "errors": failed[:10],
        "summary_provenance": summary_meta,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
    args = parser.parse_args()

    report = backfill_edgar_provenance(dry_run=args.dry_run)
    if not args.dry_run:
        print("\n" + "=" * 60)
        print("EDGAR PROVENANCE BACKFILL")
        print("=" * 60)
        print(f"  Status:              {report['status']}")
        print(f"  Files scanned:       {report['files_scanned']}")
        print(f"  Files updated:       {report['files_updated']}")
        print(f"  Already had:         {report['files_already_had_provenance']}")
        print(f"  Failed:              {report['files_failed']}")
        print("=" * 60)