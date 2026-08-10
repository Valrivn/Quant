"""SEC fundamentals lane — bulk quarterly Financial Statement Data Sets.

Downloads the quarterly data-set zips from data.sec.gov, streams ``num.txt``
through a roster-CIK filter (never loads the full file into RAM), and upserts
PIT-dated fundamentals. Facts carry their filing ``filed`` date so every gate
evaluation can be look-ahead free.

Fallback path uses the existing per-CIK ``companyfacts`` API for tickers the
bulk sets do not cover.
"""

import io
import os
import zipfile
from typing import Dict, List, Optional

import pandas as pd
import requests

from discovery.sentinel import queue as q

# friendly -> US-GAAP tag (first candidate that yields a value wins).
TAG_MAP = {
    "ocf": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "revenue": ["Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerExcludingAssessedTax"],
    "gross_profit": ["GrossProfit"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsAndShortTermInvestments",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
             "CashAndDueFromBanks"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "total_assets": ["Assets"],
    "total_liabilities": ["Liabilities"],
    "equity": ["StockholdersEquity"],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    "ebit": ["OperatingIncomeLoss"],
}

# Cost-of-revenue tags used to DERIVE gross profit when a filer does not tag
# ``GrossProfit`` (Alphabet, Walmart, Qualcomm report CostOfRevenue; the energy
# and consumer names report CostOfGoodsAndServicesSold). META reports neither,
# so its gross margin stays genuinely unavailable.
COST_MAP = {
    "gross_cost": ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfServices"],
}

_FORMS = {"10-K", "10-Q"}


def _download(url: str, dest: str, user_agent: str) -> bool:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with requests.get(url, headers={"User-Agent": user_agent}, timeout=600, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    return True


def _read_sub(zh: zipfile.ZipFile) -> Dict[str, dict]:
    """Map adsh -> {cik, form, filed} for filings we care about."""
    out = {}
    raw = zh.read("sub.txt").decode("utf-8", errors="replace")
    header = None
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if header is None:
            header = parts
            continue
        d = dict(zip(header, parts))
        form = d.get("form", "").strip()
        if form not in _FORMS:
            continue
        filed = d.get("filed", "").strip()
        if len(filed) >= 10:
            filed = filed[:10]
        elif len(filed) == 8 and filed.isdigit():
            filed = f"{filed[:4]}-{filed[4:6]}-{filed[6:8]}"
        else:
            continue
        out[d["adsh"]] = {
            "cik": d.get("cik", "").strip(),
            "form": form,
            "filed": filed,
        }
    return out


def _parse_num_stream(zh: zipfile.ZipFile, sub: Dict[str, dict], cik_to_ticker: Dict[str, str]) -> List[dict]:
    """Stream num.txt keeping only roster rows for our tags. Low RAM."""
    reverse_tags = {}
    for friendly, tags in TAG_MAP.items():
        for t in tags:
            reverse_tags[t] = friendly

    rows = []
    with zh.open("num.txt") as f:
        header = None
        for raw in f:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if header is None:
                header = parts
                continue
            if len(parts) < len(header):
                continue
            d = dict(zip(header, parts))
            meta = sub.get(d["adsh"])
            if meta is None:
                continue
            ticker = cik_to_ticker.get(meta["cik"])
            if ticker is None:
                continue
            tag = d["tag"]
            friendly = reverse_tags.get(tag)
            if friendly is None:
                continue
            if d.get("uom", "") != "USD":
                continue
            try:
                value = float(d["value"])
            except (TypeError, ValueError):
                continue
            rows.append({
                "ticker": ticker,
                "fiscal_end": d["ddate"],
                "filed_date": meta["filed"],
                "form": meta["form"],
                "qtrs": int(d.get("qtrs") or 0),
                "friendly": friendly,
                "value": value,
            })
    return rows


def _rows_to_upserts(rows: List[dict]) -> List[dict]:
    """Group facts by (ticker, fiscal_end, filed_date); keep latest filed version.

    Within a group a field can carry several duration rows (qtrs=1..4) plus an
    instant row (qtrs=0). Prefer the highest ``qtrs`` per field so cumulative
    income/flow totals (full-year at the 10-K end) win over partial quarters,
    while instant balance items (qtrs=0) keep their only value.
    """
    grouped: Dict[tuple, dict] = {}
    qtrs: Dict[tuple, Dict[str, int]] = {}
    for r in rows:
        key = (r["ticker"], r["fiscal_end"], r["filed_date"])
        rec = grouped.setdefault(key, {
            "ticker": r["ticker"], "fiscal_end": r["fiscal_end"],
            "filed_date": r["filed_date"], "form": r["form"], "qtrs": r["qtrs"],
        })
        seen = qtrs.setdefault(key, {})
        if r["qtrs"] >= seen.get(r["friendly"], -1):
            rec[r["friendly"]] = r["value"]
            seen[r["friendly"]] = r["qtrs"]
    for rec in grouped.values():
        rev = rec.get("revenue")
        gp = rec.get("gross_profit")
        if rev is not None and gp is not None and rev != 0:
            rec["gross_margin"] = gp / rev
        else:
            rec["gross_margin"] = None
    return list(grouped.values())


def sync_quarterly_datasets(
    conn, cik_to_ticker: Dict[str, str], cfg: Dict,
    year: int, quarter: int,
) -> int:
    """Sync one quarter's data set into sentinel_fundamentals. Returns rows stored."""
    sec = cfg["lanes"]["sec"]
    base = sec["dataset_base"]
    url = f"{base}/{year}q{quarter}.zip"
    cache_dir = sec.get("zip_cache_dir", "data/sec_datasets")
    dest = os.path.join(cache_dir, f"{year}q{quarter}.zip")

    try:
        _download(url, dest, sec["user_agent"])
    except requests.HTTPError:
        return 0

    stored = 0
    with zipfile.ZipFile(dest) as zh:
        sub = _read_sub(zh)
        if not sub:
            return 0
        rows = _parse_num_stream(zh, sub, cik_to_ticker)
        for rec in _rows_to_upserts(rows):
            rec["source"] = f"sec_dataset_{year}q{quarter}"
            q.upsert_fundamental(conn, rec)
            stored += 1

    if not sec.get("keep_zip", False):
        try:
            os.remove(dest)
        except OSError:
            pass
    return stored


def sync_per_cik_fallback(
    conn, tickers: List[str], cik_resolver, cfg: Dict,
) -> int:
    """Per-CIK companyfacts fallback for tickers the bulk sets do not cover.

    Tries every candidate US-GAAP tag per friendly field and coalesces across
    them (some filers report e.g. CAPEX under PaymentsToAcquireProductiveAssets
    in recent periods, PaymentsToAcquirePropertyPlantAndEquipment in older
    ones). Merges fields by fiscal end with an outer join.
    """
    from valuation_alpha.datastore import xbrl_financials

    sec = cfg["lanes"]["sec"]

    stored = 0
    for ticker in tickers:
        cik = cik_resolver(ticker)
        if not cik:
            continue
        facts = xbrl_financials.fetch_companyfacts(cik, user_agent=sec["user_agent"])
        if not facts:
            continue
        combined = None
        filed_global = pd.Series(dtype=object)
        for friendly, tags in list(TAG_MAP.items()) + list(COST_MAP.items()):
            series = pd.Series(dtype=float)
            filed_series = pd.Series(dtype=object)
            for tag in tags:
                df = xbrl_financials.extract_quarterly_financials(
                    facts, {friendly: tag})
                if df is not None and not df.empty and friendly in df.columns:
                    if series.empty:
                        series = df[friendly].copy()
                    else:
                        series = series.combine_first(df[friendly])
                    if "filed_date" in df.columns:
                        if filed_series.empty:
                            filed_series = df["filed_date"].copy()
                        else:
                            filed_series = filed_series.combine_first(df["filed_date"])
            if series.empty:
                continue
            tmp = series.rename(friendly).to_frame()
            combined = tmp if combined is None else combined.join(tmp, how="outer")
            if filed_global.empty:
                filed_global = filed_series.copy()
            elif not filed_series.empty:
                filed_global = filed_global.combine_first(filed_series)
        if combined is None or combined.empty:
            continue
        if not filed_global.empty:
            combined = combined.join(filed_global.rename("filed_date"), how="left")
        combined = combined.sort_index()
        for fiscal_end, row in combined.iterrows():
            fdate = row.get("filed_date")
            rec = {
                "ticker": ticker,
                "fiscal_end": str(pd.Timestamp(fiscal_end).date()),
                "filed_date": str(pd.Timestamp(fdate).date())
                if pd.notna(fdate) else str(pd.Timestamp(fiscal_end).date()),
                "form": "10-Q", "qtrs": 1,
                "source": "companyfacts",
            }
            for col in ("ocf", "capex", "revenue", "gross_profit", "gross_cost",
                        "cash", "current_assets", "current_liabilities", "total_assets",
                        "total_liabilities", "equity", "retained_earnings", "ebit"):
                v = row.get(col)
                rec[col] = float(v) if pd.notna(v) else None
            rev, gp = rec.get("revenue"), rec.get("gross_profit")
            if gp is None and rev is not None:
                cost = rec.get("gross_cost")
                if cost is not None:
                    gp = rev - cost
                    rec["gross_profit"] = gp
            rec["gross_margin"] = (gp / rev) if (rev and gp is not None and rev != 0) else None
            q.upsert_fundamental(conn, rec)
            stored += 1
    return stored


def sync_lane(conn, tickers: List[str], cik_resolver, cfg: Dict,
              start_year: Optional[int] = None, years_back: int = 3) -> Dict:
    """Sync fundamentals from companyfacts (sole source) + optional bulk sets.

    Returns per-path counts. Bulk ingestion is OFF by default
    (``lanes.sec.use_bulk_datasets``) because the quarterly data sets carry
    segment/geography-disaggregated rows for large filers and cannot be turned
    into reliable per-company totals.
    """
    sec = cfg["lanes"]["sec"]
    start = start_year or sec.get("default_start_year", 2024)

    counts = {"bulk": 0, "fallback": 0, "quarters": 0}
    if sec.get("use_bulk_datasets", True):
        cik_to_ticker = {}
        for t in tickers:
            cik = cik_resolver(t)
            if cik:
                cik_to_ticker[str(cik).lstrip("0")] = t

        now_year = 2026
        for year in range(start, now_year + 1):
            q_range = range(1, 5) if year < now_year else range(1, 2)
            for quarter in q_range:
                stored = sync_quarterly_datasets(conn, cik_to_ticker, cfg, year, quarter)
                counts["quarters"] += 1
                counts["bulk"] += stored

    if sec.get("per_cik_fallback", True):
        targets = tickers if not sec.get("use_bulk_datasets", True) else \
            [t for t in tickers if not q.get_fundamentals(conn, t)]
        if targets:
            counts["fallback"] = sync_per_cik_fallback(conn, targets, cik_resolver, cfg)
    return counts
