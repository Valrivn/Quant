"""Comprehensive §8 provenance manifest backfill — PROV-01/02 (Workstream B).

Builds / refreshes ``data/provenance/manifest.jsonl`` for the **tracked data
estate** (91 canonical Parquet files once the rebuild completes) with the full
Data_Strategy.md §8 schema:

    source, retrieval_timestamp, source_version, pit_timestamp_column,
    entity_key, transformations, sha256, row_count, date_range,
    mapping_confidence

Scope — identical semantics to ``validation.provenance_audit.track_parquets()``:
all ``data/`` Parquet files under the tracked trees

    factors/  qual/  universe/  prices/  provenance/  fred_cache/
    pit_fundamentals/  data_lake/fred/  data_lake/alfred/  source/yfinance/

excluding ``_tests/``, ``cache/``, ``checkpoints/``, ``tmp*`` / ``_*`` dirs and
everything outside the tracked trees (master_prices/, master_dividends/,
archive/, damodaran/, … caches are deliberately out of scope).

Per-file conventions (source / source_version / PIT anchor / entity key /
transformations) are directory-aware with per-file overrides; ``sha256``,
``row_count`` and ``date_range`` are always computed from the live Parquet
bytes/schema so the manifest is a truthful record of what exists:

- ``data/factors/rpo.parquet`` + ``mda_tone.parquet``  -> merge their
  ``.provenance.json`` sidecars (sidecar truth for source/pit/entity key,
  live hashes override).
- ``data/data_lake/fred/*.parquet`` + ``alfred/*.parquet`` -> FRED / ALFRED,
  pit_timestamp_column = date (vintage realtime_start kept in metadata when
  present).
- ``data/pit_fundamentals/`` -> SEC_EDGAR, pit_timestamp_column = filing_date.
- ``data/source/yfinance/*/prices.parquet`` -> YAHOO, pit_timestamp_column =
  date.
- ``data/universe/pit_universe_2014_2024.parquet`` -> legacy, marked
  deprecated (superseded by the 2018-2024 / 2020-2026 PIT universes).
- Qual files carry ``mapping_confidence`` taken from the inline column
  distribution (dominant row-level value) so an over-declared 100% "high"
  reading is visible to the audit.

The manifest is **append-only** (the audit resolves latest-per-path). Re-runs
are idempotent: an entry whose latest existing manifest line already matches
the freshly computed entry byte-for-byte is not re-appended.

CLI::

    python -m scripts.backfill_provenance_manifest --run --data-root data/
    python -m scripts.backfill_provenance_manifest --verify --data-root data/
    python -m scripts.backfill_provenance_manifest --inventory --data-root data/
    python -m scripts.backfill_provenance_manifest --run --dry-run --data-root data/

``--verify`` reconciles manifest vs tree and reports
passed / hash_mismatch / missing_manifest + the estate gap (canonical files
the rebuild has not produced yet). Exit code 0 only when 0 hash mismatches
and 0 files lack a manifest entry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add project root to sys.path (works for `python -m scripts.backfill_...`
# and `python scripts/....py` alike).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pyarrow.parquet as pq  # noqa: E402
import pandas as pd  # noqa: E402

from validation.provenance_audit import (  # noqa: E402  (single source of truth)
    REPO_ROOT,
    _repo_rel,
    load_manifest_latest,
    sha256_file,
    track_parquets,
)

DATA_PREFIX = "data/"
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
MANIFEST_PATH = DEFAULT_DATA_ROOT / "provenance" / "manifest.jsonl"
VERIFY_REPORT_PATH = PROJECT_ROOT / "data" / "validation" / "provenance_manifest_check.json"
BACKFILL_SUMMARY_PATH = PROJECT_ROOT / "data" / "validation" / "provenance_manifest_backfill.json"

# ---------------------------------------------------------------------------
# §8 field order for the canonical flat entry
# ---------------------------------------------------------------------------

CONTRACT_FIELDS: List[str] = [
    "source", "retrieval_timestamp", "source_version", "pit_timestamp_column",
    "entity_key", "transformations", "sha256", "row_count", "date_range",
    "mapping_confidence",
]

# ---------------------------------------------------------------------------
# Directory-aware conventions (tree prefix -> defaults)
# ---------------------------------------------------------------------------

#: Candidate PIT anchor columns in preference order.  The first column that
#: exists in the Parquet schema wins; all candidates are physical columns.
DATE_CANDIDATES: List[str] = [
    "filed_date", "filing_date", "filed", "created_at", "date_received",
    "DateReceived", "grant_date", "review_date", "posted_at", "posted_date",
    "rebalance_date", "delist_filed", "date", "DATE", "realtime_start",
]

TREE_CONVENTIONS: Dict[str, Dict[str, Any]] = {
    "factors/": {
        "source": "SEC_EDGAR",
        "source_version": "companyfacts_20260901",
        "pit_candidates": ["filed_date", "filed", "date"],
        "entity_key": "cik|ticker",
        "transformations": [],
        "mapping_confidence": "high",
        "sidecar_merge": True,
    },
    "qual/": {
        "source": None,  # per-file
        "source_version": "v1.0",
        "pit_candidates": ["created_at", "DateReceived", "grant_date",
                           "posted_at", "review_date", "date"],
        "entity_key": "ticker",
        "transformations": [],
        "mapping_confidence": None,  # from inline column when present
        "inline_confidence": True,
    },
    "universe/": {
        "source": "SEC_EDGAR",
        "source_version": "sec_edgar_universe_20260913",
        "pit_candidates": ["date", "rebalance_date", "delist_filed"],
        "entity_key": "ticker",
        "transformations": ["reconstitution_monthly", "pit_market_cap",
                            "delist_filter", "is_active"],
    },
    "prices/": {
        "source": "YAHOO",
        "source_version": "price_cache_v1.0",
        "pit_candidates": ["date"],
        "entity_key": "ticker",
        "transformations": ["ffill_2d", "price_stale_flag", "multi_source_fill",
                            "synthetic_fill_flag"],
        "mapping_confidence": "high",
    },
    "provenance/": {
        "source": "INTERNAL",
        "source_version": "v1.0",
        "pit_candidates": ["date"],
        "entity_key": "ticker",
        "transformations": ["qual_coverage_matrix"],
    },
    "fred_cache/": {
        "source": "FRED",
        "source_version": "ff5_daily_202609",
        "pit_candidates": ["date", "DATE"],
        "entity_key": "market",
        "transformations": ["ken_french_ff5_daily"],
    },
    "pit_fundamentals/": {
        "source": "SEC_EDGAR",
        "source_version": "companyfacts_20260901",
        "pit_candidates": ["filing_date", "filed", "date"],
        "entity_key": "cik|ticker",
        "transformations": ["xbrl_companyfacts", "pit_fundamentals"],
        "mapping_confidence": "high",
    },
    "data_lake/fred/": {
        "source": "FRED",
        "source_version": "fred_api_202609",
        "pit_candidates": ["date", "DATE", "realtime_start"],
        "entity_key": "series_id",
        "transformations": ["fred_series_observations"],
    },
    "data_lake/alfred/": {
        "source": "ALFRED",
        "source_version": "alfred_vintage_202609",
        "pit_candidates": ["date", "DATE", "realtime_start"],
        "entity_key": "series_id",
        "transformations": ["alfred_vintage_batched_x10"],
    },
    "source/yfinance/": {
        "source": "YAHOO",
        "source_version": "yfinance_202609",
        "pit_candidates": ["date", "Date", "DATE"],
        "entity_key": "ticker",
        "transformations": ["price_fetch"],
    },
}

# ---------------------------------------------------------------------------
# Per-file overrides (key = tree-relative path, e.g. "qual/gh_velocity.parquet")
# ---------------------------------------------------------------------------

FILE_CONVENTIONS: Dict[str, Dict[str, Any]] = {
    # ---- qual estate ------------------------------------------------------
    "qual/gh_velocity.parquet": {
        "source": "GH_ARCHIVE", "source_version": "gharchive_202609",
        "pit_candidates": ["created_at", "date"],
        "entity_key": "org|ticker",
        "transformations": ["velocity_90d", "org_ticker_mapping"],
    },
    "qual/nhtsa_signals.parquet": {
        "source": "NHTSA", "source_version": "nhtsa_api_202609",
        "pit_candidates": ["DateReceived", "created_at", "date"],
        "entity_key": "make_model|ticker",
        "transformations": ["complaints_90d", "recalls_90d",
                            "severity_score", "make_model_ticker_mapping"],
    },
    "qual/cfpb_velocity.parquet": {
        "source": "CFPB", "source_version": "cfpb_api_202609",
        "pit_candidates": ["DateReceived", "date"],
        "entity_key": "product|company|ticker",
        "transformations": ["complaints_90d_velocity", "company_ticker_mapping"],
    },
    "qual/ats_mapping.parquet": {
        "source": "SEC_EDGAR", "source_version": "sec_10k_career_urls_202609",
        "pit_candidates": ["source_filing_date", "date", "retrieval_timestamp"],
        "entity_key": "token|ticker",
        "transformations": ["sec_10k_career_url_extract", "greenhouse_lever_token_map"],
        "mapping_confidence": "medium",
    },
    "qual/ats_velocity.parquet": {
        "source": "ATS_RSS", "source_version": "greenhouse_lever_rss_202609",
        "pit_candidates": ["posted_at", "posted_date", "date"],
        "entity_key": "token|ticker",
        "transformations": ["job_posting_velocity_90d"],
    },
    "qual/patents_signals.parquet": {
        "source": "USPTO", "source_version": "patentsview_odp_202609",
        "pit_candidates": ["grant_date", "date"],
        "entity_key": "assignee|ticker",
        "transformations": ["citations_1y", "grants_1y", "small_entity_flag",
                            "assignee_ticker_mapping"],
    },
    "qual/app_store_velocity.parquet": {
        "source": "APP_STORE", "source_version": "app_store_rss_202609",
        "pit_candidates": ["date", "review_date"],
        "entity_key": "app_id|brand|ticker",
        "transformations": ["review_velocity_90d", "app_title_ticker_mapping"],
    },
    "qual/amazon_reviews.parquet": {
        "source": "AMAZON", "source_version": "hf_mcauley_2023",
        "pit_candidates": ["review_date", "date"],
        "entity_key": "brand|ticker",
        "transformations": ["review_sentiment", "review_velocity_90d",
                            "brand_ticker_mapping"],
    },
    "qual/inst_ownership.parquet": {
        "source": "SEC_13F", "source_version": "form-13f-data-sets_202609",
        "pit_candidates": ["filed_date", "date"],
        "entity_key": "cusip|ticker",
        "transformations": ["13f_holdings_panel", "institutional_ownership"],
    },
    "qual/apewisdom_mentions.parquet": {
        "source": "APEWISDOM", "source_version": "apewisdom_api_202609",
        "pit_candidates": ["posted_date", "date", "created_at"],
        "entity_key": "ticker",
        "transformations": ["mention_volume", "weekly_top_500"],
    },
    # ---- universe estate --------------------------------------------------
    "universe/delisting_events.parquet": {
        "source": "SEC_EDGAR", "source_version": "sec_edgar_8k_delist_1.0",
        "pit_candidates": ["delist_filed"],
        "entity_key": "cik|ticker",
        "transformations": ["sec_8k_delist_events"],
    },
    "universe/pit_universe_2018_2024.parquet": {
        "source": "SEC_EDGAR", "source_version": "sec_edgar_universe_20260913",
        "pit_candidates": ["date"],
        "entity_key": "ticker",
        "transformations": ["reconstitution_monthly", "pit_market_cap",
                            "delist_filter", "is_active", "price_stale_flag",
                            "synthetic_shares_flag"],
    },
    "universe/pit_universe_2020_2026.parquet": {
        "source": "SEC_EDGAR", "source_version": "sec_edgar_universe_20260913",
        "pit_candidates": ["date"],
        "entity_key": "ticker",
        "transformations": ["reconstitution_monthly", "pit_market_cap",
                            "delist_filter", "is_active", "price_stale_flag",
                            "synthetic_shares_flag", "as_of_projection"],
        "notes": "Extended window: rows beyond the build date are planned "
                 "reconstitutions (as_of_projection), not observed history.",
    },
    "universe/monthly_reconstitution.parquet": {
        "source": "SEC_EDGAR", "source_version": "sec_edgar_universe_20260913",
        "pit_candidates": ["rebalance_date", "reconstitution_month"],
        "entity_key": "ticker",
        "transformations": ["reconstitution_monthly", "pit_market_cap"],
    },
    "universe/pit_universe_2014_2024.parquet": {
        "source": "INTERNAL", "source_version": "v1.0",
        "pit_candidates": ["date"],
        "entity_key": "ticker",
        "transformations": ["reconstitution_monthly", "pit_market_cap",
                            "delist_filter", "is_active"],
        "deprecated": True,
        "notes": "LEGACY pre-rebuild universe (hash-based synthetic shares, "
                 "2014-2017 RPO-imputation window). Deprecated - superseded by "
                 "pit_universe_2018_2024 / pit_universe_2020_2026.",
    },
    # ---- factor store (sidecar-merged) -----------------------------------
    "factors/rpo.parquet": {
        "source": "SEC_EDGAR", "source_version": "companyfacts_20260901",
        "pit_candidates": ["filed_date", "filed", "date"],
        "entity_key": "cik|ticker",
        "transformations": ["deferred_revenue_contract_liabilities",
                            "rpo_growth", "imputed_rpo_flag"],
        "mapping_confidence": "high",
        "sidecar_merge": True,
    },
    "factors/mda_tone.parquet": {
        "source": "SEC_EDGAR", "source_version": "companyfacts_20260901",
        "pit_candidates": ["filed_date", "filed", "date"],
        "entity_key": "cik|ticker",
        "transformations": ["FinBERT_sentiment", "Loughran_McDonald"],
        "mapping_confidence": "high",
        "sidecar_merge": True,
    },
    # ---- macro / prices auxiliaries --------------------------------------
    "fred_cache/ff5_factors_daily.parquet": {
        "source": "FRED", "source_version": "ff5_daily_202609",
        "pit_candidates": ["date", "DATE"],
        "entity_key": "market",
        "transformations": ["ken_french_ff5_daily"],
    },
    "pit_fundamentals/pit_fundamentals.parquet": {
        "source": "SEC_EDGAR", "source_version": "companyfacts_20260901",
        "pit_candidates": ["filing_date", "filed", "date"],
        "entity_key": "cik|ticker",
        "transformations": ["xbrl_companyfacts", "pit_fundamentals"],
        "mapping_confidence": "high",
    },
}

# ---------------------------------------------------------------------------
# Expected estate (canonical 91-file inventory for --inventory / --verify gap
# reporting).  fred/alfred expectations are derived from the live series
# catalog; source/yfinance is dynamic (per-ticker prices.parquet).
# ---------------------------------------------------------------------------

EXPECTED_FILES: Dict[str, List[str]] = {
    "factors/": ["rpo.parquet", "mda_tone.parquet"],
    "qual/": [
        "gh_velocity.parquet", "nhtsa_signals.parquet", "cfpb_velocity.parquet",
        "ats_mapping.parquet", "ats_velocity.parquet", "patents_signals.parquet",
        "app_store_velocity.parquet", "amazon_reviews.parquet",
        "inst_ownership.parquet", "apewisdom_mentions.parquet",
    ],
    "universe/": ["pit_universe_2014_2024.parquet", "pit_universe_2018_2024.parquet",
                  "pit_universe_2020_2026.parquet", "monthly_reconstitution.parquet",
                  "delisting_events.parquet"],
    "prices/": ["price_cache.parquet"],
    "provenance/": ["qual_coverage_matrix.parquet"],
    "fred_cache/": ["ff5_factors_daily.parquet"],
    "pit_fundamentals/": ["pit_fundamentals.parquet"],
    "data_lake/fred/": [],    # populated from EXPANDED_SERIES_CATALOG (financial_daily)
    "data_lake/alfred/": [],  # populated from EXPANDED_SERIES_CATALOG (macro)
    "source/yfinance/": [],   # dynamic: f"{ticker}/prices.parquet"
}

ESTATE_DESCRIPTIONS: Dict[str, str] = {
    "factors/": "SEC quant factors (RPO, MD&A tone)",
    "qual/": "Qualitative entity-mapped signals (10 canonical files)",
    "universe/": "PIT universes + reconstitution + SEC 8-K delistings",
    "prices/": "Price cache (multi-source, 2-day ffill cap)",
    "provenance/": "Qual coverage matrix (canonical output)",
    "fred_cache/": "FF5 daily factor (Ken French via FRED cache)",
    "pit_fundamentals/": "PIT company fundamentals (SEC companyfacts XBRL)",
    "data_lake/fred/": "FRED daily financial series observations",
    "data_lake/alfred/": "ALFRED macro vintages (point-in-time)",
    "source/yfinance/": "Yahoo Finance per-ticker price snapshots",
}

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _posix(p: Path) -> str:
    return str(p).replace("\\", "/")


def schema_columns(pf: Path) -> List[str]:
    """Physical Parquet column names (pandas index metadata bypassed)."""
    try:
        return list(pq.ParquetFile(pf).schema.names)
    except Exception:  # noqa: BLE001
        return []


def parquet_rows(pf: Path) -> int:
    try:
        return int(pq.ParquetFile(pf).metadata.num_rows)
    except Exception:  # noqa: BLE001
        return 0


def read_col(path: Path, col: str) -> "Any":
    """Read one physical column via pyarrow (survives pandas index quirks)."""
    try:
        table = pq.read_table(path, columns=[col])
        return table.column(col).to_pandas()
    except Exception:  # noqa: BLE001
        return None


def pick_pit_column(pf: Path, candidates: List[str],
                    prefer: Optional[str] = None) -> Optional[str]:
    cols = schema_columns(pf)
    if prefer and prefer in cols:
        return prefer
    for cand in candidates:
        if cand in cols:
            return cand
    return None


def compute_date_range(pf: Path, pit_col: Optional[str],
                       schema: List[str]) -> Optional[Dict[str, str]]:
    """min/max over the PIT column (or the first date-ish column / index)."""
    target = pit_col or next((c for c in DATE_CANDIDATES if c in schema), None)
    series = read_col(pf, target) if target else None
    if series is None and target is None:
        return None
    if series is None:
        # fall back to a full read so pandas can reconstruct a DatetimeIndex
        try:
            df = pd.read_parquet(pf)
            idx = getattr(df, "index", None)
            if isinstance(idx, pd.DatetimeIndex):
                series = idx
        except Exception:  # noqa: BLE001
            return None
    if series is None:
        return None
    d = pd.to_datetime(series, errors="coerce") if not hasattr(series, "dt") else series
    if getattr(d, "dt", None) is not None:
        d = d.dt.tz_localize(None) if d.dt.tz is not None else d
    valid = d.dropna()
    if valid.empty:
        return None
    return {"min": str(valid.min().date()), "max": str(valid.max().date())}


def load_sidecar(pf: Path) -> Dict[str, Any]:
    sidecar_path = Path(str(pf) + ".provenance.json")
    if not sidecar_path.exists():
        return {}
    try:
        return json.loads(sidecar_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"_parse_error": True}


def inline_first(path: Path, col: str) -> Optional[str]:
    series = read_col(path, col)
    if series is None:
        return None
    vals = series.dropna()
    if vals.empty:
        return None
    v = vals.iloc[0]
    if isinstance(v, (datetime,)):
        return v.isoformat()
    s = str(v)
    return s if s.strip() and s.lower() != "nan" else None


def inline_dominant(path: Path, col: str = "mapping_confidence") -> Optional[str]:
    series = read_col(path, col)
    if series is None or series.dropna().empty:
        return None
    try:
        vc = series.dropna().astype(str).str.lower().value_counts()
        return str(vc.index[0])
    except Exception:  # noqa: BLE001
        return None


def mtime_iso(pf: Path) -> str:
    try:
        return datetime.fromtimestamp(pf.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return datetime.now(timezone.utc).isoformat()


def sha256_of(pf: Path) -> str:
    return sha256_file(pf)


# ---------------------------------------------------------------------------
# Expected estate (fred/alfred derived from the live catalog)
# ---------------------------------------------------------------------------


def _fred_alfred_expectations() -> Tuple[List[str], List[str]]:
    """(expected fred filenames, expected alfred filenames) from the catalog.
    Domain rule mirrors ingestion.fred_alfred.ingest_key_series:
    alfred = macro vintages, fred = everything else."""
    fred, alfred = [], []
    try:
        from ingestion.fred_alfred import EXPANDED_SERIES_CATALOG
        for _, series_list in EXPANDED_SERIES_CATALOG.items():
            for s in series_list:
                name = f"{s['series_id']}.parquet"
                if s.get("type") == "macro":
                    alfred.append(name)
                else:
                    fred.append(name)
    except Exception:  # noqa: BLE001
        pass
    return sorted(set(fred)), sorted(set(alfred))


def expected_estate() -> Dict[str, List[str]]:
    fred, alfred = _fred_alfred_expectations()
    out = {k: list(v) for k, v in EXPECTED_FILES.items()}
    out["data_lake/fred/"] = fred
    out["data_lake/alfred/"] = alfred
    return out


# ---------------------------------------------------------------------------
# Entry construction
# ---------------------------------------------------------------------------


def conventions_for(rel: str) -> Dict[str, Any]:
    """Tree prefix defaults merged over per-file overrides (file wins)."""
    key = rel.replace("\\", "/")
    if key.startswith(DATA_PREFIX):
        key = key[len(DATA_PREFIX):]
    tree = next(t for t in TREE_CONVENTIONS if key.startswith(t))
    conv = dict(TREE_CONVENTIONS[tree])
    override = FILE_CONVENTIONS.get(key, {})
    conv.update(override)
    return conv


def build_entry(pf: Path, data_root: Path,
                now_iso: str) -> Dict[str, Any]:
    rel = _repo_rel(pf)
    conv = conventions_for(rel)
    schema = schema_columns(pf)
    rows = parquet_rows(pf)
    sha = sha256_of(pf)
    sidecar = load_sidecar(pf)

    pit_col = pick_pit_column(pf, conv.get("pit_candidates", DATE_CANDIDATES),
                              prefer=conv.get("pit_timestamp_column"))

    # retrieval_timestamp: inline column (pipeline run stamp) > sidecar >
    # file mtime > now.
    retrieval = (inline_first(pf, "retrieval_timestamp")
                 or sidecar.get("retrieval_timestamp") or mtime_iso(pf))

    # source_version: inline column > per-file / tree default > sidecar > v1.0.
    source_version = (inline_first(pf, "source_version")
                      or conv.get("source_version")
                      or sidecar.get("source_version") or "v1.0")

    # mapping_confidence: qual files report the inline dominant row-level
    # value (so 100%-high over-declaration is visible); SEC-direct files are
    # explicitly high; others omit the field (waived for non-qual outputs).
    mapping_confidence: Optional[str] = None
    if conv.get("inline_confidence"):
        mapping_confidence = inline_dominant(pf) or conv.get("mapping_confidence")
    elif "mapping_confidence" in conv and conv["mapping_confidence"] is not None:
        mapping_confidence = str(conv["mapping_confidence"])

    date_range = compute_date_range(pf, pit_col, schema)

    entry: Dict[str, Any] = {
        "file_path": rel,
        "source": (conv.get("source") or sidecar.get("source") or "INTERNAL"),
        "retrieval_timestamp": retrieval,
        "source_version": source_version,
        "pit_timestamp_column": pit_col or "date",
        "entity_key": conv.get("entity_key") or "ticker",
        "transformations": list(conv.get("transformations") or []),
        "sha256": sha,
        "row_count": rows,
        "date_range": date_range or {"min": None, "max": None},
    }
    if mapping_confidence is not None:
        entry["mapping_confidence"] = mapping_confidence
    if conv.get("deprecated"):
        entry["deprecated"] = True
    if conv.get("notes"):
        entry["notes"] = conv["notes"]
    # series_id for data-lake macro/financial series.
    if rel.startswith("data_lake/"):
        entry["series_id"] = Path(rel).stem
        if "realtime_start" in schema:
            entry.setdefault("notes", "")
            entry["notes"] = (entry["notes"] + "; vintage realtime_start "
                              "available").strip("; ")
    return entry


# ---------------------------------------------------------------------------
# Run / verify / inventory
# ---------------------------------------------------------------------------


def latest_entries_equal(manifest_path: Path, rel: str, entry: Dict[str, Any]) -> bool:
    """True if the manifest's newest entry for this path already matches."""
    latest = load_manifest_latest(manifest_path.parent.parent)
    old = latest.get(rel)
    if not old:
        return False
    ignore = {"retrieval_timestamp"}
    a = {k: v for k, v in entry.items() if k not in ignore}
    b = {k: v for k, v in old.items() if k not in ignore}
    return a == b


def run_backfill(data_root: Path, dry_run: bool = False,
                 report_path: Optional[Path] = None) -> Dict[str, Any]:
    data_root = Path(data_root)
    now_iso = datetime.now(timezone.utc).isoformat()
    manifest_path = data_root / "provenance" / "manifest.jsonl"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    files = track_parquets(data_root)
    appended: List[Dict[str, Any]] = []
    skipped: List[str] = []
    failed: List[Dict[str, Any]] = []

    for pf in files:
        rel = _repo_rel(pf)
        try:
            entry = build_entry(pf, data_root, now_iso)
        except Exception as exc:  # noqa: BLE001
            failed.append({"file": rel, "error": str(exc)})
            continue
        if latest_entries_equal(manifest_path, rel, entry):
            skipped.append(rel)
            continue
        appended.append(entry)
        if not dry_run:
            with open(manifest_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
        print(f"[manifest] append {pf.name:32s} src={entry['source']:12s} "
              f"pit={entry['pit_timestamp_column']:14s} rows={entry['row_count']:>8d} "
              f"sha={entry['sha256'][:12]}…")
    for rel in skipped:
        print(f"[manifest] keep   (unchanged latest entry) {rel}")

    summary = {
        "ran_at": now_iso,
        "dry_run": dry_run,
        "data_root": _posix(Path(data_root)),
        "files_tracked": len(files),
        "entries_appended": len(appended),
        "entries_unchanged": len(skipped),
        "entries_failed": len(failed),
        "failed": failed,
    }
    print(f"\n[manifest] {len(appended)} appended, {len(skipped)} unchanged, "
          f"{len(failed)} failed across {len(files)} tracked files "
          f"({'(dry run) ' if dry_run else ''}manifest={_posix(manifest_path)})")

    if report_path is not None and not dry_run:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, indent=2, sort_keys=True,
                                          default=str), encoding="utf-8")
    return summary


def verify_manifest(data_root: Path,
                    report_path: Optional[Path] = None) -> Dict[str, Any]:
    data_root = Path(data_root)
    manifest = load_manifest_latest(data_root)
    files = track_parquets(data_root)

    passed, mismatched, missing = [], [], []
    for pf in files:
        rel = _repo_rel(pf)
        entry = manifest.get(rel)
        if entry is None:
            missing.append(rel)
            continue
        expected = entry.get("sha256")
        actual = sha256_file(pf)
        if expected and actual == expected:
            passed.append(rel)
        else:
            mismatched.append({"file": rel, "actual": actual,
                               "expected": expected or "(missing)"})

    estate = expected_estate()
    present = {_repo_rel(pf) for pf in files}
    gap: Dict[str, List[str]] = {}
    for tree, names in sorted(estate.items()):
        missing_names = [
            n for n in names
            if f"data/{tree}{n}" not in present and f"data\\{tree}{n}" not in present
        ]
        if missing_names:
            gap[tree] = missing_names

    report: Dict[str, Any] = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "data_root": _posix(Path(data_root)),
        "total": len(files),
        "passed": len(passed),
        "hash_mismatch": mismatched,
        "missing_manifest": missing,
        "estate_gap": gap,  # canonical files the rebuild has not produced yet
        "estate_gap_count": sum(len(v) for v in gap.values()),
    }

    print(f"Manifest coverage: {len(passed)}/{len(files)} passed, "
          f"{len(mismatched)} hash_mismatch, {len(missing)} missing_manifest "
          f"(estate not-yet-rebuilt: {report['estate_gap_count']})")
    for m in mismatched:
        print(f"  [HASH-MISMATCH] {m['file']}")
    for rel in missing:
        print(f"  [NO-MANIFEST] {rel}")
    for tree, names in gap.items():
        print(f"  [ESTATE-GAP] {tree} ({len(names)} expected): "
              f"{', '.join(names[:6])}{'…' if len(names) > 6 else ''}")

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True,
                                          default=str), encoding="utf-8")
        print(f"[verify] report: {_posix(report_path)}")
    return report


def show_inventory(data_root: Path) -> int:
    data_root = Path(data_root)
    estate = expected_estate()
    present = {_repo_rel(pf) for pf in track_parquets(data_root)}
    fred_n, alfred_n = len(estate["data_lake/fred/"]), len(estate["data_lake/alfred/"])
    print("Tracked data estate (canonical §8 manifest scope)")
    print("=" * 78)
    total_expected = 0
    total_present = 0
    fred, alfred = 0, 0
    for tree, names in sorted(estate.items()):
        desc = ESTATE_DESCRIPTIONS.get(tree, "")
        present_names = [n for n in names
                         if f"data/{tree}{n}" in present or f"data\\{tree}{n}" in present]
        n_missing = len(names) - len(present_names)
        if tree == "data_lake/fred/":
            fred = len(present_names)
        if tree == "data_lake/alfred/":
            alfred = len(present_names)
        total_expected += len(names)
        total_present += len(present_names)
        print(f"{tree:22s} expected={len(names):4d} on_disk={len(present_names):4d} "
              f"missing={n_missing:4d}  {desc}")
    dynamic = sum(1 for p in present
                  if p.startswith("data/source/yfinance/"))
    print("-" * 78)
    print(f"Estates: factors={sum(1 for p in present if p.startswith('data/factors/'))} "
          f"qual={sum(1 for p in present if p.startswith('data/qual/'))} "
          f"universe={sum(1 for p in present if p.startswith('data/universe/'))} "
          f"prices={sum(1 for p in present if p.startswith('data/prices/'))} "
          f"provenance={sum(1 for p in present if p.startswith('data/provenance/'))} "
          f"fred_cache={sum(1 for p in present if p.startswith('data/fred_cache/'))} "
          f"pit_fundamentals={sum(1 for p in present if p.startswith('data/pit_fundamentals/'))} "
          f"fred={fred} alfred={alfred} yfinance={dynamic} (dynamic)")
    print(f"Total expected: {total_expected} (+ dynamic yfinance prices); "
          f"on disk now: {total_present + dynamic}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill the full §8 provenance manifest for the tracked "
                    "data estate (PROV-01/02).")
    parser.add_argument("--run", action="store_true",
                        help="Scan tracked Parquets and append fresh manifest entries")
    parser.add_argument("--verify", action="store_true",
                        help="Reconcile manifest vs tree (passed / hash_mismatch / "
                             "missing_manifest)")
    parser.add_argument("--inventory", action="store_true",
                        help="Print the canonical estate vs what is on disk")
    parser.add_argument("--data-root", type=str, default=str(DEFAULT_DATA_ROOT),
                        help="Data tree root (default data/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --run: compute entries without appending")
    parser.add_argument("--report", type=str, default=None,
                        help="Verification report JSON path")
    args = parser.parse_args()

    data_root = Path(args.data_root)
    report_path = Path(args.report) if args.report else VERIFY_REPORT_PATH

    if args.inventory:
        return show_inventory(data_root)

    if args.verify:
        report = verify_manifest(data_root, report_path)
        return 0 if not report["hash_mismatch"] and not report["missing_manifest"] else 1

    if args.run:
        summary = run_backfill(data_root, dry_run=args.dry_run,
                               report_path=BACKFILL_SUMMARY_PATH)
        return 0 if not summary["entries_failed"] else 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())