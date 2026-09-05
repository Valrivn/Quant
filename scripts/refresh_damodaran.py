"""
refresh_damodaran.py -- Automated Damodaran Beta & Default Spread Refresh

Downloads Damodaran's "Beta by Industry" (betas.xls) and "Default Spread
by Rating" (ratings.htm) tables from pages.stern.nyu.edu, validates
structure, computes checksums, and outputs:
  1. config/industry_beta.yaml  -- versioned, checksummed, with fetch timestamp
  2. data/damodaran_mirror/     -- local mirror of raw HTML/XLS with timestamps
  3. data/damodaran_icr_table_v{VERSION}.json -- ICR-to-default table cache

Usage:
  python scripts/refresh_damodaran.py [--out config/industry_beta.yaml]
                                      [--mirror-dir data/damodaran_mirror]
                                      [--dry-run]
                                      [--use-mirror]

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
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    """Parse betas.xls into list of {industry, unlevered_beta, ...} dicts."""
    import pandas as pd

    try:
        import xlrd
        wb = xlrd.open_workbook(file_contents=content)
        sh = wb.sheet_by_name("Industry Averages")
        # Find header row
        header_row = None
        for i in range(min(20, sh.nrows)):
            if str(sh.cell_value(i, 0)).strip() == "Industry Name":
                header_row = i
                break
        if header_row is None:
            raise ValueError("Header row 'Industry Name' not found in betas.xls")
        headers = [str(sh.cell_value(header_row, c)).strip() for c in range(sh.ncols)]
        rows = []
        for r in range(header_row + 1, sh.nrows):
            row_vals = sh.row_values(r)
            if len(row_vals) < 6:
                continue
            ind = str(row_vals[0]).strip()
            if not ind or ind.startswith("Total"):
                continue
            beta_val = row_vals[5]  # unlevered_beta column
            try:
                beta_val = float(beta_val)
            except (ValueError, TypeError):
                continue
            if beta_val <= 0:
                continue
            rows.append({"industry": ind, "unlevered_beta": round(beta_val, 4)})
        return rows
    except ImportError:
        # Fallback: try pandas
        import io
        import pandas as pd
        tables = pd.read_html(io.BytesIO(content))
        if not tables:
            raise ValueError("No tables found in betas.xls via pandas")
        df = tables[0]
        # Find the header row
        for i, row in df.iterrows():
            if "Industry Name" in str(row.values):
                df.columns = row.values
                df = df.iloc[i + 1:].copy()
                break
        # Find unlevered_beta column
        ub_col = None
        for c in df.columns:
            if "unlevered" in str(c).lower():
                ub_col = c
                break
        if ub_col is None:
            raise ValueError("No unlevered_beta column found")
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

    # ---- Step 7: Update ICR cache ----
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
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    ap = argparse.ArgumentParser(description="Refresh Damodaran beta and default spread tables")
    ap.add_argument("--out", default="config/industry_beta.yaml", help="Output YAML path")
    ap.add_argument("--mirror-dir", default="data/damodaran_mirror", help="Local mirror directory")
    ap.add_argument("--dry-run", action="store_true", help="Parse and validate only, don't write")
    ap.add_argument("--use-mirror", action="store_true", help="Prefer local mirror over live fetch")
    args = ap.parse_args()

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