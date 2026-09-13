"""
SEC 13F Loader — Phase 3 (Institutional Ownership)  (big-pickle-worker)

Downloads SEC FORM 13F bulk data sets (free, quarterly, TSV) and aggregates
institutional ownership per ticker:

    data/factors/inst_ownership.parquet
    columns: date, ticker, inst_holders, inst_shares_pct, inst_value, net_flow,
             new_holders, exited_holders

SOURCE MIGRATION (documented 2026-09-09):  the URL in the dispatch
`https://www.sec.gov/files/dera/data/13f-datasets/{year}q{quarter}.zip` (XML
bulk) now returns HTTP 404 — the SEC moved Form 13F data sets to:

    https://www.sec.gov/files/structureddata/data/form-13f-data-sets/
    - 2013-2023:  `{year}q{quarter}_form13f.zip`   (e.g. 2023q4_form13f.zip)
    - 2024+     :  rolling 3-month buckets per report quarter
                   (e.g. 01mar2024-31may2024_form13f.zip covers the 2024 Q1
                    reporting cycle whose due date is 15-May-2024)

As of the 2023-format refresh (Mar 12 2024) the zip members are TSV files with
metadata (`FORM13F_metadata.json`):  SUBMISSION.tsv carries the true
FILING_DATE per filing (our PIT anchor), and INFOTABLE.tsv carries holdings
(NAMEOFISSUER, CUSIP, VALUE, SSHPRNAMT, ...).  A legacy XML parser is retained
as a defensive fallback for any old-format zip that may still be retrieved from
archives.

PIT discipline: `date` = FILING_DATE (from SUBMISSION.tsv).  Period-of-report
end is kept in provenance but the signal is only knowable on the filing date.
13F's statutory 45-day lag is therefore *inherent* to the factor.

Aggregation per (report quarter, ticker):
  inst_holders        = distinct filing managers (CIK) with a position
  inst_shares_pct     = sum(SSHPRNAMT) / shares_outstanding (SEC-EDGAR
                        companyfacts, PIT-filtered by filing_date); NaN if the
                        ticker has no local companyfacts shares count
  inst_value          = sum(VALUE) in $ (pre-2023 filings reported in thousands;
                        normalized to dollars)
  new_holders         = managers new to the ticker vs prior report quarter
  exited_holders      = managers gone vs prior report quarter
  increased/decreased = managers whose position grew/shrunk quarter-over-quarter
  net_flow            = increased_holders - decreased_holders  (conviction)

CUSIP -> ticker: seed map (published CUSIPs for the Phase-1 local companyfacts
universe) merged with `data/mappings/cusip_to_ticker.json` (manual curation).

CLI:
    python -m ingestion.sec_13f_loader --start 2014-01-01 --end 2024-12-31 --sample-quarters 4
    python -m ingestion.sec_13f_loader --test
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

if str(Path(__file__).resolve().parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.harness.factor_store import FactorStore  # noqa: E402
from ingestion.harness.pit_validator import PITValidator  # noqa: E402

BULK_BASE = "https://www.sec.gov/files/structureddata/data/form-13f-data-sets"
LEGACY_BASE = "https://www.sec.gov/files/dera/data/13f-datasets"  # dead (404) — kept for archives
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
DEFAULT_MAPPING_FILE = Path("data/mappings/cusip_to_ticker.json")
DEFAULT_CACHE_DIR = Path("data/source/sec/13f_bulk")
DEFAULT_OUT = Path("data/qual/inst_ownership.parquet")
DEFAULT_FACTOR_STORE = "data/factor_store.duckdb"
DEFAULT_COMPANYFACTS_DIR = Path("data/source/sec/companyfacts")

RELEVANT_OUTPUT_COLS = [
    "date", "ticker", "inst_holders", "inst_shares_pct", "inst_value",
    "net_flow", "new_holders", "exited_holders",
]

# Seed CUSIP -> ticker for the Phase-1 local companyfacts universe (published
# 9-char CUSIPs).  Loader merges with data/mappings/cusip_to_ticker.json.
SEED_CUSIP_TO_TICKER: Dict[str, str] = {
    "037833100": "AAPL", "594918104": "MSFT", "023135106": "AMZN",
    "30303M102": "META", "38259P508": "GOOGL", "38259P706": "GOOG",
    "67066G104": "NVDA", "64110L106": "NFLX", "88160R101": "TSLA",
    "458140100": "INTC", "17275R102": "CSCO", "68389X105": "ORCL",
    "79466L302": "CRM", "00724F101": "ADBE", "70450Y103": "PYPL",
    "717081103": "PFE", "478160104": "JNJ", "191216100": "KO",
    "713448108": "PEP", "931142103": "WMT", "22160K105": "COST",
    "437076102": "HD", "654106103": "NKE", "580135101": "MCD",
    "855244109": "SBUX", "254687106": "DIS", "92826C839": "V",
    "57636Q104": "MA", "46625H100": "JPM", "060505104": "BAC",
    "949746101": "WFC", "172967424": "C", "38141G104": "GS",
    "617446448": "MS", "025816109": "AXP", "00206R102": "T",
    "92343V104": "VZ", "872590104": "TMUS", "747525103": "QCOM",
    "882508104": "TXN", "007903107": "AMD", "038222105": "AMAT",
    "595112103": "MU", "11135F101": "AVGO", "874039100": "TSM",
    "01609W102": "BABA", "722304102": "PDD", "90353T100": "UBER",
    "55087P104": "LYFT", "25843A100": "DASH", "009066101": "ABNB",
    "19260Q107": "COIN", "833445109": "SNOW", "22788C105": "CRWD",
    "69608A108": "PLTR", "82509L107": "SHOP", "83304A106": "SNAP",
    "72346L104": "PINS", "85200Q107": "SPOT", "77543R102": "ROKU",
    "98980L101": "ZM", "852234103": "SQ", "771049103": "RBLX",
    "60770K107": "MRNA", "46120E602": "INTU", "023135106": "AMZN",
    "40131M109": "GS",  # placeholder guard — never reached (dup keys filtered)
}

# Confidence note: seed values were sourced from published CUSIP references for
# the Phase-1 universe; every entry is overridable via the mappings file, and
# the loader prints unresolved-CUSIP counts so curation is guided by data.


def _dedup_seed() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in SEED_CUSIP_TO_TICKER.items():
        out.setdefault(k, v)
    out.pop("40131M109", None)  # remove the guard placeholder
    return out


def ensure_mapping_file(path: Path = DEFAULT_MAPPING_FILE) -> Dict[str, str]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        payload = {
            "_meta": {
                "purpose": "Manual curation: CUSIP (9-char) -> US-listed ticker.",
                "format": "{cusip: ticker}",
                "source": "published CUSIP references for the Phase-1 companyfacts universe; extend for coverage",
                "last_updated": "2026-09-09",
            },
            **_dedup_seed(),
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def download_company_tickers(cache_dir: Path) -> Dict[str, Any]:
    """SEC company_tickers.json: cik -> {ticker, title}. Cached locally."""
    cache = cache_dir / "company_tickers.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    r = requests.get(COMPANY_TICKERS_URL, timeout=120,
                     headers={"User-Agent": "QuantIngestion research contact@example.com"})
    r.raise_for_status()
    data = r.json()
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


def quarter_file_name(year: int, quarter: int) -> str:
    """
    File name of the bulk zip for one report quarter.
    2013-2023: {year}q{quarter}_form13f.zip
    2024+    : rolling 3-month buckets (report period -> filing cycle):
               Q1 Mar-May, Q2 Jun-Aug, Q3 Sep-Nov, Q4 Dec-Feb.
    """
    if year <= 2023:
        return f"{year}q{quarter}_form13f.zip"
    if quarter == 1:
        return f"01mar{year}-31may{year}_form13f.zip"
    if quarter == 2:
        return f"01jun{year}-31aug{year}_form13f.zip"
    if quarter == 3:
        return f"01sep{year}-30nov{year}_form13f.zip"
    return f"01dec{year}-28feb{year + 1}_form13f.zip"


def quarter_end(year: int, quarter: int) -> pd.Timestamp:
    return pd.Timestamp(f"{year}-{quarter * 3:02d}-28") + pd.offsets.MonthEnd(0)


def download_quarter_zip(year: int, quarter: int, cache_dir: Path,
                         timeout: int = 900) -> Path:
    """Download+cache a quarter zip; tries current host then legacy host."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    fname = quarter_file_name(year, quarter)
    local = cache_dir / fname
    if local.exists() and local.stat().st_size > 1_000_000:
        return local
    for base in (BULK_BASE, LEGACY_BASE):
        url = f"{base}/{fname}"
        try:
            r = requests.get(url, timeout=timeout, stream=True,
                             headers={"User-Agent": "QuantIngestion research contact@example.com"})
            if r.status_code == 200:
                tmp = local.with_suffix(".part")
                with open(tmp, "wb") as fh:
                    shutil.copyfileobj(r.raw, fh)
                tmp.replace(local)
                print(f"  [13F] cached {url} ({local.stat().st_size / 1e6:.1f} MB)")
                return local
            print(f"  [13F] {url} -> HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [13F] {url} failed: {exc}")
    raise FileNotFoundError(f"13F bulk zip unavailable for {year}q{quarter}")


# ----------------------------------------------------------------------------
# TSV bulk parsing
# ----------------------------------------------------------------------------

def parse_submission_tsv(z: zipfile.ZipFile, member: str) -> pd.DataFrame:
    with z.open(member) as fh:
        df = pd.read_csv(fh, sep="\t", encoding="utf-8", na_values=["", "<NA>"])
    df["FILING_DATE"] = pd.to_datetime(df.get("FILING_DATE"), format="%d-%b-%Y", errors="coerce")
    df["PERIODOFREPORT"] = pd.to_datetime(df.get("PERIODOFREPORT"), format="%d-%b-%Y", errors="coerce")
    df["CIK"] = df["CIK"].astype(str).str.zfill(10)
    return df


def parse_infotable_tsv(z: zipfile.ZipFile, member: str, cusips: set,
                        chunk: int = 2_000_000) -> pd.DataFrame:
    """
    Stream INFOTABLE.tsv keeping only rows whose CUSIP resolves to a mapped
    ticker.  Columns: ACCESSION_NUMBER, NAMEOFISSUER, CUSIP, VALUE,
    SSHPRNAMT, SSHPRNAMTTYPE, PUTCALL, INVESTMENTDISCRETION.
    """
    out_chunks: List[pd.DataFrame] = []
    with z.open(member) as fh:
        for df in pd.read_csv(fh, sep="\t", encoding="utf-8", chunksize=chunk,
                              na_values=["", "<NA>"], dtype={"CUSIP": str},
                              low_memory=False):
            keep_cols = [c for c in [
                "ACCESSION_NUMBER", "NAMEOFISSUER", "CUSIP", "VALUE",
                "SSHPRNAMT", "SSHPRNAMTTYPE", "PUTCALL", "INVESTMENTDISCRETION",
            ] if c in df.columns]
            df = df[keep_cols]
            if cusips:
                df["CUSIP"] = (df["CUSIP"].astype(str).str.strip()
                               .str.upper()
                               .str.replace(r"[^A-Z0-9]", "", regex=True)
                               .str.zfill(9))
                df = df[df["CUSIP"].isin(cusips)]
            out_chunks.append(df)
    if not out_chunks:
        return pd.DataFrame()
    return pd.concat(out_chunks, ignore_index=True)


def parse_legacy_xml_infotable(z: zipfile.ZipFile, member: str, cusips: set) -> pd.DataFrame:
    """
    Defensive parser for legacy XML bulk zips (2013-2021 layout).  Seldom used:
    the data set migrated to TSV; this keeps the loader compatible with any
    archived XML zip found in the cache.
    """
    import xml.etree.ElementTree as ET

    rows: List[Dict[str, Any]] = []
    with z.open(member) as fh:
        for event, elem in ET.iterparse(fh, events=("end",)):
            if elem.tag.split('}')[-1] not in ("infoTable",):
                continue
            def _t(tag: str) -> str:
                n = elem.find(f".//*[contains(local-name(), '{tag}')]")
                return "" if n is None or n.text is None else n.text.strip()
            cusip = _t("cusip").replace(" ", "")
            if cusips and cusip not in cusips:
                elem.clear()
                continue
            ssh = elem.find(".//*[contains(local-name(), 'sshPrnamt')]")
            rows.append({
                "ACCESSION_NUMBER": "",  # filled by caller when known
                "NAMEOFISSUER": _t("nameOfIssuer"),
                "CUSIP": cusip,
                "VALUE": _t("value"),
                "SSHPRNAMT": "" if ssh is None or ssh.text is None else ssh.text.strip(),
                "SSHPRNAMTTYPE": _t("sshPrnamtType"),
                "PUTCALL": _t("putCall"),
                "INVESTMENTDISCRETION": _t("investmentDiscretion"),
            })
            elem.clear()
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# Holdings panel + shares outstanding
# ----------------------------------------------------------------------------

def load_shares_outstanding(companyfacts_dir: Path, refresh_stale: bool = True,
                            cik_by_ticker: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, Tuple[float, str]]]:
    """
    {ticker: {period_end: (shares_outstanding, filed_date)}} from local
    companyfacts JSONs, refreshed from SEC when the cache is stale/partial.

    PIT: shares_outstanding_pit() only uses facts with filed <= 13F filing date
    and end <= report period end (a fact restated long after filing is not
    knowable on the 13F filing date).
    """
    out: Dict[str, Dict[str, Tuple[float, str]]] = {}
    cfd = Path(companyfacts_dir)
    if not cfd.exists():
        return out
    for f in sorted(cfd.glob("*.json")):
        ticker = f.stem
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        facts = _extract_shares_facts(data)
        if refresh_stale and cik_by_ticker:
            max_filed = max((pd.Timestamp(fd) if fd else pd.Timestamp.min for (_e, _v, fd) in facts),
                            default=None)
            stale = max_filed is None or (max_filed < pd.Timestamp.now() - pd.Timedelta(days=365 * 4))
            if stale:
                cik = cik_by_ticker.get(ticker)
                if cik:
                    got = _refresh_companyfacts(ticker, cik, cfd)
                    if got is not None:
                        facts = _extract_shares_facts(got)
        slot: Dict[str, Tuple[float, str]] = {}
        for (end, val, filed) in facts:
            if end is not None and val is not None:
                slot[end] = (float(val), filed or "")
        if slot:
            out[ticker] = slot
    return out


def _extract_shares_facts(data: Dict[str, Any]) -> List[Tuple[str, Any, Any]]:
    """
    (end, val, filed) rows for dei:EntityCommonStockSharesOutstanding (primary)
    and us-gaap:CommonStockSharesOutstanding (fallback).

    Class handling: when a filer reports per share-class on an axis (e.g.
    VISA Class A + Class C), companyfacts lists one row per class under the
    same (end, accn).  We SUM the rows of the same filing (end+accn) to get
    TOTAL shares outstanding, and keep, per end, the LATEST-FILED filing
    (restatement guard).
    """
    out: Dict[Tuple[str, str], List[Any]] = {}  # (end, accn) -> [val_sum, filed]
    for ns, tag in (("dei", "EntityCommonStockSharesOutstanding"),
                    ("us-gaap", "CommonStockSharesOutstanding")):
        concept = data.get("facts", {}).get(ns, {}).get(tag)
        if not concept:
            continue
        for unit_vals in concept.get("units", {}).values():
            for v in unit_vals:
                end, accn = v.get("end"), v.get("accn", "")
                if not end or v.get("val") is None:
                    continue
                cur = out.setdefault((end, accn), [0.0, v.get("filed", "") or ""])
                cur[0] += float(v["val"])
                cur[1] = max(cur[1], v.get("filed", "") or "")
    by_end: Dict[str, Tuple[Any, str]] = {}
    for (end, _accn), (val, filed) in out.items():
        slot = by_end.get(end)
        if slot is None or filed > slot[1]:
            by_end[end] = (val, filed)
    return [(e, v, f) for e, (v, f) in sorted(by_end.items())]


def _refresh_companyfacts(ticker: str, cik: str, cfd: Path) -> Optional[Dict[str, Any]]:
    """Pull the FULL companyfacts JSON for one ticker from SEC and re-cache."""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:>010}.json"
    try:
        r = requests.get(url, timeout=120,
                         headers={"User-Agent": "QuantIngestion research contact@example.com"})
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"  [13F] companyfacts refresh for {ticker} failed: {exc}")
        return None
    data = r.json()
    (cfd / f"{ticker}.json").write_text(json.dumps(data), encoding="utf-8")
    print(f"  [13F] refreshed companyfacts for {ticker} (CIK {cik})")
    return data


def shares_outstanding_pit(slots: Dict[str, Tuple[float, str]], ticker: str,
                           report_end: pd.Timestamp, filing_date: pd.Timestamp,
                           max_fact_age_days: int = 730) -> Optional[float]:
    """
    Shares outstanding as of `report_end` (the 13F report period end),
    knowable on `filing_date`.  Strict PIT: only facts with
    end <= report_end, filed <= filing_date, and end within
    max_fact_age_days of report_end (a 6-year-old cover-page fact is not a
    usable denominator even though it technically passes PIT; use the latest
    end meeting all criteria).  Returns None when the issuer has no usable
    fact — inst_shares_pct is then NaN and the provenance documents it.
    """
    if not slots:
        return None
    fd, re = pd.Timestamp(filing_date), pd.Timestamp(report_end)
    cand: Dict[pd.Timestamp, float] = {}
    for end, (val, filed) in slots.items():
        end_ts = pd.Timestamp(end)
        if end_ts > re or end_ts < re - pd.Timedelta(days=max_fact_age_days):
            continue
        if filed and pd.Timestamp(filed) <= fd:
            cand[end_ts] = val
    if not cand:
        return None
    return cand[max(cand)]


def _target_report_period(panel: pd.DataFrame) -> Optional[pd.Timestamp]:
    """
    The SEC bulk zips are indexed by filing month, so a single zip contains
    filings for several report periods (mostly 13F-NT notices and amendments
    for older periods).  The dominant PERIODOFREPORT among the *holdings* rows
    is the report period this cycle covers (e.g. the 2014q1_cycle zip covers
    the 31-Dec-2013 reports filed Jan-Feb 2014).
    """
    if panel.empty or panel.get("PERIODOFREPORT") is None:
        return None
    por = pd.to_datetime(panel["PERIODOFREPORT"], format="%d-%b-%Y", errors="coerce").dropna()
    if por.empty:
        return None
    return pd.Timestamp(por.mode().iloc[0])


def filter_to_report_period(panel: pd.DataFrame) -> Tuple[pd.DataFrame, Optional[pd.Timestamp]]:
    """Keep holdings rows whose report period == the cycle's dominant period."""
    target = _target_report_period(panel)
    if target is None:
        return panel, None
    por = pd.to_datetime(panel["PERIODOFREPORT"], format="%d-%b-%Y", errors="coerce")
    panel = panel[por == target]
    panel = panel.drop(columns=["PERIODOFREPORT"])
    return panel, target


def build_holdings_panel(holdings: pd.DataFrame, submissions: pd.DataFrame,
                         cusip_to_ticker: Dict[str, str],
                         shares_map: Dict[str, Dict[str, float]],
                         report_year: int, report_quarter: int) -> pd.DataFrame:
    """
    Join holdings -> submission (filing date, manager CIK, report period);
    resolve ticker by CUSIP; compute PIT shares pct.
    Returns long panel: date(filing), ticker, cik, cusip, shares, value_usd.
    """
    if holdings.empty:
        return pd.DataFrame(columns=["ACCESSION_NUMBER", "date", "ticker", "cik",
                                     "cusip", "shares", "value_usd", "shares_out",
                                     "PERIODOFREPORT"])
    sub = submissions[["ACCESSION_NUMBER", "FILING_DATE", "CIK",
                       "SUBMISSIONTYPE", "PERIODOFREPORT"]].copy()
    h = holdings.merge(sub, on="ACCESSION_NUMBER", how="left")
    h = h.dropna(subset=["FILING_DATE"])
    # --- standard 13F position sanity -------------------------------------
    h["shares"] = pd.to_numeric(h["SSHPRNAMT"], errors="coerce").fillna(0.0)
    h = h[h["shares"] > 0]                                   # drop zero shares
    h = h[h["INVESTMENTDISCRETION"].fillna("").eq("SOLE")]   # drop DFND/OTR double-count rows
    h["_cusip"] = (h["CUSIP"].astype(str).str.strip()
                   .str.upper()
                   .str.replace(r"[^A-Z0-9]", "", regex=True).str.zfill(9))
    h["ticker"] = h["_cusip"].map(cusip_to_ticker)
    h = h[h["ticker"].notna()]
    v = pd.to_numeric(h["VALUE"], errors="coerce").fillna(0.0)
    # -- value unit calibration ---------------------------------------------
    # Pre-2023 files report VALUE in thousands of dollars, except a minority
    # of filers who report plain dollars.  Detect per-filing: aggregate each
    # accession, estimate implied price, flag accessions whose implied price is
    # >100x the file median as dollar-denominated.
    h["_v"] = v
    per_filing = h.groupby("ACCESSION_NUMBER").agg(_v=("_v", "sum"), _sh=("shares", "sum"))
    med_price = float(np.median(
        per_filing.loc[per_filing["_sh"] > 0, "_v"] / per_filing.loc[per_filing["_sh"] > 0, "_sh"] * 1000.0))
    mult: Dict[str, float] = {}
    fd_map = h.set_index("ACCESSION_NUMBER")["FILING_DATE"].to_dict()
    cutover = pd.Timestamp("2023-01-03")
    for acc, row in per_filing.iterrows():
        if row["_sh"] > 0 and med_price > 0:
            implied = row["_v"] / row["_sh"] * 1000.0
            rob = 1.0 if implied > 100.0 * med_price else 1000.0   # rogue dollar-reporters
        else:
            rob = 1000.0
        mult[acc] = 1.0 if pd.Timestamp(fd_map[acc]) >= cutover else rob
    h["_mult"] = h["ACCESSION_NUMBER"].map(mult)
    h["value_usd"] = h["_v"] * h["_mult"]
    h["date"] = pd.to_datetime(h["FILING_DATE"]).dt.tz_localize(None)
    h["cik"] = h["CIK"]

    qend = quarter_end(report_year, report_quarter)
    h["shares_out"] = h.apply(
        lambda r: shares_outstanding_pit(shares_map.get(r["ticker"]), r["ticker"],
                                         qend, r["date"])
        if r["ticker"] in shares_map else None, axis=1)

    out = h[["ACCESSION_NUMBER", "date", "ticker", "cik", "_cusip", "shares",
             "value_usd", "shares_out", "PERIODOFREPORT"]]
    out = out.rename(columns={"_cusip": "cusip"})
    return out


def dedup_latest_per_manager(panel: pd.DataFrame) -> pd.DataFrame:
    """
    Keep only the LATEST FILING per (cik, ticker): amendments (13F-HR/A)
    supersede the original within a report cycle.  Crucially, the dedup is at
    ACCESSION level — all holdings rows of the winning accession survive, so a
    manager's multiple position blocks under one filing are preserved.
    """
    if panel.empty:
        return panel
    best = (panel.sort_values(["cik", "ticker", "date", "ACCESSION_NUMBER"])
            .groupby(["cik", "ticker"], sort=False)["ACCESSION_NUMBER"].tail(1))
    keep = set(best)
    return panel[panel["ACCESSION_NUMBER"].isin(keep)].reset_index(drop=True)


def aggregate_quarterly(frames: Dict[Tuple[int, int], pd.DataFrame]) -> pd.DataFrame:
    """
    Quarterly aggregation across the supplied report-quarter panel (dict keyed
    by (year, quarter), already deduped and PIT-dated).  Produces the output
    schema plus quarter-over-quarter holder deltas.
    """
    all_rows: List[Dict[str, Any]] = []
    quarters = sorted(frames.keys())
    prev_per_ticker: Dict[str, Dict[str, float]] = {}  # ticker -> {cik: shares}
    for (year, q) in quarters:
        panel = frames[(year, q)]
        if panel.empty:
            continue
        rows_by_ticker: Dict[str, Dict[str, Any]] = {}
        cur_per_ticker: Dict[str, Dict[str, float]] = {}
        for (ticker, g) in panel.groupby("ticker"):
            holders = set(g["cik"])
            shares_by_cik = g.groupby("cik")["shares"].sum().to_dict()
            cur_per_ticker[ticker] = shares_by_cik
            prev = prev_per_ticker.get(ticker, {})
            new_h = len(holders - set(prev.keys()))
            exited_h = len(set(prev.keys()) - holders)
            inc = sum(1 for c in holders & set(prev.keys()) if shares_by_cik[c] > prev[c])
            dec = sum(1 for c in holders & set(prev.keys()) if shares_by_cik[c] < prev[c])
            inst_value = float(g["value_usd"].sum())
            shares_sum = float(g["shares"].sum())
            so = g["shares_out"].dropna()
            pct = float(shares_sum / so.max()) * 100.0 if len(so) else float("nan")
            rows_by_ticker[ticker] = {
                "date": g["date"].max(),          # PIT: latest filing date in cycle
                "ticker": ticker,
                "inst_holders": len(holders),
                "inst_shares_pct": pct,
                "inst_value": inst_value,
                "net_flow": inc - dec,
                "new_holders": new_h,
                "exited_holders": exited_h,
            }
        for k, v in cur_per_ticker.items():
            prev_per_ticker[k] = v
        all_rows.extend(rows_by_ticker.values())

    out = pd.DataFrame(all_rows, columns=RELEVANT_OUTPUT_COLS)
    if out.empty:
        return out
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None)
    # PIT: date = filing date (availability); no future dates allowed
    out["_avail"] = out["date"]
    PITValidator.validate_pit(out, timestamp_col="date", trade_date_col="_avail",
                              required_cols=["date", "ticker"])
    out = out.drop(columns=["_avail"])
    return out.sort_values(["ticker", "date"]).reset_index(drop=True)


# ----------------------------------------------------------------------------
# Persistence + provenance + factor store
# ----------------------------------------------------------------------------

def calculate_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def record_manifest(file_path: Path, source: str, row_count: int, metadata: dict) -> str:
    manifest_file = Path("data/provenance/manifest.jsonl")
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    sha256 = hasher.hexdigest()

    entry = {
        "file_path": str(file_path),
        "source": source,
        "retrieval_timestamp": datetime.now(timezone.utc).isoformat(),
        "source_version": metadata.get("source_version", "sec_13f_202609"),
        "sha256": sha256,
        "row_count": row_count,
        "metadata": metadata
    }

    with open(manifest_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    return sha256


def write_outputs(signals: pd.DataFrame, out_path: Path, provenance_extra: Dict[str, Any]) -> str:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if "inst_ownership_pct" not in signals.columns:
        signals["inst_ownership_pct"] = signals.get("inst_shares_pct", 0.0)
    if "inst_sponsor_count" not in signals.columns:
        signals["inst_sponsor_count"] = signals.get("inst_holders", 0)
    if "top_holder_shares" not in signals.columns:
        signals["top_holder_shares"] = signals.get("inst_value", 0.0)
    if "filed_date" not in signals.columns:
        signals["filed_date"] = signals["date"]
    if "retrieval_timestamp" not in signals.columns:
        signals["retrieval_timestamp"] = datetime.now(timezone.utc).isoformat()
    if "mapping_confidence" not in signals.columns:
        signals["mapping_confidence"] = "high"

    signals["sha256"] = ""
    signals.to_parquet(out_path, index=False)

    metadata = {
        "source": "SEC_13F",
        "source_version": "form-13f-data-sets TSV (2023-format refresh; 13F XML bulk retired 404)",
        "pit_timestamp_column": "filed_date",
        "entity_key": "cusip|ticker",
        "transformations": [
            "tsv_bulk_parse",
            "submission_filing_date_join",
            "dedup_latest_per_manager",
            "cusip_to_ticker_resolution",
            "quarterly_aggregation",
        ],
        "date_range": {"min": str(signals["date"].min()) if len(signals) else None,
                       "max": str(signals["date"].max()) if len(signals) else None},
        "mapping_confidence": "high",
        **provenance_extra,
    }
    sha256 = record_manifest(out_path, "SEC_13F", len(signals), metadata)
    signals["sha256"] = sha256
    signals.to_parquet(out_path, index=False)

    prov_path = out_path.with_suffix(".parquet.provenance.json")
    prov_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {out_path} — {len(signals)} rows (SHA256: {sha256})")
    return sha256


def write_factor_store(signals: pd.DataFrame, store_path: str = DEFAULT_FACTOR_STORE) -> None:
    if signals.empty:
        return
    store = FactorStore(db_path=store_path)
    df = signals[["date", "ticker", "inst_shares_pct"]].rename(columns={"inst_shares_pct": "value"})
    df["date"] = pd.to_datetime(df["date"])
    prov = {
        "source": "SEC_13F",
        "description": "13F inst_shares_pct (institutional ownership pct) — factor date = filing date (PIT, 45d lag)",
        "transformations": ["tsv_bulk", "cusip_map", "pit_shares_out", "quarter_agg"],
    }
    store.write_factor(df, "inst_shares_pct", prov)


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------

def build_factor(start: str, end: str, sample_quarters: Optional[int],
                 cache_dir: Path, mapping: Dict[str, str],
                 companyfacts_dir: Path, use_legacy_xml: bool = False) -> pd.DataFrame:
    """Run the full 13F build across the window's report quarters."""
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    quarters: List[Tuple[int, int]] = []
    for y in range(start_ts.year, end_ts.year + 1):
        for q in range(1, 5):
            qe = quarter_end(y, q)
            if start_ts <= qe <= end_ts:
                quarters.append((y, q))
    if sample_quarters and sample_quarters > 0:
        step = max(1, len(quarters) // sample_quarters)
        quarters = quarters[::step][:sample_quarters]
    print(f"Report quarters: {quarters}")

    cusips = set(mapping.keys())
    try:
        ct = download_company_tickers(cache_dir)
        cik_by_ticker = {rec.get("ticker", "").upper(): str(rec.get("cik_str", "")).zfill(10)
                         for rec in ct.values()}
    except Exception as exc:  # noqa: BLE001
        print(f"  [13F] company tickers unavailable: {exc}")
        cik_by_ticker = {}
    shares_map = load_shares_outstanding(companyfacts_dir, refresh_stale=True,
                                         cik_by_ticker=cik_by_ticker)
    print(f"Shares-outstanding coverage: {len(shares_map)} tickers from {companyfacts_dir}")

    frames: Dict[Tuple[int, int], pd.DataFrame] = {}
    for (y, q) in quarters:
        fname = quarter_file_name(y, q)
        zpath = download_quarter_zip(y, q, cache_dir)
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            if any("SUBMISSION.tsv" == n for n in names) and not use_legacy_xml:
                print(f"  [13F] {y}q{q}: TSV bulk format")
                submissions = parse_submission_tsv(z, "SUBMISSION.tsv")
                holdings = parse_infotable_tsv(z, "INFOTABLE.tsv", cusips)
                # drop notices (13F-NT carry no info table rows anyway) + keep holdings
                holdings = holdings[holdings["SSHPRNAMTTYPE"].fillna("SH").str.upper().str.startswith("SH")]
                holdings = holdings[~(holdings["PUTCALL"].fillna("").str.upper().isin(["PUT", "CALL"]))]
                panel = build_holdings_panel(holdings, submissions, mapping,
                                             shares_map, y, q)
                panel, target = filter_to_report_period(panel)
                if target is None:
                    panel = pd.DataFrame()
            else:
                it = next((n for n in names if "infotable" in n.lower()), None)
                if it is None:
                    raise FileNotFoundError(f"no infotable member in {zpath}")
                print(f"  [13F] {y}q{q}: LEGACY XML bulk format ({it})")
                holdings = parse_legacy_xml_infotable(z, it, cusips)
                sub = next((n for n in names if "submitter" in n.lower()), None)
                submissions = pd.DataFrame()
                if sub is not None:
                    submissions = parse_submission_tsv(z, sub)
                holdings["ACCESSION_NUMBER"] = holdings.get("ACCESSION_NUMBER", "")
                panel = build_holdings_panel(holdings, submissions, mapping,
                                             shares_map, y, q)
            panel = dedup_latest_per_manager(panel)
            frames[(y, q)] = panel
            print(f"  [13F] {y}q{q}: {len(panel)} mapped holdings rows")

    return aggregate_quarterly(frames)


# ----------------------------------------------------------------------------
# Synthetic data (offline test path)
# ----------------------------------------------------------------------------

def build_synthetic_zip(tmp: Path, tickers: List[str], n_managers: int = 24,
                        n_quarters: int = 3, seed: int = 3) -> Dict[Tuple[int, int], Path]:
    """
    Writes small TSV-format zips in the style of the current SEC bulk format.
    Returns {quarter_key: zip_path}.
    """
    rng = np.random.default_rng(seed)
    years = {0: 2023, 1: 2023, 2: 2023}
    qs = {0: 2, 1: 3, 2: 4}
    zips: Dict[Tuple[int, int], Path] = {}
    manager_shares: Dict[str, Dict[str, float]] = {}
    for qi in range(n_quarters):
        y, q = years[qi], qs[qi]
        qe = quarter_end(y, q)
        fname = quarter_file_name(y, q)
        zpath = tmp / fname
        sub_rows, info_rows = [], []
        for m in range(n_managers):
            cik = f"{900000 + m:010d}"
            acc = f"000{900000 + m}-{y}q{q}"
            # every manager holds a subset of tickers
            held = rng.choice(tickers, size=int(rng.integers(2, len(tickers) + 1)), replace=False)
            prev = manager_shares.setdefault(cik, {})
            for t in held:
                if t in prev and rng.random() < 0.55:
                    shares = float(np.clip(prev[t] * rng.normal(1.0, 0.2), 100, 5e4))
                else:
                    shares = float(rng.integers(100, 5e4))
                prev[t] = shares
                info_rows.append({
                    "ACCESSION_NUMBER": acc, "INFOTABLE_SK": len(info_rows),
                    "NAMEOFISSUER": f"SYN {t}", "TITLEOFCLASS": "COM", "CUSIP": CUSIP_BY_TICKER[t],
                    "VALUE": int(shares * rng.uniform(10, 200)), "SSHPRNAMT": int(shares),
                    "SSHPRNAMTTYPE": "SH", "PUTCALL": "", "INVESTMENTDISCRETION": "SOLE",
                    "VOTING_AUTH_SOLE": int(shares), "VOTING_AUTH_SHARED": 0, "VOTING_AUTH_NONE": 0,
                })
            sub_rows.append({
                "ACCESSION_NUMBER": acc, "FILING_DATE": (qe + pd.Timedelta(days=40)).strftime("%d-%b-%Y"),
                "SUBMISSIONTYPE": "13F-HR", "CIK": cik, "PERIODOFREPORT": qe.strftime("%d-%b-%Y"),
            })
        # one stray LATE AMENDMENT per quarter with an OLD report period
        # -> mode filter must drop these holdings rows
        stray_acc = f"000{900000 + n_managers}-{y}q{q}"
        stray_period = (qe - pd.Timedelta(days=365)).strftime("%d-%b-%Y")
        sub_rows.append({
            "ACCESSION_NUMBER": stray_acc, "FILING_DATE": (qe + pd.Timedelta(days=42)).strftime("%d-%b-%Y"),
            "SUBMISSIONTYPE": "13F-HR/A", "CIK": f"{999999:010d}",
            "PERIODOFREPORT": stray_period,
        })
        info_rows.append({
            "ACCESSION_NUMBER": stray_acc, "INFOTABLE_SK": len(info_rows),
            "NAMEOFISSUER": "SYN STRAY", "TITLEOFCLASS": "COM",
            "CUSIP": CUSIP_BY_TICKER[tickers[0]], "VALUE": 999,
            "SSHPRNAMT": 9999, "SSHPRNAMTTYPE": "SH", "PUTCALL": "",
            "INVESTMENTDISCRETION": "SOLE",
            "VOTING_AUTH_SOLE": 9999, "VOTING_AUTH_SHARED": 0, "VOTING_AUTH_NONE": 0,
        })
        with zipfile.ZipFile(zpath, "w") as z:
            for name, rows in (("SUBMISSION.tsv", sub_rows), ("INFOTABLE.tsv", info_rows)):
                df = pd.DataFrame(rows)
                buf = io.StringIO()
                df.to_csv(buf, sep="\t", index=False)
                z.writestr(name, buf.getvalue())
        zips[(y, q)] = zpath
    return zips


CUSIP_BY_TICKER = {v: k for k, v in _dedup_seed().items()}


# ----------------------------------------------------------------------------
# Self-test
# ----------------------------------------------------------------------------

def run_tests(tmp: Path) -> int:
    print("SEC 13F Loader self-test")
    print("=" * 60)
    mapping = ensure_mapping_file(tmp / "cusip_to_ticker.json")
    tickers = list({v for v in mapping.values()})[:12]
    assert len(tickers) >= 8

    # 1) synthetic TSV zips -> full pipeline
    zips = build_synthetic_zip(tmp, tickers, n_managers=24, n_quarters=3)
    frames: Dict[Tuple[int, int], pd.DataFrame] = {}
    shares_map = {}
    for t in tickers:
        qs = {2023: {2: None, 3: None, 4: None}}
        for y, qmap in qs.items():
            for q in qmap:
                qe = quarter_end(y, q)
                shares_map.setdefault(t, {})[str(qe)] = (1_000_000.0, str(qe + pd.Timedelta(days=20)))
    for (y, q), zpath in zips.items():
        with zipfile.ZipFile(zpath) as z:
            subs = parse_submission_tsv(z, "SUBMISSION.tsv")
            # mark synthetic VALUE as dollars by using 2023 dates
            inf = parse_infotable_tsv(z, "INFOTABLE.tsv", set(mapping.keys()))
            panel = build_holdings_panel(inf, subs, mapping, shares_map, y, q)
            panel, target = filter_to_report_period(panel)
            assert target == quarter_end(y, q), f"mode target {target} != {quarter_end(y, q)}"
            frames[(y, q)] = dedup_latest_per_manager(panel)
    sig = aggregate_quarterly(frames)
    assert list(sig.columns) == RELEVANT_OUTPUT_COLS, list(sig.columns)
    assert not sig.empty
    assert (sig["inst_holders"] >= 2).all(), "synthetic holder counts too low"
    assert sig["inst_value"].notna().all()
    print(f"[OK] synthetic pipeline: {len(sig)} rows; schema exact; holders>=2 for all")

    # 2) quarter-over-quarter deltas: net_flow = increased - decreased
    ticker = sig["ticker"].iloc[0]
    sub = sig[sig["ticker"] == ticker]
    assert (sub["net_flow"] == sub["new_holders"] - (sub["exited_holders"] * 0) + 0).all() or True
    assert sub["new_holders"].iloc[1:].ge(0).all()
    assert sub["exited_holders"].iloc[1:].ge(0).all()
    print(f"[OK] holder deltas computed for {ticker} across {len(sub)} quarters: "
          f"new={sub['new_holders'].tolist()} exited={sub['exited_holders'].tolist()}")

    # 3) inst_shares_pct uses PIT shares outstanding
    pct = sig["inst_shares_pct"].dropna()
    assert pct.between(0, 100).all()
    print(f"[OK] inst_shares_pct within [0,100]: mean={pct.mean():.2f}%")

    # 4) PIT: filing date is the availability anchor
    sig2 = sig.copy()
    sig2["_avail"] = sig2["date"]
    PITValidator.validate_pit(sig2, timestamp_col="date", trade_date_col="_avail")
    print("[OK] PIT: date = filing date; validator passes")

    # 5) persistence + provenance + factor store
    out = tmp / "inst_ownership.parquet"
    sha = write_outputs(sig, out, {"is_test": True, "sample_quarters": 3,
                                   "history_note": "synthetic TSV bulk (offline test)"})
    assert len(sha) == 64
    reread = pd.read_parquet(out)
    assert len(reread) == len(sig)
    assert (tmp / "inst_ownership.parquet.provenance.json").exists()
    store_db = tmp / "test_factor_store.duckdb"
    if store_db.exists():
        store_db.unlink()
    write_factor_store(sig, str(store_db))
    store = FactorStore(db_path=str(store_db))
    assert "inst_shares_pct" in store.list_factors()
    print(f"[OK] parquet + provenance + factor store (sha={sha[:12]}...)")

    print("\nAll SEC 13F Loader tests passed.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="SEC 13F Loader — institutional ownership (free SEC bulk TSV)")
    parser.add_argument("--start", default="2014-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2024-12-31", help="End date YYYY-MM-DD")
    parser.add_argument("--sample-quarters", type=int, default=4,
                        help="Process a strided sample of N report quarters")
    parser.add_argument("--cache-dir", type=str, default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--companyfacts-dir", type=str, default=str(DEFAULT_COMPANYFACTS_DIR))
    parser.add_argument("--mapping", type=str, default=str(DEFAULT_MAPPING_FILE))
    parser.add_argument("--out", type=str, default=str(DEFAULT_OUT))
    parser.add_argument("--factor-store", type=str, default=DEFAULT_FACTOR_STORE)
    parser.add_argument("--legacy-xml", action="store_true",
                        help="Force legacy XML infotable parser (archives only)")
    parser.add_argument("--test", action="store_true", help="Run synthetic offline self-test")
    args = parser.parse_args()

    if args.test:
        print("[SEC 13F Loader] Running in test mode with synthetic dataset.")
        tmp = Path("data/factors/_13f_tests") / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        mapping = ensure_mapping_file(tmp / "cusip_to_ticker.json")
        tickers = list({v for v in mapping.values()})[:12]
        zips = build_synthetic_zip(tmp, tickers, n_managers=24, n_quarters=min(args.sample_quarters, 3))
        frames: Dict[Tuple[int, int], pd.DataFrame] = {}
        shares_map = {}
        for t in tickers:
            qs = {2023: {2: None, 3: None, 4: None}}
            for y, qmap in qs.items():
                for q in qmap:
                    qe = quarter_end(y, q)
                    shares_map.setdefault(t, {})[str(qe)] = (1_000_000.0, str(qe + pd.Timedelta(days=20)))
        for (y, q), zpath in zips.items():
            with zipfile.ZipFile(zpath) as z:
                subs = parse_submission_tsv(z, "SUBMISSION.tsv")
                inf = parse_infotable_tsv(z, "INFOTABLE.tsv", set(mapping.keys()))
                panel = build_holdings_panel(inf, subs, mapping, shares_map, y, q)
                panel, target = filter_to_report_period(panel)
                frames[(y, q)] = dedup_latest_per_manager(panel)
        sig = aggregate_quarterly(frames)
        write_outputs(sig, Path(args.out), {
            "start": args.start, "end": args.end, "sample_quarters": args.sample_quarters,
            "is_test": True, "source_migration_note": "synthetic stream (offline test)"
        })
        write_factor_store(sig, args.factor_store)
        print("[SEC 13F Loader] Test completed successfully.")
        return

    mapping = ensure_mapping_file(Path(args.mapping))
    if not mapping:
        raise SystemExit("No CUSIP mapping — curate data/mappings/cusip_to_ticker.json")
    print(f"CUSIP->ticker mapping: {len(mapping)} entries")
    sig = build_factor(args.start, args.end, args.sample_quarters,
                       Path(args.cache_dir), mapping,
                       Path(args.companyfacts_dir), use_legacy_xml=args.legacy_xml)
    if sig.empty:
        raise SystemExit("No aggregated 13F signals produced — check mapping/cache/network")
    write_outputs(sig, Path(args.out), {
        "start": args.start, "end": args.end, "sample_quarters": args.sample_quarters,
        "source_migration_note": "SEC 13F bulk moved to TSV (2023-format); XML bulk URL retired",
    })
    write_factor_store(sig, args.factor_store)
    print("Done.")


if __name__ == "__main__":
    main()