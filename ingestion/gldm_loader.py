"""
gldm_loader.py -- GLDM (SPDR Gold MiniShares) defensive-instrument ingestion
=============================================================================

CEO Ruling D6 (logged logs/college/2026-10-06/college.jsonl,
``ceo_ruling_d6_gldm_sourcing``)::

    "Source GLDM daily adjusted closes mid-2018->present via Yahoo/AlphaVantage
     into data/master_prices/GLDM.parquet; pre-2018 splice from
     GOLDPMGBD228NLBM or IAU net of 0.10% ER."

WHAT THIS DOES
--------------
1. Fetches GLDM **daily adjusted closes** from inception through the backtest
   horizon and writes ``data/master_prices/GLDM.parquet`` in exactly the
   schema the rest of the pipeline expects (``DatetimeIndex`` named ``Date``,
   tz-aware ``America/New_York``, columns ``[Open, High, Low, Close, Volume]``
   -- byte-compatible with the existing ``IAU``/``BIL``/``SHY`` files).
2. Splices a pre-inception gold-proxy history so the series is unbroken for
   regime look-backs and drawdown statistics.
3. Records the splice **transparently** in a sidecar
   (``data/master_prices/GLDM.splice.json``) with SHA-256 digests, plus a
   provenance row in ``data/provenance/manifest.jsonl``.

REUSES THE EXISTING PRICE PIPELINE (ingestion/price_cache.py)
--------------------------------------------------------------
The fetch/cache/provenance contract is inherited, not re-invented:
  * ``ingestion.price_cache.record_provenance`` writes the SHA-256 manifest row
    (same schema, same file).
  * ``ingestion.price_cache.PriceCache._read_master_price`` is the reader
    downstream consumers use; this module writes the file that reader consumes.
  * AlphaVantage is only attempted when ``ALPHAVANTAGE_API_KEY`` is present in
    the environment. **This module never reads, logs or persists a key.**

WHY THE SPLICE IS FLAGGED AS NON-TRADABLE
-----------------------------------------
GLDM began trading on **2018-05-04** (ETF inception; first yfinance print is
2018-06-26). Anything before that in the file is a *gold proxy*, not GLDM. It
exists so that:

  * ``realized_sharpe`` / ``max_drawdown`` windows in the composite have enough
    history, and
  * the ``regime`` look-back is not silently truncated at the instrument's
    inception.

It is therefore spliced **only in the pre-inception region**, is marked with a
dedicated ``Source`` column value, and is never used to satisfy a fill before
2018-05-04. Every consumer can filter on ``Source == "yahoo"`` (or
``Source == "alphavantage"``) to get tradable history only.

SPLICE SOURCE PRECEDENCE (per ruling D6)
-----------------------------------------
1. ``GOLDPMGBD228NLBM`` (FRED, London PM gold fix, USD/oz) -- the preferred
   proxy. Fetched via the ``fredgraph.csv`` endpoint that
   ``ingestion/fred_alfred.py`` already uses; requires no API key.
2. ``IAU`` (iShares Gold Trust) **net of its 0.10% expense ratio**, read from
   the repo-local ``data/master_prices/IAU.parquet`` (already present, history
   back to 2005-01-28).

If neither is reachable the loader falls back to 2 with a warning; if 2 is also
absent the loader writes the tradable-only series and records
``splice_available: false`` in the sidecar. It never fabricates a pre-inception
series.

GEOMETRY OF THE SPLICE
----------------------
Neither proxy can be concatenated onto GLDM directly: IAU trades near $8-$80
and GLDM near $25-$82, and the London fix is quoted in USD/oz (~$400-$3400).
All three are mapped onto GLDM's own scale by an **anchored-ratio join**: on the
overlap window the ratio ``GLDM / proxy`` is computed, and that median ratio is
applied to the proxy's whole history. This preserves GLDM's *observed* price
level (so downstream consumers see real dollar prices and correct
vol/drawdown in GLDM terms) while inheriting the proxy's *shape* pre-inception.

Usage
-----
    python -m ingestion.gldm_loader                     # default horizon, writes parquet
    python -m ingestion.gldm_loader --end 2026-10-02
    python -m ingestion.gldm_loader --dry-run           # fetch + report, no write
    python -m ingestion.gldm_loader --no-splice         # tradable history only
    python -m ingestion.gldm_loader --strict-splice     # fail loudly if no splice source

Cadence: re-run whenever the backtest horizon advances (monthly is ample).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingestion.price_cache import record_provenance  # noqa: E402

logger = logging.getLogger("gldm_loader")

# --- Contract constants ------------------------------------------------------
TICKER = "GLDM"
INCEPTION_DATE = "2018-05-04"          # first day GLDM was tradable (ruling D6)
DEFAULT_START = "2005-01-01"           # proxy history start (IAU inception-ish)
DEFAULT_END = "2026-10-02"             # last completed session in the horizon
MASTER_PRICES_DIR = Path("data/master_prices")
OUTPUT_PARQUET = MASTER_PRICES_DIR / f"{TICKER}.parquet"
SIDECAR_JSON = MASTER_PRICES_DIR / f"{TICKER}.splice.json"

#: FRED London PM gold fix -- ruling D6's preferred pre-2018 proxy.
FRED_GOLD_SERIES = "GOLDPMGBD228NLBM"
FRED_CSV_URL = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={FRED_GOLD_SERIES}"

#: IAU expense ratio charged against the proxy so the splice is not
#: flattered by a fee GLDM's 0.10% ER already accounts for.  Ruling D6 says
#: "IAU net of 0.10% expense ratio".
IAU_ANNUAL_EXPENSE_RATIO = 0.0010
IAU_PARQUET = MASTER_PRICES_DIR / "IAU.parquet"

#: AlphaVantage endpoint (same function id as ingestion/price_cache.py).
AV_URL = ("https://www.alphavantage.co/query"
          "?function=TIME_SERIES_DAILY_ADJUSTED&symbol={symbol}"
          "&outputsize=full&apikey={apikey}")
AV_KEY_ENV = "ALPHAVANTAGE_API_KEY"      # read from env only; never logged

SCHEMA_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
MASTER_PRICES_TZ = "America/New_York"

#: Values written into the ``Source`` column of the output frame.
SRC_YAHOO = "yahoo"
SRC_ALPHAVANTAGE = "alphavantage"
SRC_FRED_SPLICE = "fred_splice:GOLDPMGBD228NLBM"
SRC_IAU_SPLICE = "iau_splice_net_er"
SRC_FETCH_ERROR = "fetch_error"

SPLICE_SOURCES = (SRC_FRED_SPLICE, SRC_IAU_SPLICE)


class GldmIngestionError(RuntimeError):
    """Raised when GLDM history cannot be produced at all."""


# ---------------------------------------------------------------------------
# Provenance helpers
# ---------------------------------------------------------------------------
def sha256_of_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_of_frame(df: pd.DataFrame) -> str:
    """Content hash of a price frame (stable across parquet encoding)."""
    canon = df.copy()
    canon.index = canon.index.tz_convert("UTC").tz_localize(None)
    canon.index = canon.index.strftime("%Y-%m-%d")
    payload = canon.to_csv(float_format="%.10g").encode("utf-8")
    return sha256_of_bytes(payload)


# ---------------------------------------------------------------------------
# Fetch: Yahoo (primary)
# ---------------------------------------------------------------------------
def fetch_yahoo(start: str, end: str, symbol: str = TICKER,
                tries: int = 3, delay_seconds: float = 6.0) -> Tuple[Optional[pd.DataFrame], str]:
    """
    Fetch daily ADJUSTED closes from Yahoo via yfinance.

    Yahoo throttles bursts with an empty frame + a "possibly delisted" log
    line, which is indistinguishable from a genuinely unlisted symbol.  A short
    bounded retry with linear backoff separates the two without hiding a real
    outage.

    Returns ``(frame, source)``. ``frame`` is ``None`` when yfinance is not
    installed or every attempt produced nothing. ``source`` is a short status
    string for the sidecar -- never a key or a credential.
    """
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - environment dependent
        logger.warning("yfinance unavailable (%s); trying AlphaVantage next", exc)
        return None, "yfinance_not_installed"

    hist = None
    for attempt in range(1, tries + 1):
        try:
            hist = yf.Ticker(symbol).history(
                start=start, end=end, interval="1d", auto_adjust=True
            )
        except Exception as exc:
            logger.warning("Yahoo attempt %d/%d raised for %s: %s",
                           attempt, tries, symbol, type(exc).__name__)
            hist = None
        if hist is not None and not hist.empty:
            break
        if attempt < tries:
            backoff = delay_seconds * attempt
            logger.warning("Yahoo attempt %d/%d returned no rows for %s "
                           "(throttle or delisted); retrying in %.0fs",
                           attempt, tries, symbol, backoff)
            time.sleep(backoff)

    if hist is None or hist.empty:
        return None, "yahoo_empty_after_retries"

    df = hist.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index().set_index("Date")
    if df.index.tz is None:
        df.index = df.index.tz_localize(MASTER_PRICES_TZ)
    else:
        df.index = df.index.tz_convert(MASTER_PRICES_TZ)
    df.index.name = "Date"

    for col in SCHEMA_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan
    df = df[SCHEMA_COLUMNS]
    df["Source"] = SRC_YAHOO
    return df[~df.index.duplicated(keep="last")].sort_index(), SRC_YAHOO


# ---------------------------------------------------------------------------
# Fetch: AlphaVantage (fallback, key from env only)
# ---------------------------------------------------------------------------
def fetch_alphavantage(start: str, end: str, symbol: str = TICKER) -> Tuple[Optional[pd.DataFrame], str]:
    """
    Fallback daily-adjusted fetch. Uses ``ALPHAVANTAGE_API_KEY`` from the
    environment ONLY.

    The key is never echoed into logs, the sidecar, or the provenance manifest
    -- only ``alphavantage:key_present`` / ``alphavantage:no_key`` is recorded.
    """
    api_key = os.environ.get(AV_KEY_ENV)
    if not api_key:
        logger.warning(
            "%s not set -- AlphaVantage fallback skipped. Export it to enable "
            "the fallback source.", AV_KEY_ENV)
        return None, "alphavantage:no_key"

    try:
        import requests
        url = AV_URL.format(symbol=symbol, apikey=api_key)
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        payload = json.loads(resp.text)
    except Exception as exc:
        logger.warning("AlphaVantage request failed: %s", type(exc).__name__)
        return None, f"alphavantage:error:{type(exc).__name__}"

    series = payload.get("Time Series (Daily)") or {}
    if not series:
        # Free tier returns an {"Error Message": ...} or {"Note": ...} body.
        note = payload.get("Error Message") or payload.get("Note") or "no_series"
        logger.warning("AlphaVantage returned no series (%s)", str(note)[:80])
        return None, "alphavantage:no_series"

    rows = []
    for d_str, val in series.items():
        if not (start <= d_str <= end):
            continue
        rows.append({
            "Open": float(val.get("1. open", "nan")),
            "High": float(val.get("2. high", "nan")),
            "Low": float(val.get("3. low", "nan")),
            # TIME_SERIES_DAILY_ADJUSTED splits the raw and adjusted close;
            # the adjusted close is what the rest of the pipeline stores.
            "Close": float(val.get("5. adjusted close", val.get("4. close", "nan"))),
            "Volume": float(val.get("6. volume", 0.0)),
            "Source": SRC_ALPHAVANTAGE,
        })
    if not rows:
        return None, "alphavantage:empty_in_window"

    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex(pd.to_datetime(df.index)).tz_localize(MASTER_PRICES_TZ)
    df.index.name = "Date"
    return df.sort_index(), SRC_ALPHAVANTAGE


# ---------------------------------------------------------------------------
# Pre-2018 splice sources
# ---------------------------------------------------------------------------
def load_fred_gold(start: str, end: str, timeout: int = 45) -> Tuple[Optional[pd.Series], str]:
    """
    Preferred pre-2018 proxy: FRED ``GOLDPMGBD228NLBM`` (London PM gold fix,
    USD per troy ounce). Same ``fredgraph.csv`` endpoint ingestion/fred_alfred.py
    already uses, so no API key is required.
    """
    try:
        import requests
        resp = requests.get(FRED_CSV_URL, timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        raw = pd.read_csv(io.StringIO(resp.text))
    except Exception as exc:
        logger.warning("FRED gold proxy unavailable: %s (%s)",
                       type(exc).__name__, str(exc)[:90])
        return None, f"fred_error:{type(exc).__name__}"

    if raw.empty or raw.shape[1] < 2:
        return None, "fred_empty"

    date_col, val_col = raw.columns[0], raw.columns[1]
    s = (pd.to_numeric(raw[val_col], errors="coerce").dropna())
    s.index = pd.to_datetime(raw.loc[s.index, date_col])
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s.loc[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
    if s.empty:
        return None, "fred_empty_in_window"
    s.name = FRED_GOLD_SERIES
    return s, "fred_ok"


def load_iau_net_of_er(start: str, end: str) -> Tuple[Optional[pd.Series], str]:
    """
    Secondary pre-2018 proxy: repo-local ``IAU`` close series **net of the
    0.10% annual expense ratio** (ruling D6), compounded daily.

    The cost is subtracted geometrically, i.e. the series is divided by a
    continuously-compounded expense accumulator, so a multi-year history is not
    understated by a single annual haircut.
    """
    if not IAU_PARQUET.exists():
        return None, f"iau_missing:{IAU_PARQUET}"

    try:
        df = pd.read_parquet(IAU_PARQUET)
    except Exception as exc:
        return None, f"iau_error:{type(exc).__name__}"

    s = pd.to_numeric(df["Close"], errors="coerce").dropna()
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    s.index = idx
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s.loc[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
    if s.empty:
        return None, "iau_empty_in_window"

    years = (s.index - s.index[0]).days / 365.25
    s = s / np.exp(IAU_ANNUAL_EXPENSE_RATIO * years)
    s.name = f"IAU_net_{IAU_ANNUAL_EXPENSE_RATIO:.4f}ER"
    return s, "iau_ok"


# ---------------------------------------------------------------------------
# Splice assembly (anchored-ratio join)
# ---------------------------------------------------------------------------
#: Trading days of GLDM's own history used to anchor the proxy scale.
_OVERLAP_DAYS = 60
_MIN_ANCHOR_DAYS = 20


def _tzna(s: pd.Series) -> pd.Series:
    """Business-day-resampled, tz-naive copy of a daily series."""
    out = s.copy()
    out.index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_convert("UTC").tz_localize(None)
    return out.resample("B").last().dropna()


def _anchored_ratio(target: pd.Series, proxy: pd.Series,
                    min_overlap_days: int = _MIN_ANCHOR_DAYS) -> Tuple[Optional[float], str]:
    """
    Median ``target / proxy`` over the target's FIRST ``_OVERLAP_DAYS``
    trading days (immediately post-inception), which is the window in which
    proxy and GLDM describe the same underlying bullion with the fewest
    intervening structural changes.

    Returns ``(ratio, note)``.  ``ratio`` is ``None`` when the overlap is too
    thin to anchor on -- which would make the splice an unconstrained scale
    guess -- or when the ratio is not finite/positive.
    """
    t = _tzna(target).iloc[:_OVERLAP_DAYS].to_frame("t")
    p = _tzna(proxy).to_frame("p")
    overlap = t.join(p, how="inner").dropna()
    if len(overlap) < min_overlap_days:
        return None, f"overlap_too_thin:{len(overlap)}d"
    denom = overlap["p"].replace(0.0, np.nan).dropna()
    if len(denom) < min_overlap_days:
        return None, f"proxy_zeroes:{len(denom)}"
    ratio = float(np.median(overlap.loc[denom.index, "t"] / denom))
    if not np.isfinite(ratio) or ratio <= 0:
        return None, "ratio_invalid"
    return ratio, f"overlap_{len(overlap)}d_early"


def build_spliced_series(gldm_close: pd.Series,
                         proxy: pd.Series,
                         proxy_source: str,
                         splice_start: str = DEFAULT_START) -> Dict[str, Any]:
    """
    Build the pre-inception GLDM-equivalent series via an anchored-ratio join.

    ``proxy * median(GLDM/proxy)`` preserves GLDM's *observed* price level (so
    downstream consumers see real dollar prices, correct vol and correct
    drawdown in GLDM terms) while inheriting the proxy's *shape* pre-inception.
    """
    ratio, note = _anchored_ratio(gldm_close, proxy)
    if ratio is None:
        return {"ok": False, "reason": note}

    proxy_tzna = _tzna(proxy)
    last_tradable = _tzna(gldm_close).index[0] - pd.Timedelta(days=1)
    spliced = (proxy_tzna * ratio).loc[:last_tradable]
    spliced = spliced.loc[spliced.index >= pd.Timestamp(splice_start)]
    if spliced.empty:
        return {"ok": False, "reason": "proxy_predates_splice_start_only"}

    return {
        "ok": True,
        "series": spliced,
        "anchor_ratio": ratio,
        "anchor_note": note,
        "proxy_source": proxy_source,
        "rows": int(len(spliced)),
        "first": str(spliced.index.min().date()),
        "last": str(spliced.index.max().date()),
    }


# ---------------------------------------------------------------------------
# Output assembly
# ---------------------------------------------------------------------------
def _to_master_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce to the master_prices schema (index/col/dtype parity with IAU)."""
    out = df.copy()
    out.index.name = "Date"
    if out.index.tz is None:
        out.index = out.index.tz_localize(MASTER_PRICES_TZ)
    else:
        out.index = out.index.tz_convert(MASTER_PRICES_TZ)
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]

    for col in SCHEMA_COLUMNS:
        if col not in out.columns:
            out[col] = np.nan
    extra = [c for c in out.columns if c not in SCHEMA_COLUMNS]
    out = out[SCHEMA_COLUMNS + extra]

    for col in ("Open", "High", "Low", "Close"):
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    out["Volume"] = pd.to_numeric(out["Volume"], errors="coerce").fillna(0.0).astype("int64")
    return out


@dataclass
class GldmIngestResult:
    ok: bool
    rows: int
    first: str
    last: str
    tradable_first: str
    tradable_rows: int
    fetch_source: str
    splice_source: Optional[str]
    splice_rows: int
    splice_first: Optional[str]
    splice_last: Optional[str]
    splice_anchor_ratio: Optional[float]
    splice_anchor_note: str
    parquet_path: str
    parquet_sha256: Optional[str]
    sidecar_path: str
    sidecar_sha256: Optional[str]
    warnings: List[str] = field(default_factory=list)
    provenance_sha256: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def ingest(start: str = DEFAULT_START,
           end: str = DEFAULT_END,
           dry_run: bool = False,
           do_splice: bool = True,
           strict_splice: bool = False,
           manifest_path: Path = Path("data/provenance/manifest.jsonl")) -> GldmIngestResult:
    """
    Produce ``data/master_prices/GLDM.parquet`` plus its splice sidecar.

    ``dry_run`` fetches, splices and reports without writing anything.
    """
    warnings: List[str] = []

    # ---- Step 1: tradable history ----------------------------------------
    gldm, source = fetch_yahoo(start, end)
    if gldm is None or gldm.empty:
        warnings.append(f"yahoo_primary_unavailable:{source}")
        gldm, source = fetch_alphavantage(start, end)
    if gldm is None or gldm.empty:
        raise GldmIngestionError(
            "GLDM could not be fetched from any source.\n"
            f"  yahoo: {source}\n"
            f"  alphavantage: requires {AV_KEY_ENV} in the environment\n"
            "Refusing to fabricate a price series -- the zero-synthetic rule in "
            "pre_registration_v7 forbids it."
        )
    inception_bound = pd.Timestamp(INCEPTION_DATE, tz=MASTER_PRICES_TZ) + pd.Timedelta(days=35)
    if source == SRC_YAHOO and gldm.index.min() > inception_bound:
        warnings.append(
            f"first tradable print {gldm.index.min().date()} is well after GLDM "
            f"inception {INCEPTION_DATE}; check the source")

    tradable_first = str(gldm.index.min().date())

    # ---- Step 2: pre-2018 splice ------------------------------------------
    splice_source: Optional[str] = None
    splice_series: Optional[pd.Series] = None
    splice_rows = 0
    splice_first = splice_last = None
    anchor_ratio: Optional[float] = None
    anchor_note = "no_splice_requested"
    gldm_close = gldm["Close"].dropna()

    if do_splice:
        proxy, proxy_status = load_fred_gold(start, end)
        if proxy is not None:
            splice_candidate = build_spliced_series(gldm_close, proxy, SRC_FRED_SPLICE,
                                                    splice_start=start)
        else:
            warnings.append(f"fred_gold_proxy_unavailable:{proxy_status}")
            splice_candidate = {"ok": False, "reason": proxy_status}

        if not splice_candidate.get("ok"):
            iau, iau_status = load_iau_net_of_er(start, end)
            if iau is not None:
                splice_candidate = build_spliced_series(gldm_close, iau, SRC_IAU_SPLICE,
                                                        splice_start=start)
                splice_candidate["fred_status"] = proxy_status
            else:
                warnings.append(f"iau_proxy_unavailable:{iau_status}")
                splice_candidate = {"ok": False, "reason": iau_status}

        if splice_candidate.get("ok"):
            splice_series = splice_candidate["series"]
            splice_source = splice_candidate["proxy_source"]
            splice_rows = splice_candidate["rows"]
            splice_first = splice_candidate["first"]
            splice_last = splice_candidate["last"]
            anchor_ratio = splice_candidate["anchor_ratio"]
            anchor_note = splice_candidate["anchor_note"]
        else:
            anchor_note = f"splice_failed:{splice_candidate.get('reason')}"
            warnings.append(anchor_note)
            if strict_splice:
                raise GldmIngestionError(
                    f"--strict-splice: no usable pre-2018 splice source "
                    f"({anchor_note}). Refusing to write a GLDM series whose "
                    f"history silently starts at inception.")

    # ---- Step 3: assemble --------------------------------------------------
    if splice_series is not None and not splice_series.empty:
        spliced = _to_master_schema(pd.DataFrame(
            {"Open": splice_series, "High": splice_series,
             "Low": splice_series, "Close": splice_series,
             "Volume": 0.0, "Source": splice_source}))
        # The splice is constructed to stop strictly before the first tradable
        # print, so the two frames are disjoint on the index and a vertical
        # concat is the correct (and column-preserving) union.  The guard makes
        # that assumption loud if it ever stops holding.
        overlap_idx = spliced.index.intersection(gldm.index)
        if len(overlap_idx):
            warnings.append(f"splice_index_overlap:{len(overlap_idx)}d_dropped")
        combined = pd.concat([spliced, gldm[~gldm.index.isin(overlap_idx)]], axis=0)
        combined = combined.sort_index()
        combined = combined.loc[(combined.index <= pd.Timestamp(end, tz=MASTER_PRICES_TZ))]
    else:
        combined = _to_master_schema(gldm)

    # OHLCV coherence: a spliced bar carries no intraday range.
    combined["Open"] = combined["Open"].fillna(combined["Close"])
    combined["High"] = combined["High"].fillna(combined["Close"])
    combined["Low"] = combined["Low"].fillna(combined["Close"])
    combined["Volume"] = combined["Volume"].fillna(0).astype("int64")
    combined["Source"] = combined["Source"].ffill().fillna(SRC_FETCH_ERROR)
    combined = combined.dropna(subset=["Close"])
    combined = _to_master_schema(combined)

    content_sha = _sha256_of_frame(combined)
    out_path = OUTPUT_PARQUET
    parquet_sha: Optional[str] = None
    prov_sha: Optional[str] = None

    if not dry_run:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        combined.to_parquet(out_path)
        parquet_sha = sha256_of_bytes(out_path.read_bytes())

        sidecar = {
            "ticker": TICKER,
            "schema_version": "1.0",
            "generated_utc": _utc_now(),
            "generated_by": "ingestion/gldm_loader.py",
            "ruling": "CEO-RULING-D6 (ceo_ruling_d6_gldm_sourcing, 2026-10-06)",
            "inception_date": INCEPTION_DATE,
            "window": {"requested_start": start, "requested_end": end},
            "tradable_history": {
                "source": source,
                "first": tradable_first,
                "rows": int(len(gldm)),
                "note": "Real GLDM prints. Rows with Source == this value are "
                        "tradable; rows with Source in splice_sources are NOT.",
            },
            "pre_2018_splice": {
                "available": splice_source is not None,
                "source": splice_source,
                "proxy_series": (
                    FRED_GOLD_SERIES if splice_source == SRC_FRED_SPLICE
                    else (f"IAU net of {IAU_ANNUAL_EXPENSE_RATIO:.4f} annual expense ratio"
                          if splice_source == SRC_IAU_SPLICE else None)),
                "proxy_source_url": (
                    FRED_CSV_URL if splice_source == SRC_FRED_SPLICE
                    else (str(IAU_PARQUET) if splice_source == SRC_IAU_SPLICE else None)),
                "anchor_ratio": anchor_ratio,
                "anchor_note": anchor_note,
                "method": "anchored-ratio join: median(GLDM/proxy) over the first "
                          f"{_OVERLAP_DAYS} trading days of GLDM history, applied to "
                          "the proxy's full history. Preserves GLDM's observed price "
                          "level; inherits the proxy's pre-inception shape.",
                "rows": splice_rows,
                "first": splice_first,
                "last": splice_last,
                "caution": "PRE-INCEPTION PROXY. NOT TRADABLE GLDM. Excluded from "
                           "any fill/position logic; present only so regime look-back "
                           "and drawdown windows are not truncated at inception.",
            },
            "content_sha256": content_sha,
            "parquet_sha256": parquet_sha,
            "row_count": int(len(combined)),
            "columns": list(combined.columns),
            "source_values": sorted(combined["Source"].unique().tolist()),
            "warnings": warnings,
        }
        SIDECAR_JSON.write_text(json.dumps(sidecar, indent=2, sort_keys=True),
                                encoding="utf-8")
        sidecar_sha = sha256_of_bytes(SIDECAR_JSON.read_bytes())
        logger.info("Wrote %s (%d rows) + %s", out_path, len(combined), SIDECAR_JSON)
    else:
        sidecar_sha = None
        logger.info("[dry-run] would write %s (%d rows)", out_path, len(combined))

    if not dry_run:
        prov_sha = record_provenance(
            out_path,
            source=f"GLDM/{source}" + (f"+splice:{splice_source}" if splice_source else ""),
            row_count=len(combined),
            metadata={
                "inception_date": INCEPTION_DATE,
                "tradable_first": tradable_first,
                "splice_source": splice_source,
                "splice_rows": splice_rows,
                "splice_anchor_ratio": anchor_ratio,
                "content_sha256": content_sha,
                "sidecar": str(SIDECAR_JSON),
                "sidecar_sha256": sidecar_sha,
                "schema_version": "1.0",
                "ruling": "CEO-RULING-D6",
            },
        )

    return GldmIngestResult(
        ok=True,
        rows=int(len(combined)),
        first=str(combined.index.min().date()),
        last=str(combined.index.max().date()),
        tradable_first=tradable_first,
        tradable_rows=int(len(gldm)),
        fetch_source=source,
        splice_source=splice_source,
        splice_rows=splice_rows,
        splice_first=splice_first,
        splice_last=splice_last,
        splice_anchor_ratio=anchor_ratio,
        splice_anchor_note=anchor_note,
        parquet_path=str(out_path),
        parquet_sha256=parquet_sha,
        sidecar_path=str(SIDECAR_JSON),
        sidecar_sha256=sidecar_sha,
        provenance_sha256=prov_sha,
        warnings=warnings,
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Ingest GLDM daily adjusted closes + pre-2018 splice "
                    "(CEO Ruling D6)")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=DEFAULT_END)
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch + splice + report, write nothing")
    ap.add_argument("--no-splice", action="store_true",
                    help="tradable history only (no pre-inception proxy)")
    ap.add_argument("--strict-splice", action="store_true",
                    help="fail loudly if no pre-2018 splice source is usable")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        res = ingest(start=args.start, end=args.end, dry_run=args.dry_run,
                     do_splice=not args.no_splice, strict_splice=args.strict_splice)
    except GldmIngestionError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1

    print("=" * 68)
    print("GLDM INGESTION SUMMARY (CEO Ruling D6)")
    print("=" * 68)
    print(f"fetch source      : {res.fetch_source}")
    print(f"rows written      : {res.rows}  ({res.first} -> {res.last})")
    print(f"tradable rows     : {res.tradable_rows} (first {res.tradable_first})")
    print(f"splice source     : {res.splice_source}")
    print(f"splice rows       : {res.splice_rows} "
          f"({res.splice_first} -> {res.splice_last})")
    print(f"anchor ratio      : {res.splice_anchor_ratio}  [{res.splice_anchor_note}]")
    print(f"parquet sha256    : {res.parquet_sha256}")
    print(f"sidecar           : {res.sidecar_path}")
    print(f"sidecar sha256    : {res.sidecar_sha256}")
    print(f"provenance sha256 : {res.provenance_sha256}")
    if res.warnings:
        print("warnings:")
        for w in res.warnings:
            print(f"  - {w}")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())