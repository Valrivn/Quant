"""SEC EDGAR SIC vs. Damodaran industry cross-check module.

Double-checks candidate company Damodaran industry membership against SEC
EDGAR SIC codes/descriptions to prevent misassigning overly broad or incorrect
betas. Fail-closed on mismatch.
"""

import json
import os
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

SUBS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_UA = "HouseOfQuant research (contact@example.com)"


def classify(sic_code: Optional[str]) -> str:
    """Coarse SIC-Division classifier mapping numeric SIC codes to industry buckets.

    Used for coarse mismatch detection when standard comparisons cannot be completed.
    Returns one of: 'manufacturing', 'services', 'finance', 'oil_gas', 'utilities',
    'agriculture', or 'other'.
    """
    if sic_code is None:
        return "other"

    try:
        val = int(sic_code)
    except (ValueError, TypeError):
        return "other"

    if 100 <= val <= 999:
        return "agriculture"
    elif 1300 <= val <= 1399:
        return "oil_gas"
    elif 2000 <= val <= 3999:
        return "manufacturing"
    elif 4900 <= val <= 4999:
        return "utilities"
    elif 6000 <= val <= 6999:
        return "finance"
    elif 7000 <= val <= 7379:
        return "services"
    else:
        return "other"


def sec_submissions_df_from_response(payload: dict) -> dict:
    """Pure parser for SEC EDGAR GET /submissions/CIK*.json payload.

    Extracts sic, sicDescription, industry, cik, and name with fallback to None.
    """
    if not isinstance(payload, dict):
        return {
            "sic": None,
            "sic_description": None,
            "industry": None,
            "cik": "",
            "name": None,
        }

    raw_sic = payload.get("sic")
    sic_str = str(raw_sic) if raw_sic is not None else None

    raw_cik = payload.get("cik", "")
    cik_str = str(raw_cik) if raw_cik is not None else ""

    return {
        "sic": sic_str,
        "sic_description": payload.get("sicDescription"),
        "industry": payload.get("industry"),
        "cik": cik_str,
        "name": payload.get("name"),
    }


def fetch_sec_sic(cik: str, timeout: int = 30) -> dict:
    """Fetch SEC submissions JSON for given CIK over network and return parsed dict.

    Gated behind DISCOVERY_LIVE=1 environment variable check.
    """
    if os.environ.get("DISCOVERY_LIVE") != "1":
        raise RuntimeError("network disabled (set DISCOVERY_LIVE=1)")

    try:
        cik_int = int(cik)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid CIK format: {cik!r}") from exc

    url = SUBS_URL.format(cik=cik_int)
    req = urllib.request.Request(url, headers={"User-Agent": EDGAR_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"SEC request failed with status {resp.status}")
        payload = json.loads(resp.read().decode("utf-8"))

    return sec_submissions_df_from_response(payload)


def compare(
    damodaran_sic: Optional[str],
    sec_sic: Optional[str],
    dsec: Optional[str] = None,
    sec_description: Optional[str] = None,
    damodaran_industry: Optional[str] = None,
    sec_industry: Optional[str] = None,
) -> dict:
    """Compare Damodaran SIC/industry with SEC SIC/industry data.

    Returns dict with status ('ok', 'mismatch', 'unknown') and reason string.
    """
    d_sic_clean = str(damodaran_sic).strip() if damodaran_sic is not None else None
    s_sic_clean = str(sec_sic).strip() if sec_sic is not None else None

    d_num = None
    if d_sic_clean is not None:
        try:
            d_num = int(d_sic_clean)
        except ValueError:
            pass

    s_num = None
    if s_sic_clean is not None:
        try:
            s_num = int(s_sic_clean)
        except ValueError:
            pass

    # Both SIC present and numeric
    if d_num is not None and s_num is not None:
        if d_num == s_num:
            return {"status": "ok", "reason": f"SIC codes match: {d_num}"}
        else:
            d_ind_str = f" ({damodaran_industry})" if damodaran_industry else ""
            s_ind_str = f" ({sec_industry})" if sec_industry else ""
            return {
                "status": "mismatch",
                "reason": (
                    f"SIC mismatch: Damodaran {d_num}{d_ind_str} vs "
                    f"SEC {s_num}{s_ind_str}"
                ),
            }

    # Both SIC present but at least one is non-numeric
    if d_sic_clean is not None and s_sic_clean is not None:
        return {
            "status": "unknown",
            "reason": f"Non-numeric SIC comparison: {d_sic_clean!r} vs {s_sic_clean!r}",
        }

    # One or both SIC missing: attempt conservative text matching or classify fallback
    if damodaran_industry and sec_industry:
        d_tokens = set(str(damodaran_industry).lower().split())
        s_tokens = set(str(sec_industry).lower().split())
        if d_tokens and s_tokens and (d_tokens & s_tokens):
            return {
                "status": "ok",
                "reason": (
                    f"Industry name token overlap: '{damodaran_industry}' vs "
                    f"'{sec_industry}'"
                ),
            }
        else:
            return {
                "status": "mismatch",
                "reason": (
                    f"Industry name token mismatch: '{damodaran_industry}' vs "
                    f"'{sec_industry}'"
                ),
            }

    # Classify bucket check if one SIC is present
    present_sic = d_sic_clean if d_sic_clean is not None else s_sic_clean
    if present_sic is not None:
        bucket = classify(present_sic)
        return {
            "status": "unknown",
            "reason": f"Single SIC present ({present_sic}, bucket '{bucket}'), insufficient metadata",
        }

    return {"status": "unknown", "reason": "Insufficient SIC and industry metadata"}


def check_ticker(
    ticker: str,
    cik_lookup: dict,
    sec_fetcher: Callable[[str], dict],
    damodaran_row: dict,
) -> dict:
    """Orchestrate industry comparison for a single ticker."""
    cik = cik_lookup.get(ticker)

    damodaran_sic = damodaran_row.get("sic_code")
    damodaran_industry = damodaran_row.get("industry_group")

    if not cik:
        return {
            "ticker": ticker,
            "cik": None,
            "damodaran_industry": damodaran_industry,
            "damodaran_sic": damodaran_sic,
            "sec_sic": None,
            "sec_sic_description": None,
            "sec_industry": None,
            "status": "unknown",
            "reason": f"Ticker {ticker} not found in CIK lookup",
        }

    sec_info = sec_fetcher(str(cik))
    sec_sic = sec_info.get("sic")
    sec_sic_desc = sec_info.get("sic_description")
    sec_industry = sec_info.get("industry")

    comp = compare(
        damodaran_sic=damodaran_sic,
        sec_sic=sec_sic,
        sec_description=sec_sic_desc,
        damodaran_industry=damodaran_industry,
        sec_industry=sec_industry,
    )

    return {
        "ticker": ticker,
        "cik": cik,
        "damodaran_industry": damodaran_industry,
        "damodaran_sic": damodaran_sic,
        "sec_sic": sec_sic,
        "sec_sic_description": sec_sic_desc,
        "sec_industry": sec_industry,
        "status": comp["status"],
        "reason": comp["reason"],
    }


def check_members(
    members_df,
    cik_lookup: dict,
    sec_fetcher: Optional[Callable[[str], dict]] = None,
    tickers: Optional[List[str]] = None,
) -> List[dict]:
    """Batch check Damodaran members DataFrame against SEC data.

    Returns list of record dicts sorted by (status desc, ticker).
    """
    if sec_fetcher is None:
        if os.environ.get("DISCOVERY_LIVE") != "1":
            raise RuntimeError("network disabled (set DISCOVERY_LIVE=1)")
        sec_fetcher = fetch_sec_sic

    results = []
    # Work with pandas DataFrame or dict/records list
    if hasattr(members_df, "to_dict"):
        rows = members_df.to_dict(orient="records")
    elif isinstance(members_df, list):
        rows = members_df
    else:
        rows = []

    ticker_set = set(tickers) if tickers else None

    for row in rows:
        t = row.get("ticker")
        if not t:
            continue
        if ticker_set is not None and t not in ticker_set:
            continue
        if t not in cik_lookup:
            continue

        res = check_ticker(
            ticker=t,
            cik_lookup=cik_lookup,
            sec_fetcher=sec_fetcher,
            damodaran_row=row,
        )
        results.append(res)

    results.sort(key=lambda r: (r["status"], r["ticker"]), reverse=False)
    # Sort status desc (e.g. unknown > ok > mismatch or mismatch > ok > unknown based on string desc,
    # prompt asks for sorted by (status desc, ticker)):
    results.sort(key=lambda r: (-ord(r["status"][0]) if r["status"] else 0, r["ticker"]))
    return results


def main():
    """CLI driver for sec_industry_check."""
    import argparse

    parser = argparse.ArgumentParser(description="SEC vs Damodaran industry cross-check")
    parser.add_argument("--tickers", type=str, help="Comma-separated tickers")
    parser.add_argument("--members", type=str, default="data/damodaran/members_latest.csv", help="Members CSV path")
    parser.add_argument("--live", action="store_true", help="Enable live SEC network calls")
    args = parser.parse_args()

    if not args.live:
        print("network disabled; provide --live")
        return

    os.environ["DISCOVERY_LIVE"] = "1"
    if not os.path.exists(args.members):
        print(f"Error: members file not found: {args.members}")
        return

    import pandas as pd

    members_df = pd.read_csv(args.members)

    # Fetch CIK lookup table
    req = urllib.request.Request(TICKERS_URL, headers={"User-Agent": EDGAR_UA})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    cik_lookup = {}
    for entry in data.values():
        t = entry.get("ticker")
        c = entry.get("cik_str")
        if t and c is not None:
            cik_lookup[str(t).upper()] = str(c)

    target_tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    results = check_members(members_df, cik_lookup, sec_fetcher=fetch_sec_sic, tickers=target_tickers)
    for r in results:
        print(r)


if __name__ == "__main__":
    main()
