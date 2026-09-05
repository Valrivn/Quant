#!/usr/bin/env python
"""Point-in-time credit spread lookup for FRED series (BAA10Y, DGS10, etc.).

Provides ``get_spread_pit(series_id, as_of)`` returning the spread value that
would have been observable on ``as_of`` (latest observation <= as_of). Uses
local FRED cache (``data/fred_cache/*.json``) which stores full history.

This eliminates look-ahead bias in macro regime classification and fee_sim3
macro decisions: the backtest sees only data that was actually available at
each decision date.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "fred_cache"
_CACHE: dict[str, pd.Series] = {}

# Series IDs used by the macro/fee engines
CREDIT_SERIES = {
    "BAA10Y": "Moody's Baa Corporate Bond Spread (10Y)",
    "DGS10": "10-Year Treasury Constant Maturity",
    "GOLDPMGBD228NLBM": "Gold Fixing Price (London PM)",
}


def _load_series(series_id: str) -> pd.Series:
    """Load a FRED series from cache into memory (lazy)."""
    if series_id in _CACHE:
        return _CACHE[series_id]

    cache_path = CACHE_DIR / f"{series_id}.json"
    if not cache_path.exists():
        logger.warning(f"FRED cache missing: {cache_path}")
        _CACHE[series_id] = pd.Series(dtype=float)
        return _CACHE[series_id]

    try:
        data = json.loads(cache_path.read_text())
        obs = data.get("observations", [])
        if not obs:
            _CACHE[series_id] = pd.Series(dtype=float)
            return _CACHE[series_id]

        # Build Series: date -> value
        dates = [datetime.fromisoformat(o["date"].replace("Z", "")).date() for o in obs]
        values = [float(o["value"]) for o in obs]
        s = pd.Series(values, index=pd.to_datetime(dates))
        s = s.sort_index()
        s = s[~s.index.duplicated(keep="first")]  # dedupe
        _CACHE[series_id] = s
        logger.debug(f"Loaded {len(s)} observations for {series_id} from cache")
        return s
    except Exception as e:
        logger.error(f"Failed to load {series_id} cache: {e}")
        _CACHE[series_id] = pd.Series(dtype=float)
        return _CACHE[series_id]


def get_spread_pit(series_id: str, as_of: str | datetime) -> Optional[float]:
    """
    Get the credit spread / rate value observable *on or before* ``as_of``.

    Parameters
    ----------
    series_id : str
        FRED series identifier (e.g., "BAA10Y", "DGS10").
    as_of : str or datetime
        The decision date. Returns the latest observation with date <= as_of.

    Returns
    -------
    float or None
        The spread/rate value in percentage points (e.g., 1.91 = 191 bps),
        or None if no data available before as_of.
    """
    s = _load_series(series_id)
    if s.empty:
        return None

    ts = pd.Timestamp(as_of).normalize()
    # Latest observation <= as_of
    mask = s.index <= ts
    if not mask.any():
        return None
    val = float(s[mask].iloc[-1])
    return val


def get_spread_spread_pit(as_of: str | datetime) -> Optional[float]:
    """
    Convenience: Get the BAA10Y credit spread (Moody's Baa corporate spread
    over 10Y Treasury) as of ``as_of``. Returns spread in percentage points.

    This is the primary signal used by ``macro_state.classify_state``.
    Note: BAA10Y from FRED is already defined as "corporate yield relative to
    10-Year Treasury" — it IS the spread, not the raw corporate yield.
    """
    baa = get_spread_pit("BAA10Y", as_of)
    return baa


def get_dgs10_pit(as_of: str | datetime) -> Optional[float]:
    """Get 10Y Treasury yield as-of date (for gold valuation, etc.)."""
    return get_spread_pit("DGS10", as_of)


def get_gold_fix_pit(as_of: str | datetime) -> Optional[float]:
    """Get gold fixing price as-of date."""
    return get_spread_pit("GOLDPMGBD228NLBM", as_of)


def preload_all() -> None:
    """Warm the cache for all registered series."""
    for sid in CREDIT_SERIES:
        _load_series(sid)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    preload_all()

    # Quick self-test
    test_dates = ["2021-01-04", "2022-01-03", "2023-01-02", "2024-01-02", "2025-01-02"]
    for d in test_dates:
        spread = get_spread_spread_pit(d)
        baa = get_spread_pit("BAA10Y", d)
        dgs = get_spread_pit("DGS10", d)
        print(f"{d}: BAA10Y={baa:.2f} DGS10={dgs:.2f} spread={spread:.2f}%" if spread else f"{d}: N/A")