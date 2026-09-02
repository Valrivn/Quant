"""Damodaran online dataset discovery module (ruling D-20260901-001).

Structural Firewall: Research-only data layer. This code and its outputs must
never feed or be integrated into backtest-agent without explicit ruling.

Website single source of truth for Damodaran industry betas and company roster.
Resolves IPv4 only for stern.nyu.edu requests to prevent getaddrinfo issues.
"""

import os
from datetime import date
from typing import Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

BETAS_URL = "https://www.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
MEMBERS_URL = "https://www.stern.nyu.edu/~adamodar/pc/datasets/indname.xls"
HEADERS = {"User-Agent": "Quant Research (contact@example.com)"}
DEFAULT_CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "damodaran")

_STERN_HOSTS = ("pages.stern.nyu.edu", "www.stern.nyu.edu", "stern.nyu.edu")


def _pin_ipv4() -> None:
    """Force IPv4-only resolution for stern.nyu.edu hosts.

    The box's DNS resolver intermittently fails on the AAAA query; pinning
    AF_INET at the socket layer (with an urllib3 hint) makes the fetch reliable.
    """
    try:
        import urllib3.util.connection
        urllib3.util.connection.HAS_IPV6 = False
    except Exception:  # noqa: BLE001
        pass

    import socket

    try:
        _orig_getaddrinfo = socket.getaddrinfo

        def _getaddrinfo(*_args, **_kwargs):
            _res = _orig_getaddrinfo(*_args, **_kwargs)
            _host = _kwargs.get("host") or (_args[0] if _args else None)
            if isinstance(_host, str) and _host.endswith(_STERN_HOSTS):
                return [r for r in _res if r[0] == socket.AF_INET]
            return _res

        socket.getaddrinfo = _getaddrinfo
    except Exception:  # noqa: BLE001
        pass


def _normalize_industries(df: "pd.DataFrame", cols: list) -> "pd.DataFrame":
    """Collapse whitespace in industry names and coerce numeric columns."""
    import pandas as pd

    df["industry"] = df["industry"].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    df = df[~df["industry"].str.startswith("Total Market", na=False)].copy()
    numeric_cols = [c for c in cols if c != "industry"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["unlevered_beta"]).copy()
    df = df[df["unlevered_beta"] > 0].copy()
    return df.reset_index(drop=True)


# Column order of the betas.xls "Industry Averages" sheet (header row 9).
_BETAS_COLS = [
    "industry",
    "number_of_firms",
    "beta",
    "debt_to_equity",
    "effective_tax_rate",
    "unlevered_beta",
    "cash_firm_value",
    "unlevered_beta_cash",
    "hilo_risk",
    "sd_equity",
    "sd_operating_income",
]


def parse_betas_xls(content: bytes) -> "pd.DataFrame":
    """Parse raw betas.xls content into a normalized DataFrame.

    The .xls holds the exact canonical industry names Damodaran uses in the
    member roster; the public HTML page truncates long names (~30 chars).
    """
    import io

    import pandas as pd
    import xlrd

    wb = xlrd.open_workbook(file_contents=content)
    sh = wb.sheet_by_name("Industry Averages")
    header_row = None
    for i in range(min(20, sh.nrows)):
        if str(sh.cell_value(i, 0)).strip() == "Industry Name":
            header_row = i
            break
    if header_row is None:
        raise ValueError("header row not found in betas.xls")

    rows = [sh.row_values(r)[: len(_BETAS_COLS)] for r in range(header_row + 1, sh.nrows)]
    df = pd.DataFrame(rows, columns=_BETAS_COLS)
    return _normalize_industries(df, _BETAS_COLS)


def parse_betas_html(content: bytes) -> "pd.DataFrame":
    """Parse raw Damodaran Betas.html content into a normalized DataFrame."""
    import pandas as pd
    from bs4 import BeautifulSoup  # noqa: F401 - ensures default flavor availability

    tables = pd.read_html(content)
    if not tables:
        raise ValueError("No table found in Betas.html")
    df = tables[0]

    # Row 0 is header row
    headers = df.iloc[0].tolist()
    df = df.iloc[1:].copy()
    df.columns = headers

    df = df.iloc[:, : len(_BETAS_COLS)].copy()
    df.columns = _BETAS_COLS
    return _normalize_industries(df, _BETAS_COLS)


def parse_members_xls(content: bytes) -> "pd.DataFrame":
    """Parse raw Damodaran indname.xls content into a normalized DataFrame."""
    import io
    import pandas as pd
    import xlrd

    wb = xlrd.open_workbook(file_contents=content)
    sh = wb.sheet_by_name("By industry")
    raw_rows = [sh.row_values(r, end_colx=6) for r in range(1, sh.nrows)]

    cleaned_rows = []
    for r in raw_rows:
        row_vals = []
        for val in r[:6]:
            if val is None or val == "":
                row_vals.append(pd.NA)
            elif isinstance(val, float) and val.is_integer():
                row_vals.append(str(int(val)))
            else:
                row_vals.append(str(val).strip())
        cleaned_rows.append(row_vals)

    cols = ["company", "ticker", "industry_group", "primary_sector", "sic_code", "country"]
    df = pd.DataFrame(cleaned_rows, columns=cols)
    return df


def _get_content(url: str, timeout: int, tries: int = 3) -> bytes:
    """GET with IPv4 pin + bounded retry; raises after ``tries`` attempts."""
    import time

    import requests

    _pin_ipv4()
    last_exc: Optional[Exception] = None
    for attempt in range(tries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:  # noqa: BLE001 - transient DNS/TLS, retried
            last_exc = exc
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


def fetch_betas(timeout: int = 90) -> "pd.DataFrame":
    """Fetch live Damodaran industry betas table (authoritative .xls)."""
    return parse_betas_xls(_get_content(BETAS_URL, timeout))


def fetch_members(timeout: int = 180) -> "pd.DataFrame":
    """Fetch live Damodaran company membership roster."""
    return parse_members_xls(_get_content(MEMBERS_URL, timeout))


def refresh(cache_dir: Optional[str] = None) -> Dict[str, str]:
    """Fetch live data and save betas_latest.csv & members_latest.csv to cache."""
    import yaml

    cdir = cache_dir or DEFAULT_CACHE_DIR
    os.makedirs(cdir, exist_ok=True)

    betas_df = fetch_betas()
    members_df = fetch_members()

    betas_path = os.path.join(cdir, "betas_latest.csv")
    members_path = os.path.join(cdir, "members_latest.csv")
    meta_path = os.path.join(cdir, "betas_meta.yaml")

    betas_df.to_csv(betas_path, index=False)
    members_df.to_csv(members_path, index=False)

    today_str = str(date.today())
    meta = {
        "captured": today_str,
        "source": BETAS_URL,
        "members_source": MEMBERS_URL,
    }
    with open(meta_path, "w") as f:
        yaml.safe_dump(meta, f)

    return {
        "betas_path": betas_path,
        "members_path": members_path,
        "captured": today_str,
    }


def load_betas(cache_dir: Optional[str] = None) -> "pd.DataFrame":
    """Load betas table from cache if existing, else fetch live."""
    import pandas as pd

    cdir = cache_dir or DEFAULT_CACHE_DIR
    path = os.path.join(cdir, "betas_latest.csv")
    if os.path.exists(path):
        return pd.read_csv(path)
    df = fetch_betas()
    os.makedirs(cdir, exist_ok=True)
    df.to_csv(path, index=False)
    return df


def load_members(cache_dir: Optional[str] = None) -> "pd.DataFrame":
    """Load members table from cache if existing, else fetch live."""
    import pandas as pd

    cdir = cache_dir or DEFAULT_CACHE_DIR
    path = os.path.join(cdir, "members_latest.csv")
    if os.path.exists(path):
        return pd.read_csv(path)
    df = fetch_members()
    os.makedirs(cdir, exist_ok=True)
    df.to_csv(path, index=False)
    return df


def industry_beta_map(betas: "pd.DataFrame") -> Dict[str, float]:
    """Map industry -> rounded unlevered_beta preserving table order."""
    out = {}
    for _, row in betas.iterrows():
        ind = str(row["industry"])
        val = float(row["unlevered_beta"])
        out[ind] = round(val, 4)
    return out


def sorted_companies(
    betas: "pd.DataFrame",
    members: "pd.DataFrame",
    ascending: bool = True,
    country_filter: Optional[str] = None,
) -> "pd.DataFrame":
    """Join members to betas by industry and sort by unlevered_beta then company."""
    import pandas as pd

    merged = pd.merge(
        members,
        betas,
        left_on="industry_group",
        right_on="industry",
        how="inner",
    )

    if country_filter == "US":
        ticker_str = merged["ticker"].fillna("").astype(str)
        is_us = (merged["country"] == "United States") | (
            ticker_str.str.contains("Nasdaq|NYSE|NASDAQ|AMEX", regex=True)
        )
        merged = merged[is_us].copy()
    elif country_filter is not None:
        merged = merged[merged["country"] == country_filter].copy()

    cols = [
        "company",
        "ticker",
        "country",
        "industry",
        "primary_sector",
        "sic_code",
        "beta",
        "unlevered_beta",
        "sd_equity",
    ]
    res = merged.copy()
    res = res.rename(columns={"beta": "levered_beta"})
    out_cols = [
        "company",
        "ticker",
        "country",
        "industry",
        "primary_sector",
        "sic_code",
        "levered_beta",
        "unlevered_beta",
        "sd_equity",
    ]
    res = res[out_cols].copy()

    res = res.sort_values(
        by=["unlevered_beta", "company"],
        ascending=[ascending, True],
    ).reset_index(drop=True)
    return res


def sanity_trial(betas: "pd.DataFrame", members: "pd.DataFrame") -> dict:
    """Return trial metrics dictionary for testing / verification."""
    sc_us = sorted_companies(betas, members, country_filter="US")
    min_b = float(betas["unlevered_beta"].min()) if not betas.empty else 0.0
    max_b = float(betas["unlevered_beta"].max()) if not betas.empty else 0.0
    return {
        "industries": len(betas),
        "companies": len(members),
        "us_companies": len(sc_us),
        "beta_range": (round(min_b, 4), round(max_b, 4)),
    }
