"""
SEC RPO Parser — Phase 1 ingestion  (big-pickle-worker)

Parses XBRL deferred-revenue / contract-liability / remaining-performance
obligation facts from SEC EDGAR `companyfacts` JSON files (local cache or the
bulk zip) into a PIT-clean panel:

    data/factors/rpo.parquet
    columns: date, cik, ticker, deferred_rev, contract_liab,
             rpo_actual, rpo_imputed, rpo_is_imputed,
             imputation_model_version, filed_date

Semantics (Data_Strategy.md §4 SYNTHETIC VALIDATION — binding):
  - `date`       = period end (`end` of the XBRL fact)
  - `filed_date` = SEC filing timestamp (PIT anchor; data becomes available here)
  - `deferred_rev` = DeferredRevenue                    (fallback Current + Noncurrent)
  - `contract_liab` = ContractLiabilities               (fallback Current + Noncurrent)
  - `rpo_actual`  = RemainingPerformanceObligations tag, ONLY for period ends >= 2018
                    (ASC 606 mandatory); NaN otherwise
  - `rpo_imputed` = rpo_actual if available, else deferred_rev + contract_liab
  - `rpo_is_imputed` = True for every pre-2018 row AND for 2018+ rows lacking a
                    reported RPO tag (mandatory on every row)
  - `imputation_model_version` = "deferred_rev+contract_liab v1" on imputed rows,
                    else None (strategy §4 PIT flagging)

PIT discipline: every output row is validated by
`pit_validator.PITValidator.validate_pit(timestamp=date, trade=filed_date)`
(period end must be <= filing date; null keys rejected).

All outputs are also logged to the DuckDB FactorStore (factor date = filed_date,
the true PIT availability timestamp) with SHA-256 provenance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ingestion.harness.factor_store import FactorStore
from ingestion.harness.pit_validator import PITValidator, PITValidationError

# ----------------------------------------------------------------------------
# Pre-registered tag sets (SEC us-gaap concept names)
# ----------------------------------------------------------------------------
RPO_TAGS = {
    "RevenueRemainingPerformanceObligation",
    "RemainingPerformanceObligations",
    "RemainingPerformanceObligation",
    "ContractWithCustomerLiabilityRemainingPerformanceObligation",
}
# ASC 606 revenue proxy tag for RPO (dispatch contract: the §6 RPO concept
# chain is us-gaap:DeferredRevenue / ContractLiabilities /
# RevenueFromContractsWithCustomers, with the revenue tag standing in for RPO
# for 2018+ period ends when no dedicated RPO tag is reported).
RPO_PROXY_TAGS = {
    "RevenueFromContractsWithCustomers",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
}
DEFERRED_REV_TAGS = {
    "DeferredRevenue",            # lump sum (pre-ASC606 and some filers post)
    "DeferredRevenueCurrent",
    "DeferredRevenueNoncurrent",
}
CONTRACT_LIAB_TAGS = {
    "ContractLiabilities",
    "ContractLiabilitiesCurrent",
    "ContractLiabilitiesNoncurrent",
    "ContractWithCustomerLiability",                 # ASC 606 designation (e.g. AAPL)
    "ContractWithCustomerLiabilityCurrent",          # US-GAAP current/noncurrent split
    "ContractWithCustomerLiabilityNoncurrent",
}
ACCEPTED_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A"}
ASC606_PERIOD_START = pd.Timestamp("2018-01-01")
IMPUTATION_MODEL_VERSION = "deferred_rev+contract_liab v1"

DEFAULT_FACTS_DIR = "data/source/sec/companyfacts"
DEFAULT_OUT = "data/factors/rpo.parquet"
DEFAULT_FACTOR_STORE = "data/harness/factors.duckdb"

REQUIRED_OUTPUT_COLS = [
    "date", "cik", "ticker", "deferred_rev", "contract_liab",
    "rpo_actual", "rpo_imputed", "rpo_is_imputed",
    "imputation_model_version", "filed_date",
]

# get_rpo_facts() output contract (dispatch: supporting XBRL concept chain)
RPO_FACTS_COLS = [
    "cik", "filing_date", "deferred_revenue",
    "contract_liabilities", "rpo", "concept_version",
]


# ----------------------------------------------------------------------------
# Fact extraction
# ----------------------------------------------------------------------------

def _concept_frame(concept: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flattens one companyfacts concept block into fact dicts."""
    rows: List[Dict[str, Any]] = []
    for unit, vals in concept.get("units", {}).items():
        for v in vals:
            if v.get("end") is None or v.get("val") is None:
                continue
            rows.append({
                "end": v["end"],
                "val": float(v["val"]),
                "filed": v.get("filed"),
                "form": v.get("form", ""),
                "accn": v.get("accn", ""),
                "start": v.get("start"),
            })
    return rows


def parse_cik_facts(data: Dict[str, Any], ticker: str
                    ) -> pd.DataFrame:
    """
    Builds the merged (period-end x tag) fact frame for one CIK from its raw
    companyfacts JSON.  Returns a DataFrame with one row per (end, tag).
    """
    cik = str(data.get("cik"))
    gaap = data.get("facts", {}).get("us-gaap", {})
    records: List[Dict[str, Any]] = []
    for tag in set(RPO_TAGS) | set(DEFERRED_REV_TAGS) | set(CONTRACT_LIAB_TAGS):
        concept = gaap.get(tag)
        if not concept:
            continue
        for r in _concept_frame(concept):
            if r["form"] not in ACCEPTED_FORMS and r["form"] != "":
                continue  # only periodic reports carry point-in-time balances
            records.append({
                "cik": cik, "ticker": ticker,
                "end": r["end"], "tag": tag, "val": r["val"],
                "filed": r["filed"], "form": r["form"], "accn": r["accn"],
            })
    if not records:
        return pd.DataFrame(columns=["cik", "ticker", "end", "tag", "val", "filed", "form", "accn"])
    return pd.DataFrame(records)


def _sum_breakdown(facts: pd.DataFrame, tags: set, end: str,
                   filed_before: Optional[str] = None) -> Optional[float]:
    """Sums a tag family (Current+Noncurrent) at one period end, PIT-filtered."""
    sub = facts[(facts["end"] == end) & (facts["tag"].isin(tags))]
    if filed_before is not None:
        sub = sub[pd.to_datetime(sub["filed"], errors="coerce") <= filed_before]
    if sub.empty:
        return None
    return float(sub["val"].sum())


def build_rpo_panel(facts_dir: Path, sample_n: Optional[int] = None,
                    progress: bool = True) -> pd.DataFrame:
    """
    Scans every companyfacts JSON in `facts_dir`, builds the RPO panel.

    PIT rule per (cik, ticker, period end): keep the EARLIEST filing that
    reports the fact (first availability — no lookahead).
    """
    files = sorted(facts_dir.glob("*.json"))
    if sample_n is not None and sample_n > 0:
        files = files[:sample_n]

    all_rows: List[Dict[str, Any]] = []
    n_skipped = 0

    for i, f in enumerate(files):
        if progress and (i % 25 == 0 or i == len(files) - 1):
            print(f"  [{i + 1}/{len(files)}] {f.name}", flush=True)
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"  WARN: unreadable {f.name}: {e}")
            n_skipped += 1
            continue

        ticker = f.stem
        facts = parse_cik_facts(data, ticker)
        if facts.empty:
            n_skipped += 1
            continue

        cik = facts["cik"].iloc[0]
        ends = sorted(facts["end"].unique())
        for end in ends:
            end_ts = pd.Timestamp(end)
            end_rows = facts[facts["end"] == end]

            # earliest-filed fact per tag family (PIT)
            def _earliest(tags: set) -> Optional[Tuple[float, str]]:
                sub = end_rows[end_rows["tag"].isin(tags)]
                if sub.empty:
                    return None
                sub = sub.copy()
                sub["filed_ts"] = pd.to_datetime(sub["filed"], errors="coerce")
                best = sub.sort_values(["filed_ts", "form"], ascending=[True, False]).iloc[0]
                return best["val"], str(best["filed"])

            # Deferred revenue: lump tag first, else Current + Noncurrent
            def _sum_by_priority(priority_tags: List[set]) -> Optional[Tuple[float, str]]:
                for tags in priority_tags:
                    if not tags & set(end_rows["tag"]):
                        continue
                    sub = end_rows[end_rows["tag"].isin(tags)]
                    if all(sub["val"].isnull()):
                        continue
                    filed_ts = pd.to_datetime(sub["filed"], errors="coerce").min()
                    return float(sub["val"].sum()), str(filed_ts.date())
                return None

            deferred = _sum_by_priority([
                {"DeferredRevenue"},
                {"DeferredRevenueCurrent", "DeferredRevenueNoncurrent"},
                {"DeferredRevenueCurrent"},
                {"DeferredRevenueNoncurrent"},
            ])
            contract = _sum_by_priority([
                {"ContractLiabilities"},
                {"ContractWithCustomerLiability"},
                {"ContractLiabilitiesCurrent", "ContractLiabilitiesNoncurrent"},
                {"ContractWithCustomerLiabilityCurrent", "ContractWithCustomerLiabilityNoncurrent"},
                {"ContractLiabilitiesCurrent"},
                {"ContractWithCustomerLiabilityCurrent"},
            ])
            rpo = _earliest(RPO_TAGS)

            deferred_rev = deferred[0] if deferred else None
            contract_liab = contract[0] if contract else None
            rpo_actual = (rpo[0] if rpo and end_ts >= ASC606_PERIOD_START else None)

            if deferred_rev is None and contract_liab is None and rpo_actual is None:
                if rpo and end_ts < ASC606_PERIOD_START:
                    pass  # pre-2018 RPO tag exists but is not "actual" — impute below
                else:
                    continue  # no signal at this period end

            # Imputation (mandatory flag on every row)
            if rpo_actual is not None:
                rpo_imputed = rpo_actual
                is_imputed = False
                model = None
            else:
                rpo_imputed = (deferred_rev or 0.0) + (contract_liab or 0.0)
                is_imputed = True
                model = IMPUTATION_MODEL_VERSION

            filed_date = min(
                [d for d in (deferred[1] if deferred else None,
                             contract[1] if contract else None,
                             rpo[1] if rpo else None) if d]
            ) if any(d for d in (deferred, contract, rpo)) else None
            if filed_date is None:
                continue

            all_rows.append({
                "date": end_ts.date(),
                "cik": cik,
                "ticker": ticker,
                "deferred_rev": deferred_rev,
                "contract_liab": contract_liab,
                "rpo_actual": rpo_actual,
                "rpo_imputed": rpo_imputed,
                "rpo_is_imputed": bool(is_imputed),
                "imputation_model_version": model,
                "filed_date": pd.Timestamp(filed_date).date(),
            })

    panel = pd.DataFrame(all_rows, columns=REQUIRED_OUTPUT_COLS)
    if panel.empty:
        return panel

    # PIT dedup: per (cik, ticker, date) keep the EARLIEST filed date
    panel = panel.sort_values(["cik", "ticker", "date", "filed_date"]) \
                 .drop_duplicates(subset=["cik", "ticker", "date"], keep="first") \
                 .reset_index(drop=True)
    panel["rpo_is_imputed"] = panel["rpo_is_imputed"].astype(bool)

    # Validation hook (Strategy §8 — PIT mandatory)
    PITValidator.validate_pit(
        panel,
        timestamp_col="date",       # period end
        trade_date_col="filed_date",  # data availability anchor
        required_cols=["date", "cik", "ticker", "rpo_imputed", "rpo_is_imputed", "filed_date"],
    )
    return panel


# ----------------------------------------------------------------------------
# Point-in-time fact interface (dispatch contract)
# ----------------------------------------------------------------------------

def _load_companyfacts_for_cik(cik: Any, facts_dir: Path) -> Optional[Tuple[Dict[str, Any], str]]:
    """Scans a companyfacts cache dir for the file whose JSON cik matches."""
    cik_s = str(cik).lstrip("0")
    facts_dir = Path(facts_dir)
    if not facts_dir.exists():
        return None
    for f in sorted(facts_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if str(data.get("cik", "")).lstrip("0") == cik_s:
            return data, f.stem
    return None


def get_rpo_facts(
    cik: Any,
    start_date: Any = None,
    end_date: Any = None,
    companyfacts: Optional[Dict[str, Any]] = None,
    facts_dir: Optional[Path] = None,
    ticker: Optional[str] = None,
) -> pd.DataFrame:
    """
    Point-in-time RPO fact interface (dispatch contract).

      get_rpo_facts(cik, start_date, end_date) -> DataFrame with EXACT columns
        cik, filing_date, deferred_revenue, contract_liabilities, rpo,
        concept_version

    PIT key = FILE DATE (the SEC filing date of the fact), NOT period end.
    `start_date`/`end_date` filter the FILING-date window (inclusive; both
    optional).  One row per filing (accession) that reports any of the concept
    chain us-gaap:DeferredRevenue / ContractLiabilities /
    RevenueFromContractsWithCustomers (RPO proxy, 2018+ period ends).

    `rpo` resolution per filing (:binding; recorded in `concept_version`):
      1. dedicated RPO tag (RemainingPerformanceObligation & siblings) when any
         disclosed period end >= 2018-01-01 (ASC 606);
      2. else RevenueFromContractsWithCustomers (ASC 606 revenue proxy) when any
         period end >= 2018-01-01;
      3. else imputed deferred_revenue + contract_liabilities
         ("DeferredRevenue+ContractLiabilities (imputed)");

    Pure logic: accepts an in-memory companyfacts dict (`companyfacts`).  When
    omitted, loads the matching cache file under `facts_dir` by CIK
    (I/O convenience only — the parsing itself is side-effect free).
    """
    cik_s = str(cik)
    if companyfacts is None:
        if facts_dir is None:
            raise ValueError("get_rpo_facts: pass companyfacts (pure) or facts_dir "
                             "(file-backed convenience) — not both None")
        loaded = _load_companyfacts_for_cik(cik_s, Path(facts_dir))
        if loaded is None:
            return pd.DataFrame(columns=RPO_FACTS_COLS)
        companyfacts, _ = loaded

    start_ts = pd.Timestamp(start_date) if start_date is not None else None
    end_ts = pd.Timestamp(end_date) if end_date is not None else None

    gaap = (companyfacts.get("facts", {}) or {}).get("us-gaap", {}) or {}
    tag_sets = {"def": DEFERRED_REV_TAGS, "cl": CONTRACT_LIAB_TAGS,
                "rpo": RPO_TAGS, "proxy": RPO_PROXY_TAGS}

    rows: List[Dict[str, Any]] = []
    for tag, concept in gaap.items():
        if tag not in set(RPO_TAGS) | set(RPO_PROXY_TAGS) | set(DEFERRED_REV_TAGS) | set(CONTRACT_LIAB_TAGS):
            continue
        for unit, vals in (concept.get("units", {}) or {}).items():
            for v in vals:
                if v.get("val") is None:
                    continue
                filed = v.get("filed")
                if not filed:
                    continue
                filed_ts = pd.Timestamp(filed)
                if start_ts is not None and filed_ts < start_ts:
                    continue
                if end_ts is not None and filed_ts > end_ts:
                    continue
                if v.get("form") not in ACCEPTED_FORMS and v.get("form") != "":
                    continue
                # Accession is the true filing key; when absent (custom caches)
                # synthesise a stable composite key from filing date + tag + end.
                accn = v.get("accn") or f"{cik_s}|{filed_ts.date()}|{tag}|{v.get('end') or ''}"
                rows.append({
                    "accn": str(accn), "filed_ts": filed_ts,
                    "end": v.get("end"), "tag": tag,
                    "val": float(v["val"]) if not isinstance(v["val"], str) else None,
                    "form": v.get("form", ""),
                })
    if not rows:
        return pd.DataFrame(columns=RPO_FACTS_COLS)
    facts = pd.DataFrame(rows)

    out_rows: List[Dict[str, Any]] = []
    for accn, grp in facts.groupby("accn", sort=False):
        grp = grp.sort_values("filed_ts")
        filing_date = grp["filed_ts"].iloc[0].normalize()
        # any disclosed period end >= ASC606 => RPO / proxy branch eligible
        ends = [e for e in grp["end"] if e is not None]
        any_606 = any(pd.Timestamp(e) >= ASC606_PERIOD_START for e in ends)

        deferred = grp.loc[grp["tag"].isin(DEFERRED_REV_TAGS) & grp["val"].notna(), "val"]
        contract = grp.loc[grp["tag"].isin(CONTRACT_LIAB_TAGS) & grp["val"].notna(), "val"]
        d_val = float(deferred.sum()) if not deferred.empty else None
        c_val = float(contract.sum()) if not contract.empty else None

        rpo_facts = grp[grp["tag"].isin(RPO_TAGS) & grp["val"].notna()]
        proxy_facts = grp[grp["tag"].isin(RPO_PROXY_TAGS) & grp["val"].notna()]

        if not rpo_facts.empty and any_606:
            rpo_val = float(rpo_facts["val"].iloc[0])
            concept_version = str(rpo_facts["tag"].iloc[0])
        elif not proxy_facts.empty and any_606:
            rpo_val = float(proxy_facts["val"].iloc[0])
            concept_version = str(proxy_facts["tag"].iloc[0])
        elif d_val is not None or c_val is not None:
            rpo_val = (d_val or 0.0) + (c_val or 0.0)
            concept_version = "DeferredRevenue+ContractLiabilities (imputed)"
        else:
            continue

        out_rows.append({
            "cik": cik_s,
            "filing_date": filing_date.date(),
            "deferred_revenue": d_val,
            "contract_liabilities": c_val,
            "rpo": rpo_val,
            "concept_version": concept_version,
        })

    out = pd.DataFrame(out_rows, columns=RPO_FACTS_COLS)
    return out.sort_values("filing_date").reset_index(drop=True) if not out.empty else out


# ----------------------------------------------------------------------------
# Persistence + provenance
# ----------------------------------------------------------------------------

def calculate_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_outputs(panel: pd.DataFrame, out_path: Path,
                  facts_dir: Path, sample_n: Optional[int]) -> Dict[str, Any]:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    panel.to_parquet(out_path, index=False)

    prov = {
        "source": "SEC_EDGAR_companyfacts",
        "source_dir": str(facts_dir),
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "transformations": [
            "xbrl_tag_extraction",
            "deferred_rev+contract_liab imputation (pre-2018)",
            "rpo_is_imputed flagging",
            "pit_dedup_earliest_filed",
        ],
        "pit_timestamp_column": "filed_date",
        "entity_key": "cik|ticker",
        "row_count": int(len(panel)),
        "rpo_imputed_rows": int(panel["rpo_is_imputed"].sum()),
        "sha256": calculate_sha256(out_path),
        "sample_n": sample_n,
    }
    prov_path = out_path.with_suffix(".parquet.provenance.json")
    prov_path.write_text(json.dumps(prov, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {out_path} — {len(panel)} rows (imputed: {prov['rpo_imputed_rows']})")
    print(f"Provenance: {prov_path}")
    return prov


def write_factor_store(panel: pd.DataFrame, store_path: str) -> None:
    """
    Logs rpo factors into the DuckDB FactorStore.
    Factor-panel date = filed_date (true PIT availability); period-end is kept
    in provenance metadata.
    """
    if panel.empty:
        print("No rows to write to factor store.")
        return
    store = FactorStore(db_path=store_path)

    df_impl = panel[["filed_date", "ticker", "rpo_imputed"]].rename(
        columns={"filed_date": "date", "ticker": "ticker", "rpo_imputed": "value"})
    df_impl["date"] = pd.to_datetime(df_impl["date"])
    prov_impl = {
        "source": "SEC_EDGAR_companyfacts",
        "description": "rpo_imputed (actual or deferred_rev+contract_liab) — factor date = filed_date (PIT)",
        "transformations": ["xbrl_tag_extraction", "imputed_rpo_flag"],
    }
    store.write_factor(df_impl, "rpo_imputed", prov_impl)

    df_flag = panel[["filed_date", "ticker", "rpo_is_imputed"]].rename(
        columns={"filed_date": "date", "ticker": "ticker", "rpo_is_imputed": "value"})
    df_flag["value"] = df_flag["value"].astype(float)
    df_flag["date"] = pd.to_datetime(df_flag["date"])
    prov_flag = {
        "source": "SEC_EDGAR_companyfacts",
        "description": "rpo_is_imputed flag (1.0/0.0) — factor date = filed_date (PIT)",
    }
    store.write_factor(df_flag, "rpo_is_imputed", prov_flag)


# ----------------------------------------------------------------------------
# Synthetic test data + self-test
# ----------------------------------------------------------------------------

def build_synthetic_companyfacts(n_ciks: int = 12) -> Dict[str, Dict[str, Any]]:
    """Synthetic companyfacts dicts covering pre/post 2018, with/without RPO."""
    from collections import OrderedDict
    out: Dict[str, Dict[str, Any]] = OrderedDict()
    for i in range(n_ciks):
        ticker = f"SYN{i + 1:03d}"
        cik = 100000 + i
        deferred_entries = [
            {"end": "2016-12-31", "val": 1000.0 + i, "filed": "2017-02-10", "form": "10-K"},
            {"end": "2017-12-31", "val": 1200.0 + i, "filed": "2018-02-12", "form": "10-K"},
            {"end": "2018-12-31", "val": 1400.0 + i, "filed": "2019-02-15", "form": "10-K"},
            {"end": "2019-12-31", "val": 1600.0 + i, "filed": "2020-02-13", "form": "10-K"},
        ]
        contract_entries = [
            {"end": "2018-12-31", "val": 200.0 + i, "filed": "2019-02-15", "form": "10-K"},
            {"end": "2019-12-31", "val": 300.0 + i, "filed": "2020-02-13", "form": "10-K"},
        ]
        rpo_entries = ([] if i % 2 == 0 else [
            {"end": "2018-12-31", "val": 1500.0 + i, "filed": "2019-02-15", "form": "10-K"},
            {"end": "2019-12-31", "val": 1800.0 + i, "filed": "2020-02-13", "form": "10-K"},
        ])
        gaap = {}
        # DeferredRevenue current+noncurrent breakdown (pre-2018 style)
        gaap["DeferredRevenueCurrent"] = {"units": {"USD": [
            {"end": e["end"], "val": e["val"], "filed": e["filed"], "form": e["form"]} for e in deferred_entries]}}
        gaap["ContractLiabilities"] = {"units": {"USD": contract_entries}}
        if rpo_entries:
            gaap["RevenueRemainingPerformanceObligation"] = {"units": {"USD": rpo_entries}}
        out[ticker] = {"cik": cik, "entityName": f"Synthetic {ticker}", "facts": {"us-gaap": gaap}}
    return out


def run_tests(tmp: Path) -> int:
    print("SEC RPO Parser self-test")
    print("=" * 60)

    # 1) build synthetic data and parse
    synth = build_synthetic_companyfacts(n_ciks=12)
    test_dir = tmp / "companyfacts"
    test_dir.mkdir(parents=True, exist_ok=True)
    for tk, data in synth.items():
        (test_dir / f"{tk}.json").write_text(json.dumps(data), encoding="utf-8")

    panel = build_rpo_panel(test_dir, progress=False)
    assert not panel.empty, "panel empty"
    assert list(panel.columns) == REQUIRED_OUTPUT_COLS, f"schema mismatch: {list(panel.columns)}"
    print(f"[OK] panel built: {len(panel)} rows x {len(panel.columns)} cols; columns exact match")

    # 2) mandatory rpo_is_imputed flag on every row (no NaN, no missing)
    assert panel["rpo_is_imputed"].notna().all()
    assert panel["rpo_is_imputed"].dtype == bool
    print("[OK] rpo_is_imputed present and boolean on every row (no nulls)")

    # 3) pre-2018 rows: imputed ALWAYS
    pre = panel[pd.to_datetime(panel["date"]) < pd.Timestamp("2018-01-01")]
    assert not pre.empty
    assert pre["rpo_is_imputed"].all(), "pre-2018 row not flagged imputed"
    assert (pre["imputation_model_version"] == IMPUTATION_MODEL_VERSION).all()
    np_recheck = pre["rpo_imputed"].round(4) == (pre["deferred_rev"].fillna(0) + pre["contract_liab"].fillna(0)).round(4)
    assert np_recheck.all(), "pre-2018 rpo_imputed != deferred_rev + contract_liab"
    print(f"[OK] pre-2018: {len(pre)} rows all imputed via deferred_rev + contract_liab")

    # 4) 2018+ with actual RPO: rpo_imputed == rpo_actual, flag False
    post = panel[pd.to_datetime(panel["date"]) >= pd.Timestamp("2018-01-01")]
    # synthetic gives RPO tags to tickers with EVEN index i -> suffixes 002..012
    odd = post[post["ticker"].str.endswith(("002", "004", "006", "008", "010", "012"))]
    assert not odd.empty and odd["rpo_actual"].notna().all()
    assert ~odd["rpo_is_imputed"].any(), "actual-RPO row flagged imputed"
    assert (odd["rpo_imputed"] == odd["rpo_actual"]).all()
    print(f"[OK] 2018+ actual RPO rows ({len(odd)}): rpo_imputed == rpo_actual, flag False")

    # 5) 2018+ without actual RPO: imputed w/ flag True
    even = post[~post["ticker"].str.endswith(("002", "004", "006", "008", "010", "012"))]
    if not even.empty:
        assert even["rpo_is_imputed"].all(), "2018+ missing-RPO row not flagged"
    print("[OK] 2018+ rows lacking RPO tag are imputed + flagged")

    # 6) PIT validation hook — no lookahead possible in well-formed data;
    #    force a violation and expect PITValidationError
    bad = panel.copy()
    bad["filed_date"] = pd.Timestamp("2015-06-01").date()
    try:
        PITValidator.validate_pit(bad, timestamp_col="date", trade_date_col="filed_date")
        assert False, "expected PITValidationError"
    except PITValidationError:
        print("[OK] PIT hook rejects filed-before-end violations")

    # 7) persist parquet + provenance + factor store
    out_path = tmp / "rpo.parquet"
    prov = write_outputs(panel, out_path, test_dir, sample_n=len(synth))
    assert prov["rpo_imputed_rows"] == int(panel["rpo_is_imputed"].sum())
    re_read = pd.read_parquet(out_path)
    assert len(re_read) == len(panel)
    assert (tmp / "rpo.parquet.provenance.json").exists()
    print(f"[OK] parquet + provenance written; sha256={prov['sha256'][:12]}...")

    store_db = tmp / "test_factor_store.duckdb"
    if store_db.exists():
        store_db.unlink()
    write_factor_store(panel, str(store_db))
    from ingestion.harness.factor_store import FactorStore
    store = FactorStore(db_path=str(store_db))
    factors = store.list_factors()
    assert {"rpo_imputed", "rpo_is_imputed"} <= set(factors), f"missing factors: {factors}"
    df_r = store.read_factor("rpo_imputed")
    assert not df_r.empty and "provenance_hash" in df_r.columns
    print(f"[OK] factor store write: factors={sorted(factors)}; rpo_imputed rows={len(df_r)}")

    # 8) get_rpo_facts PIT interface (dispatch contract)
    g = get_rpo_facts("100001", companyfacts=synth["SYN001"])
    assert list(g.columns) == RPO_FACTS_COLS, f"get_rpo_facts schema: {list(g.columns)}"
    assert not g.empty and pd.to_datetime(g["filing_date"]).is_monotonic_increasing
    # SYN001 has NO dedicated RPO tag -> concept chain must resolve to the
    # ASC606 revenue proxy or the imputed deferred+contract path.
    assert g["concept_version"].isin(
        {"RevenueFromContractsWithCustomers",
         "RevenueFromContractWithCustomerExcludingAssessedTax",
         "DeferredRevenue+ContractLiabilities (imputed)"}).all(), \
        f"concept chain broken: {g['concept_version'].unique()}"
    post = g[pd.to_datetime(g["filing_date"]) >= pd.Timestamp("2019-01-01")]
    assert not post.empty
    assert (post["rpo"] == post["deferred_revenue"].fillna(0) +
            post["contract_liabilities"].fillna(0)).all()
    print(f"[OK] get_rpo_facts: schema exact, PIT key=filing_date, "
          f"resolved concepts={sorted(g['concept_version'].unique())}")

    # 8b) dedicated RPO tag branch (SYN002 carries RPO entries; period ends 2018+)
    g2 = get_rpo_facts("100002", companyfacts=synth["SYN002"])
    assert not g2.empty
    rpo_rows = g2[g2["concept_version"].str.contains("RemainingPerformance", na=False)]
    assert not rpo_rows.empty and (rpo_rows["rpo"] > 0).all()
    print(f"[OK] get_rpo_facts: dedicated RPO tag branch active "
          f"({len(rpo_rows)} filings)")

    # 8c) ASC606 revenue proxy branch: inject RevenueFromContractsWithCustomers
    prox = json.loads(json.dumps(synth["SYN001"]))
    prox["facts"]["us-gaap"]["RevenueFromContractsWithCustomers"] = {"units": {"USD": [
        {"end": "2018-12-31", "val": 5000.0, "filed": "2019-02-15", "form": "10-K",
         "accn": "0001000001-19-000010"},
        {"end": "2019-12-31", "val": 6000.0, "filed": "2020-02-13", "form": "10-K",
         "accn": "0001000001-20-000010"},
    ]}}
    g3 = get_rpo_facts("100001", start_date="2018-01-01", end_date="2020-12-31",
                       companyfacts=prox)
    proxy_rows = g3[g3["concept_version"] == "RevenueFromContractsWithCustomers"]
    assert len(proxy_rows) == 2, f"expected 2 proxy filings, got {len(proxy_rows)}"
    assert set(proxy_rows["filing_date"].astype(str)) == {"2019-02-15", "2020-02-13"}
    assert set(proxy_rows["rpo"].round(0).astype(int)) == {5000, 6000}
    # PIT window respected: pre-window FILING (2016-end, filed 2017-02-10)
    # excluded; later filings keyed by their own filing date are kept.
    all_filed = set(g3["filing_date"].astype(str))
    assert "2017-02-10" not in all_filed, f"pre-window filing leaked: {all_filed}"
    assert g3["concept_version"].isin(
        {"RevenueFromContractsWithCustomers",
         "RevenueFromContractWithCustomerExcludingAssessedTax",
         "DeferredRevenue+ContractLiabilities (imputed)"}).all()
    print(f"[OK] get_rpo_facts: RevenueFromContractsWithCustomers proxy branch "
          f"({len(proxy_rows)} filings; PIT window respected)")

    print("\nAll SEC RPO Parser tests passed.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="SEC RPO Parser — DeferredRevenue/ContractLiabilities/RPO from companyfacts")
    parser.add_argument("--facts-dir", type=str, default=DEFAULT_FACTS_DIR,
                        help="Directory of companyfacts JSON files (default: data/source/sec/companyfacts)")
    parser.add_argument("--sample", type=int, default=None,
                        help="Process only the first N ticker files (sample mode)")
    parser.add_argument("--out", type=str, default=DEFAULT_OUT,
                        help="Output parquet path")
    parser.add_argument("--factor-store", type=str, default=DEFAULT_FACTOR_STORE,
                        help="DuckDB factor store path")
    parser.add_argument("--test", action="store_true", help="Run synthetic self-test")
    parser.add_argument("--validate-pit", action="store_true", help="Validate PIT alignment on facts")
    parser.add_argument("--filing-date-only", action="store_true", help="Enforce filing date anchor only")
    args = parser.parse_args()

    if args.test:
        tmp = Path("data/factors/_rpo_tests") / "tmp"
        raise SystemExit(run_tests(tmp / "work"))

    facts_dir = Path(args.facts_dir)
    if not facts_dir.exists():
        raise SystemExit(f"facts dir not found: {facts_dir} (run sec_bulk_loader first)")

    print(f"Parsing companyfacts from {facts_dir}" + (f" — sample={args.sample}" if args.sample else ""))
    panel = build_rpo_panel(facts_dir, sample_n=args.sample)
    if panel.empty:
        raise SystemExit(f"No RPO/deferred-revenue facts found under {facts_dir}")
    write_outputs(panel, Path(args.out), facts_dir, sample_n=args.sample)
    write_factor_store(panel, args.factor_store)
    print("Done.")


if __name__ == "__main__":
    main()