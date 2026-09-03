"""
default_probability_table.py — Damodaran ICR-to-Default Probability Mapping

Maps a company's Interest Coverage Ratio (ICR) to a synthetic credit rating
using Damodaran's published mapping, then derives the empirical 1-year
probability of default (p_default) for that rating.

These probabilities serve as the canonical input for the Bernoulli shock
filter, enabling a data-driven catastrophe probability rather than
hardcoded estimates.

Supports auto-fetching the latest table from Damodaran's website with
local caching and version tracking. Falls back to hardcoded values if
the network fetch fails.

References:
  - Aswath Damodaran, "Measuring Value in the Face of Uncertainty"
  - Damodaran's ICR-to-Synthetic-Rating table (spread_table.xls)
  - Moody's/S&P historical default rate studies (1920-2023)
"""

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants — versioning & source
# ---------------------------------------------------------------------------
TABLE_VERSION = "2024.1"
TABLE_SOURCE_URL = (
    "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/ratings.htm"
)

# Directory for cached table files (relative to repo root)
_CACHE_DIR = Path("data")


@dataclass(frozen=True)
class RatingTier:
    """A single credit rating tier with its ICR threshold and default stats."""
    icr_threshold: float
    rating: str
    spread: float
    p_default_1yr: float
    p_default_5yr: float
    recovery_rate: float


# ---------------------------------------------------------------------------
# Hardcoded fallback table (backward-compatible)
#
# p_default_1yr: Empirical 1-year probability of default for this rating.
#   Source: Moody's Annual Default Study, S&P Global Ratings Direct,
#   and Damodaran's published tables. These are long-run averages across
#   economic cycles.
#
# p_default_5yr: Empirical cumulative 5-year probability of default.
#
# recovery_rate: Historical average recovery rate for this rating tier
#   in the event of default (used for LGD calculations).
# ---------------------------------------------------------------------------

RATING_TABLE: list[RatingTier] = [
    RatingTier(icr_threshold=12.5, rating="AAA", spread=0.0063, p_default_1yr=0.0001, p_default_5yr=0.0004, recovery_rate=0.60),
    RatingTier(icr_threshold=9.5,  rating="AA",  spread=0.0075, p_default_1yr=0.0003, p_default_5yr=0.0015, recovery_rate=0.58),
    RatingTier(icr_threshold=7.5,  rating="A+",  spread=0.0090, p_default_1yr=0.0005, p_default_5yr=0.0025, recovery_rate=0.56),
    RatingTier(icr_threshold=6.0,  rating="A",   spread=0.0105, p_default_1yr=0.0008, p_default_5yr=0.0040, recovery_rate=0.55),
    RatingTier(icr_threshold=4.5,  rating="A-",  spread=0.0120, p_default_1yr=0.0012, p_default_5yr=0.0060, recovery_rate=0.53),
    RatingTier(icr_threshold=4.0,  rating="BBB", spread=0.0150, p_default_1yr=0.0018, p_default_5yr=0.0090, recovery_rate=0.50),
    RatingTier(icr_threshold=3.5,  rating="BB+", spread=0.0200, p_default_1yr=0.0035, p_default_5yr=0.0180, recovery_rate=0.45),
    RatingTier(icr_threshold=3.0,  rating="BB",  spread=0.0250, p_default_1yr=0.0070, p_default_5yr=0.0350, recovery_rate=0.42),
    RatingTier(icr_threshold=2.5,  rating="B+",  spread=0.0325, p_default_1yr=0.0120, p_default_5yr=0.0600, recovery_rate=0.38),
    RatingTier(icr_threshold=2.0,  rating="B",   spread=0.0400, p_default_1yr=0.0180, p_default_5yr=0.0900, recovery_rate=0.35),
    RatingTier(icr_threshold=1.5,  rating="B-",  spread=0.0525, p_default_1yr=0.0300, p_default_5yr=0.1400, recovery_rate=0.32),
    RatingTier(icr_threshold=1.0,  rating="CCC", spread=0.0650, p_default_1yr=0.0500, p_default_5yr=0.2200, recovery_rate=0.28),
    RatingTier(icr_threshold=0.5,  rating="CC",  spread=0.0850, p_default_1yr=0.0800, p_default_5yr=0.3500, recovery_rate=0.22),
    RatingTier(icr_threshold=0.0,  rating="C",   spread=0.1000, p_default_1yr=0.1200, p_default_5yr=0.5000, recovery_rate=0.15),
]

# Distressed default tier (ICR <= 0)
DISTRESSED_TIER = RatingTier(
    icr_threshold=-1.0, rating="D", spread=0.1200,
    p_default_1yr=0.2500, p_default_5yr=0.7000, recovery_rate=0.10,
)

# ---------------------------------------------------------------------------
# Empirical default probability mapping (Moody's/S&P long-run averages)
#
# Damodaran's page provides only ICR thresholds, ratings, and spreads.
# Default probabilities and recovery rates come from historical studies:
#   - Moody's Annual Default Study (1920-2023)
#   - S&P Global Ratings Direct
#   - Damodaran's published tables
# ---------------------------------------------------------------------------

_DEFAULT_PROBABILITY_MAP: Dict[str, Dict[str, float]] = {
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

# Module-level loaded table — starts as RATING_TABLE (hardcoded fallback).
# Call load_or_fetch_table() to replace with fetched data.
_loaded_table: list[RatingTier] = list(RATING_TABLE)


# ---------------------------------------------------------------------------
# Table fetching & caching
# ---------------------------------------------------------------------------

def fetch_damodaran_table() -> list[RatingTier]:
    """
    Download and parse Damodaran's ICR-to-Synthetic-Rating table.

    Fetches the HTML table from Damodaran's NYU Stern page, extracts
    ICR thresholds, ratings, and spreads, then enriches each tier with
    empirical default probabilities and recovery rates from historical data.

    Returns:
        List of RatingTier dataclasses sorted by ICR threshold (descending).

    Raises:
        RuntimeError: If the HTTP request fails or parsing produces no data.
    """
    import requests
    from bs4 import BeautifulSoup

    logger.info("Fetching Damodaran ICR table from %s", TABLE_SOURCE_URL)

    resp = requests.get(TABLE_SOURCE_URL, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("No <table> found on Damodaran ratings page")

    tiers: list[RatingTier] = []
    rows = table.find_all("tr")

    for row in rows:
        cells = row.find_all("td")
        # Non-financial ICR table has 4 data columns:
        #   > ICR lower bound | ≤ to upper bound | Rating | Spread
        # Financial table has 4 more after a blank column.
        # We skip header rows and non-data rows.
        if len(cells) < 4:
            continue

        try:
            lower_bound = float(cells[0].get_text(strip=True).replace(",", ""))
            upper_bound = float(cells[1].get_text(strip=True).replace(",", ""))
            rating_text = cells[2].get_text(strip=True)
            spread_text = cells[3].get_text(strip=True).rstrip("%")
            spread = float(spread_text) / 100.0
        except (ValueError, IndexError):
            continue

        # Enrich with empirical default probability and recovery rate
        defaults = _DEFAULT_PROBABILITY_MAP.get(rating_text, {
            "p_default_1yr": 0.05,
            "p_default_5yr": 0.22,
            "recovery_rate": 0.30,
        })

        tiers.append(RatingTier(
            icr_threshold=lower_bound,
            rating=rating_text,
            spread=spread,
            p_default_1yr=defaults["p_default_1yr"],
            p_default_5yr=defaults["p_default_5yr"],
            recovery_rate=defaults["recovery_rate"],
        ))

    if not tiers:
        raise RuntimeError("Failed to parse any rating tiers from Damodaran page")

    # Sort descending by ICR threshold (highest first)
    tiers.sort(key=lambda t: t.icr_threshold, reverse=True)

    logger.info("Parsed %d rating tiers from Damodaran table", len(tiers))
    return tiers


def _cache_path(version: str) -> Path:
    """Return the cache file path for a given table version."""
    return _CACHE_DIR / f"damodaran_icr_table_v{version}.json"


def _compute_checksum(data: list[RatingTier]) -> str:
    """Compute a SHA-256 checksum of the serialized table data."""
    serialized = json.dumps([asdict(t) for t in data], sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_or_fetch_table(force_refresh: bool = False) -> list[RatingTier]:
    """
    Load the Damodaran ICR table, using cache when available.

    Priority:
      1. Local cache file (validated by version + checksum)
      2. Fresh fetch from Damodaran's site (saves to cache)
      3. Hardcoded RATING_TABLE fallback (no network required)

    After loading, updates the module-level _loaded_table so that
    lookup_rating_tier() and other functions use the loaded data.

    Args:
        force_refresh: If True, bypass cache and fetch fresh data.

    Returns:
        List of RatingTier dataclasses (always returns something).
    """
    global _loaded_table

    if not force_refresh:
        cache = _cache_path(TABLE_VERSION)
        if cache.exists():
            try:
                raw = json.loads(cache.read_text(encoding="utf-8"))
                if raw.get("version") == TABLE_VERSION:
                    tiers = [RatingTier(**t) for t in raw["data"]]
                    checksum = _compute_checksum(tiers)
                    if checksum == raw.get("checksum"):
                        logger.info(
                            "Loaded %d tiers from cache (v%s, checksum ok)",
                            len(tiers), TABLE_VERSION,
                        )
                        _loaded_table = tiers
                        return _loaded_table
                    else:
                        logger.warning(
                            "Cache checksum mismatch (expected %s, got %s); "
                            "re-fetching",
                            raw.get("checksum"), checksum,
                        )
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning("Cache file corrupt (%s); re-fetching", exc)

    # Try fetching fresh data
    try:
        tiers = fetch_damodaran_table()
        _loaded_table = tiers

        # Persist to cache
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache = _cache_path(TABLE_VERSION)
        payload = {
            "version": TABLE_VERSION,
            "checksum": _compute_checksum(tiers),
            "source_url": TABLE_SOURCE_URL,
            "data": [asdict(t) for t in tiers],
        }
        cache.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        logger.info("Saved fetched table to %s", cache)
        return _loaded_table

    except Exception as exc:
        logger.warning(
            "Failed to fetch Damodaran table (%s); using hardcoded fallback",
            exc,
        )
        _loaded_table = list(RATING_TABLE)
        return _loaded_table


def lookup_rating_tier(icr: float) -> RatingTier:
    """
    Look up the synthetic credit rating tier for a given ICR.

    Uses the loaded table (fetched or hardcoded) sorted by ICR threshold
    descending. Returns the first tier where icr >= threshold.
    """
    if icr <= 0:
        return DISTRESSED_TIER
    for tier in _loaded_table:
        if icr >= tier.icr_threshold:
            return tier
    return DISTRESSED_TIER


def get_default_probability(icr: float, horizon: int = 1) -> float:
    """
    Get the empirical probability of default for a given ICR.

    Args:
        icr: Interest Coverage Ratio
        horizon: 1 for 1-year probability, 5 for 5-year cumulative

    Returns:
        Probability of default in [0.0, 1.0]
    """
    tier = lookup_rating_tier(icr)
    if horizon <= 1:
        return tier.p_default_1yr
    elif horizon <= 5:
        return tier.p_default_5yr
    else:
        # Extrapolate conservatively (cap at 5-year value)
        return tier.p_default_5yr


def get_synthetic_rating(icr: float) -> str:
    """Return the synthetic credit rating string for a given ICR."""
    return lookup_rating_tier(icr).rating


def get_credit_spread(icr: float) -> float:
    """Return the credit spread above risk-free rate for a given ICR."""
    return lookup_rating_tier(icr).spread


def get_recovery_rate(icr: float) -> float:
    """Return the historical recovery rate for a given ICR's rating tier."""
    return lookup_rating_tier(icr).recovery_rate


def compute_shock_penalty_multiplier(
    icr: float,
    shock_severity: float = 1.0,
) -> float:
    """
    Compute the FCFE penalty multiplier when a Bernoulli shock fires.

    Uses the rating tier's default probability and recovery rate to
    estimate the expected loss given default (LGD):
        LGD = p_default * (1 - recovery_rate)

    The penalty multiplier scales the FCFE downward:
        penalty = 1.0 - (LGD * shock_severity)

    For a BB+ rated company (p_default=1.8%, recovery=45%):
        LGD = 0.018 * 0.55 = 0.0099 -> penalty ~ 0.99 (mild)
    For a CCC rated company (p_default=5.0%, recovery=28%):
        LGD = 0.050 * 0.72 = 0.036 -> penalty ~ 0.964

    When shock_severity > 1.0 (e.g., for concentrated supply chains),
    the penalty amplifies proportionally.
    """
    tier = lookup_rating_tier(icr)
    lgd = tier.p_default_1yr * (1.0 - tier.recovery_rate)
    raw_penalty = 1.0 - (lgd * shock_severity)
    return max(0.0, min(1.0, raw_penalty))


def build_default_probability_map() -> Dict[str, float]:
    """
    Build a lookup dictionary mapping synthetic rating -> p_default_1yr.
    Useful for bulk lookups and caching.
    """
    return {tier.rating: tier.p_default_1yr for tier in _loaded_table}
