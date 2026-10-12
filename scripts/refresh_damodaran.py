"""
refresh_damodaran.py -- Automated Damodaran Beta, Ratings & ERP Refresh

CEO RULING D3 (logged logs/college/2026-10-06/college.jsonl,
``ceo_ruling_d3_damodaran_refresh``)::

    "Dynamic automated download every 2 months, version-stamped with SHA-256,
     logged."

REFRESH CADENCE
---------------
**Run this every 2 months.**  Damodaran republishes his dataset bundle once a
year at the start of the calendar year (stated on the page itself: "updated
once a year, at the start of the year"), and rolls the country-risk-premium
workbook forward quarterly (the page lists, e.g., "January 1, 2026 update",
"April 1, 2026 update", "July 1, 2026 update").  A 2-month cadence therefore
straddles the quarterly rolls and re-checks the annual bundle without polling
the site daily.  Suggested schedule: the first week of every even-numbered
month.  Set the version + SHA-256 into the config ``update:`` block each time,
so a stale config is detectable rather than silent.

DISCOVERY
---------
File URLs are **discovered dynamically** from
https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datacurrent.html
(``DATCURRENT_URL``).  Damodaran rotates the country-risk-premium workbook's
filename every quarter (``ctryprem.xlsx`` -> ``ctrypremApr26.xlsx`` ->
``ctrypremJuly26.xlsx``), so hard-coding URLs guarantees a stale download.
``discover_dataset_urls()`` parses the index page and resolves each logical
dataset to its current-year download, keeping the hard-coded
:data:`FALLBACK_URLS` only as an offline fallback.

MODES
-----
``--check``  (read-only, the default-safe mode)
    Download every dataset, compute its SHA-256, state the version found in
    the page/file, compare both against the ``update:`` block recorded in the
    target config manifest, and report CHANGED / UNCHANGED per dataset.
    **Writes nothing** -- no config, no manifest, no staging file.

``--apply``  (writes)
    Parse the downloads and write refreshed YAML **staging** files under
    ``config/staging_damodaran/``.  The live configs
    (``config/industry_beta.yaml``, ``config/damodaran_erp.yaml``) are
    **NOT** overwritten unless ``--apply`` is combined with an explicit
    ``--out`` path.  Staging output carries version + SHA-256 + fetch timestamp
    + source URL in an ``update:`` block so a reviewer can diff before
    ratifying.

Every run -- check or apply -- appends a college-audit record to
``logs/college/<date>/college.jsonl`` and ``college_append.jsonl`` stating the
versions and checksums observed.

Usage:
  # read-only drift report (writes nothing)
  python scripts/refresh_damodaran.py --check

  # write staging YAML under config/staging_damodaran/
  python scripts/refresh_damodaran.py --apply

  # ratify a staged file over a live config (explicit --out required)
  python scripts/refresh_damodaran.py --apply --out config/damodaran_erp.yaml

  # legacy single-target flows (unchanged)
  python scripts/refresh_damodaran.py [--out config/industry_beta.yaml]
                                      [--mirror-dir data/damodaran_mirror]
                                      [--dry-run] [--use-mirror]
  python scripts/refresh_damodaran.py --emit-sector-beta
                                      [--from config/industry_beta.yaml]
                                      [--out-sector config/sector_beta.yaml]

Design:
  - Defensive parsing: tries live fetch first, falls back to local mirror.
  - Validates structure: checks required industry names, beta ranges,
    default spread ranges, and minimum row counts.
  - Outputs YAML with version (YYYY.MM format), SHA-256 checksum,
    fetch timestamp, and data source URLs.
  - Never writes to production tables; this is a data refresh script only.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("refresh_damodaran")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BETAS_URL = "https://www.stern.nyu.edu/~adamodar/pc/datasets/betas.xls"
RATINGS_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/ratings.htm"
INDNAMES_URL = "https://www.stern.nyu.edu/~adamodar/pc/datasets/indname.xls"
HEADERS = {"User-Agent": "Quant Research (contact@example.com) - monthly refresh"}

# --- CEO Ruling D3 plumbing -------------------------------------------------
#: Dataset index page. Every download URL is resolved from here at run time.
DATCURRENT_URL = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datacurrent.html"

#: Ruling D3 cadence. Damodaran republishes annually at the start of the year
#: and rolls the country-risk-premium workbook quarterly; a 2-month cadence
#: straddles the rolls without hammering the site.
REFRESH_CADENCE = "every 2 months"

#: Where --apply writes by default. Live configs are never touched without an
#: explicit --out.
STAGING_DIR = Path("config/staging_damodaran")

#: Live config targets. Only written when --apply AND --out name them.
LIVE_INDUSTRY_BETA = Path("config/industry_beta.yaml")
LIVE_DAMODARAN_ERP = Path("config/damodaran_erp.yaml")
PROTECTED_LIVE_CONFIGS = (LIVE_INDUSTRY_BETA, LIVE_DAMODARAN_ERP)

# Minimum expected rows for validation
MIN_BETA_INDUSTRIES = 70
MIN_RATING_TIERS = 10

# Required industry names that MUST appear in the beta table (spot checks)
REQUIRED_INDUSTRIES = [
    "Semiconductor",
    "Software (System & Application)",
    "Aerospace/Defense",
    "Banks (Regional)",
    "Oil/Gas (Integrated)",
    "Pharmaceutical",  # partial match
]

# Beta value sanity bounds
MIN_BETA = 0.05
MAX_BETA = 3.0

# ---------------------------------------------------------------------------
# GICS Level-2 -> Damodaran industry mapping (Phase 8 WACC engine v2)
# ---------------------------------------------------------------------------
# Phase 8 bigpickle analysis §2.1 / §Appendix B: bottom-up beta requires a
# GICS L2 -> Damodaran 94-industry mapping so every tradable ticker can be
# assigned an unlevered beta.  The mapping is static (GICS taxonomy is stable
# at L2 since 2018; any industry regrouping is reviewed by a human and bumps
# the mapping_version below) and lives here so config/sector_beta.yaml is
# reproducible from the same source of record.
GICS_L2_BETA_MAPPING_VERSION = "2026.09"

#: {gics_l2_code: damodaran_industry_name} — code = GICS 6-digit level-2 code
#: (sector_id * 10000 + industry_group_id * 100), e.g. 101010 = Energy
#: Equip & Svcs.  Industry names must match industry_beta.yaml exactly.
GICS_L2_TO_DAMODARAN: Dict[str, str] = {
    # Energy (10)
    "101010": "Oil/Gas (Production and Exploration)",
    "101020": "Oil/Gas Distribution",
    # Materials (15)
    "151010": "Chemical (Diversified)",
    "151020": "Building Materials",
    "151030": "Packaging & Container",
    "151040": "Metals & Mining",
    "151050": "Paper/Forest Products",
    # Industrials (20)
    "201010": "Aerospace/Defense",
    "201020": "Building Materials",
    "201030": "Engineering/Construction",
    "201040": "Electrical Equipment",
    "201050": "Machinery",
    "201060": "Trucking",
    "202010": "Business & Consumer Services",
    "202020": "Environmental & Waste Services",
    "203010": "Air Transport",
    "203020": "Transportation",
    "203030": "Transportation (Railroads)",
    # Consumer Discretionary (25)
    "251010": "Auto & Truck",
    "251020": "Auto Parts",
    "252010": "Apparel",
    "252020": "Furn/Home Furnishings",
    "252030": "Recreation",
    "253010": "Hotel/Gaming",
    "253020": "Restaurant/Dining",
    "254010": "Entertainment",
    "255010": "Retail (General)",
    "255020": "Retail (Automotive)",
    "255030": "Retail (Building Supply)",
    "255040": "Retail (Distributors)",
    "255050": "Retail (Special Lines)",
    # Consumer Staples (30)
    "301010": "Food Wholesalers",
    "301020": "Retail (Grocery and Food)",
    "302010": "Beverage (Alcoholic)",
    "302020": "Beverage (Soft)",
    "302030": "Food Processing",
    "303010": "Household Products",
    "303020": "Tobacco",
    # Health Care (35)
    "351010": "Healthcare Products",
    "351020": "Healthcare Support Services",
    "351030": "Hospitals/Healthcare Facilities",
    "352010": "Drugs (Biotechnology)",
    "352020": "Drugs (Pharmaceutical)",
    # Financials (40) — financial carve-out sample (sector raw beta used)
    "401010": "Banks (Regional)",
    "401020": "Bank (Money Center)",
    "402010": "Brokerage & Investment Banking",
    "402020": "Investments & Asset Management",
    "402030": "Financial Svcs. (Non-bank & Insurance)",
    "403010": "Insurance (General)",
    "403020": "Insurance (Life)",
    "403030": "Insurance (Prop/Cas.)",
    # Information Technology (45)
    "451010": "Software (System & Application)",
    "451020": "Software (Internet)",
    "451030": "Software (Entertainment)",
    "452010": "Computers/Peripherals",
    "452020": "Telecom. Equipment",
    "452030": "Electronics (General)",
    "452040": "Electronics (Consumer & Office)",
    "452050": "Office Equipment & Services",
    "453010": "Semiconductor",
    "453020": "Semiconductor Equip",
    # Communication Services (50)
    "501010": "Telecom. Services",
    "501020": "Telecom (Wireless)",
    "502010": "Advertising",
    "502020": "Broadcasting",
    "502030": "Entertainment",
    "502040": "Cable TV",
    "502050": "Publishing & Newspapers",
    # Utilities (55)
    "551010": "Power",
    "551020": "Utility (General)",
    "551030": "Utility (Water)",
    # Real Estate (60)
    "601010": "R.E.I.T.",
    "601020": "Retail (REITs)",
    "601030": "Real Estate (Development)",
    "601040": "Real Estate (General/Diversified)",
    "601050": "Real Estate (Operations & Services)",
}

#: 7 custom-sector medians used by fallback layer 2 (Appendix B).
CUSTOM_SECTOR_MEDIAN_DEFAULT: Dict[str, float] = {
    "Hardware": 1.15,
    "Software": 1.25,
    "Consumer": 0.85,
    "Industrial": 1.05,
    "Healthcare": 0.95,
    "Energy": 1.10,
    "Financial": 0.90,
}

#: sub_area taxonomy in industry_beta.yaml -> custom sector (fallback key).
SUB_AREA_TO_CUSTOM_SECTOR: Dict[str, str] = {
    "chip_design": "Hardware", "chip_equipment": "Hardware",
    "hardware_oem": "Hardware", "telecom": "Hardware",
    "application_software": "Software", "it_services": "Software",
    "data_services": "Software", "retail": "Consumer",
    "food_beverage": "Consumer", "hospitality": "Consumer",
    "autos": "Consumer", "media_content": "Consumer",
    "aero_defense": "Industrial", "construction": "Industrial",
    "machinery_equipment": "Industrial", "transport": "Industrial",
    "environment": "Industrial", "diversified": "Industrial",
    "materials": "Industrial", "agri": "Industrial",
    "pharma": "Healthcare", "biotech": "Healthcare",
    "healthcare": "Healthcare", "energy": "Energy",
    "financials": "Financial", "insurance": "Financial",
    "utilities": "Energy", "real_estate": "Financial",
    "ad_services": "Software",
}


def _gics_l2_name(code: str) -> str:
    """Human-readable GICS L2 label for a 6-digit code (sector + industry group)."""
    sector = int(code[:2])
    group = int(code[2:4])
    labels = {
        10: "Energy", 15: "Materials", 20: "Industrials",
        25: "Consumer Discretionary", 30: "Consumer Staples",
        35: "Health Care", 40: "Financials", 45: "Information Technology",
        50: "Communication Services", 55: "Utilities", 60: "Real Estate",
    }
    return f"{labels.get(sector, str(sector))} {group:02d}"

# ---------------------------------------------------------------------------
# IPv4 pinning for stern.nyu.edu (intermittent AAAA DNS failures)
# ---------------------------------------------------------------------------
_STERN_HOSTS = ("pages.stern.nyu.edu", "www.stern.nyu.edu", "stern.nyu.edu")


def _pin_ipv4() -> None:
    """Force IPv4-only resolution for stern.nyu.edu hosts."""
    try:
        import urllib3.util.connection
        urllib3.util.connection.HAS_IPV6 = False
    except Exception:
        pass

    import socket
    try:
        _orig = socket.getaddrinfo
        def _patched(*args, **kwargs):
            res = _orig(*args, **kwargs)
            host = kwargs.get("host") or (args[0] if args else None)
            if isinstance(host, str) and any(host.endswith(h) for h in _STERN_HOSTS):
                return [r for r in res if r[0] == socket.AF_INET]
            return res
        socket.getaddrinfo = _patched
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------
def _download(url: str, timeout: int = 90, tries: int = 3) -> bytes:
    """Download URL with IPv4 pin, bounded retry, and exponential backoff."""
    import requests
    _pin_ipv4()
    last_exc = None
    for attempt in range(tries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            last_exc = exc
            logger.warning("Attempt %d/%d failed for %s: %s", attempt + 1, tries, url, exc)
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Failed to download {url} after {tries} attempts: {last_exc}")


def _mirror_path(mirror_dir: Path, url: str, content: bytes) -> Path:
    """Save raw content to mirror directory with timestamp, return path."""
    mirror_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # Derive filename from URL
    basename = url.rsplit("/", 1)[-1].split("?")[0]
    stem, ext = os.path.splitext(basename)
    out = mirror_dir / f"{stem}_{ts}{ext}"
    out.write_bytes(content)
    # Also write a .meta sidecar
    meta = out.with_suffix(out.suffix + ".meta")
    meta.write_text(json.dumps({
        "url": url,
        "fetched_utc": datetime.utcnow().isoformat(),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }, indent=2), encoding="utf-8")
    logger.info("Mirrored %s -> %s (%d bytes)", url, out, len(content))
    return out


def _load_mirror_latest(mirror_dir: Path, url: str) -> Optional[bytes]:
    """Load the most recent mirrored version of a URL, if available."""
    basename = url.rsplit("/", 1)[-1].split("?")[0]
    stem = os.path.splitext(basename)[0]
    candidates = sorted(mirror_dir.glob(f"{stem}_*"), reverse=True)
    for c in candidates:
        if c.suffix == ".meta":
            continue
        if c.exists() and c.stat().st_size > 0:
            logger.info("Using mirror: %s", c)
            return c.read_bytes()
    return None


# ---------------------------------------------------------------------------
# Parsing: Betas
# ---------------------------------------------------------------------------
def _parse_betas_xls(content: bytes) -> List[Dict[str, Any]]:
    """
    Parse betas.xls into a list of ``{industry, unlevered_beta}`` dicts.

    Reads the workbook through **xlrd directly**, not ``pandas.read_excel``:
    this repo pins xlrd 1.2.0, and pandas >= 2.0.1 refuses to drive xlrd < 2.0.1
    for legacy .xls even though xlrd reads the file perfectly well.  Going
    straight to xlrd's API sidesteps that version gate instead of pinning the
    whole repo's xlrd to a version that would break other readers.
    """
    import xlrd

    wb = xlrd.open_workbook(file_contents=content)
    sheet_name = "Industry Averages" if "Industry Averages" in wb.sheet_names() \
        else wb.sheet_names()[0]
    sh = wb.sheet_by_name(sheet_name)

    # Locate the header row, then the unlevered-beta column by name.
    header_row = None
    for i in range(min(30, sh.nrows)):
        vals = [str(sh.cell_value(i, c)).strip() for c in range(sh.ncols)]
        if any(v.lower() == "industry name" for v in vals):
            header_row = i
            headers = vals
            break
    if header_row is None:
        raise ValueError("Header row 'Industry Name' not found in betas.xls")

    ub_col = None
    for ci, h in enumerate(headers):
        if "unlevered" in h.lower():
            ub_col = ci
            break
    if ub_col is None:
        raise ValueError("No unlevered-beta column found in betas.xls header")

    rows: List[Dict[str, Any]] = []
    for r in range(header_row + 1, sh.nrows):
        ind = str(sh.cell_value(r, 0)).strip()
        if not ind or ind.lower().startswith("total"):
            continue
        try:
            beta_val = float(sh.cell_value(r, ub_col))
        except (ValueError, TypeError, IndexError):
            continue
        if beta_val <= 0:
            continue
        rows.append({"industry": ind, "unlevered_beta": round(beta_val, 4)})
    if not rows:
        raise ValueError("betas.xls parsed to zero industries")
    return rows


def _parse_betas_html(content: bytes) -> List[Dict[str, Any]]:
    """Parse Damodaran Betas.html (fallback) into industry beta list."""
    import pandas as pd
    import io
    tables = pd.read_html(io.BytesIO(content))
    if not tables:
        raise ValueError("No table found in Betas.html")
    df = tables[0]
    # Detect header
    for i, row in df.iterrows():
        vals = [str(v).lower() for v in row.values]
        if any("industry" in v for v in vals) and any("beta" in v for v in vals):
            df.columns = row.values
            df = df.iloc[i + 1:].copy()
            break
    ub_col = None
    for c in df.columns:
        if "unlevered" in str(c).lower():
            ub_col = c
            break
    if ub_col is None:
        # try just "beta" column
        for c in df.columns:
            if "beta" in str(c).lower():
                ub_col = c
                break
    if ub_col is None:
        raise ValueError("No beta column found in HTML")
    rows = []
    for _, row in df.iterrows():
        ind = str(row.iloc[0]).strip()
        if not ind or ind.startswith("Total"):
            continue
        try:
            beta_val = float(row[ub_col])
        except (ValueError, TypeError):
            continue
        if beta_val <= 0:
            continue
        rows.append({"industry": ind, "unlevered_beta": round(beta_val, 4)})
    return rows


# ---------------------------------------------------------------------------
# Parsing: Default Spreads / Ratings
# ---------------------------------------------------------------------------
def _parse_ratings_html(content: bytes) -> List[Dict[str, Any]]:
    """Parse Damodaran ratings.htm into list of rating tier dicts.

    The page has a table with columns:
      ICR lower bound | <= ICR upper bound | Rating | Default Spread (%)
    plus sometimes additional columns.
    """
    from bs4 import BeautifulSoup
    import re

    soup = BeautifulSoup(content, "html.parser")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("No <table> found on Damodaran ratings page")

    tiers = []
    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        try:
            lower_text = cells[0].get_text(strip=True).replace(",", "")
            upper_text = cells[1].get_text(strip=True).replace(",", "")
            rating_text = cells[2].get_text(strip=True)
            spread_text = cells[3].get_text(strip=True).rstrip("%")
            lower = float(lower_text)
            upper = float(upper_text)
            spread = float(spread_text) / 100.0
        except (ValueError, IndexError):
            continue
        if not rating_text:
            continue
        tiers.append({
            "icr_threshold": lower,
            "rating": rating_text,
            "spread": round(spread, 6),
        })
    return tiers


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate_betas(rows: List[Dict[str, Any]]) -> List[str]:
    """Validate beta table structure, return list of warnings."""
    warnings = []
    if len(rows) < MIN_BETA_INDUSTRIES:
        warnings.append(f"Only {len(rows)} industries found (expected >= {MIN_BETA_INDUSTRIES})")

    all_names = [r["industry"] for r in rows]
    for req in REQUIRED_INDUSTRIES:
        found = any(req.lower() in n.lower() for n in all_names)
        if not found:
            warnings.append(f"Required industry '{req}' not found")

    for r in rows:
        b = r["unlevered_beta"]
        if b < MIN_BETA or b > MAX_BETA:
            warnings.append(f"Industry '{r['industry']}' has beta {b} outside [{MIN_BETA}, {MAX_BETA}]")

    return warnings


def _validate_ratings(tiers: List[Dict[str, Any]]) -> List[str]:
    """Validate ratings table structure, return list of warnings."""
    warnings = []
    if len(tiers) < MIN_RATING_TIERS:
        warnings.append(f"Only {len(tiers)} rating tiers found (expected >= {MIN_RATING_TIERS})")
    return warnings


# ---------------------------------------------------------------------------
# YAML output
# ---------------------------------------------------------------------------
def _compute_checksum(data: Any) -> str:
    """Compute SHA-256 checksum of JSON-serialized data."""
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _build_yaml_payload(
    betas: List[Dict[str, Any]],
    ratings: List[Dict[str, Any]],
    beta_warnings: List[str],
    rating_warnings: List[str],
    fetch_ts: str,
    version: str,
    existing_sub_areas: Dict[str, str],
    existing_thread_b: Dict[str, Any],
    existing_aliases: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the full YAML payload for industry_beta.yaml."""
    industries = {}
    for b in betas:
        name = b["industry"]
        sub_area = existing_sub_areas.get(name, "unknown")
        industries[name] = {
            "unlevered_beta": b["unlevered_beta"],
            "sub_area": sub_area,
        }

    # Compute checksums
    beta_checksum = _compute_checksum(betas)
    rating_checksum = _compute_checksum(ratings)
    combined_checksum = _compute_checksum({"betas": betas, "ratings": ratings})

    payload = {
        "industries": dict(sorted(industries.items())),
        "sub_area_aliases": existing_aliases.get("sub_area_aliases", {}),
        "industry_aliases": existing_aliases.get("industry_aliases", {}),
        "thread_b": existing_thread_b,
        "default_ratings": {
            "tiers": ratings,
            "source_url": RATINGS_URL,
            "checksum": rating_checksum,
        },
        "update": {
            "source": "Damodaran unlevered beta by industry - live betas.xls + ratings.htm (pages.stern.nyu.edu)",
            "last_updated": fetch_ts,
            "refresh_cadence": "monthly",
            "version": version,
            "beta_checksum": beta_checksum,
            "combined_checksum": combined_checksum,
            "fetch_timestamp_utc": fetch_ts,
            "beta_warnings": beta_warnings,
            "rating_warnings": rating_warnings,
            "candidate_path": "config/industry_beta_candidate.yaml",
            "auto_replace": False,
        },
        "staging": {"candidate": {}},
    }
    return payload


# ---------------------------------------------------------------------------
# Sector beta (Phase 8) — GICS L2 -> Damodaran unlevered beta map
# ---------------------------------------------------------------------------
def build_sector_beta_payload(betas: List[Dict[str, Any]],
                              version: Optional[str] = None,
                              fetch_ts: Optional[str] = None) -> Dict[str, Any]:
    """Build the config/sector_beta.yaml payload.

    Maps the live Damodaran 94-industry unlevered betas onto GICS Level-2
    codes via the static GICS_L2_TO_DAMODARAN table.  Every GICS L2 code in
    the mapping MUST resolve to a fetched industry; a missing industry raises
    (data drift protection).  Two-sample or industry-split candidates that are
    not in the fetched table are skipped and reported via ``unmapped_codes``.

    Returns the full YAML payload dict (sector_beta key at top level).
    """
    beta_by_industry: Dict[str, float] = {b["industry"]: b["unlevered_beta"] for b in betas}
    unmapped: List[Dict[str, str]] = []
    industries: Dict[str, Dict[str, Any]] = {}

    for code, ind_name in sorted(GICS_L2_TO_DAMODARAN.items()):
        beta = beta_by_industry.get(ind_name)
        if beta is None:
            unmapped.append({"gics_l2": code, "industry": ind_name})
            continue
        industries[code] = {
            "gics_l2_name": _gics_l2_name(code),
            "damodaran_industry": ind_name,
            "beta_unlevered": round(beta, 4),
        }

    if unmapped:
        raise RuntimeError(
            f"{len(unmapped)} GICS L2 codes map to industries missing from the "
            f"fetched beta table: {unmapped}. Fix GICS_L2_TO_DAMODARAN or the "
            "fetched table before emitting sector_beta.yaml.")

    payload = {
        "sector_beta": {
            "version": version or _compute_version(),
            "retrieval_timestamp": fetch_ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mapping_version": GICS_L2_BETA_MAPPING_VERSION,
            "sha256": "TBD_AFTER_REFRESH",  # replaced inline before dump
            "schema": "config/sector_beta.yaml (v1) — GICS L2 -> Damodaran 94-industry",
            "industries": industries,
            "fallback": {
                "custom_sector_medians": CUSTOM_SECTOR_MEDIAN_DEFAULT,
                "universe_median": round(
                    float(sorted(b["unlevered_beta"] for b in betas)[len(betas) // 2]), 4),
                "sub_area_to_custom_sector": SUB_AREA_TO_CUSTOM_SECTOR,
            },
        }
    }
    # Compute SHA-256 over the *content* payload minus the sha256 field itself
    # so the hash is stable and verifiable.
    content = json.loads(json.dumps(payload, default=str))
    content["sector_beta"].pop("sha256", None)
    payload["sector_beta"]["sha256"] = _compute_checksum(content["sector_beta"])
    return payload


def write_sector_beta(payload: Dict[str, Any], out_path: str = "config/sector_beta.yaml") -> Path:
    """Write the sector_beta payload (and a .sha256 sidecar) to disk."""
    import yaml
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
    sha = payload["sector_beta"]["sha256"]
    out.with_suffix(out.suffix + ".sha256").write_text(f"{sha}  {out.name}\n", encoding="utf-8")
    logger.info("Wrote %s (sha256 %s)", out, sha[:16])
    return out


# ---------------------------------------------------------------------------
# ICR table cache update
# ---------------------------------------------------------------------------
def _update_icr_cache(ratings: List[Dict[str, Any]], version: str) -> None:
    """Update the default_probability_table JSON cache from fetched ratings."""
    import hashlib as _hl

    # Map rating names to empirical default probabilities
    _DEFAULT_PROBABILITY_MAP = {
        "D2/D":     {"p_default_1yr": 0.2500, "p_default_5yr": 0.7000, "recovery_rate": 0.10},
        "C2/C":     {"p_default_1yr": 0.1200, "p_default_5yr": 0.5000, "recovery_rate": 0.15},
        "Ca2/CC":   {"p_default_1yr": 0.0800, "p_default_5yr": 0.3500, "recovery_rate": 0.22},
        "Caa/CCC":  {"p_default_1yr": 0.0500, "p_default_5yr": 0.2200, "recovery_rate": 0.28},
        "B3/B-":    {"p_default_1yr": 0.0300, "p_default_5yr": 0.1400, "recovery_rate": 0.32},
        "B2/B":     {"p_default_1yr": 0.0180, "p_default_5yr": 0.0900, "recovery_rate": 0.35},
        "B1/B+":    {"p_default_1yr": 0.0120, "p_default_5yr": 0.0600, "recovery_rate": 0.38},
        "Ba2/BB":   {"p_default_1yr": 0.0070, "p_default_5yr": 0.0350, "recovery_rate": 0.42},
        "Ba1/BB+":  {"p_default_1yr": 0.0035, "p_default_5yr": 0.0180, "recovery_rate": 0.45},
        "Baa2/BBB": {"p_default_1yr": 0.0018, "p_default_5yr": 0.0090, "recovery_rate": 0.50},
        "A3/A-":    {"p_default_1yr": 0.0012, "p_default_5yr": 0.0060, "recovery_rate": 0.53},
        "A2/A":     {"p_default_1yr": 0.0008, "p_default_5yr": 0.0040, "recovery_rate": 0.55},
        "A1/A+":    {"p_default_1yr": 0.0005, "p_default_5yr": 0.0025, "recovery_rate": 0.56},
        "Aa2/AA":   {"p_default_1yr": 0.0003, "p_default_5yr": 0.0015, "recovery_rate": 0.58},
        "Aaa/AAA":  {"p_default_1yr": 0.0001, "p_default_5yr": 0.0004, "recovery_rate": 0.60},
    }

    enriched = []
    for t in ratings:
        rating = t["rating"]
        defaults = _DEFAULT_PROBABILITY_MAP.get(rating, {
            "p_default_1yr": 0.05, "p_default_5yr": 0.22, "recovery_rate": 0.30,
        })
        enriched.append({
            "icr_threshold": t["icr_threshold"],
            "rating": rating,
            "spread": t["spread"],
            "p_default_1yr": defaults["p_default_1yr"],
            "p_default_5yr": defaults["p_default_5yr"],
            "recovery_rate": defaults["recovery_rate"],
        })

    payload = {
        "version": version,
        "source_url": RATINGS_URL,
        "data": enriched,
    }
    payload["checksum"] = _compute_checksum(enriched)

    cache_path = ROOT / "data" / f"damodaran_icr_table_v{version}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("Updated ICR cache: %s", cache_path)


# ---------------------------------------------------------------------------
# Load existing sub_areas and thread_b from current YAML
# ---------------------------------------------------------------------------
def _load_existing_config(yaml_path: Path) -> Tuple[Dict[str, str], Dict[str, Any], Dict[str, Any]]:
    """Load existing sub_areas, thread_b, and aliases from current YAML."""
    import yaml

    sub_areas = {}
    thread_b = {}
    aliases = {}
    if yaml_path.exists():
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            for name, info in cfg.get("industries", {}).items():
                if isinstance(info, dict) and "sub_area" in info:
                    sub_areas[name] = info["sub_area"]
            thread_b = cfg.get("thread_b", {})
            aliases = {
                "sub_area_aliases": cfg.get("sub_area_aliases", {}),
                "industry_aliases": cfg.get("industry_aliases", {}),
            }
        except Exception as exc:
            logger.warning("Could not load existing config: %s", exc)
    return sub_areas, thread_b, aliases


# ---------------------------------------------------------------------------
# Version management
# ---------------------------------------------------------------------------
def _compute_version(fetch_date: Optional[date] = None) -> str:
    """Compute version string in YYYY.MM format."""
    d = fetch_date or date.today()
    return f"{d.year}.{d.month:02d}"


def _get_existing_version(yaml_path: Path) -> Optional[str]:
    """Extract existing version from YAML update section."""
    import yaml
    if not yaml_path.exists():
        return None
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("update", {}).get("version")
    except Exception:
        return None


# ===========================================================================
# CEO RULING D3 -- dynamic discovery, version stamping, --check / --apply
# ===========================================================================
@dataclass(frozen=True)
class Dataset:
    """
    One logical Damodaran dataset.

    ``anchor``        a href substring unique to the dataset's row on the index
                      page (``datafile/Betas.html`` etc).
    ``url_substrings`` ordered download-URL substrings.  The first one that the
                      index page actually offers wins; ``fallback_urls`` are
                      used only when the index page yields nothing (offline or
                      Damodaran relaid-out the index).
    ``target_config`` the config whose ``update:`` block records this dataset's
                      version + SHA-256.  This is the "config manifest" that
                      --check compares against.
    """
    key: str
    label: str
    anchor: str
    url_substrings: Tuple[str, ...]
    fallback_urls: Tuple[str, ...]
    target_config: Path
    parse_as: str            # "industry_betas" | "ratings" | "implied_erp" | "country_risk_premium"


DATASETS: Tuple[Dataset, ...] = (
    Dataset(
        key="industry_beta",
        label="Levered and Unlevered Betas by Industry (US)",
        anchor="datafile/Betas.html",
        url_substrings=("pc/datasets/betas.xls", "pc/dataset/betas.xls"),
        fallback_urls=(BETAS_URL, "https://pages.stern.nyu.edu/~adamodar/pc/dataset/Betas.html"),
        target_config=LIVE_INDUSTRY_BETA,
        parse_as="industry_betas",
    ),
    Dataset(
        key="ratings",
        label="Ratings, Spreads and Interest Coverage Ratios",
        anchor="datafile/ratings.htm",
        url_substrings=("datafile/ratings.htm", "pc/datasets/ratings.htm", "pc/ratings.xls"),
        fallback_urls=(RATINGS_URL, "https://pages.stern.nyu.edu/~adamodar/pc/ratings.xls"),
        target_config=LIVE_INDUSTRY_BETA,
        parse_as="ratings",
    ),
    Dataset(
        key="implied_erp",
        label="Implied Equity Risk Premiums - United States",
        anchor="datafile/histimpl.html",
        url_substrings=("pc/datasets/histimpl.xls", "pc/datasets/histimpl.xlsx"),
        fallback_urls=("https://pages.stern.nyu.edu/~adamodar/pc/datasets/histimpl.xls",),
        target_config=LIVE_DAMODARAN_ERP,
        parse_as="implied_erp",
    ),
    Dataset(
        key="country_risk_premiums",
        label="Risk Premiums for Other Markets (ERP breakdowns, current year)",
        anchor="datafile/ctryprem.html",
        url_substrings=("pc/datasets/ctryprem",),
        fallback_urls=(),
        target_config=LIVE_DAMODARAN_ERP,
        parse_as="country_risk_premium",
    ),
)


def _absolute(href: str) -> str:
    if href.startswith("http"):
        return href
    return "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/" + href.lstrip("./")


def _label_sort_key(label: str, url: str) -> Tuple[int, str]:
    """
    Rank two candidate downloads of the SAME dataset: newest vintage first.

    Damodaran's quarterly country-risk-premium workbooks all share the
    ``pc/datasets/ctryprem`` prefix (``ctryprem.xlsx``, ``ctrypremApr26.xlsx``,
    ``ctrypremJuly26.xlsx``), so a plain alphabetical sort would pick the
    OLDEST.  ``rank`` is a preference tier (lower wins); within a tier the
    caller picks the MAXIMUM of the returned key, i.e. the newest vintage.

    Returns ``(rank, sort_key)``:
      tier 0 -- the index page states a date for this link
      tier 1 -- no date, but the filename carries a vintage
      tier 2 -- neither; fall back to the URL for determinism
    """
    label_date = _version_from_page_label(label)
    if label_date:
        for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y"):
            try:
                return (0, datetime.strptime(label_date, fmt).strftime("%Y%m%d"))
            except ValueError:
                continue
        return (0, label_date)
    vintage = _fallback_version(url)
    if vintage:
        return (1, vintage)
    return (2, url)


def _pick_newest(candidates: List[Tuple[str, str]]) -> Tuple[str, str]:
    """Best href/label pair: lowest tier, then highest sort key, then URL."""
    keyed = [(lt, sk, href, txt) for href, txt in candidates
             for lt, sk in [_label_sort_key(txt, href)]]
    best_tier = min(k[0] for k in keyed)
    within = [k for k in keyed if k[0] == best_tier]
    top_key = max(k[1] for k in within)
    within = [k for k in within if k[1] == top_key]
    winner = sorted(within, key=lambda k: k[2])[0]
    return winner[2], winner[3]


def discover_dataset_urls(anchor: str,
                          url_substrings: Tuple[str, ...],
                          fallback_urls: Tuple[str, ...] = ()) -> Tuple[Optional[str], Dict[str, str]]:
    """
    Resolve one dataset's current-year download URL from the index page.

    Resolution order is the dataset's own declared ``url_substrings``
    preference (so e.g. the HTML datafile page can win over a workbook for the
    ratings table, whose HTML carries the same tiers in a shape this repo's
    parser already understands).  Within one substring, the newest vintage the
    index page labels wins.

    Returns ``(url, page_labels)`` where ``page_labels`` maps every href on the
    page to its link text -- the index page labels quarterly rolls
    ("July 1, 2026 update"), which is where the *version* comes from when a
    file itself carries no usable stamp.
    """
    try:
        from bs4 import BeautifulSoup
        content = _download(DATCURRENT_URL, timeout=60)
        soup = BeautifulSoup(content, "html.parser")
    except Exception as exc:
        logger.warning("Index page %s unavailable (%s); using fallbacks",
                       DATCURRENT_URL, type(exc).__name__)
        return (fallback_urls[0] if fallback_urls else None), {}

    page_labels: Dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        page_labels.setdefault(a["href"], " ".join(a.get_text().split()))

    # Pass 1 -- declared preference order.
    for sub in url_substrings:
        matches = [(h, txt) for h, txt in page_labels.items()
                   if sub.lower() in h.lower()]
        if matches:
            href, label = _pick_newest(matches)
            logger.info("Discovered %s -> %s (label: %s)", anchor, href, label)
            return _absolute(href), page_labels

    # Pass 2 -- the dataset's own datafile page, which the index always links.
    page_matches = [(h, txt) for h, txt in page_labels.items()
                    if anchor.lower() in h.lower()]
    if page_matches:
        href, label = page_matches[0]
        logger.info("Discovered %s -> %s (datafile page; label: %s)", anchor, href, label)
        return _absolute(href), page_labels

    if fallback_urls:
        logger.warning("No indexed download for %s; falling back to %s",
                       anchor, fallback_urls[0])
        return fallback_urls[0], page_labels
    return None, page_labels


# --- version extraction -----------------------------------------------------
_EXCEL_EPOCH = datetime(1899, 12, 30)


def _version_from_xls(content: bytes) -> Optional[str]:
    """
    Damodaran stamps every workbook with ``Date updated: <excel serial>`` in
    cell A1/B1 of sheet 0.  Read it directly with xlrd rather than via
    ``pandas.read_excel`` -- this repo pins xlrd 1.2.0, which pandas >=2 refuses
    to drive for legacy .xls (it demands >= 2.0.1), and that refusal has
    nothing to do with whether xlrd can read the file.
    """
    try:
        import xlrd
    except ImportError:
        return None
    try:
        wb = xlrd.open_workbook(file_contents=content)
        # The stamp lives on the DATA sheet ("Industry Averages"), not on sheet
        # 0 (which is usually "Explanations & FAQ"), so scan the first few.
        for si in range(min(4, wb.nsheets)):
            sh = wb.sheet_by_index(si)
            for row in range(0, min(6, sh.nrows)):
                label = str(sh.cell_value(row, 0)).strip().lower()
                if "date updated" not in label and "last updated" not in label:
                    continue
                val = sh.cell_value(row, 1)
                if isinstance(val, (int, float)) and 30000 < float(val) < 60000:
                    return (_EXCEL_EPOCH + timedelta(days=float(val))).strftime("%Y-%m-%d")
                if isinstance(val, str) and re.search(r"\d{4}", val):
                    return val.strip()
    except Exception as exc:
        logger.debug("xls version probe failed: %s", exc)
    return None


def _version_from_html(content: bytes) -> Optional[str]:
    """Look for a date stamp inside an HTML datafile."""
    try:
        from bs4 import BeautifulSoup
        text = " ".join(BeautifulSoup(content, "html.parser").get_text(" ").split())
    except Exception:
        return None
    m = re.search(r"(?:Date updated|Updated|Last updated)[^0-9]{0,40}"
                  r"((?:January|February|March|April|May|June|July|August|September|"
                  r"October|November|December)\s+\d{1,2},?\s+\d{4})", text, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"\b((?:January|February|March|April|May|June|July|August|September|"
                  r"October|November|December)\s+\d{1,2},?\s+\d{4})", text, re.I)
    return m.group(1).strip() if m else None


def _version_from_page_label(label: str) -> Optional[str]:
    m = re.search(r"((?:January|February|March|April|May|June|July|August|September|"
                  r"October|November|December)\s+\d{1,2},?\s+\d{4})", label or "")
    return m.group(1).strip() if m else None


def _version_from_content(content: bytes) -> Optional[str]:
    """Best-effort version from the payload bytes alone."""
    head = content[:4096]
    if b"PK" == content[:2] or b"\xd0\xcf\x11\xe0" == content[:4]:   # xlsx / xls
        v = _version_from_xls(content)
        if v:
            return v
        # xlsx is a zip; try the sharedStrings/docProps date as a fallback
        try:
            import io
            import zipfile
            zf = zipfile.ZipFile(io.BytesIO(content))
            for name in ("docProps/core.xml", "xl/workbook.xml"):
                if name in zf.namelist():
                    m = re.search(rb"(\d{4}-\d{2}-\d{2})", zf.read(name))
                    if m:
                        return m.group(1).decode("ascii")
        except Exception:
            pass
        return None
    return _version_from_html(content)


def _fallback_version(url: str) -> str:
    """
    Derive a version from a dataset filename -- Damodaran embeds the vintage in
    it (``ctrypremJuly26.xlsx``).  Returns ``""`` when the filename says
    nothing useful, so callers can distinguish "no vintage" from a real one.
    """
    base = url.rsplit("/", 1)[-1]
    m = re.search(r"([A-Za-z]{3,9})(\d{2})\.(?:xls|xlsx|csv)$", base)
    if m:
        months = {m_: i + 1 for i, m_ in enumerate(
            ["jan", "feb", "mar", "apr", "may", "jun",
             "jul", "aug", "sep", "oct", "nov", "dec"])}
        mon = months.get(m.group(1).lower()[:3])
        if mon:
            return f"20{m.group(2)}-{mon:02d}"
    m = re.search(r"(20\d{2})", base)
    if m:
        return m.group(1)
    return ""


# --- college audit log ------------------------------------------------------
COLLEGE_LOG_ROOT = ROOT / "logs" / "college"
COLLEGE_APPEND = ROOT / "college_append.jsonl"


def college_log(event: str, **payload) -> str:
    """
    Append a college-audit record.

    Writes to BOTH ``logs/college/<UTC date>/college.jsonl`` and the flat
    ``college_append.jsonl``, in the same schema the 2026-10-06 rulings used::

        {"ts": "...+00:00", "level": "INFO", "logger": "quant_audit",
         "message": "AUDIT_EVENT: <event> | {<json payload>}"}
    """
    ts = datetime.now(timezone.utc).isoformat()
    message = f"AUDIT_EVENT: {event} | {json.dumps(payload, default=str)}"
    record = {"ts": ts, "level": "INFO", "logger": "quant_audit", "message": message}
    line = json.dumps(record)

    day_dir = COLLEGE_LOG_ROOT / datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        day_dir.mkdir(parents=True, exist_ok=True)
        with open(day_dir / "college.jsonl", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:
        logger.warning("Could not append to college day log: %s", exc)
    try:
        with open(COLLEGE_APPEND, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as exc:
        logger.warning("Could not append to college_append.jsonl: %s", exc)

    try:
        from config.logging_config import college_event
        college_event(event, **payload)
    except Exception:
        pass
    return line


# --- ERP parsing ------------------------------------------------------------
def _parse_implied_erp(content: bytes) -> Dict[str, Any]:
    """
    Parse ``histimpl.xls`` (Implied Equity Risk Premiums, US) into
    ``{historical_erp: {year: premium}, period_averages: {...}, default_erp: float}``.

    The live config's ``default_erp`` convention is the trailing-10-year
    average implied premium (the workbook's ``2016-2025`` / ``<Y-9>-<Y>`` row),
    which is what a refresh should re-derive rather than hand-set.
    """
    import xlrd
    wb = xlrd.open_workbook(file_contents=content)
    sh = wb.sheet_by_index(0)

    year_col, premium_col = None, None
    for r in range(sh.nrows):
        vals = [str(sh.cell_value(r, c)).strip().lower() for c in range(min(sh.ncols, 12))]
        if "year" in vals and year_col is None:
            year_col = vals.index("year")
            for ci, v in enumerate(vals):
                if v in ("implied premium", "implied erp", "implied equity risk premium"):
                    premium_col = ci
                    break
            if premium_col is None:
                # First numeric-looking column to the right of Year.
                for ci in range(year_col + 1, min(sh.ncols, 6)):
                    if "riskfree" not in vals[ci] and "treasury" not in vals[ci]:
                        premium_col = ci
                        break
            break

    if year_col is None or premium_col is None:
        raise ValueError("histimpl.xls: could not locate Year + implied premium columns")

    historical: Dict[str, float] = {}
    max_year = 0
    for r in range(sh.nrows):
        y_raw = sh.cell_value(r, year_col)
        if not isinstance(y_raw, (int, float)) or not (1900 <= float(y_raw) <= 2100):
            continue
        if float(y_raw) != int(float(y_raw)):
            continue
        year = int(float(y_raw))
        p_raw = sh.cell_value(r, premium_col)
        try:
            prem = float(p_raw)
        except (TypeError, ValueError):
            continue
        if not (0.0 < prem < 0.5):
            continue
        historical[str(year)] = round(prem, 6)
        max_year = max(max_year, year)

    if not historical:
        raise ValueError("histimpl.xls: no annual implied ERP rows parsed")

    # Trailing period averages block ("1960-2025", "2016-2025", ...).
    period_averages: Dict[str, float] = {}
    for r in range(sh.nrows):
        label = str(sh.cell_value(r, 0)).strip()
        m = re.fullmatch(r"\(?(\d{4})\s*-\s*(\d{4})\)?", label)
        if not m:
            continue
        try:
            period_averages[label.strip("()")] = round(float(sh.cell_value(r, 1)), 6)
        except (TypeError, ValueError):
            continue

    default_erp = period_averages.get(f"{max_year - 9}-{max_year}")
    if default_erp is None:
        recent = [historical[str(y)] for y in range(max_year - 9, max_year + 1)
                  if str(y) in historical]
        default_erp = round(sum(recent) / len(recent), 6) if recent else historical[str(max_year)]

    return {
        "default_erp": default_erp,
        "historical_erp": dict(sorted(historical.items())),
        "period_averages": period_averages,
        "max_year": max_year,
        "years_parsed": len(historical),
    }


def _parse_country_risk_premiums(content: bytes) -> Dict[str, Any]:
    """
    Record-only parse of the current-year ``ctryprem*`` workbook.

    The workbook is an .xlsx; pandas needs openpyxl for that.  This refresh does
    NOT restructure the live ERP config from it -- it records the download's
    provenance (version, SHA-256, row counts by rating tier where cheaply
    available) so the ERP *breakdown* input has a dated, checksummed audit
    trail, which is what ruling D3 asks for.
    """
    import io
    info: Dict[str, Any] = {"tables": {}}
    try:
        import pandas as pd
        sheets = pd.read_excel(io.BytesIO(content), sheet_name=None)
        info["tables"] = {name: int(df.shape[0]) for name, df in sheets.items()}
    except Exception as exc:
        logger.warning("ctryprem: sheet-level parse unavailable (%s); "
                       "recording download provenance only", type(exc).__name__)
        info["tables"] = {}
        info["parse_note"] = f"sheet_parse_unavailable:{type(exc).__name__}"
    return info


def _dataset_version(ds: Dataset, content: bytes, page_label: str, url: str = "") -> str:
    """
    Version precedence, most authoritative first:

    1. **Index-page label** -- the publisher's own statement of the roll
       ("July 1, 2026 update").  Authoritative because a workbook's internal
       stamp can be inherited from a template: ``ctrypremJuly26.xlsx`` still
       carries a 2025-01-08 document property.
    2. **File stamp** -- ``Date updated: <serial>`` in the workbook, or a
       date in the datafile page body.
    3. **Filename vintage** -- ``ctrypremJuly26`` -> 2026-07.
    4. **Run month**, explicitly labelled as having no upstream stamp.
    """
    v = _version_from_page_label(page_label)
    if v:
        return f"{v} (index page)"
    v = _version_from_content(content)
    if v:
        return f"{v} (file)"
    v = _fallback_version(url)
    if v:
        return f"{v} (filename)"
    return f"{datetime.now(timezone.utc).strftime('%Y.%m')} (no stamp)"


def _config_manifest_entry(config_path: Path, ds: Dataset) -> Dict[str, Any]:
    """
    Read the recorded version + checksums for ``ds`` from a config's ``update:``
    block.  This is the "config manifest" --check compares against.

    ``industry_beta.yaml`` records ``beta_checksum`` and ``combined_checksum``;
    ``damodaran_erp.yaml`` has no ``update:`` block yet, so it returns the
    absent markers and --check reports CHANGED with ``first_record=True``.
    """
    import yaml
    out: Dict[str, Any] = {
        "path": str(config_path), "exists": config_path.exists(),
        "recorded_version": None, "recorded_checksum": None,
        "recorded_checksum_kind": None, "last_updated": None,
    }
    if not config_path.exists():
        return out
    try:
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}"
        return out
    upd = cfg.get("update") or {}
    out["recorded_version"] = upd.get("version")
    out["last_updated"] = upd.get("last_updated") or upd.get("fetch_timestamp_utc")
    kind = {"industry_beta": "beta_checksum",
            "ratings": "combined_checksum",
            "implied_erp": "default_erp_checksum",
            "country_risk_premiums": "breakdown_checksum"}.get(ds.key)
    if kind and upd.get(kind):
        out["recorded_checksum"] = upd[kind]
        out["recorded_checksum_kind"] = kind
    elif upd.get("combined_checksum"):
        out["recorded_checksum"] = upd["combined_checksum"]
        out["recorded_checksum_kind"] = "combined_checksum"
    return out


@dataclass
class DatasetReport:
    key: str
    label: str
    url: Optional[str]
    version: Optional[str]
    sha256: Optional[str]
    bytes_len: int
    page_label: str
    target_config: str
    manifest: Dict[str, Any]
    changed: Optional[bool] = None
    status: str = "unknown"
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


def run_check(datasets: Optional[Sequence[Dataset]] = None,
              timeout: int = 90) -> List[DatasetReport]:
    """
    Download every dataset, stamp it, compare against the config manifest.

    **Writes nothing** -- no staging file, no manifest, no config edit.
    Returns one :class:`DatasetReport` per dataset.
    """
    datasets = tuple(datasets or DATASETS)
    reports: List[DatasetReport] = []
    for ds in datasets:
        url, page_labels = discover_dataset_urls(ds.anchor, ds.url_substrings, ds.fallback_urls)
        manifest = _config_manifest_entry(ds.target_config, ds)
        if url is None:
            reports.append(DatasetReport(
                key=ds.key, label=ds.label, url=None, version=None, sha256=None,
                bytes_len=0, page_label="", target_config=str(ds.target_config),
                manifest=manifest, status="SKIPPED",
                note="index page offered no download for this dataset"))
            continue
        label = ""
        for href, txt in page_labels.items():
            if href.rsplit("/", 1)[-1] == url.rsplit("/", 1)[-1]:
                label = txt
                break
        try:
            content = _download(url, timeout=timeout)
        except Exception as exc:
            reports.append(DatasetReport(
                key=ds.key, label=ds.label, url=url, version=None, sha256=None,
                bytes_len=0, page_label=label, target_config=str(ds.target_config),
                manifest=manifest, status="ERROR",
                note=f"download failed: {type(exc).__name__}: {str(exc)[:140]}"))
            continue

        sha = hashlib.sha256(content).hexdigest()
        version = _dataset_version(ds, content, label, url=url)
        rec_ver = manifest.get("recorded_version")
        rec_sha = manifest.get("recorded_checksum")

        if rec_sha is None and rec_ver is None:
            status, changed, note = "CHANGED", True, "no update: block recorded yet (first refresh)"
        elif rec_sha is not None:
            changed = rec_sha != sha
            status = "CHANGED" if changed else "UNCHANGED"
            note = f"checksum {'differs from' if changed else 'matches'} {ds.target_config}"
            if rec_ver and version and rec_ver not in version and str(rec_ver)[:4] not in version:
                note += f"; recorded version {rec_ver} vs fetched {version}"
        else:
            changed = True
            status = "CHANGED"
            note = f"config records version {rec_ver} but no checksum for this dataset"

        reports.append(DatasetReport(
            key=ds.key, label=ds.label, url=url, version=version, sha256=sha,
            bytes_len=len(content), page_label=label,
            target_config=str(ds.target_config), manifest=manifest,
            changed=changed, status=status, note=note))
        logger.info("%-22s %-9s version=%s sha256=%s", ds.key, status, version, sha[:16])
    return reports


def _write_yaml_with_update(payload: Dict[str, Any], out: Path,
                            update_block: Dict[str, Any]) -> Path:
    """
    Write YAML plus a ``.sha256`` sidecar, and stamp the artifact's own
    provenance.

    Two distinct digests, because a file cannot contain its own byte digest:

    ``<name>.sha256``        digest of the file as written on disk
    ``update.content_sha256`` digest of the payload with ``content_sha256``
                             itself removed -- so a reviewer can recompute it
                             from the parsed document and confirm the config was
                             not hand-edited after staging.
    """
    import yaml

    def _dump(obj: Dict[str, Any]) -> str:
        return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False,
                              allow_unicode=True)

    payload.setdefault("update", dict(update_block))
    payload["update"].pop("content_sha256", None)
    payload["update"]["content_sha256"] = _compute_checksum(payload)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_dump(payload), encoding="utf-8")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    out.with_suffix(out.suffix + ".sha256").write_text(
        f"{digest}  {out.name}\n", encoding="utf-8")
    logger.info("Wrote %s (sha256 %s)", out, digest[:16])
    return out


def run_apply(reports: Sequence[DatasetReport],
              out: Optional[str] = None,
              staging_dir: Path = STAGING_DIR) -> Dict[str, Any]:
    """
    Write refreshed YAML staging from the datasets fetched by :func:`run_check`.

    ``out`` is the ONLY way a live config gets overwritten, and only when the
    caller passed it explicitly alongside ``--apply``.  Anything else lands in
    ``staging_dir``.
    """
    import yaml

    by_key = {r.key: r for r in reports}
    fetch_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    written: List[Dict[str, Any]] = []
    errors: List[str] = []

    # ---- industry_beta.yaml (+ sector beta) --------------------------------
    beta_rep, rating_rep = by_key.get("industry_beta"), by_key.get("ratings")
    if beta_rep and beta_rep.status in ("CHANGED", "UNCHANGED"):
        try:
            content = _download(beta_rep.url, timeout=90)
            betas = _parse_betas_xls(content) if beta_rep.url.endswith((".xls", ".xlsx")) \
                else _parse_betas_html(content)
            ratings = []
            if rating_rep and rating_rep.status in ("CHANGED", "UNCHANGED"):
                ratings = _parse_ratings_html(_download(rating_rep.url, timeout=60))
            sub_areas, thread_b, aliases = _load_existing_config(LIVE_INDUSTRY_BETA)
            payload = _build_yaml_payload(
                betas=betas, ratings=ratings, beta_warnings=_validate_betas(betas),
                rating_warnings=_validate_ratings(ratings), fetch_ts=fetch_ts,
                version=_compute_version(), existing_sub_areas=sub_areas,
                existing_thread_b=thread_b, existing_aliases=aliases)
            payload["update"].update({
                "source": f"{DATCURRENT_URL} (discovered)",
                "refresh_cadence": REFRESH_CADENCE,
                "source_urls": {
                    "industry_beta": beta_rep.url,
                    "ratings": rating_rep.url if rating_rep else None,
                    "index": DATCURRENT_URL,
                },
                "dataset_versions": {
                    "industry_beta": beta_rep.version,
                    "ratings": rating_rep.version if rating_rep else None,
                },
                "dataset_sha256": {
                    "industry_beta": beta_rep.sha256,
                    "ratings": rating_rep.sha256 if rating_rep else None,
                },
                "beta_checksum": payload["update"]["beta_checksum"],
                "staged": True,
            })
            target = Path(out) if out else staging_dir / "industry_beta.yaml"
            _write_yaml_with_update(payload, target, payload["update"])
            written.append({"dataset": "industry_beta", "path": str(target),
                            "sha256": payload["update"]["content_sha256"]})
            logger.info("Wrote %s", target)
        except Exception as exc:
            errors.append(f"industry_beta: {type(exc).__name__}: {exc}")
            logger.error("industry_beta staging failed: %s", exc)

    # ---- damodaran_erp.yaml -------------------------------------------------
    erp_rep, crp_rep = by_key.get("implied_erp"), by_key.get("country_risk_premiums")
    if erp_rep and erp_rep.status in ("CHANGED", "UNCHANGED"):
        try:
            erp = _parse_implied_erp(_download(erp_rep.url, timeout=90))
            payload = {
                "default_erp": erp["default_erp"],
                "historical_erp": erp["historical_erp"],
                "period_averages": erp["period_averages"],
                "update": {
                    "source": f"{DATCURRENT_URL} (discovered) -- implied_erp / ctryprem",
                    "version": erp_rep.version,
                    "last_updated": fetch_ts,
                    "fetch_timestamp_utc": fetch_ts,
                    "refresh_cadence": REFRESH_CADENCE,
                    "source_url": erp_rep.url,
                    "index_page": DATCURRENT_URL,
                    "default_erp_checksum": _compute_checksum(
                        {"default_erp": erp["default_erp"],
                         "historical_erp": erp["historical_erp"]}),
                    "dataset_sha256": {
                        "implied_erp": erp_rep.sha256,
                        "country_risk_premiums": crp_rep.sha256 if crp_rep else None,
                    },
                    "dataset_versions": {
                        "implied_erp": erp_rep.version,
                        "country_risk_premiums": crp_rep.version if crp_rep else None,
                    },
                    "erp_breakdown_source_url": crp_rep.url if crp_rep else None,
                    "staged": True,
                },
            }
            if crp_rep and crp_rep.status in ("CHANGED", "UNCHANGED"):
                payload["update"]["erp_breakdown"] = _parse_country_risk_premiums(
                    _download(crp_rep.url, timeout=90))
            target = Path(out) if out else staging_dir / "damodaran_erp.yaml"
            _write_yaml_with_update(payload, target, payload["update"])
            written.append({"dataset": "damodaran_erp", "path": str(target),
                            "sha256": payload["update"]["content_sha256"]})
            logger.info("Wrote %s", target)
        except Exception as exc:
            errors.append(f"damodaran_erp: {type(exc).__name__}: {exc}")
            logger.error("damodaran_erp staging failed: %s", exc)

    # ---- staging manifest (provenance for the apply run itself) ------------
    manifest = {
        "schema_version": "1.0",
        "generated_utc": fetch_ts,
        "generated_by": "scripts/refresh_damodaran.py --apply",
        "refresh_cadence": REFRESH_CADENCE,
        "index_page": DATCURRENT_URL,
        "live_configs_overwritten": [str(Path(out))] if out else [],
        "staging_dir": str(staging_dir),
        "datasets": [
            {k: v for k, v in r.as_dict().items()
             if k in ("key", "label", "url", "version", "sha256", "status", "changed",
                      "target_config", "note")}
            for r in reports
        ],
        "written": written,
        "errors": errors,
    }
    manifest_path = staging_dir / "manifest.json"
    manifest["manifest_sha256"] = _compute_checksum(
        {k: v for k, v in manifest.items() if k != "manifest_sha256"})
    staging_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str),
                             encoding="utf-8")
    manifest_path.with_suffix(".json.sha256").write_text(
        f"{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}  {manifest_path.name}\n",
        encoding="utf-8")
    logger.info("Wrote %s", manifest_path)

    return {"written": written, "errors": errors, "manifest_path": str(manifest_path),
            "manifest_sha256": manifest["manifest_sha256"], "fetch_timestamp": fetch_ts}


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def run_refresh(
    out_path: str = "config/industry_beta.yaml",
    mirror_dir: str = "data/damodaran_mirror",
    dry_run: bool = False,
    use_mirror: bool = False,
) -> Dict[str, Any]:
    """Execute the full refresh pipeline.

    Returns dict with keys: version, beta_count, rating_count, warnings, paths.
    Raises RuntimeError on critical failure (no data at all).
    """
    import yaml

    out = Path(out_path)
    mirror = Path(mirror_dir)
    version = _compute_version()
    fetch_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    all_warnings: List[str] = []
    result: Dict[str, Any] = {"version": version, "fetch_timestamp": fetch_ts}

    # ---- Step 1: Fetch betas ----
    logger.info("=== Step 1: Fetching betas ===")
    betas_content = None
    if use_mirror:
        betas_content = _load_mirror_latest(mirror, BETAS_URL)
        if betas_content is None:
            logger.warning("No mirror available for betas, falling back to live fetch")
            use_mirror = False

    if betas_content is None:
        try:
            betas_content = _download(BETAS_URL)
            if not dry_run:
                _mirror_path(mirror, BETAS_URL, betas_content)
        except Exception as exc:
            # Try HTML fallback
            logger.warning("betas.xls fetch failed (%s), trying HTML fallback", exc)
            betas_html_url = "https://pages.stern.nyu.edu/~adamodar/pc/dataset/Betas.html"
            try:
                betas_content = _download(betas_html_url, timeout=60)
                if not dry_run:
                    _mirror_path(mirror, betas_html_url, betas_content)
                # Mark that we used HTML
                result["betas_source"] = "html"
            except Exception as exc2:
                # Last resort: try mirror
                betas_content = _load_mirror_latest(mirror, BETAS_URL)
                if betas_content is None:
                    raise RuntimeError(
                        f"Cannot fetch betas from any source.\n"
                        f"  Live XLS failed: {exc}\n"
                        f"  Live HTML failed: {exc2}\n"
                        f"  Mirror empty: {mirror}\n"
                        f"Check network connectivity to stern.nyu.edu."
                    )
                result["betas_source"] = "mirror"

    # Parse betas
    try:
        betas = _parse_betas_xls(betas_content)
    except Exception:
        try:
            betas = _parse_betas_html(betas_content)
            result["betas_source"] = result.get("betas_source", "html")
        except Exception as exc:
            raise RuntimeError(f"Failed to parse betas content: {exc}")

    beta_warnings = _validate_betas(betas)
    all_warnings.extend(beta_warnings)
    result["beta_count"] = len(betas)
    result["beta_warnings"] = beta_warnings
    logger.info("Parsed %d industries from beta table", len(betas))

    # ---- Step 2: Fetch ratings/default spreads ----
    logger.info("=== Step 2: Fetching default spreads ===")
    ratings_content = None
    if use_mirror:
        ratings_content = _load_mirror_latest(mirror, RATINGS_URL)
        if ratings_content is None:
            logger.warning("No mirror available for ratings, falling back to live fetch")
            use_mirror = False

    if ratings_content is None:
        try:
            ratings_content = _download(RATINGS_URL, timeout=60)
            if not dry_run:
                _mirror_path(mirror, RATINGS_URL, ratings_content)
        except Exception as exc:
            # Try mirror
            ratings_content = _load_mirror_latest(mirror, RATINGS_URL)
            if ratings_content is None:
                raise RuntimeError(
                    f"Cannot fetch default spreads from any source.\n"
                    f"  Live fetch failed: {exc}\n"
                    f"  Mirror empty: {mirror}\n"
                    f"Check network connectivity to pages.stern.nyu.edu."
                )
            result["ratings_source"] = "mirror"

    # Parse ratings
    try:
        ratings = _parse_ratings_html(ratings_content)
    except Exception as exc:
        raise RuntimeError(f"Failed to parse ratings content: {exc}")

    rating_warnings = _validate_ratings(ratings)
    all_warnings.extend(rating_warnings)
    result["rating_count"] = len(ratings)
    result["rating_warnings"] = rating_warnings
    logger.info("Parsed %d rating tiers from ratings table", len(ratings))

    # ---- Step 3: Load existing config for sub_area preservation ----
    logger.info("=== Step 3: Loading existing config ===")
    existing_sub_areas, existing_thread_b, existing_aliases = _load_existing_config(out)
    logger.info("Loaded %d existing sub_area mappings", len(existing_sub_areas))

    # ---- Step 4: Build YAML payload ----
    logger.info("=== Step 4: Building YAML payload ===")
    payload = _build_yaml_payload(
        betas=betas,
        ratings=ratings,
        beta_warnings=beta_warnings,
        rating_warnings=rating_warnings,
        fetch_ts=fetch_ts,
        version=version,
        existing_sub_areas=existing_sub_areas,
        existing_thread_b=existing_thread_b,
        existing_aliases=existing_aliases,
    )

    # ---- Step 5: Check for changes ----
    existing_version = _get_existing_version(out)
    if existing_version == version:
        logger.info("Version %s already current; checking for data changes...", version)
        if out.exists():
            with open(out, "r", encoding="utf-8") as f:
                old_cfg = yaml.safe_load(f) or {}
            old_checksum = old_cfg.get("update", {}).get("combined_checksum")
            new_checksum = payload["update"]["combined_checksum"]
            if old_checksum == new_checksum:
                result["changed"] = False
                logger.info("No data changes detected (checksums match)")
                if dry_run:
                    return result
                logger.info("Output unchanged; skipping write")
                return result
            else:
                logger.info("Data changed (old=%s, new=%s)", old_checksum[:12], new_checksum[:12])

    result["changed"] = True

    # ---- Step 6: Write output ----
    if dry_run:
        logger.info("DRY RUN: Would write %s", out)
        logger.info("  Version: %s", version)
        logger.info("  Industries: %d", len(betas))
        logger.info("  Rating tiers: %d", len(ratings))
        logger.info("  Warnings: %s", all_warnings)
        return result

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
    logger.info("Wrote %s", out)

    # ---- Step 7: Emit sector_beta.yaml (Phase 8 WACC engine v2 input) ----
    sector_payload = build_sector_beta_payload(betas, version=version, fetch_ts=fetch_ts)
    sector_path = write_sector_beta(sector_payload, "config/sector_beta.yaml")
    result["sector_beta_path"] = str(sector_path)
    result["sector_beta_sha256"] = sector_payload["sector_beta"]["sha256"]

    # ---- Step 8: Update ICR cache ----
    _update_icr_cache(ratings, version)

    result["paths"] = {
        "yaml": str(out),
        "icr_cache": f"data/damodaran_icr_table_v{version}.json",
        "mirror_dir": str(mirror),
    }
    result["warnings"] = all_warnings

    logger.info("=== Refresh complete ===")
    logger.info("  Version: %s", version)
    logger.info("  Industries: %d (warnings: %d)", len(betas), len(beta_warnings))
    logger.info("  Rating tiers: %d (warnings: %d)", len(ratings), len(rating_warnings))
    logger.info("  Checksum: %s", payload["update"]["combined_checksum"][:16])

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_check_report(reports: Sequence[DatasetReport]) -> None:
    print(f"\n{'='*78}")
    print(f"DAMODARAN --check  (read-only; nothing written)")
    print(f"index page : {DATCURRENT_URL}")
    print(f"cadence    : {REFRESH_CADENCE}")
    print(f"{'='*78}")
    header = f"{'dataset':<24} {'status':<10} {'version':<26} {'sha256 (16)':<18}"
    print(header)
    print("-" * 78)
    for r in reports:
        print(f"{r.key:<24} {r.status:<10} {str(r.version or '-'):<26} "
              f"{(r.sha256 or '-')[:16]:<18}")
        print(f"    url      : {r.url}")
        print(f"    manifest : {r.target_config} "
              f"version={r.manifest.get('recorded_version')} "
              f"checksum={(r.manifest.get('recorded_checksum') or '-')[:16]}")
        if r.note:
            print(f"    note     : {r.note}")
        print()
    changed = [r.key for r in reports if r.changed]
    unchanged = [r.key for r in reports if r.changed is False]
    bad = [r.key for r in reports if r.status in ("ERROR", "SKIPPED")]
    print("-" * 78)
    print(f"CHANGED   ({len(changed)}): {', '.join(changed) or '-'}")
    print(f"UNCHANGED ({len(unchanged)}): {', '.join(unchanged) or '-'}")
    print(f"ERROR/SKIP ({len(bad)}): {', '.join(bad) or '-'}")
    print(f"{'='*78}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    ap = argparse.ArgumentParser(
        description=f"Refresh Damodaran beta / ratings / ERP tables "
                    f"(cadence: {REFRESH_CADENCE})")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="Download, stamp, compare against the config manifest, "
                           "report changed/unchanged. Writes nothing.")
    mode.add_argument("--apply", action="store_true",
                      help="Write refreshed YAML staging under config/staging_damodaran/. "
                           "Live configs need an explicit --out.")
    ap.add_argument("--out", default=None,
                    help="With --apply: write to this path instead of staging. "
                         "Naming config/industry_beta.yaml or config/damodaran_erp.yaml "
                         "is the only way a live config is overwritten.")
    ap.add_argument("--staging-dir", default=str(STAGING_DIR),
                    help="Default --apply output directory")
    ap.add_argument("--only", nargs="*", default=None,
                    help="Restrict to these dataset keys: "
                         "industry_beta ratings implied_erp country_risk_premiums")
    ap.add_argument("--timeout", type=int, default=90, help="Per-download timeout (s)")
    ap.add_argument("--mirror-dir", default="data/damodaran_mirror", help="Local mirror directory")
    ap.add_argument("--dry-run", action="store_true", help="Parse and validate only, don't write")
    ap.add_argument("--use-mirror", action="store_true", help="Prefer local mirror over live fetch")
    ap.add_argument("--emit-sector-beta", action="store_true",
                    help="Standalone: build config/sector_beta.yaml from an existing "
                         "industry_beta.yaml (--from) without network fetch")
    ap.add_argument("--from", dest="from_path", default=None,
                    help="industry_beta.yaml to convert when --emit-sector-beta")
    args = ap.parse_args()

    # ---- ruling D3: --check / --apply ------------------------------------
    if args.check or args.apply:
        datasets = DATASETS
        if args.only:
            wanted = {k.strip() for k in args.only}
            unknown = wanted - {d.key for d in DATASETS}
            if unknown:
                print(f"FATAL: unknown dataset key(s): {sorted(unknown)}", file=sys.stderr)
                sys.exit(1)
            datasets = tuple(d for d in DATASETS if d.key in wanted)

        reports = run_check(datasets, timeout=args.timeout)

        if args.check:
            _print_check_report(reports)
            college_log(
                "damodaran_refresh_check",
                mode="check",
                cadence=REFRESH_CADENCE,
                index_page=DATCURRENT_URL,
                versions={r.key: r.version for r in reports},
                sha256={r.key: r.sha256 for r in reports},
                changed=[r.key for r in reports if r.changed],
                unchanged=[r.key for r in reports if r.changed is False],
                errors=[f"{r.key}:{r.note}" for r in reports if r.status in ("ERROR", "SKIPPED")],
                status="COMPLETE" if not any(r.status == "ERROR" for r in reports) else "PARTIAL",
            )
            return

        # --apply
        if args.out and Path(args.out).name in {"industry_beta.yaml", "damodaran_erp.yaml"}:
            logger.warning("--apply --out %s overwrites a LIVE config", args.out)
        result = run_apply(reports, out=args.out, staging_dir=Path(args.staging_dir))
        print(f"\n{'='*78}")
        print(f"DAMODARAN --apply   (cadence: {REFRESH_CADENCE})")
        print(f"{'='*78}")
        for w in result["written"]:
            print(f"  wrote {w['dataset']:<14} -> {w['path']}  sha256={w['sha256'][:16]}")
        if not result["written"]:
            print("  (no staging files written -- every dataset failed to parse)")
        print(f"  manifest        -> {result['manifest_path']}  "
              f"sha256={result['manifest_sha256'][:16]}")
        print(f"  live overwritten: {args.out if args.out else '(none - staging only)'}")
        for e in result["errors"]:
            print(f"  ERROR: {e}")
        print(f"{'='*78}")
        college_log(
            "damodaran_refresh_apply",
            mode="apply",
            cadence=REFRESH_CADENCE,
            index_page=DATCURRENT_URL,
            versions={r.key: r.version for r in reports},
            sha256={r.key: r.sha256 for r in reports},
            written=result["written"],
            errors=result["errors"],
            live_overwritten=[args.out] if args.out else [],
            manifest_path=result["manifest_path"],
            manifest_sha256=result["manifest_sha256"],
            status="COMPLETE" if not result["errors"] else "PARTIAL",
        )
        if result["errors"]:
            sys.exit(1)
        return

    # ---- legacy single-target flow ----------------------------------------
    if args.emit_sector_beta:
        import yaml as _y
        src = Path(args.from_path) if args.from_path else Path("config/industry_beta.yaml")
        if not src.exists():
            print(f"FATAL: source not found: {src}", file=sys.stderr)
            sys.exit(1)
        with open(src, "r", encoding="utf-8") as f:
            cfg = _y.safe_load(f) or {}
        betas = [{"industry": k, "unlevered_beta": v["unlevered_beta"]}
                 for k, v in sorted(cfg.get("industries", {}).items())]
        fetch_ts = cfg.get("update", {}).get("fetch_timestamp_utc")
        version = cfg.get("update", {}).get("version")
        payload = build_sector_beta_payload(betas, version=version, fetch_ts=fetch_ts)
        write_sector_beta(payload, "config/sector_beta.yaml")
        print(f"Sector beta emitted: {len(payload['sector_beta']['industries'])} GICS L2 codes, "
              f"sha256={payload['sector_beta']['sha256'][:16]}...")
        return

    try:
        result = run_refresh(
            out_path=args.out,
            mirror_dir=args.mirror_dir,
            dry_run=args.dry_run,
            use_mirror=args.use_mirror,
        )
        # Print summary for CI
        print(f"\n{'='*60}")
        print(f"Damodaran Refresh Summary")
        print(f"{'='*60}")
        print(f"Version:             {result['version']}")
        print(f"Fetch timestamp:     {result['fetch_timestamp']}")
        print(f"Beta industries:     {result['beta_count']}")
        print(f"Rating tiers:        {result['rating_count']}")
        print(f"Data changed:        {result.get('changed', True)}")
        if result.get("beta_warnings"):
            print(f"Beta warnings:       {len(result['beta_warnings'])}")
            for w in result["beta_warnings"]:
                print(f"  - {w}")
        if result.get("rating_warnings"):
            print(f"Rating warnings:     {len(result['rating_warnings'])}")
            for w in result["rating_warnings"]:
                print(f"  - {w}")
        if result.get("paths"):
            print(f"Paths:")
            for k, v in result["paths"].items():
                print(f"  {k}: {v}")
        print(f"{'='*60}")
    except RuntimeError as exc:
        print(f"\nFATAL: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"\nUNEXPECTED ERROR: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    main()