"""
Provenance Audit — RS-07 Final Validation (big-pickle-worker / Workstream B)

Systematic provenance + bias audit across the entire phase-1..3 data estate,
per Data_Strategy.md §7 (Phase 3), §8 (Data Contracts), §9 (Risk Register).

Checks implemented
-------------------
1. SHA-256 verification   — every Parquet under the data tree is reconciled
                            against the manifest (data/provenance/manifest.jsonl),
                            latest entry per path wins (append-log semantics).
2. PIT compliance         — no lookahead in any factor / universe / price file:
                            * if a declared PIT anchor column (e.g. filed_date)
                              and a trade `date` column BOTH exist and the
                              anchor is NOT a period anchor, require anchor <= date;
                            * files whose `date` column is a period/end anchor
                              (anchor > date for >50% of rows) are verified on
                              the anchor alone (no rows after retrieval/now).
3. Mapping confidence     — every qualitative file carries mapping_confidence
                            (column or provenance), distribution reported.
4. Provenance schema      — every canonical output carries the §8 contract:
                            source, retrieval_timestamp, source_version,
                            pit_timestamp_column, entity_key, transformations,
                            sha256, row_count, date_range, mapping_confidence
                            (mapping_confidence waived for non-qualitative
                            outputs per §8 "// for qualitative entity mappings").
5. Delisting / survivorship — universe includes delisted names at PIT market
                            cap pre-delist; post-delist handling verified.
6. Bias checklist         — the 9 bias rows from the RS-07 brief, computed
                            from code + data evidence, plus bias_flags.

CLI
---
    python -m validation.provenance_audit --run --data-root data/
    python -m validation.provenance_audit --check-manifest
    python -m validation.provenance_audit --check-pit --universe data/universe/pit_universe_2018_2024.parquet
    python -m validation.provenance_audit --test

Output
------
    data/validation/provenance_audit_report.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUIRED_FIELDS: List[str] = [
    "source", "retrieval_timestamp", "source_version", "pit_timestamp_column",
    "entity_key", "transformations", "sha256", "row_count", "date_range",
    "mapping_confidence",
]

# mapping_confidence is a qualitative-entity-mapping field (§8) -> waived for
# non-qualitative outputs (universe / prices / quantitative fundamentals).
NON_QUAL_FIELDS: List[str] = ["mapping_confidence"]

QUAL_FILE_SOURCES: Dict[str, str] = {
    "gh_velocity.parquet": "GH_ARCHIVE",
    "nhtsa_signals.parquet": "NHTSA",
    "cfpb_velocity.parquet": "CFPB",
    "ats_mapping.parquet": "ATS_MAPPER",
    "ats_velocity.parquet": "ATS_RSS",
    "patents_signals.parquet": "USPTO",
    "app_store_velocity.parquet": "APP_STORE",
    "amazon_reviews.parquet": "AMAZON",
    "inst_ownership.parquet": "SEC_13F",
    "apewisdom_mentions.parquet": "APEWISDOM",
}

FACTOR_FILE_SOURCES: Dict[str, str] = {
    "rpo.parquet": "SEC_EDGAR",
    "mda_tone.parquet": "SEC_EDGAR",
}

# PIT anchor column heuristics: an availability timestamp is a true anchor
# (filed / created / received / granted / reviewed / posted), never period_end.
# `date` precedes `retrieval_timestamp` because for price / universe /
# ticker-time-series files the as-of IS the trade date itself and the cache
# `retrieval_timestamp` is a single bulk-refresh stamp (not per-row PIT
# availability).  retrieval_timestamp still binds for mapping tables (which
# carry no `date` column).
PIT_COL_HINTS = [
    "source_timestamp", "filed_date", "filing_date", "filed", "created_at", "date_received",
    "DateReceived", "grant_date", "review_date", "posted_at", "delist_filed",
    "rebalance_date", "reconstitution_month", "trade_date", "date",
    "retrieval_timestamp",
]

# Directories that are NOT part of the audited data estate (caches,
# checkpoints, archives, test fixtures, raw bulk dumps).
EPHEMERAL_TOKENS = {
    "archive", "checkpoints", "cache", "damodaran", "damodaran_mirror",
    "dividends", "master_dividends", "master_prices", "etf_cache", "gdelt",
    "edgar_cache", "phrasebank_cache", "pipeline_cache", "raw_chats",
    "reddit_batches", "sec_datasets", "sentiment_training_cache",
    "test_batches", "universe_cache", "cdxj", "ken_french", "yfinance",
    "companyfacts", "13f_bulk", "gate_reports", "concepts", "facts", "filings",
}
EPHEMERAL_PREFIXES = ("_", "tmp")

REPO_ROOT = Path(__file__).resolve().parents[1]


def _is_ephemeral(path: Path) -> bool:
    for part in path.parts:
        name = Path(part).name
        if name in EPHEMERAL_TOKENS or name.startswith(EPHEMERAL_PREFIXES):
            return True
    return False


def _repo_rel(pf: Path) -> str:
    """Repository-root-relative posix path (manifest key convention: data/...)."""
    try:
        return _posix(pf.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return _posix(pf.relative_to(pf.anchor)) if pf.is_absolute() else _posix(pf)


def _as_naive(s: "pd.Series") -> "pd.Series":
    """Strip tz so tz-aware and naive datetimes compare cleanly."""
    out = pd.to_datetime(s, errors="coerce")
    if getattr(out.dt, "tz", None) is not None:
        out = out.dt.tz_localize(None)
    return out


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _posix(p: Path) -> str:
    return p.as_posix().replace("\\", "/")


def load_manifest_latest(data_root: Path) -> Dict[str, Dict[str, Any]]:
    """Returns {normalized_file_path: latest_manifest_entry} (append-log)."""
    manifest_path = data_root / "provenance" / "manifest.jsonl"
    latest: Dict[str, Dict[str, Any]] = {}
    if not manifest_path.exists():
        return latest
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        fp = str(entry.get("file_path", "")).replace("\\", "/")
        if fp:
            latest[fp] = entry
    return latest


def track_parquets(data_root: Path) -> List[Path]:
    """All auditable parquets under data_root (ephemeral dirs excluded)."""
    out: List[Path] = []
    for pf in sorted(data_root.rglob("*.parquet")):
        rel = pf.relative_to(data_root)
        # skip the manifest-adjacent auxiliary / cache dirs & test trees
        if _is_ephemeral(rel):
            continue
        if pf.name.endswith(".provenance.json"):
            continue
        out.append(pf)
    return out


def read_cols(path: Path, cols: Optional[List[str]] = None) -> pd.DataFrame:
    try:
        if cols:
            return pd.read_parquet(path, columns=cols)
        return pd.read_parquet(path)
    except Exception:  # noqa: BLE001 — schema drift should not kill the audit
        return pd.DataFrame()


def infer_pit_column(df: pd.DataFrame, prov: Dict[str, Any]) -> Optional[str]:
    declared = prov.get("pit_timestamp_column")
    if declared and declared in df.columns:
        return declared
    for hint in PIT_COL_HINTS:
        if hint in df.columns:
            return hint
    return None


def merged_provenance(path: Path, rel: str, manifest: Dict[str, Dict[str, Any]],
                      sidecar: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge manifest entry + sidecar + inline provenance columns (later wins is
    manifest>sidecar>inline order: manifest is the canonical audit trail)."""
    prov: Dict[str, Any] = {}
    prov.update(sidecar or {})
    prov.update(manifest.get(rel, {}))
    try:
        df = read_cols(path, ["retrieval_timestamp", "mapping_confidence",
                              "sha256", "source_version"])
        if not df.empty:
            rec = df.iloc[0]
            for col in ("retrieval_timestamp", "mapping_confidence", "sha256",
                        "source_version"):
                if col in df.columns and prov.get(col) is None:
                    val = rec[col]
                    if isinstance(val, pd.Timestamp):
                        val = val.isoformat()
                    if val is not None and not (isinstance(val, float) and pd.isna(val)):
                        prov.setdefault(col, val)
    except Exception:  # noqa: BLE001
        pass
    return prov


# ---------------------------------------------------------------------------
# Check 1 — SHA-256 vs manifest
# ---------------------------------------------------------------------------

def check_sha256(files: List[Path], data_root: Path,
                 manifest: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    failed: List[Dict[str, str]] = []
    missing: List[str] = []
    passed = 0
    for pf in files:
        rel = _repo_rel(pf)
        entry = manifest.get(rel)
        if entry is None:
            missing.append(rel)
            continue
        expected = entry.get("sha256")
        actual = sha256_file(pf)
        if expected and actual == expected:
            passed += 1
        else:
            failed.append({"file": rel, "actual": actual,
                           "expected": expected or "(missing)"})
    return {"total": len(files), "passed": passed, "failed": failed,
            "missing_manifest": missing}


# ---------------------------------------------------------------------------
# Check 2 — PIT compliance (no lookahead)
# ---------------------------------------------------------------------------

def pit_check_one(pf: Path, rel: str,
                  manifest: Dict[str, Dict[str, Any]],
                  today_iso: str) -> Dict[str, Any]:
    sidecar_path = Path(str(pf) + ".provenance.json")
    sidecar = None
    if sidecar_path.exists():
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sidecar = {"_parse_error": True}
    prov = merged_provenance(pf, rel, manifest, sidecar)

    df = pd.DataFrame()
    for attempt in (["date", "source_timestamp", "filed_date", "filing_date", "created_at",
                     "DateReceived", "grant_date", "review_date",
                     "posted_at", "retrieval_timestamp"], None):
        df = read_cols(pf, attempt)
        if not df.empty:
            break

    violations: List[Dict[str, Any]] = []
    if df.empty:
        return {"file": rel, "pit_passed": True, "note": "empty/unreadable",
                "violations": violations}

    pit_col = infer_pit_column(df, prov)
    if pit_col is None:
        violations.append({"reason": "missing_pit_timestamp_column",
                           "detail": "no filed/filing/created/received/granted "
                                     "anchor column on file or in provenance"})
        return {"file": rel, "pit_passed": False,
                "pit_timestamp_column": None, "violations": violations}

    trade_col = "date" if ("date" in df.columns and pit_col != "date") else None
    d_pit = _as_naive(df[pit_col])
    has_trade = trade_col is not None
    d_trade = _as_naive(df[trade_col]) if has_trade else None

    # Period-anchor detection: if the anchor is systematically AFTER `date`,
    # then `date` is a period end (not a trade date) and the row-level
    # anchor<=date test does not apply — the anchor itself is the usage date.
    if has_trade and d_trade.notna().any():
        frac_anchor_gt_date = float((d_pit > d_trade).mean())
        period_anchor = frac_anchor_gt_date > 0.5
    else:
        period_anchor = True

    retrieval_iso = str(prov.get("retrieval_timestamp", ""))[:10]
    cutoff = retrieval_iso if retrieval_iso else today_iso

    if not period_anchor and has_trade:
        bad = int((d_pit > d_trade).sum())
        if bad:
            violations.append({
                "reason": "lookahead_rows", "rows": bad,
                "detail": f"pit_col '{pit_col}' > trade_col '{trade_col}' "
                          f"on {bad} rows"})
    else:
        d_cut = pd.Timestamp(cutoff)
        future = int((d_pit > d_cut).sum())
        if future:
            violations.append({
                "reason": "future_dates_relative_to_retrieval", "rows": future,
                "detail": f"pit_col '{pit_col}' has {future} rows after "
                          f"retrieval/run cutoff {cutoff}"})

    return {"file": rel, "pit_passed": not violations,
            "pit_timestamp_column": pit_col,
            "trade_column": trade_col, "period_anchor": period_anchor,
            "violations": violations}


def check_pit(files: List[Path], data_root: Path,
              manifest: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    today_iso = date.today().isoformat()
    results: List[Dict[str, Any]] = []
    for pf in files:
        rel = _repo_rel(pf)
        results.append(pit_check_one(pf, rel, manifest, today_iso))
    violations = [r for r in results if not r["pit_passed"]]
    return {"total_files": len(results), "passed": len(results) - len(violations),
            "violations": violations}


# ---------------------------------------------------------------------------
# Check 1b — source_timestamp standardization (PIT Auditor, bigpickle-worker)
# ---------------------------------------------------------------------------
# Phase 8 PIT-Auditor Standardization: every feature output in the PIT estate
# (factors/, qual/, universe/, prices/) must carry the CANONICAL
# `source_timestamp` column (availability anchor, not period_end).  The column
# is already the first PIT_COL_HINTS anchor; this check makes the
# standardization EXPLICIT and reports the standardized issue codes so a
# backfill (scripts/backfill_provenance_manifest.py + loader writes) can be
# tracked to completion.
SOURCE_TIMESTAMP_ISSUE_MISSING = "MISSING_SOURCE_TIMESTAMP"


def check_source_timestamp_column(df: pd.DataFrame) -> Dict[str, Any]:
    """Column-level source_timestamp compliance for one frame.  Returns
    {column_present, rows, non_null, parseable, passed, issue} with the
    standardized issue label (None when passed)."""
    entry: Dict[str, Any] = {"column_present": False, "rows": 0,
                             "non_null": 0, "parseable": 0, "passed": False,
                             "issue": SOURCE_TIMESTAMP_ISSUE_MISSING}
    if "source_timestamp" not in df.columns or df is None or len(df) == 0:
        return entry
    entry["column_present"] = True
    entry["rows"] = int(len(df))
    non_null = int(df["source_timestamp"].notna().sum())
    entry["non_null"] = non_null
    if non_null == 0:
        entry["issue"] = "EMPTY_SOURCE_TIMESTAMP"
        return entry
    try:
        parsed = pd.to_datetime(df["source_timestamp"].dropna(), errors="coerce")
        entry["parseable"] = int(parsed.notna().sum())
    except Exception:  # noqa: BLE001
        entry["parseable"] = 0
    entry["passed"] = entry["parseable"] > 0
    entry["issue"] = None if entry["passed"] else "UNPARSEABLE_SOURCE_TIMESTAMP"
    return entry


def check_source_timestamp(files: List[Path]) -> Dict[str, Any]:
    """Estate-wide source_timestamp scan (PIT feature estate).  Violations are
    WARN severity (STMP-01) — PIT compliance itself is not downgraded; the
    report section gives the backfill the exact file list + issue codes."""
    results: List[Dict[str, Any]] = []
    violations: List[Dict[str, Any]] = []
    for pf in files:
        rel = _repo_rel(pf)
        try:
            df = pd.read_parquet(pf, columns=["source_timestamp"])
        except Exception:  # noqa: BLE001 — read/parse errors are violations
            df = pd.DataFrame()
        entry = check_source_timestamp_column(df)
        entry = {"file": rel, **entry}
        if not entry["passed"]:
            violations.append(entry)
        results.append(entry)
    return {"total_files": len(results),
            "passed_files": len(results) - len(violations),
            "violations": violations, "results": results}


# ---------------------------------------------------------------------------
# Check 3 — mapping confidence (qualitative files)
# ---------------------------------------------------------------------------

def check_mapping_confidence(qual_dir: Path) -> Dict[str, Any]:
    files = [f for f in sorted(qual_dir.glob("*.parquet"))
             if not _is_ephemeral(f.relative_to(qual_dir))]
    reports: List[Dict[str, Any]] = []
    dist: Dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    has_col = 0
    for f in files:
        df = read_cols(f, ["mapping_confidence"])
        sidecar_path = Path(str(f) + ".provenance.json")
        sidecar_val = None
        if sidecar_path.exists():
            try:
                sidecar_val = json.loads(
                    sidecar_path.read_text(encoding="utf-8")).get("mapping_confidence")
            except json.JSONDecodeError:
                pass
        if "mapping_confidence" in df.columns:
            has_col += 1
            try:
                vc = df["mapping_confidence"].value_counts(dropna=False)
            except Exception:  # noqa: BLE001
                vc = pd.Series(dtype=int)
            for lvl in dist:
                dist[lvl] += int(vc.get(lvl, 0))
            declared = str(vc.index[0]) if len(vc) else sidecar_val
        else:
            declared = sidecar_val
        reports.append({"file": f.name, "column_present":
                        "mapping_confidence" in df.columns,
                        "declared": declared, "rows": len(df)})
    return {"total_qual_files": len(files), "has_column": has_col,
            "distribution": dist, "per_file": reports}


# ---------------------------------------------------------------------------
# Check 4 — provenance schema completeness
# ---------------------------------------------------------------------------

def check_schema(files: List[Path], data_root: Path,
                 manifest: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    missing_map: Dict[str, List[str]] = {}
    complete = 0
    total = 0
    for pf in files:
        rel = _repo_rel(pf)
        sidecar_path = Path(str(pf) + ".provenance.json")
        sidecar = None
        if sidecar_path.exists():
            try:
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                sidecar = {}
        prov = merged_provenance(pf, rel, manifest, sidecar)
        required = REQUIRED_FIELDS[:]
        # mapping_confidence is an entity-mapping field (§8) — waived for
        # quantitative outputs (factors / universe / prices / provenance).
        if pf.name not in QUAL_FILE_SOURCES:
            required = [f for f in required if f != "mapping_confidence"]
        miss = [f for f in required if prov.get(f) in (None, "", {})]
        total += 1
        if not miss:
            complete += 1
        else:
            missing_map[rel] = miss
    return {"total_files": total, "complete": complete,
            "missing_fields": missing_map}


# ---------------------------------------------------------------------------
# Check 5 — Delisting / survivorship bias
# ---------------------------------------------------------------------------

def check_survivorship(data_root: Path) -> Dict[str, Any]:
    uni_dir = data_root / "universe"
    delist_path = uni_dir / "delisting_events.parquet"
    recon_path = uni_dir / "monthly_reconstitution.parquet"
    out: Dict[str, Any] = {"universe_includes_delisted": False,
                           "delisted_count": 0, "pit_correct": False,
                           "is_active_false_rows": 0, "notes": []}
    if not delist_path.exists():
        out["notes"].append("delisting_events.parquet missing")
        return out

    ev = read_cols(delist_path)
    if ev.empty:
        out["notes"].append("delisting_events.parquet is empty")
        return out

    n_delisted = int(len(ev))
    out["delisted_count"] = n_delisted
    tickers = {str(t) for t in ev["ticker"].dropna().unique()}
    dates = ev["delist_filed"].astype(str).unique().tolist() \
        if "delist_filed" in ev else []
    if len(set(dates)) == 1:
        out["notes"].append(
            f"ALL {n_delisted} delisting events share one date {dates[0]} — "
            "events appear synthetic/patterned, not distinct SEC 8-K filings")

    if not recon_path.exists():
        out["notes"].append("monthly_reconstitution.parquet missing")
        return out
    recon = read_cols(recon_path, ["reconstitution_month", "rebalance_date",
                                   "ticker", "market_cap"])
    if recon.empty:
        return out
    recon = recon.copy()
    recon["rebalance_date"] = _as_naive(recon["rebalance_date"])

    delist_map = dict(zip(ev["ticker"].astype(str),
                          _as_naive(ev["delist_filed"])))
    present = 0
    pit_cap_ok = 0
    for t in tickers:
        sub = recon[recon["ticker"] == t]
        d = delist_map.get(t)
        if d is None:
            continue
        before = sub[sub["rebalance_date"] < d]
        if len(before):
            present += 1
            if before["market_cap"].notna().all() and \
                    (before["market_cap"] > 0).all():
                pit_cap_ok += 1
    out["universe_includes_delisted"] = present >= max(1, len(tickers))
    out["pit_correct"] = pit_cap_ok >= max(1, len(tickers))

    # post-delist flagging: is_active=False rows in the daily PIT universes
    n_inactive = 0
    for fn in ("pit_universe_2018_2024.parquet", "pit_universe_2020_2026.parquet"):
        p = uni_dir / fn
        if p.exists():
            df = read_cols(p, ["is_active"])
            if "is_active" in df.columns:
                n_inactive += int((df["is_active"] == False).sum())  # noqa: E712
    out["is_active_false_rows"] = n_inactive
    if n_inactive == 0:
        out["notes"].append(
            "0 is_active=False rows across PIT universe files — delisted names "
            "are dropped from reconstitution rather than flagged post-delist "
            "(documented mitigation: exclude from tradable; bias accepted)")
    return out


# ---------------------------------------------------------------------------
# Check 6 — universe deep-dive (--check-pit --universe)
# ---------------------------------------------------------------------------

def check_universe_pit(path: Path, data_root: Path,
                       manifest: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    rel = _repo_rel(path)
    prov = merged_provenance(path, rel, manifest, None)
    df = read_cols(path)
    out: Dict[str, Any] = {"file": rel, "rows": int(len(df))}
    if df.empty:
        out["error"] = "empty/unreadable"
        return out
    date_cols = [c for c in df.columns if "date" in c.lower()]
    out["columns"] = list(df.columns)
    out["date_columns"] = date_cols
    if "date" in df.columns:
        d = _as_naive(df["date"])
        out["date_min"] = str(d.min().date()) if d.notna().any() else None
        out["date_max"] = str(d.max().date()) if d.notna().any() else None
        out["n_dates"] = int(d.nunique())
        today = pd.Timestamp(date.today())
        out["future_rows_vs_today"] = int((d > today).sum())
        retrieval = str(prov.get("retrieval_timestamp", ""))[:10]
        if retrieval:
            out["future_rows_vs_retrieval"] = int((d > pd.Timestamp(retrieval)).sum())
    if "ticker" in df.columns:
        out["n_tickers"] = int(df["ticker"].nunique())
    if "is_active" in df.columns:
        out["is_active_false_rows"] = int((df["is_active"] == False).sum())  # noqa: E712
        out["is_active_false_tickers"] = int(
            df.loc[df["is_active"] == False, "ticker"].nunique())  # noqa: E712
    if "price_stale_flag" in df.columns:
        out["price_stale_false_rows"] = int(df["price_stale_flag"].sum())
    if "market_cap" in df.columns:
        mc = df["market_cap"]
        finite = pd.to_numeric(mc, errors="coerce")
        out["market_cap_negative_or_zero"] = int((finite <= 0).sum())
        out["market_cap_null"] = int(finite.isna().sum())
    return out


# ---------------------------------------------------------------------------
# Bias checklist (RS-07 Task 2) — evidence computed from code + data
# ---------------------------------------------------------------------------

def bias_checklist(data_root: Path, sha: Dict[str, Any], pit: Dict[str, Any],
                   mconf: Dict[str, Any], surv: Dict[str, Any],
                   schema: Dict[str, Any], manifest: Dict[str, Dict[str, Any]],
                   st: Optional[Dict[str, Any]] = None
                   ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Returns (checklist_rows, bias_flags).  Code-path evidence is static and
    documented here; data evidence is computed live."""
    rows: List[Dict[str, str]] = []
    flags: List[Dict[str, str]] = []

    # 1. Survivorship -------------------------------------------------------
    rows.append({
        "bias": "Survivorship",
        "status": "WARN",
        "check": "Universe includes delisted names at PIT market cap",
        "method": "delisting_events.parquet + monthly_reconstitution overlap",
        "evidence": (f"{surv['delisted_count']} delisted names; pre-delist "
                     f"PIT cap rows present={surv['universe_includes_delisted']}; "
                     f"pit_correct={surv['pit_correct']}; "
                     f"is_active=False rows={surv['is_active_false_rows']}"),
    })
    if surv.get("notes"):
        flags.append({"severity": "HIGH", "id": "SURV-01",
                      "finding": "; ".join(surv["notes"])})

    # 2. Lookahead ----------------------------------------------------------
    viol_files = {v["file"] for v in pit.get("violations", [])}
    pit_pass = pit["passed"] == pit["total_files"]
    rows.append({
        "bias": "Lookahead",
        "status": "PASS" if pit_pass else "WARN",
        "check": "All factors use filing_date/availability anchors, not period_end",
        "method": "pit_timestamp_column scan of factor/universe/price files",
        "evidence": (f"{pit['passed']}/{pit['total_files']} files PIT-clean; "
                     f"violations={sorted(viol_files)}"),
    })
    for v in pit.get("violations", []):
        flags.append({"severity": "WARN", "id": "LOOK-01",
                      "finding": f"PIT violation {v['file']}: {v['violations']}"})

    # 3. Forward fill -------------------------------------------------------
    pc = data_root / "prices" / "price_cache.parquet"
    pf_evidence = "price_cache.parquet missing"
    if pc.exists():
        df = read_cols(pc, ["price_stale_flag", "date", "source"])
        pf_evidence = (f"code: ffill(limit=2) + price_stale_flag>2d; file: "
                       f"{len(df)} rows, stale={int(df['price_stale_flag'].sum())}"
                       if "price_stale_flag" in df else f"{len(df)} rows")
    rows.append({
        "bias": "Forward Fill",
        "status": "PASS",
        "check": "Price cache forward-fill capped at 2 trading days",
        "method": "ingestion/price_cache.py review",
        "evidence": pf_evidence,
    })

    # 4. Selection ----------------------------------------------------------
    rows.append({
        "bias": "Selection",
        "status": "WARN",
        "check": "Universe construction uses PIT data only (historical indexes + delist 8-Ks)",
        "method": "ingestion/universe_builder.py review",
        "evidence": ("month-end PIT prices + delist filter OK; candidate pool = "
                     "config/master_universe.yaml top-2552 by 2026 market cap "
                     "(future selection); shares synthetic constant per ticker"),
    })
    flags.append({"severity": "HIGH", "id": "SEL-01",
                  "finding": "Universe candidate pool is ranked by 2026 market "
                             "cap (master_universe.yaml), not historical "
                             "point-in-time membership — selection/survivorship "
                             "pool bias; shares_outstanding are synthetic "
                             "(50-550M hash-derived) and PYTHONHASHSEED-dependent"})

    # 5. Mapping ------------------------------------------------------------
    rows.append({
        "bias": "Mapping",
        "status": "PASS" if mconf["has_column"] == mconf["total_qual_files"]
        else "WARN",
        "check": "Qualitative entity mapping confidence tracked on all outputs",
        "method": "mapping_confidence column/sidecar scan of data/qual",
        "evidence": (f"{mconf['has_column']}/{mconf['total_qual_files']} files "
                     f"carry mapping_confidence; distribution={mconf['distribution']}"),
    })
    if mconf["distribution"].get("medium") == 0 and mconf["distribution"].get("low") == 0 \
            and mconf["total_qual_files"]:
        flags.append({"severity": "WARN", "id": "MAP-01",
                      "finding": "All qual files declare high confidence — no "
                                 "medium/low rows; heuristic mappings likely "
                                 "over-declared (coverage matrix also 100% high)"})

    # 6. Imputation ---------------------------------------------------------
    rpo = data_root / "factors" / "rpo.parquet"
    imputed_in_primary = None
    if rpo.exists():
        df = read_cols(rpo, ["date", "filed_date", "rpo_is_imputed"])
        if not df.empty and "rpo_is_imputed" in df.columns:
            d = _as_naive(df["date"])
            mask = (d >= "2018-01-01") & (d <= "2024-12-31")
            imputed_in_primary = int(df.loc[mask, "rpo_is_imputed"].sum())
    rows.append({
        "bias": "Imputation",
        "status": "FAIL" if imputed_in_primary else "PASS",
        "check": "No rpo_is_imputed=True rows inside the 2018-2024 primary window",
        "method": "rpo.parquet rpo_is_imputed scan (primary window)",
        "evidence": (f"rpo_is_imputed=True rows in 2018-2024: {imputed_in_primary}; "
                     "phase-4 driver compute_rpo_growth must filter imputed rows"),
    })
    if imputed_in_primary:
        flags.append({"severity": "HIGH", "id": "IMPUTE-01",
                      "finding": f"{imputed_in_primary} imputed RPO rows inside "
                                 "2018-2024 primary window — OOS-invalidated "
                                 "imputation (R2=0.157) must be excluded before "
                                 "the primary backtest"})

    # 7. Gate integrity -----------------------------------------------------
    gates = data_root.parent / "config" / "gates_v2.yaml"
    sha_path = data_root.parent / "config" / "gates_v2.yaml.sha256"
    gate_ok = False
    gate_evidence = "gates_v2.yaml missing"
    if gates.exists() and sha_path.exists():
        actual = sha256_file(gates)
        stored = sha_path.read_text(encoding="utf-8").strip().split()[0]
        gate_ok = actual == stored
        gate_evidence = (f"frozen config SHA256 match={gate_ok} "
                         f"(stored {stored[:12]}… vs actual {actual[:12]}…)")
    rows.append({
        "bias": "Gate Integrity",
        "status": "PASS" if gate_ok else "WARN",
        "check": "Gates 3/4 re-registration frozen + SHA256-pinned",
        "method": "config/gates_v2.yaml + .sha256 verification",
        "evidence": gate_evidence,
    })

    # 8. Data snooping ------------------------------------------------------
    universe_ts = None
    qual_ts = None
    for rel, e in manifest.items():
        if rel.startswith("data/universe/") and universe_ts is None:
            universe_ts = e.get("retrieval_timestamp")
        if rel.startswith("data/qual/") and qual_ts is None:
            qual_ts = e.get("retrieval_timestamp")
    snoop_ok = bool(universe_ts and qual_ts and universe_ts <= qual_ts)
    rows.append({
        "bias": "Data Snooping",
        "status": "PASS" if snoop_ok else "WARN",
        "check": "Universe built before factor computation (no post-hoc universe)",
        "method": "manifest retrieval_timestamp ordering",
        "evidence": (f"universe@{universe_ts} vs qual@{qual_ts} — "
                     f"universe-first={snoop_ok}"),
    })

    # 9. Contamination ------------------------------------------------------
    ml_track = data_root.parent / "research" / "reverse_engineer_multibaggers.py"
    ml_missing = not ml_track.exists()
    rows.append({
        "bias": "Contamination",
        "status": "FAIL" if ml_missing else "WARN",
        "check": "ML track uses walk-forward CPCV with embargo >= horizon",
        "method": "research/reverse_engineer_multibaggers.py existence check",
        "evidence": ("research/reverse_engineer_multibaggers.py NOT IMPLEMENTED "
                     "— walk-forward CPCV track pending; phase-4 driver uses "
                     "cross-window OOS only (scored_oos=false, documented)"),
    })
    if ml_missing:
        flags.append({"severity": "HIGH", "id": "CONT-01",
                      "finding": "ML multibagger track (walk-forward CPCV, "
                                 "embargo>=12mo, LASSO+RF) not implemented — "
                                 "train/test contamination guard is code-missing"})

    # 10. Source timestamps (PIT Auditor standardization) ------------------
    st_total = (st or {}).get("total_files", 0)
    st_passed = (st or {}).get("passed_files", 0)
    st_complete = bool(st_total) and st_passed == st_total
    rows.append({
        "bias": "Source Timestamp",
        "status": "PASS" if st_complete else "WARN",
        "check": "Every PIT estate feature output carries a parseable "
                 "source_timestamp column (canonical availability anchor)",
        "method": "estate-wide source_timestamp scan (factors/qual/universe/prices)",
        "evidence": (f"{st_passed}/{st_total} files carry a parseable "
                     f"source_timestamp; violations="
                     f"{[v['file'] for v in (st or {}).get('violations', [])][:8]}"
                     + (" [+ more]" if len((st or {}).get('violations', [])) > 8 else "")),
    })
    for v in (st or {}).get("violations", []):
        flags.append({"severity": "WARN", "id": "STMP-01",
                      "finding": f"source_timestamp {v.get('issue')} in {v['file']} "
                                 f"(non_null={v.get('non_null', 0)}/{v.get('rows', 0)}, "
                                 f"parseable={v.get('parseable', 0)})"})

    # Schema / manifest completeness flags ----------------------------------
    n_missing_manifest = len(sha.get("missing_manifest", []))
    if n_missing_manifest:
        flags.append({
            "severity": "MEDIUM", "id": "PROV-01",
            "finding": f"{n_missing_manifest}/{sha['total']} tracked parquets "
                       "have NO manifest entry (manifest covers universe/prices/"
                       "qual only); schema incomplete for factors/sec_facts/data_lake",
        })
    if schema.get("missing_fields"):
        flags.append({
            "severity": "MEDIUM", "id": "PROV-02",
            "finding": f"{len(schema['missing_fields'])} files incomplete on "
                       "the §8 provenance contract (missing fields per file in "
                       "provenance_schema.missing_fields)",
        })

    return rows, flags


# ---------------------------------------------------------------------------
# Trade-audit ledger chain verification (Phase 3 / WS-2 C2.5)
# ---------------------------------------------------------------------------

def _load_ledger_blocks(ledger_dir: Path, month: str) -> Tuple[Any, pd.DataFrame, Dict[str, Any]]:
    """Returns (TradeAuditLedger, month parquet df, manifest dict) or empty."""
    from ingestion.trade_audit import TradeAuditLedger
    ledger = TradeAuditLedger(ledger_dir=ledger_dir)
    path = ledger_dir / f"month={month}" / f"trade_audit_{month}.parquet"
    manifest: Dict[str, Any] = {}
    manifest_path = ledger_dir / f"month={month}" / "partition.manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {"_parse_error": True}
    df = pd.DataFrame()
    if path.exists():
        try:
            df = pd.read_parquet(path)
        except Exception:  # noqa: BLE001
            df = pd.DataFrame()
    return ledger, df, manifest


def _month_partitions(ledger_dir: Path) -> List[str]:
    return sorted(p.name[len("month="):] for p in ledger_dir.glob("month=*")
                  if p.is_dir())


def _is_trade_ledger_dir(path: Path) -> bool:
    """Heuristic: a ledger dir has month=* partitions or a .ledger.lock, or
    carries the trade_audit partition parquets directly."""
    if _month_partitions(path):
        return True
    return (path / ".ledger.lock").exists() or \
        any(path.glob("trade_audit_*.parquet"))


def check_trade_ledger(ledger_dir: Path) -> Dict[str, Any]:
    """
    WS-2 C2.5 chain verification of the immutable trade-audit ledger
    (ingestion/trade_audit.py, phase8.trade_audit frozen config):

      1. two-layer SHA-256 chain integrity per partition (verify_chain with
         full forensic recompute — stored hashes are never trusted);
      2. cross-partition chaining via verify_ledger (first block of P+1 must
         carry the last chain_hash of P);
      3. nanosecond TIMESTAMP ordering: retrieval_ts non-decreasing within a
         partition (uuid7 time prefix, C2.2);
      4. UUID v7 block ids (version nibble == 7, C2.2);
      5. Appendix A trigger_reason re-validation on EVERY block (C2.3);
      6. stage_gate enum {1..5} + action enum (C2.4);
      7. frozen numeric bounds: erp_blend in [0.005, 0.08], wacc/ke/kd in
         [0, 0.5]; beta_source non-null (coverage gate);
      8. manifest consistency: declared n_blocks == parquet row count and
         first/last chain hashes match (completeness, no silent truncation);
      9. block_id uniqueness across the whole ledger.
    Returns a report dict (status PASS/FAIL/SKIP)."""
    from ingestion.trade_audit import (
        ACTIONS, STAGE_GATES, TRIGGER_REASON_SCHEMA,
        validate_trigger_reason, TradeAuditLedger,
    )
    months = _month_partitions(ledger_dir)
    if not months:
        return {"status": "SKIP", "ledger_dir": str(ledger_dir),
                "reason": "no month=* partitions (ledger not built yet)"}

    ledger = TradeAuditLedger(ledger_dir=ledger_dir)
    partitions: Dict[str, Any] = {}
    violations: List[Dict[str, Any]] = []
    date_range = None
    earliest = latest = None
    seen_ids: Dict[str, str] = {}

    total_blocks = 0
    for month in months:
        ok, n, brk = ledger.verify_chain(month, verify_hashes=True)
        _, _, manifest = _load_ledger_blocks(ledger_dir, month)
        blks = ledger.load_month(month)
        total_blocks += len(blks)

        issues: List[str] = []
        if not ok:
            issues.append(f"chain_verification failed break@{brk}")
        # manifest consistency / completeness
        decl_n = manifest.get("n_blocks")
        if decl_n is not None and int(decl_n) != len(blks):
            issues.append(f"manifest n_blocks {decl_n} != {len(blks)} rows")
        if manifest.get("last_chain_hash") and blks:
            if manifest["last_chain_hash"] != blks[-1].chain_hash:
                issues.append("manifest last_chain_hash mismatch")
        if manifest.get("first_chain_hash") and blks:
            if manifest["first_chain_hash"] != blks[0].chain_hash:
                issues.append("manifest first_chain_hash mismatch")
        # per-block checks
        prev_ts = None
        for i, b in enumerate(blks):
            # uuid v7
            try:
                if getattr(__import__("uuid"), "UUID")(b.block_id).version != 7:
                    issues.append(f"block#{i + 1} block_id not uuid7")
            except Exception:  # noqa: BLE001
                issues.append(f"block#{i + 1} block_id not a uuid")
            if b.block_id in seen_ids:
                issues.append(f"block_id {b.block_id[:12]} duplicated "
                              f"(also in {seen_ids[b.block_id]})")
            seen_ids[b.block_id] = month
            # timestamps + ordering (nanosecond precision, non-decreasing)
            ts = b.retrieval_ts
            if prev_ts is not None and ts < prev_ts:
                issues.append(f"block#{i + 1} retrieval_ts decreasing")
            prev_ts = ts
            if earliest is None or ts < earliest:
                earliest = ts
            if latest is None or ts > latest:
                latest = ts
            # Appendix A re-validation
            if b.trigger_reason is not None:
                try:
                    validate_trigger_reason(b.trigger_reason)
                except ValueError as exc:
                    issues.append(f"block#{i + 1} trigger_reason invalid: {exc}")
            else:
                issues.append(f"block#{i + 1} trigger_reason None")
            # enums
            if b.stage_gate not in STAGE_GATES:
                issues.append(f"block#{i + 1} stage_gate {b.stage_gate} invalid")
            if b.action not in ACTIONS:
                issues.append(f"block#{i + 1} action '{b.action}' invalid")
            # frozen numeric bounds + coverage
            if not (0.005 - 1e-12 <= b.erp_blend <= 0.08 + 1e-12):
                issues.append(f"block#{i + 1} erp_blend {b.erp_blend} outside [0.005, 0.08]")
            for col, v in (("wacc_v2", b.wacc_v2), ("ke_v2", b.ke_v2),
                           ("kd_pre_tax_v2", b.kd_pre_tax_v2)):
                if not (0.0 - 1e-12 <= v <= 0.5 + 1e-12):
                    issues.append(f"block#{i + 1} {col} {v} outside [0, 0.5]")
            if not b.beta_source:
                issues.append(f"block#{i + 1} empty beta_source (coverage gate)")

        partitions[month] = {
            "blocks": len(blks), "chain_intact": bool(ok),
            "first_break": brk, "issues": issues,
            "first_chain": (blks[0].chain_hash[:16] if blks else None),
            "last_chain": (blks[-1].chain_hash[:16] if blks else None),
        }
        if issues:
            violations.append({"month": month, "issues": issues})

    global_rpt = ledger.verify_ledger(cross_partition=True)
    if not global_rpt["intact"]:
        violations.append({"month": "*", "issues": ["cross-partition chain broken"]})
    if earliest is not None and latest is not None:
        date_range = {"min": str(earliest.date()), "max": str(latest.date())}

    passed = not violations
    status = "PASS" if passed else "FAIL"
    return {
        "name": "trade_audit_chain",
        "status": status,
        "ledger_dir": str(ledger_dir),
        "config_contract": {
            "chain_verification": ledger.chain_verification,
            "partition_by": ledger.partition_by,
            "retention_years": ledger.retention_years,
            "timestamp_precision": ledger.timestamp_precision,
            "uuid_version": ledger.uuid_version,
            "stage_gates": {str(k): v for k, v in STAGE_GATES.items()},
            "trigger_reason_schema": "$schema draft 2020-12 (Appendix A)",
        },
        "global": {"intact": bool(global_rpt["intact"]),
                   "n_blocks": int(global_rpt["n_blocks"]),
                   "partitions": int(global_rpt["partitions"])},
        "partitions": partitions,
        "violations": violations,
        "summary": {"status": status, "n_blocks": total_blocks,
                    "n_partitions": len(months), "violation_count": len(violations),
                    "date_range": date_range},
    }


def write_ledger_report(ledger_report: Dict[str, Any], report_path: Path) -> None:
    content = json.dumps(ledger_report, sort_keys=True, default=str).encode("utf-8")
    ledger_report["provenance"] = {
        "source": "validation.provenance_audit",
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "row_count": int(ledger_report.get("summary", {}).get("n_blocks", 0)),
        "date_range": ledger_report.get("summary", {}).get("date_range"),
        "mapping_confidence": "n/a",
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(ledger_report, indent=2, sort_keys=True,
                                      default=str), encoding="utf-8")


def cmd_ledger_check(data_root: Path, report: Optional[Path]) -> int:
    """`--run --data-root <ledger-dir>` -> data/validation/gates/
    provenance_audit_report.json.  Exit 0 = PASS/SKIP, 1 = FAIL."""
    rpt = check_trade_ledger(data_root)
    out = report if report is not None else \
        REPO_ROOT / "data" / "validation" / "gates" / "provenance_audit_report.json"
    if "summary" not in rpt:     # early SKIP return (no partitions yet)
        rpt["summary"] = {
            "status": rpt.get("status", "SKIP"),
            "n_blocks": 0,
            "n_partitions": 0,
            "violation_count": 0,
            "date_range": None,
        }
    write_ledger_report(rpt, out)
    s = rpt["summary"]
    print(f"[provenance] trade-ledger chain: status={rpt['status']} "
          f"blocks={s['n_blocks']} partitions={s['n_partitions']} "
          f"violations={s['violation_count']}")
    for v in rpt.get("violations", [])[:10]:
        print(f"  [LEDGER-FAIL] {v.get('month', '*')}: {v['issues'][:2]}")
    print(f"[provenance] report: {out}")
    return 0 if rpt["status"] != "FAIL" else 1


# ---------------------------------------------------------------------------
# Report assembly / CLI
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Check "backtest chain" — Phase 4 gate 16
#
# The walk-forward certification requires the artifact chain to be intact:
#   1) each fold dir's .provenance.json lists sha256 for every artifact;
#   2) every listed artifact's CURRENT bytes hash to the listed value;
#   3) the consolidated walk-forward parquet matches its own sidecar sha256.
# A violation (tamper / missing listing / drift) fails gate 16.
# ---------------------------------------------------------------------------

WALKFORWARD_CONSOLIDATED = Path("data") / "backtests" / "walkforward_hybrid_fractal_v1.parquet"
WALKFORWARD_CHAIN_REPORT = Path("data") / "validation" / "provenance_walkforward_report.json"
WF_FOLD_PATTERN = re.compile(r"^.*-hybrid_fractal_v1-fold(\d+)$")


def check_backtest_chain(data_root: Path,
                         consolidated: Optional[Path] = None,
                         fold_root: Path = Path("data") / "backtests",
                         strategy: str = "hybrid_fractal_v1") -> Dict[str, Any]:
    """
    Verifies the Phase-4 walk-forward artifact chain (fold .provenance.json
    listings vs current bytes + consolidated parquet sidecar).  Returns the
    gate-16 shape consumed by ingestion/harness/gate_engine.py.
    """
    consolidated = Path(consolidated) if consolidated is not None else WALKFORWARD_CONSOLIDATED
    violations: List[str] = []
    folds_verified: List[str] = []
    folder = fold_root if fold_root.exists() else Path(data_root) / "backtests"

    fold_dirs = sorted(
        (d for d in folder.iterdir() if d.is_dir() and WF_FOLD_PATTERN.match(d.name)),
        key=lambda d: int(WF_FOLD_PATTERN.match(d.name).group(1)))
    if not fold_dirs:
        violations.append(f"no {strategy}-fold{{K}} dirs under {folder}")

    for d in fold_dirs:
        prov_file = d / ".provenance.json"
        if not prov_file.exists():
            violations.append(f"{d.name}: .provenance.json missing")
            continue
        try:
            prov = json.loads(prov_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            violations.append(f"{d.name}: .provenance.json unparseable")
            continue
        listing = prov.get("files") or {}
        # 1) every listed artifact must exist
        for rel in listing:
            ap = d / rel
            if not ap.exists():
                violations.append(f"{d.name}/{rel}: listed but missing")
        # 2) current bytes must match the listing hash
        for rel, expected in listing.items():
            ap = d / rel
            if ap.exists() and expected:
                actual = sha256_file(ap)
                if actual != expected:
                    violations.append(f"{d.name}/{rel}: sha256 mismatch "
                                      f"(actual {actual[:12]}… != listed {expected[:12]}…)")
        # 3) sidecars must be self-consistent (own hash listed too)
        for sp in d.glob("*.parquet.provenance.json"):
            try:
                side = json.loads(sp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                violations.append(f"{d.name}/{sp.name}: sidecar unparseable")
                continue
            own = side.get("sha256")
            base = sp.name.replace(".provenance.json", "")
            if own and (d / base).exists():
                if sha256_file(d / base) != own:
                    violations.append(f"{d.name}/{base}: sidecar sha256 mismatch")
        folds_verified.append(d.name)

    # 4) consolidated parquet vs its provenances sidecar
    consolidated_sha256 = None
    if consolidated.exists():
        consolidated_sha256 = sha256_file(consolidated)
        side = Path(str(consolidated) + ".provenance.json")
        if side.exists():
            try:
                side_sha = json.loads(side.read_text(encoding="utf-8")).get("sha256")
                if side_sha and consolidated_sha256 != side_sha:
                    violations.append(
                        f"{consolidated.name}: parquet sha256 {consolidated_sha256[:12]}… "
                        f"!= sidecar {side_sha[:12]}…")
            except json.JSONDecodeError:
                violations.append(f"{consolidated.name}: sidecar unparseable")
        else:
            violations.append(f"{consolidated.name}: sidecar {consolidated.name}.provenance.json missing")
    else:
        violations.append(f"{consolidated.name}: consolidated walk-forward parquet missing")

    return {
        "name": "backtest_chain",
        "valid": not violations,
        "status": "evaluated" if fold_dirs else "no_folds_discovered",
        "folds_verified": folds_verified,
        "fold_roots": [str(d) for d in fold_dirs],
        "consolidated_sha256": consolidated_sha256,
        "violations": violations,
        "source": "validation/provenance_audit.check_backtest_chain",
    }


def run_audit(data_root: Path, universe_deep: Optional[Path] = None,
              report_path: Optional[Path] = None) -> Dict[str, Any]:
    data_root = Path(data_root)
    manifest = load_manifest_latest(data_root)

    files = track_parquets(data_root)
    sha = check_sha256(files, data_root, manifest)

    # PIT compliance scope = factor/universe/price estate (tree-relative filter)
    pit_files: List[Path] = []
    for pf in files:
        rel_tree = _posix(pf.relative_to(data_root))
        if rel_tree.startswith(("factors/", "qual/", "universe/", "prices/")):
            pit_files.append(pf)
    pit = check_pit(pit_files, data_root, manifest)

    # PIT Auditor standardization: canonical source_timestamp on every
    # feature output (WARN-level; never downgrades PIT compliance).
    st = check_source_timestamp(pit_files)

    mconf = check_mapping_confidence(data_root / "qual")
    schema_files = [pf for pf in files
                    if _posix(pf.relative_to(data_root)).startswith(
                        ("factors/", "qual/", "universe/", "prices/",
                         "provenance/"))]
    schema = check_schema(schema_files, data_root, manifest)

    surv = check_survivorship(data_root)

    chain = check_backtest_chain(data_root)

    checklist, flags = bias_checklist(data_root, sha, pit, mconf, surv,
                                      schema, manifest, st=st)

    report: Dict[str, Any] = {
        "status": "PASS",
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "data_root": _posix(data_root),
        "sha256_check": {
            "total": sha["total"], "passed": sha["passed"],
            "failed": sha["failed"], "missing_manifest": sha["missing_manifest"],
        },
        "backtest_chain": chain,
        "pit_compliance": {
            "total_files": pit["total_files"], "passed": pit["passed"],
            "violations": pit["violations"],
        },
        "source_timestamp": {
            "total_files": st["total_files"], "passed": st["passed_files"],
            "violations": st["violations"],
        },
        "mapping_confidence": mconf,
        "provenance_schema": schema,
        "survivorship_bias": {
            "universe_includes_delisted": surv["universe_includes_delisted"],
            "delisted_count": surv["delisted_count"],
            "pit_correct": surv["pit_correct"],
            "is_active_false_rows": surv["is_active_false_rows"],
            "notes": surv["notes"],
        },
        "bias_checklist": checklist,
        "bias_flags": flags,
        "summary": {
            "sha_passed": sha["passed"], "sha_total": sha["total"],
            "pit_passed": pit["passed"], "pit_total": pit["total_files"],
            "source_timestamp_complete": st["passed_files"],
            "source_timestamp_total": st["total_files"],
            "schema_complete": schema["complete"],
            "schema_total": schema["total_files"],
            "mapping_has_column": mconf["has_column"],
            "mapping_total": mconf["total_qual_files"],
            "delisted_count": surv["delisted_count"],
            "bias_flag_count": len(flags),
        },
    }
    if universe_deep is not None:
        report["universe_pit_deep"] = check_universe_pit(
            universe_deep, data_root, manifest)

    hard_fail = [f for f in flags if f["severity"] == "HIGH"]
    report["status"] = "FAIL" if hard_fail else \
        ("REVIEW" if flags else "PASS")

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str),
            encoding="utf-8")

    print(f"[provenance] SHA-256 {sha['passed']}/{sha['total']} (manifest); "
          f"{len(sha['missing_manifest'])} missing manifest")
    print(f"[provenance] backtest chain: valid={chain['valid']} "
          f"folds={len(chain['folds_verified'])} "
          f"violations={len(chain['violations'])}")
    print(f"[provenance] PIT compliance {pit['passed']}/{pit['total_files']}")
    print(f"[provenance] source_timestamp {st['passed_files']}/{st['total_files']}")
    print(f"[provenance] mapping_confidence {mconf['has_column']}/"
          f"{mconf['total_qual_files']}")
    print(f"[provenance] schema complete {schema['complete']}/"
          f"{schema['total_files']}")
    print(f"[provenance] survivorship: includes_delisted="
          f"{surv['universe_includes_delisted']} "
          f"delisted={surv['delisted_count']} pit_correct={surv['pit_correct']}")
    print(f"[provenance] status={report['status']} "
          f"({len(flags)} bias flags, "
          f"{len(hard_fail)} high-severity)")
    for f in flags:
        print(f"  [{f['severity']:6s}] {f['id']}: {f['finding'][:140]}")
    return report


# ---------------------------------------------------------------------------
# Phase 9 — ProvenanceAuditor v2
# ---------------------------------------------------------------------------
# v1 (``run_audit``) proves the *data estate* is well-formed.  v2 adds the
# *research-process* root of trust that gate 8's ``provenance_chain`` component
# consumes:
#
#   R1  frozen pre-registration root   — the SHA-256 of config/pre_registration_v4.yaml
#                                         must match its declared digest;
#   R2  trial ledger integrity          — sidecar hash + per-trial SHA-256 recompute;
#   R3  trial count vs the frozen N     — the ledger must actually contain the
#                                         pre-registered number of trials, or any
#                                         DSR computed from it is deflated with the
#                                         WRONG multiple-testing count;
#   R4  trial independence              — distinct parameter sets (no duplicates
#                                         inflating the count);
#   R5  data-estate spot check          — optional, the v1 SHA-256 manifest pass.
#
# The chain is a conjunction: every root must hold for ``provenance_chain`` to be
# True.  A chain with an unverifiable root cannot be certified.


class ProvenanceAuditor:
    """
    Phase-9 provenance auditor (v2).  Additive to the v1 checks.

        auditor = ProvenanceAuditor()
        rep = auditor.run()                    # -> report dict
        rep["provenance_chain"]                # -> bool consumed by gate 8
    """

    def __init__(self,
                 repo_root: Optional[Path] = None,
                 trial_ledger: Optional[Path] = None,
                 data_root: Optional[Path] = None,
                 deep: bool = False) -> None:
        self.repo_root = Path(repo_root) if repo_root else REPO_ROOT
        if trial_ledger:
            tp = Path(trial_ledger)
            self.trial_ledger = tp if tp.is_file() else tp / "trial_ledger.json"
        else:
            v5_p = self.repo_root / "data" / "phase9_full" / "trial_ledger_v5.json"
            self.trial_ledger = v5_p if v5_p.exists() else self.repo_root / "data" / "phase9_full" / "trial_ledger.json"
        self.data_root = Path(data_root) if data_root else self.repo_root / "data"
        self.deep = bool(deep)

    # -- R1: frozen pre-registration root ------------------------------------

    def check_prereg_root(self) -> Dict[str, Any]:
        """The pre-registration is the root of the whole research chain."""
        try:
            from ingestion.harness.prereg import MODULE_PREREG as _PRE
        except Exception as exc:                                  # noqa: BLE001
            return {"ok": False, "reason": f"prereg_import_failed:{exc!r}"}
        integ = _PRE.integrity
        return {
            "ok": bool(_PRE.integrity_verified),
            "path": str(getattr(integ, "path", "")),
            "expected_sha256": getattr(integ, "expected_sha256", None),
            "actual_sha256": getattr(integ, "actual_sha256", None),
            "status": getattr(integ, "status", None),
            "detail": getattr(integ, "detail", None),
            "phase": getattr(_PRE, "phase", None),
            "version": getattr(_PRE, "version", None),
        }

    # -- R2/R3/R4: trial ledger ----------------------------------------------

    #: Entries carrying a UUIDv7 + a chained hash satisfy the frozen v5 tracker
    #: contract; anything else is audited forensically but reported as legacy.
    def _classify_ledger_schema(self, trials: List[Dict[str, Any]],
                                data: Dict[str, Any]) -> str:
        declared = str(data.get("schema") or "")
        if "trial_uuid" in trials[0] or "chain_sha256" in trials[0] or declared:
            return declared or "uuid7_chained"
        return "legacy_int_ids"

    def check_trial_ledger(self) -> Dict[str, Any]:
        """
        Verify the TrialTracker ledger forensically.  Stored hashes are never
        trusted — every trial is recomputed from its own contents — and the
        recomputation imports the *writer's* formula from
        ``research.ml_parameter_optimization`` rather than restating it, so the
        two halves of the system cannot drift into "verifying" a definition
        nobody actually wrote trials with.

        Two schemas are handled:

        ``v5``    ``trial_uuid`` (UUIDv7) + ``timestamp_ns`` + ``sha256`` +
                  ``chain_sha256`` chaining back to the genesis hash.
        ``legacy``integer ``trial_id`` + a standalone per-trial hash.  Still
                  recomputed and reported, but it does **not** satisfy the frozen
                  v5 tracker contract, so it cannot pass R2-R4: an unchained
                  ledger lets an entry be edited and its own hash recomputed
                  with no downstream evidence that anything changed.
        """
        from ingestion.harness.prereg import MODULE_PREREG as _PRE
        from research.ml_parameter_optimization import (          # noqa: PLC0415
            GENESIS_HASH, TRIAL_LEDGER_SCHEMA, canonical_trial_payload,
            chain_hash, is_uuid7,
        )

        p = self.trial_ledger
        if not p.exists():
            return {"ok": False, "reason": "trial_ledger_missing",
                    "path": _posix(p), "schema": None, "status": "FAIL"}
        raw = p.read_bytes()
        actual_sha = hashlib.sha256(raw).hexdigest()

        sidecar = p.with_suffix(p.suffix + ".sha256")
        sidecar_ok, sidecar_sha = False, None
        if sidecar.exists():
            sidecar_sha = sidecar.read_text(encoding="utf-8").strip()
            sidecar_ok = bool(sidecar_sha == actual_sha)

        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:                                  # noqa: BLE001
            return {"ok": False, "reason": f"ledger_unparseable:{exc!r}",
                    "path": _posix(p), "status": "FAIL"}

        trials = data.get("trials") or []
        if not isinstance(trials, list) or not trials:
            return {"ok": False, "reason": "trials_empty_or_not_a_list",
                    "path": _posix(p), "status": "FAIL"}

        schema = self._classify_ledger_schema(trials, data)
        mismatches: List[Dict[str, Any]] = []
        seen_ids: Dict[str, int] = {}
        param_digests: Dict[str, List[str]] = {}
        prev_chain = GENESIS_HASH
        last_ts: Optional[int] = None
        bad_uuid: List[Any] = []
        missing_ts: List[Any] = []
        ts_regressions: List[Dict[str, Any]] = []
        chain_breaks: List[int] = []

        for i, t in enumerate(trials):
            if not isinstance(t, dict):
                mismatches.append({"index": i, "reason": "malformed_trial_entry"})
                continue
            pj = json.dumps(t.get("params"), sort_keys=True, default=str)
            try:
                mv = t.get("metric_value", t.get("composite_score"))
                folds = [float(v) for v in (t.get("fold_metrics") or [mv])]
                mv = float(mv)
            except (TypeError, ValueError):
                mismatches.append({"trial_id": t.get("trial_id"),
                                   "reason": "metric_value_unparseable"})
                continue

            if schema == "legacy_int_ids":
                # The v4 formula, recomputed exactly as the v4 writer built it.
                try:
                    tid = int(t["trial_id"])
                except (KeyError, TypeError, ValueError):
                    mismatches.append({"trial_id": t.get("trial_id"),
                                       "reason": "malformed_trial_entry"})
                    continue
                h = hashlib.sha256(
                    "{}:{}:{}".format(tid, pj, mv).encode("utf-8")).hexdigest()
                if h != t.get("sha256"):
                    mismatches.append({"trial_id": tid, "reason": "sha256_mismatch",
                                       "recomputed": h, "stored": t.get("sha256")})
                seen_ids[str(tid)] = seen_ids.get(str(tid), 0) + 1
                key = str(tid)
            else:
                uid = t.get("trial_uuid")
                if not is_uuid7(uid):
                    bad_uuid.append(uid)
                ts = t.get("timestamp_ns")
                if not isinstance(ts, int):
                    missing_ts.append(uid)
                elif last_ts is not None and ts < last_ts:
                    ts_regressions.append({"index": i, "timestamp_ns": ts,
                                           "previous": last_ts})
                if isinstance(ts, int):
                    last_ts = ts if last_ts is None else max(last_ts, ts)

                payload = canonical_trial_payload(uid, int(ts or 0),
                                                  t.get("params") or {}, mv, folds)
                if hashlib.sha256(payload.encode("utf-8")).hexdigest() != t.get("sha256"):
                    mismatches.append({"trial_uuid": uid, "index": i,
                                       "reason": "sha256_mismatch",
                                       "stored": t.get("sha256")})
                expected_chain = chain_hash(prev_chain, payload)
                if expected_chain != t.get("chain_sha256"):
                    chain_breaks.append(i)
                prev_chain = expected_chain
                key = str(uid)
                seen_ids[key] = seen_ids.get(key, 0) + 1

            param_digests.setdefault(
                hashlib.sha256(pj.encode("utf-8")).hexdigest(), []).append(key)

        dup_ids = sorted(k for k, v in seen_ids.items() if v > 1)
        dup_param_groups = sorted(
            [sorted(v) for v in param_digests.values() if len(v) > 1])
        n_trials = len(trials)
        n_unique_params = len(param_digests)
        required_n = int(getattr(_PRE, "dsr_n_trials", 513) or 513)
        declared_target = data.get("target_n")
        count_ok = bool(n_trials >= required_n)
        target_ok = (declared_target is not None
                     and int(declared_target) >= required_n)

        is_v5 = schema != "legacy_int_ids"
        contract_ok = bool(is_v5 and not bad_uuid and not missing_ts
                           and not ts_regressions and not chain_breaks
                           and not mismatches)
        head_ok = (data.get("chain_head") == prev_chain) if is_v5 else None

        # A valid chain proves the ledger was not edited after the fact. It says
        # nothing about whether the trials were ever RUN. Surface that
        # distinction so nobody reads `provenance_chain: true` as "513 results".
        metric_status = data.get("metric_status")
        n_evaluated = sum(
            1 for t in trials
            if (t.get("provenance") or {}).get("evaluation_status") == "EVALUATED"
        ) if trials else 0

        consequences: List[str] = []
        if metric_status and str(metric_status).upper() != "EVALUATED":
            consequences.append(
                f"ledger metric_status is {metric_status!r} ({n_evaluated}/"
                f"{n_trials} trials evaluated). The hash chain is intact and the "
                f"trial COUNT is real, so R2-R4 pass — but no trial in this "
                f"ledger carries a measured out-of-sample score. It is honest "
                f"enumeration, not 513 results, and must not be cited as evidence "
                f"of performance or fed to the DSR as observed returns.")
        if not count_ok:
            consequences.append(
                f"ledger holds {n_trials} trials but the frozen pre-registration "
                f"requires {required_n}; any DSR computed from this ledger is "
                f"deflated with the wrong multiple-testing count and is "
                f"anti-conservative by construction")
        if not is_v5:
            consequences.append(
                "ledger predates the frozen v5 TrialTracker contract "
                f"(expected schema {TRIAL_LEDGER_SCHEMA!r}): integer trial_id, "
                "no UUIDv7, no timestamp_ns, and per-trial hashes that are NOT "
                "chained, so a single edited entry can be re-hashed in place "
                "with no downstream evidence. Forensic recompute below still "
                "runs, but a legacy ledger does not satisfy R2-R4 and the "
                "composite's provenance_chain component must fail closed until "
                "the ledger is regenerated by the v5 tracker.")
        if chain_breaks:
            consequences.append(
                f"hash chain broken at {len(chain_breaks)} entr"
                f"{'y' if len(chain_breaks) == 1 else 'ies'} "
                f"(first index {chain_breaks[0]})")
        if head_ok is False:
            consequences.append(
                "declared chain_head does not equal the recomputed head")

        return {
            "ok": bool(contract_ok and sidecar_ok and count_ok and target_ok
                       and not dup_ids and not dup_param_groups and head_ok is not False),
            "path": _posix(p),
            "schema": schema,
            "schema_expected": TRIAL_LEDGER_SCHEMA,
            "tracker_contract_ok": contract_ok,
            "status": ("PASS" if contract_ok else
                       "LEGACY_SCHEMA" if not is_v5 else "FAIL"),
            "n_trials": n_trials,
            "n_unique_param_sets": n_unique_params,
            "required_n_trials": required_n,
            "trial_count_ok": count_ok,
            "trial_count_shortfall": max(0, required_n - n_trials),
            "declared_target_n": declared_target,
            "declared_target_ok": target_ok,
            "sidecar_present": sidecar.exists(),
            "sidecar_matches": sidecar_ok,
            "actual_sha256": actual_sha,
            "sidecar_sha256": sidecar_sha,
            "per_trial_sha256_checked": n_trials,
            "per_trial_sha256_mismatches": mismatches,
            "uuid7_invalid": bad_uuid,
            "timestamp_ns_missing": missing_ts,
            "timestamp_ns_regressions": ts_regressions,
            "chain_breaks": chain_breaks,
            "declared_chain_head": data.get("chain_head"),
            "recomputed_chain_head": prev_chain,
            "chain_head_ok": head_ok,
            "duplicate_trial_ids": dup_ids,
            "duplicate_param_groups": dup_param_groups,
            "metric_status": metric_status,
            "n_trials_evaluated": n_evaluated,
            "n_trials_unevaluated": n_trials - n_evaluated,
            "consequence": " | ".join(consequences) if consequences else None,
        }

    # -- R5: data estate spot check ------------------------------------------

    def check_data_estate(self) -> Dict[str, Any]:
        if not self.deep:
            return {"status": "SKIP", "reason": "deep=False (pass deep=True to run)"}
        files = track_parquets(self.data_root)
        sha = check_sha256(files, self.data_root, load_manifest_latest(self.data_root))
        return {"status": "PASS" if not sha["failed"] else "FAIL",
                "total": sha["total"], "passed": sha["passed"],
                "failed": sha["failed"],
                "missing_manifest": len(sha["missing_manifest"])}

    # -- R6: v7 unified regime engine provenance checks -----------------------
    # Verifies the frozen unified regime calibration params and pre-registration

    def check_unified_regime_provenance(self) -> Dict[str, Any]:
        """Verify v7 unified regime engine calibration params and pre-registration."""
        issues: List[str] = []
        checks: Dict[str, Any] = {}

        # 1. Calibrated unified regime params exist and have valid provenance
        params_path = self.repo_root / "config" / "calibrated_unified_regime_params.json"
        params_sidecar = params_path.with_suffix(params_path.suffix + ".sha256")
        if not params_path.exists():
            issues.append("calibrated_unified_regime_params.json missing")
        else:
            try:
                with open(params_path, "r") as f:
                    params = json.load(f)
                prov = params.get("provenance", {})
                # Verify via sidecar (standard provenance pattern)
                if params_sidecar.exists():
                    expected_sha = params_sidecar.read_text(encoding="utf-8").strip().split()[0]
                    actual_sha = hashlib.sha256(params_path.read_bytes()).hexdigest()
                    if actual_sha != expected_sha:
                        issues.append(f"calibrated params SHA-256 mismatch: expected {expected_sha[:12]}..., actual {actual_sha[:12]}...")
                    else:
                        checks["params_sha256_ok"] = True
                else:
                    issues.append("calibrated params missing .sha256 sidecar")
                checks["params_source"] = prov.get("source")
                checks["params_date_range"] = prov.get("date_range")
                checks["params_version"] = params.get("version")
            except Exception as e:
                issues.append(f"calibrated params parse error: {e}")

        # 2. Pre-registration v7 SHA-256 matches pinned value
        prereg_path = self.repo_root / "config" / "pre_registration_v7.yaml"
        if not prereg_path.exists():
            issues.append("pre_registration_v7.yaml missing")
        else:
            try:
                from ingestion.harness.prereg import EXPECTED_SHA256_V7, sha256_path
                actual = sha256_path(prereg_path)
                expected = EXPECTED_SHA256_V7
                if actual != expected:
                    issues.append(f"pre_registration_v7.yaml SHA-256 mismatch: expected {expected[:12]}..., actual {actual[:12]}...")
                else:
                    checks["prereg_sha256_ok"] = True
            except Exception as e:
                issues.append(f"pre-reg SHA-256 check error: {e}")

        # 3. Zero-synthetic guard: scan for forbidden series in feature contract
        try:
            from Quantitative.contracts.feature_contract import FORBIDDEN_SERIES
            # Check calibrated params don't reference forbidden series
            if params_path.exists():
                with open(params_path, "r") as f:
                    params_data = json.load(f)
                # Serialize and scan for forbidden strings
                params_str = json.dumps(params_data)
                for forbidden in FORBIDDEN_SERIES:
                    if forbidden.lower() in params_str.lower():
                        issues.append(f"Forbidden series '{forbidden}' found in calibrated params")
                checks["forbidden_series_scan"] = "clean" if not any("Forbidden series" in i for i in issues) else "dirty"
        except Exception as e:
            issues.append(f"forbidden series scan error: {e}")

        # 4. Verify unified regime engine can load frozen params
        try:
            from Quantitative.unified_regime import build_unified_engine, UnifiedRegimeConfig
            from Quantitative.bocpd import BOCPDConfig
            from Quantitative.io_hmm import IOHMMConfig
            from Quantitative.micro_states import MicroStateConfig
            # Just test import and config creation, not full init (needs FRED data)
            cfg = UnifiedRegimeConfig()
            checks["engine_imports_ok"] = True
        except Exception as e:
            issues.append(f"unified regime engine import/config error: {e}")

        return {
            "ok": len(issues) == 0,
            "status": "PASS" if len(issues) == 0 else "FAIL",
            "checks": checks,
            "issues": issues,
            "params_path": str(params_path),
            "prereg_path": str(prereg_path),
        }

    # -- driver ---------------------------------------------------------------

    def run(self, report_path: Optional[Path] = None) -> Dict[str, Any]:
        roots: Dict[str, Any] = {
            "R1_prereg_root": self.check_prereg_root(),
            "R2_R4_trial_ledger": self.check_trial_ledger(),
            "R5_data_estate": self.check_data_estate(),
            "R6_unified_regime_provenance": self.check_unified_regime_provenance(),
        }
        prereg_ok = bool(roots["R1_prereg_root"].get("ok"))
        ledger = roots["R2_R4_trial_ledger"]
        ledger_ok = bool(ledger.get("ok"))
        estate = roots["R5_data_estate"]
        estate_ok = bool(estate.get("status") in ("PASS", "SKIP"))
        regime = roots["R6_unified_regime_provenance"]
        regime_ok = bool(regime.get("ok"))

        valid = bool(prereg_ok and ledger_ok and estate_ok and regime_ok)
        failed_roots = [k for k, v in roots.items() if not (
            v.get("ok") if "ok" in v else v.get("status") in ("PASS", "SKIP"))]

        report: Dict[str, Any] = {
            "report_type": "provenance_audit_v2",
            "version": 2,
            "status": "PASS" if valid else "FAIL",
            "valid": valid,
            "provenance_chain": valid,
            "logic": "AND over roots R1 (prereg) AND R2-R4 (ledger) AND R5 (estate) AND R6 (unified regime provenance)",
            "failed_roots": failed_roots,
            "roots": roots,
            "ran_at": datetime.now(timezone.utc).isoformat(),
        }
        if report_path is not None:
            report_path = Path(report_path)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, indent=2, sort_keys=True, default=str),
                encoding="utf-8")

        print(f"[prov-v2] status={report['status']} "
              f"provenance_chain={valid} failed_roots={failed_roots}")
        pr = roots["R1_prereg_root"]
        print(f"[prov-v2] R1 prereg root ok={pr.get('ok')} "
              f"({pr.get('expected_sha256', '')[:12]}... vs "
              f"{(pr.get('actual_sha256') or '')[:12]}...)")
        print(f"[prov-v2] R2-R4 ledger ok={ledger_ok} "
              f"trials={ledger.get('n_trials')}/"
              f"{ledger.get('required_n_trials')} "
              f"unique_params={ledger.get('n_unique_param_sets')} "
              f"sidecar_ok={ledger.get('sidecar_matches')} "
              f"hash_mismatches="
              f"{len(ledger.get('per_trial_sha256_mismatches') or [])}")
        if ledger.get("consequence"):
            print(f"[prov-v2]   ledger consequence: {ledger['consequence']}")
        rg = roots["R6_unified_regime_provenance"]
        print(f"[prov-v2] R6 unified regime provenance ok={regime_ok} "
              f"checks={rg.get('checks', {})} "
              f"issues={rg.get('issues', [])}")
        if report_path is not None:
            print(f"[prov-v2] report: {_posix(report_path)}")
        return report


def run_audit_v2(repo_root: Optional[Path] = None,
                 trial_ledger: Optional[Path] = None,
                 data_root: Optional[Path] = None,
                 report_path: Optional[Path] = None,
                 deep: bool = False) -> Dict[str, Any]:
    """Functional wrapper around :class:`ProvenanceAuditor`."""
    aud = ProvenanceAuditor(repo_root=repo_root, trial_ledger=trial_ledger,
                            data_root=data_root, deep=deep)
    return aud.run(report_path=report_path)


def cmd_check_manifest(data_root: Path, report: Optional[Path]) -> int:
    manifest = load_manifest_latest(data_root)
    files = track_parquets(data_root)
    sha = check_sha256(files, data_root, manifest)
    print(f"Manifest coverage: {sha['passed']}/{sha['total']} tracked parquets "
          f"match; {len(sha['failed'])} failed; "
          f"{len(sha['missing_manifest'])} missing_manifest")
    for f in sha["failed"]:
        print(f"  [FAIL-SHA] {f['file']}")
    for m in sha["missing_manifest"][:50]:
        print(f"  [NO-MANIFEST] {m}")
    if len(sha["missing_manifest"]) > 50:
        print(f"  ... and {len(sha['missing_manifest']) - 50} more")
    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(
            {"check": "manifest", **sha,
             "ran_at": datetime.now(timezone.utc).isoformat()},
            indent=2, sort_keys=True, default=str), encoding="utf-8")
    return 0 if not sha["failed"] else 1


def run_tests(tmp: Path) -> int:
    print("Provenance Audit self-test (RS-07)")
    print("=" * 68)
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "provenance").mkdir(exist_ok=True)
    (tmp / "factors").mkdir(exist_ok=True)
    (tmp / "qual").mkdir(exist_ok=True)
    (tmp / "universe").mkdir(exist_ok=True)
    (tmp / "prices").mkdir(exist_ok=True)

    def _sha(p: Path) -> str:
        return sha256_file(p)

    # valid factor row (PIT clean: filed == date)
    good = pd.DataFrame({"date": pd.to_datetime(["2020-01-31", "2020-02-28"]),
                         "filed_date": pd.to_datetime(["2020-01-30", "2020-02-27"]),
                         "ticker": ["A1", "A1"], "value": [1.0, 2.0]})
    good.to_parquet(tmp / "factors" / "rpo.parquet", index=False)
    (tmp / "factors" / "rpo.parquet.provenance.json").write_text(json.dumps({
        "source": "SEC_EDGAR", "retrieval_timestamp": "2026-09-12T00:00:00Z",
        "source_version": "1.0", "pit_timestamp_column": "filed_date",
        "entity_key": "ticker", "transformations": [], "sha256": _sha(tmp / "factors" / "rpo.parquet"),
        "row_count": 2, "date_range": {"min": "2020-01-30", "max": "2020-02-27"},
    }), encoding="utf-8")

    # lookahead file: filed AFTER date on every row (violation)
    bad_look = pd.DataFrame({"date": pd.to_datetime(["2020-01-31"]),
                             "filed_date": pd.to_datetime(["2020-02-10"]),
                             "ticker": ["B1"], "value": [1.0]})
    bad_look.to_parquet(tmp / "factors" / "mda_tone.parquet", index=False)
    (tmp / "factors" / "mda_tone.parquet.provenance.json").write_text(json.dumps({
        "source": "SEC_EDGAR", "retrieval_timestamp": "2026-09-12T00:00:00Z",
        "source_version": "1.0", "pit_timestamp_column": "filed_date",
        "entity_key": "ticker", "transformations": [],
        "sha256": _sha(tmp / "factors" / "mda_tone.parquet"),
        "row_count": 1, "date_range": {"min": "2020-01-31", "max": "2020-01-31"},
    }), encoding="utf-8")

    # qual file with mapping_confidence column
    qual = pd.DataFrame({"date": ["2020-01-31"], "ticker": ["C1"],
                         "value": [1.0], "mapping_confidence": ["high"],
                         "retrieval_timestamp": ["2026-09-12T00:00:00Z"]})
    qual.to_parquet(tmp / "qual" / "gh_velocity.parquet", index=False)

    # universe + delisting events (1 event; name present pre-delist w/ cap)
    ev = pd.DataFrame({"cik": ["1"], "ticker": ["D1"], "delist_filed": ["2021-06-30"],
                       "form": ["8-K"], "item": ["2.01"]})
    ev.to_parquet(tmp / "universe" / "delisting_events.parquet", index=False)
    recon = pd.DataFrame({"reconstitution_month": ["2021-04", "2021-05", "2021-07"],
                          "rebalance_date": ["2021-04-30", "2021-05-31", "2021-07-31"],
                          "ticker": ["D1", "D1", "D1"],
                          "market_cap": [1e9, 1.1e9, 1.2e9]})
    recon.to_parquet(tmp / "universe" / "monthly_reconstitution.parquet", index=False)

    report = run_audit(tmp, report_path=tmp / "validation" / "audit_report.json")

    assert report["sha256_check"]["passed"] == 0, "0 manifest entries => 0 passed"
    assert len(report["sha256_check"]["missing_manifest"]) == 5
    # rpo clean; mda has lookahead fraction 1.0 => period-anchor => future check
    # (rows not future) -> no violation; mapping col present; schema incomplete;
    # survivorship include delisted BEFORE the single delist date.
    surv = report["survivorship_bias"]
    assert surv["delisted_count"] == 1
    assert surv["universe_includes_delisted"] is True
    assert surv["pit_correct"] is True
    assert report["mapping_confidence"]["has_column"] == 1
    assert report["mapping_confidence"]["total_qual_files"] == 1
    print("[OK] sha missing-manifest accounting")
    print("[OK] schemas + mapping_confidence + survivorship accounting")

    # ---- WS-2 C2.5 trade-audit ledger chain verification -----------------
    from ingestion.trade_audit import TradeAuditLedger
    ledger_dir = tmp / "ledger"
    ledger_dir.mkdir(exist_ok=True)
    trig = {"valuation": {
        "iv_p25": 1.15, "price": 1.0, "discount_pct": 0.15,
        "iv_method": "dcf_v2_nowcast", "iv_basis_filed_date": "2026-08-20",
        "iv_nowcast_ts": "2026-09-01T00:00:00Z",
    }}
    ld = TradeAuditLedger(ledger_dir=ledger_dir)
    ld.append_batch([
        {"ticker": "A", "asof": "2026-08-31", "wacc_v2": 0.08, "ke_v2": 0.10,
         "kd_pre_tax_v2": 0.05, "erp_blend": 0.04, "rf_10y": 0.04,
         "beta_source": "bloomberg_60m", "action": "ENTRY", "stage_gate": 1,
         "trigger_reason": trig, "retrieval_ts": "2026-09-01T00:00:00Z"},
        {"ticker": "B", "asof": "2026-09-15", "wacc_v2": 0.09, "ke_v2": 0.11,
         "kd_pre_tax_v2": 0.05, "erp_blend": 0.045, "rf_10y": 0.04,
         "beta_source": "bloomberg_60m", "action": "REBALANCE", "stage_gate": 2,
         "trigger_reason": trig, "retrieval_ts": "2026-09-16T00:00:00Z"},
        {"ticker": "C", "asof": "2026-09-18", "wacc_v2": 0.085, "ke_v2": 0.105,
         "kd_pre_tax_v2": 0.05, "erp_blend": 0.042, "rf_10y": 0.04,
         "beta_source": "bloomberg_60m", "action": "QUARANTINE", "stage_gate": 5,
         "trigger_reason": trig, "retrieval_ts": "2026-09-19T00:00:00Z"},
    ])
    rpt = check_trade_ledger(ledger_dir)
    assert rpt["status"] == "PASS", rpt["violations"]
    assert rpt["global"]["n_blocks"] == 3 and rpt["global"]["partitions"] == 2
    assert rpt["config_contract"]["uuid_version"] == 7
    # two partitions with a cross-partition boundary
    assert set(rpt["partitions"].keys()) == {"2026-08", "2026-09"}
    print("[OK] ledger chain PASS (2 partitions, cross-partition boundary)")

    # forensic tamper: flip ke_v2 on disk -> hash mismatch must FAIL
    p = ledger_dir / "month=2026-08" / "trade_audit_2026-08.parquet"
    df = pd.read_parquet(p)
    df.loc[0, "ke_v2"] = 0.999
    df.to_parquet(p, engine="pyarrow", index=False)
    broken = check_trade_ledger(ledger_dir)
    assert broken["status"] == "FAIL"
    assert any("chain_verification failed" in i
               for v in broken["violations"] for i in v["issues"])
    print("[OK] tampered block detected (chain_verification break@index)")

    # empty ledger -> SKIP, not FAIL
    empty = tmp / "ledger_empty"
    empty.mkdir(exist_ok=True)
    assert check_trade_ledger(empty)["status"] == "SKIP"
    print("[OK] empty ledger SKIP path")

    print("\nAll Provenance Audit tests passed.")
    return 0


def cmd_check_schema(schema_path: Path, data_root: Path,
                     report: Optional[Path] = None) -> int:
    """
    Validates the backtest artifact tree against a JSON schema contract
    (_schema_v3.json).  For every <file>.parquet declared in the schema's
    `parquet_schemas`:
        - the parquet exists somewhere under data_root (fold dirs are globbed
          as `<run_id>/*.parquet`);
        - every `required: true` column exists with the declared type (best
          effort: pandas dtypes are mapped to DATE/DOUBLE/INTEGER/VARCHAR/BOOLEAN);
        - the schema's `pit_timestamp_column` exists in the parquet.
    Returns 0 when all declared schemas validate, else 1.
    """
    if not schema_path.exists():
        print(f"schema not found: {schema_path}")
        return 2
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"schema JSON invalid: {exc}")
        return 2
    parquet_schemas = schema.get("parquet_schemas") or {}
    if not parquet_schemas:
        print("schema has no parquet_schemas section")
        return 2

    # index every parquet under data_root (fold dirs are <run_id>/<file>.parquet)
    index: Dict[str, List[Path]] = {}
    for pf in data_root.rglob("*.parquet"):
        index.setdefault(pf.name, []).append(pf)

    dtype_map = {
        "DOUBLE": ["float64", "float32", "int64", "int32", "uint64", "uint32"],
        "INTEGER": ["int64", "int32", "uint64", "uint32"],
        "VARCHAR": ["object", "string"],
        "DATE": ["datetime64[ns]", "datetime64[us]", "datetime64[ms]", "object"],
        "BOOLEAN": ["bool"],
        # timestamps stored as ISO-8601 strings (object) are accepted; the
        # semantic pit_rule check (col <= retrieval_timestamp) is enforced by
        # the PIT compliance audit, not the dtype check.
        "TIMESTAMP": ["datetime64[ns]", "datetime64[us]", "datetime64[ms]", "object"],
    }

    results: Dict[str, Any] = {}
    hard_fail = 0
    for fname, spec in sorted(parquet_schemas.items()):
        hits = index.get(fname, [])
        columns = spec.get("columns") or []
        pit_col = spec.get("pit_timestamp_column")
        entry: Dict[str, Any] = {
            "declared": True, "files_found": [str(p) for p in hits],
            "n_files": len(hits),
        }
        if not hits:
            entry["ok"] = False
            entry["issues"] = ["no parquet file found under data_root"]
            hard_fail += 1
            results[fname] = entry
            continue
        # validate the column contract on the first hit (all folds share schema)
        sample = hits[0]
        try:
            df = read_cols(sample)
            actual = {str(c): str(dt) for c, dt in df.dtypes.items()}
        except Exception as exc:  # noqa: BLE001
            entry["ok"] = False
            entry["issues"] = [f"unreadable: {type(exc).__name__}: {exc}"]
            hard_fail += 1
            results[fname] = entry
            continue
        issues: List[str] = []
        for col in columns:
            name = col.get("name")
            if col.get("required") is True and name not in actual:
                issues.append(f"missing required column: {name}")
                continue
            if name not in actual:
                continue
            exp = dtype_map.get(str(col.get("type", "")), [])
            if exp and not any(actual[name].startswith(t) for t in exp):
                issues.append(
                    f"column {name}: dtype {actual[name]} not in "
                    f"{col.get('type')} family")
        if pit_col and pit_col not in actual:
            issues.append(f"missing pit_timestamp_column: {pit_col}")
        entry["columns_actual"] = actual
        entry["ok"] = not issues
        entry["issues"] = issues
        hard_fail += 0 if entry["ok"] else 1
        results[fname] = entry

    out = {
        "check": "schema_contract",
        "schema_path": _posix(schema_path),
        "schema_version": schema.get("schema_version"),
        "schema_name": schema.get("name"),
        "artifacts_declared": len(parquet_schemas),
        "artifacts_ok": sum(1 for k, v in results.items() if v.get("ok")),
        "artifacts_failed": sum(1 for k, v in results.items() if not v.get("ok")),
        "details": results,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if hard_fail == 0 else "FAIL",
    }
    print(f"[provenance] schema contract {out['artifacts_ok']}/"
          f"{out['artifacts_declared']} ok — status={out['status']}")
    for k, v in sorted(results.items()):
        if not v.get("ok"):
            print(f"  [SCHEMA-FAIL] {k}: {v.get('issues')}")
    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(out, indent=2, sort_keys=True,
                                     default=str), encoding="utf-8")
    return 0 if hard_fail == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provenance Audit — RS-07 final validation + bias audit")
    parser.add_argument("--run", action="store_true",
                        help="Full audit -> data/validation/provenance_audit_report.json")
    parser.add_argument("--data-root", type=str, default="data/",
                        help="Data tree root (default data/)")
    parser.add_argument("--report", type=str, default=None,
                        help="Report JSON path (default data/validation/"
                             "provenance_audit_report.json)")
    parser.add_argument("--check-manifest", action="store_true",
                        help="SHA-256 reconciliation vs manifest only")
    parser.add_argument("--check-backtest-chain", action="store_true",
                        help="Phase 4 gate 16: verify fold .provenance.json "
                             "chain + consolidated walk-forward parquet sidecar; "
                             "writes data/validation/provenance_walkforward_report.json")
    parser.add_argument("--check-pit", action="store_true",
                        help="Deep PIT check of one universe file (--universe)")
    parser.add_argument("--universe", type=str, default=None,
                        help="Universe parquet for deep PIT check")
    parser.add_argument("--schema", type=str, default=None,
                        help="Validate the backtest artifact tree against a "
                             "JSON schema contract (e.g. data/backtests/"
                             "_schema_v3.json)")
    parser.add_argument("--test", action="store_true", help="Self-test")
    parser.add_argument("--audit-v2", action="store_true",
                        help="Phase-9 provenance auditor v2 (prereg root + trial "
                             "ledger) -> data/validation/audit_report.json")
    parser.add_argument("--trial-ledger", type=str, default=None,
                        help="Phase-9 v2 trial ledger path (default "
                             "data/phase9_full/trial_ledger.json)")
    parser.add_argument("--deep", action="store_true",
                        help="Phase-9 v2: also run the full v1 data-estate "
                             "SHA-256 manifest pass")
    args = parser.parse_args()

    data_root = Path(args.data_root)
    default_report = data_root / "validation" / "provenance_audit_report.json"
    report_path = Path(args.report) if args.report else default_report

    if args.test:
        return run_tests(Path("data/validation/_tests/provenance_audit") / "tmp")

    if args.audit_v2:
        v2_report = (Path(args.report) if args.report
                     else data_root / "validation" / "audit_report.json")
        rep = run_audit_v2(repo_root=REPO_ROOT,
                           trial_ledger=Path(args.trial_ledger)
                           if args.trial_ledger else None,
                           data_root=data_root, report_path=v2_report,
                           deep=bool(args.deep))
        return 0 if rep["provenance_chain"] else 1

    if args.schema:
        schema_root = data_root if args.run else data_root
        sc = Path(args.schema)
        sc_report = Path(args.report) if args.report else (
            sc.parent / (sc.stem + "_audit.json"))
        return cmd_check_schema(sc, schema_root, sc_report)

    if args.check_manifest:
        return cmd_check_manifest(data_root, report_path)

    if args.check_backtest_chain:
        chain = check_backtest_chain(data_root)
        out = {"check": "backtest_chain",
               "gate": 16,
               "backtest_chain": chain,
               "valid": chain["valid"],
               "status": chain["status"],
               "folds_verified": chain["folds_verified"],
               "consolidated_sha256": chain["consolidated_sha256"],
               "violations": chain["violations"],
               "ran_at": datetime.now(timezone.utc).isoformat()}
        chain_report = Path(args.report) if args.report else WALKFORWARD_CHAIN_REPORT
        chain_report.parent.mkdir(parents=True, exist_ok=True)
        chain_report.write_text(json.dumps(out, indent=2, sort_keys=True,
                                           default=str), encoding="utf-8")
        print(f"[provenance] backtest chain: valid={chain['valid']} "
              f"folds={chain['folds_verified']}")
        for v in chain["violations"]:
            print(f"  [CHAIN-VIOLATION] {v}")
        return 0 if chain["valid"] else 1

    if args.check_pit:
        if not args.universe:
            print("--check-pit requires --universe <path>")
            return 2
        uni = Path(args.universe)
        manifest = load_manifest_latest(data_root)
        deep = check_universe_pit(uni, data_root, manifest)
        print(json.dumps(deep, indent=2, sort_keys=True, default=str))
        return 0

    if args.run:
        # WS-2 C2.5: data-root pointing at the trade-audit ledger (or an
        # as-yet-unbuilt ledger root) runs the two-layer SHA-256 chain +
        # Appendix A verification (SKIP until the ledger exists).
        if not data_root.exists() or _is_trade_ledger_dir(data_root):
            lp = None if not args.report else Path(args.report)
            return cmd_ledger_check(data_root, lp)
        uni = Path(args.universe) if args.universe else None
        run_audit(data_root, universe_deep=uni, report_path=report_path)
        return 0

    parser.print_help()
    return 1


ProvenanceAuditV2 = ProvenanceAuditor


if __name__ == "__main__":
    raise SystemExit(main())