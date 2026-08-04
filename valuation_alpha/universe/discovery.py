"""Discovery universe loader (B-20260803 P1/P2).

Loads the S&P MidCap 400 and S&P SmallCap 600 constituent lists from Wikipedia
(public, refreshed quarterly) and returns roster-compatible rows: ticker, group,
bias, sector, sec_cik. GICS sectors are normalized to the lowercase taxonomy
used by the L1 roster. CIKs come from the Wikipedia table where available and
otherwise from the SEC resolver cache.
"""

import io

import pandas as pd
import requests

from valuation_alpha.universe.cik_resolver import enrich_universe

_WIKI_URLS = {
    "MID": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "SMALL": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)

# GICS Sector -> roster taxonomy. Roster uses lowercase; keep the same style.
_SECTOR_MAP = {
    "Information Technology": "information_technology",
    "Communication Services": "communication_services",
    "Consumer Discretionary": "consumer_cyclical",
    "Consumer Staples": "consumer_defensive",
    "Health Care": "healthcare",
    "Financials": "financials",
    "Energy": "energy",
    "Materials": "materials",
    "Industrials": "industrials",
    "Real Estate": "real_estate",
    "Utilities": "utilities",
}

# Small number of known fallback sector labels in case GICS wording shifts.
_SECTOR_ALIASES = {
    "tech": "information_technology",
    "technology": "information_technology",
    "semis": "semiconductor",
    "semiconductors": "semiconductor",
}


def _normalize_sector(sector: str) -> str | None:
    if not sector:
        return None
    s = sector.strip()
    mapped = _SECTOR_MAP.get(s) or _SECTOR_ALIASES.get(s.lower())
    if mapped:
        return mapped
    return s.lower().replace(" ", "_").replace("-", "_")


def fetch_constituents(index: str) -> pd.DataFrame:
    """Fetch the Wikipedia constituent table for MID (S&P 400) or SMALL (S&P 600).

    Returns a DataFrame with columns [ticker, security, sector, sub_industry,
    cik]. Empty DataFrame on any failure.
    """
    url = _WIKI_URLS.get(index)
    if url is None:
        return pd.DataFrame()
    try:
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=30)
        resp.raise_for_status()
        html = io.StringIO(resp.text)
        dfs = pd.read_html(html, header=0)
    except Exception:
        return pd.DataFrame()
    for df in dfs:
        cols = [str(c) for c in df.columns]
        if "Symbol" in cols or "Ticker" in cols:
            ticker_col = next(c for c in cols if c in ("Symbol", "Ticker"))
            sector_col = next((c for c in cols if "Sector" in c), None)
            cik_col = "CIK" if "CIK" in cols else None
            if sector_col is None:
                continue
            out = pd.DataFrame({
                "ticker": df[ticker_col].astype(str).str.upper().str.strip(),
                "security": df["Security"].astype(str).str.strip() if "Security" in cols else "",
                "sector": df[sector_col].astype(str).str.strip(),
                "sub_industry": (
                    df["GICS Sub-Industry"].astype(str).str.strip()
                    if "GICS Sub-Industry" in cols else ""
                ),
            })
            if cik_col is not None:
                out["cik"] = df[cik_col].astype(str).str.strip().str.zfill(10)
            else:
                out["cik"] = ""
            # Keep only rows with a plausible ticker (all-caps, non-empty).
            out = out[out["ticker"].str.match(r"^[A-Z.0-9-]{1,10}$")].copy()
            return out.reset_index(drop=True)
    return pd.DataFrame()


def load_discovery_universe(
    index: str = "MID", enrich_ciks: bool = True
) -> list:
    """Return roster-compatible rows for an index tier.

    Rows have the same keys as ``valuation_alpha.universe.roster`` rows:
    ticker, group, bias, sector, sec_cik. ``group`` is the tier ("MID"/"SMALL");
    ``bias`` is always False (discovery names are non-megacap).
    """
    df = fetch_constituents(index)
    if df.empty:
        return []
    rows = []
    for _, row in df.iterrows():
        rows.append({
            "ticker": row["ticker"],
            "group": index,
            "bias": False,
            "sector": _normalize_sector(row["sector"]),
            "sec_cik": row.get("cik") or None,
        })
    if enrich_ciks:
        rows = enrich_universe(rows)
    return rows


def discovery_universe_baseline() -> list:
    """Build a small, cheap, deterministic subset for offline unit tests and the
    P2 pilot before full constituent refresh: 10 mid + 10 small names."""
    baseline = [
        # (ticker, tier, sector, cik)
        ("EME", "MID", "Industrials", "0001035684"),
        ("FIX", "MID", "Industrials", "0000053430"),
        ("CNM", "MID", "Industrials", "0001849297"),
        ("FAST", "MID", "Industrials", "0000081555"),
        ("DCI", "MID", "Industrials", "0000316323"),
        ("DOV", "MID", "Industrials", "0000029905"),
        ("EVR", "MID", "Financials", "0001392367"),
        ("EXP", "MID", "Materials", "0001045302"),
        ("FND", "MID", "Consumer Discretionary", "0001704648"),
        ("WSM", "MID", "Consumer Discretionary", "0001050377"),
        ("ASO", "SMALL", "Consumer Discretionary", "0001850303"),
        ("ALKT", "SMALL", "Information Technology", "0001689921"),
        ("HIMS", "SMALL", "Health Care", "0001773757"),
        ("BMBL", "SMALL", "Communication Services", "0001830043"),
        ("PCH", "SMALL", "Real Estate", "0001308606"),
        ("GVA", "SMALL", "Industrials", "0000079642"),
        ("PRIM", "SMALL", "Industrials", "0001559720"),
        ("CVLT", "SMALL", "Information Technology", "0001631659"),
        ("LGND", "SMALL", "Health Care", "0000884983"),
        ("OSIS", "SMALL", "Information Technology", "0001035742"),
    ]
    return [
        {
            "ticker": t,
            "group": tier,
            "bias": False,
            "sector": _normalize_sector(sector),
            "sec_cik": cik,
        }
        for t, tier, sector, cik in baseline
    ]
