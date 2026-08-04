"""SEC XBRL company facts and quarterly financial extraction."""

import requests
import pandas as pd

_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


def fetch_companyfacts(cik: str, user_agent: str = "quant-research contact@example.com") -> dict:
    """Fetch the SEC companyfacts JSON for a CIK. Returns {} on any failure."""
    try:
        resp = requests.get(
            _COMPANYFACTS_URL.format(cik=cik),
            headers={"User-Agent": user_agent},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return {}


def _series(companyfacts: dict, tag: str):
    units = companyfacts.get("facts", {}).get("us-gaap", {}).get(tag, {}).get("units", {})
    return units.get("USD", [])


def _quarterly_rows(companyfacts: dict, tag: str):
    rows = []
    for entry in _series(companyfacts, tag):
        form = entry.get("form", "")
        if form != "10-Q":
            continue
        if "frame" not in entry and "end" not in entry:
            continue
        rows.append(entry)
    return rows


def extract_quarterly_financials(companyfacts: dict, fields: dict) -> pd.DataFrame:
    """Extract quarterly (10-Q) rows for the given friendly-name -> US-GAAP tag map.

    Returns a DataFrame indexed by fiscal end date with one column per friendly name.
    Missing tags yield NaN. When a tag carries point-in-time ``filed`` dates, a
    ``filed_date`` column is added so callers can filter facts by
    ``filed_date <= decision_date`` (PIT, no lookahead).
    """
    if not companyfacts or not fields:
        return pd.DataFrame()

    series = {}
    for friendly, tag in fields.items():
        entries = _quarterly_rows(companyfacts, tag)
        if not entries:
            continue
        idx = {}
        vals = {}
        for entry in entries:
            end = entry.get("end")
            if not end:
                continue
            idx[end] = end
            vals[end] = entry.get("val")
        if vals:
            series[friendly] = pd.Series(vals, dtype=float)

    if not series:
        return pd.DataFrame()

    df = pd.DataFrame(series)
    df.index = pd.to_datetime(df.index)
    df.index.name = "fiscal_end"
    df = df.sort_index()

    # Point-in-time filed date from the revenue series (same filing cadence as
    # the rest of the 10-Q facts). PIT-safe: keep the EARLIEST filing date per
    # fiscal end so restatements never leak a later date backward.
    filed = {}
    for entry in _quarterly_rows(companyfacts, list(fields.values())[0]):
        end = entry.get("end")
        f = entry.get("filed")
        if not end or not f:
            continue
        key = pd.Timestamp(end)
        filed[key] = min(filed[key], f) if key in filed else f
    if filed:
        df["filed_date"] = pd.Series(filed, dtype=str).reindex(df.index)
        df["filed_date"] = pd.to_datetime(df["filed_date"])
    return df


def fetch_employee_counts(cik, user_agent="quant-research contact@example.com") -> pd.DataFrame:
    """Fetch the SEC 10-K employee count series for a CIK.

    Returns a DataFrame with columns [date, employees]; empty if unavailable.
    """
    facts = fetch_companyfacts(cik, user_agent)
    entries = _series(facts, "Employees")
    if not entries:
        return pd.DataFrame(columns=["date", "employees"])
    rows = []
    for entry in entries:
        if entry.get("form") != "10-K":
            continue
        end = entry.get("end")
        if not end:
            continue
        rows.append({"date": end, "employees": entry.get("val")})
    df = pd.DataFrame(rows, columns=["date", "employees"])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)