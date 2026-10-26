"""
canonical_features.py — The ONLY Builder Of The Canonical 6-Feature Matrix

Contract: ``Quantitative.contracts.feature_contract.CANONICAL_FEATURES``

    0  CREDIT_SPREAD_VELOCITY_21D   d(HY OAS)_21d          PRIMARY SORT KEY
    1  YIELD_CURVE_10Y2Y            DGS10 - DGS2
    2  REALIZED_VOLATILITY_21D      21d realized vol of FF5 gross market return
    3  FINANCIAL_STRESS_INDEX       STLFSI2 level
    4  REAL_RATE_10Y                DFII10
    5  UNANTICIPATED_INFLATION      CPI YoY - T10YIE 10Y breakeven

Every column is a real FRED series. There is no placeholder, no proxy, no
``np.random``, and no zero-fill. If the cache cannot support the contract the
builder raises :class:`~Quantitative.data.fred_feed.SyntheticDataViolation` and
the caller aborts — that is the intended behaviour, not a failure to handle.

CESI (Citi Economic Surprise Index) is GONE. It is a proprietary, licensed
index that is not on FRED, so it was filled with ``np.random.normal(0, 20, n)``
in ``regime_shield_v3.py`` and with a market-momentum proxy in
``hmm_regime.py``. Its economic slot — the inflation surprise the market has
not yet priced — is filled by UNANTICIPATED_INFLATION, which is real
``CPIAUCSL`` minus real ``T10YIE``.

PIT policy: monthly series (CPI) are shifted by ``cpi_publication_lag_days``
(45, matching the ALFRED/2-week-release + revision convention used elsewhere in
this repo) so no value is visible before it could have been published.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from Quantitative.contracts.feature_contract import (
    CANONICAL_FEATURES,
    F_INDEX_CREDIT_SPREAD_VELOCITY,
    N_CANONICAL_FEATURES,
    assert_no_synthetic_data,
)
from Quantitative.data.fred_feed import (
    SyntheticDataViolation,
    assert_authentic_pit,
    assert_common_coverage,
    load_fred_series,
    load_market_returns,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CANONICAL_FEATURE_SOURCES",
    "MIN_OBS",
    "CPI_PUBLICATION_LAG_DAYS",
    "unanticipated_inflation",
    "build_canonical_features",
    "zscore_frame",
]

# Business days of common coverage the contract requires before a matrix is
# considered usable. The rolling HMM trains on 3y (~756d) windows, so anything
# under a year is not a regime detector.
MIN_OBS = 252

# CPIAUCSL is published ~14 days after month end and revised monthly.
CPI_PUBLICATION_LAG_DAYS = 45

# Series actually read to satisfy the contract, for the coverage gate.
CANONICAL_FEATURE_SOURCES: Dict[str, List[str]] = {
    "CREDIT_SPREAD_VELOCITY_21D": ["BAMLH0A0HYM2"],
    "YIELD_CURVE_10Y2Y": ["DGS10", "DGS2"],
    "REALIZED_VOLATILITY_21D": ["FF5_Mkt_RF"],
    "FINANCIAL_STRESS_INDEX": ["STLFSI2"],
    "REAL_RATE_10Y": ["DFII10"],
    "UNANTICIPATED_INFLATION": ["CPIAUCSL", "T10YIE"],
}

# FF5_Mkt_RF is a LABEL for the FF5 factors parquet (Mkt-RF + RF), not a FRED
# JSON series, so it is excluded from the JSON coverage gate. It is still
# required, and its absence is still fatal — `load_market_returns` raises.
NON_FRED_JSON_SOURCES: frozenset = frozenset({"FF5_Mkt_RF"})

_ALL_SERIES: List[str] = sorted(
    {s for v in CANONICAL_FEATURE_SOURCES.values() for s in v} - set(NON_FRED_JSON_SOURCES)
)


# ═══════════════════════════════════════════════════════════════════════════
# FEATURES
# ═══════════════════════════════════════════════════════════════════════════

def unanticipated_inflation(
    *,
    cache_dir: Optional[Any] = None,
    lag_days: int = CPI_PUBLICATION_LAG_DAYS,
) -> pd.Series:
    """CPI YoY minus the 10Y breakeven — the inflation the bond market did not price.

    Positive -> realized/incoming inflation running above market-implied
    expectations (unanticipated inflation, historically a risk-off regime).

    Replaces CESI. Uses real ``CPIAUCSL`` and real ``T10YIE`` only.
    """
    cpi = load_fred_series("CPIAUCSL", cache_dir, scale=1.0, required=True)
    t10yie = load_fred_series("T10YIE", cache_dir, scale=1.0, required=True)

    # CPI is monthly; YoY needs 12 observations of history.
    cpi_yoy = (cpi / cpi.shift(12) - 1.0) * 100.0
    cpi_yoy.name = "CPI_YOY"
    # PIT: only visible after the release + revision window.
    cpi_yoy = cpi_yoy.shift(freq=pd.tseries.frequencies.to_offset(f"{int(lag_days)}D"))

    out = (cpi_yoy - t10yie).dropna()
    out.name = "UNANTICIPATED_INFLATION"
    return out


def build_canonical_features(
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
    frequencies: Optional[Sequence[str]] = None,
    cache_dir: Optional[Any] = None,
    config: Optional[Mapping[str, Any]] = None,
    zscore: bool = False,
    min_obs: int = MIN_OBS,
    context: str = "canonical_features",
) -> pd.DataFrame:
    """Build the canonical 6-feature matrix from real FRED data.

    Args:
        start/end: optional ISO bounds applied AFTER assembly.
        frequencies: business-day / daily frequencies to reindex onto. Defaults
            to every calendar day, because the monthly CPI leg is monthly and a
            business-day reindex would punch holes into it.
        cache_dir: FRED cache override (defaults to the repo ``data/fred_cache``).
        config: pre-registration mapping. Its ``synthetic_fallback`` flag is
            read by the guard; anything other than an explicit opt-in aborts.
        zscore: z-score each column on the assembled window. The HMM does its
            own training-window standardization; this is for diagnostics only.
        min_obs: minimum rows required after trimming.

    Returns:
        DataFrame indexed by date with exactly ``CANONICAL_FEATURES`` columns in
        canonical order.

    Raises:
        SyntheticDataViolation: if the cache cannot support the contract.
    """
    freqs = tuple(frequencies) if frequencies else ("D",)

    # ---- 0. Coverage gate: refuse to assemble a partial contract -----------
    lo, hi = assert_common_coverage(
        _ALL_SERIES, cache_dir=cache_dir, min_obs=min_obs, context=context
    )
    if start is not None:
        lo = max(lo, pd.Timestamp(start))
    if end is not None:
        hi = min(hi, pd.Timestamp(end))
    if lo > hi:
        raise SyntheticDataViolation(
            f"FATAL: Missing authentic PIT data ({context}) — requested window "
            f"{start}..{end} does not intersect the cached common window "
            f"{_ALL_SERIES}. Aborting per zero-synthetic policy."
        )
    index = pd.date_range(lo, hi, freq=freqs[0])
    for extra in freqs[1:]:
        index = index.union(pd.date_range(lo, hi, freq=extra))
    index = index.sort_values()
    logger.info(
        "Canonical feature build (%s): %s .. %s over %d series",
        context, lo.date(), hi.date(), len(_ALL_SERIES),
    )

    # ---- 1. Legs ----------------------------------------------------------
    # Legs are computed over each series' FULL cached history, not over the
    # pre-intersected window, because two features are differences and need
    # warm-up history that may sit before the intersection:
    #   CREDIT_SPREAD_VELOCITY_21D needs 21 prior business days
    #   UNANTICIPATED_INFLATION needs 12 prior months of CPI
    # Warm-up rows are dropped in step 3, which is coverage-honest: the first
    # row of the contract is the first date on which every leg is computable.
    hy_oas = load_fred_series("BAMLH0A0HYM2", cache_dir, scale=1.0, required=True) / 100.0
    dgs10 = load_fred_series("DGS10", cache_dir, required=True)
    dgs2 = load_fred_series("DGS2", cache_dir, required=True)
    stlfsi2 = load_fred_series("STLFSI2", cache_dir, required=True)
    dfii10 = load_fred_series("DFII10", cache_dir, required=True)
    mkt_ret = load_market_returns(cache_dir)
    unant_infl = unanticipated_inflation(cache_dir=cache_dir)

    # CREDIT_SPREAD_VELOCITY_21D — 21 business-day change in HY OAS (pp).
    credit_vel = (hy_oas - hy_oas.shift(21)).rename("CREDIT_SPREAD_VELOCITY_21D")

    # YIELD_CURVE_10Y2Y — in percentage points.
    curve = (dgs10 - dgs2).rename("YIELD_CURVE_10Y2Y")

    # REALIZED_VOLATILITY_21D — annualized, as a fraction.
    vol = (
        mkt_ret.rolling(21, min_periods=10).std() * np.sqrt(252.0)
    ).rename("REALIZED_VOLATILITY_21D")

    frame = pd.concat(
        [
            credit_vel,
            curve,
            vol,
            stlfsi2.rename("FINANCIAL_STRESS_INDEX"),
            dfii10.rename("REAL_RATE_10Y"),
            unant_infl,
        ],
        axis=1,
    )
    # Reindex onto the target calendar. Daily FRED legs are reindexed with
    # as-of matching so a release date maps to its observation date, then
    # forward-filled; the monthly CPI leg is left on its own stamps and
    # forward-filled onto the daily grid.
    frame = frame.reindex(frame.index.union(index)).sort_index()
    frame = frame.reindex(index)

    # ---- 2. Fill only within each series' own observed history -------------
    # As-of merging (ffill, plus a bfill bounded to the leading gap) closes
    # weekends and release-date gaps. It can never invent observations outside
    # the cached range, so the coverage gate above still holds. Bfill is bounded
    # to the leading gap only, where every later value is a real publication.
    for col in CANONICAL_FEATURES:
        s = frame[col]
        observed = s.dropna()
        if observed.empty:
            raise SyntheticDataViolation(
                f"FATAL: Missing authentic PIT data for {col} ({context}) — series has no "
                f"usable observations in {lo.date()}..{hi.date()}. Aborting per "
                f"zero-synthetic policy."
            )
        # Bound the as-of fill to THIS series' own observed history. Note this
        # masks on the INDEX — clipping the Series values against index bounds
        # would compare floats to Timestamps and is a silent type error.
        filled = s.ffill().bfill()
        in_range = (filled.index >= observed.index[0]) & (filled.index <= observed.index[-1])
        frame[col] = filled.where(in_range)

    frame = frame.loc[lo:hi, list(CANONICAL_FEATURES)]

    # ---- 3. Drop the warm-up head ------------------------------------------
    # Rows before the first date on which EVERY leg is computable are removed.
    # These are the first 21 business days (credit velocity) and the first 12
    # CPI months (YoY), which are undefined by construction, not missing data.
    # Trimming is the honest treatment: they are excluded from the frame rather
    # than being filled with a substitute value. Any interior gap that survives
    # this step is genuine missing data and is caught by the guard below.
    usable = frame.dropna(how="any")
    if usable.empty:
        raise SyntheticDataViolation(
            f"FATAL: Missing authentic PIT data ({context}) — no date in "
            f"{lo.date()}..{hi.date()} on which all {N_CANONICAL_FEATURES} canonical "
            f"features are computable. Aborting per zero-synthetic policy."
        )
    dropped = len(frame) - len(usable)
    if dropped:
        logger.info(
            "Canonical build (%s): dropped %d warm-up row(s) before %s "
            "(credit velocity 21d / CPI YoY 12m)",
            context, dropped, usable.index[0].date(),
        )
    frame = usable

    # ---- 4. Hard guards ---------------------------------------------------
    # MUST run before z-scoring: a zero-std or all-zero column would otherwise
    # be laundered into a constant 0.0 that the post-hoc check cannot see.
    assert_authentic_pit(frame, list(CANONICAL_FEATURES), config, context=context)

    if len(frame) < min_obs:
        raise SyntheticDataViolation(
            f"FATAL: Missing authentic PIT data ({context}) — assembled only "
            f"{len(frame)} rows over {lo.date()}..{hi.date()}, below the "
            f"{min_obs}-row minimum. Aborting per zero-synthetic policy."
        )

    if zscore:
        frame = zscore_frame(frame)

    # Defence in depth: the contract's own validator re-checks after scaling.
    assert_no_synthetic_data(frame, list(CANONICAL_FEATURES))
    return frame


def zscore_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Z-score each column on the provided window. Diagnostics only."""
    out = frame.copy()
    for col in out.columns:
        sd = out[col].std()
        out[col] = (out[col] - out[col].mean()) / (sd if sd and sd > 0 else 1.0)
    return out
