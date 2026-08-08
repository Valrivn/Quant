"""Real data wiring for the discovery gates (B-20260807-002, lane 1).

Builds the names frame consumed by ``valuation_alpha.discovery_screen`` and the
moat signals consumed by ``AlternativeStrategyPipeline.run``. Live-first with
cached fallback, per-row provenance, honest NaN when a value is unknown.
"""

import json
import math
import os
import statistics
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from db.connection import get_connection
from valuation_alpha.alpha import ff5_residual_alpha
from valuation_alpha.datastore.factors import fetch_ff5_factors
from valuation_alpha.datastore.prices import fetch_prices
from valuation_alpha.datastore.xbrl_financials import fetch_companyfacts
from valuation_alpha.universe.cik_resolver import resolve_cik

_DEFAULT_END = "2026-07-31"
_DEFAULT_START = "2023-01-01"
_ALPHA_WINDOW_DAYS = 756
ENABLE_LIVE_XBRL = False
_RESULTS_PATH = os.path.join("center", "valuation_alpha", "results.json")

_MOAT_KEYS = [
    "product_breadth",
    "developer_momentum",
    "employee_sentiment",
    "revenue_concentration",
    "network_effect_proxy",
    "regulatory_barrier",
]

_CACHED_RESULTS: Optional[Dict[str, Dict[str, float]]] = None


def _load_results_map() -> Dict[str, Dict[str, float]]:
    """Parse ``center/valuation_alpha/results.json`` run_a into a ticker map."""
    try:
        with open(_RESULTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    out: Dict[str, Dict[str, float]] = {}
    for row in data.get("run_a", []) or []:
        ticker = row.get("ticker")
        if not ticker:
            continue
        out[ticker] = {
            "alpha_3y_ann": row.get("alpha_3y_ann"),
            "cash_burn_months_pct": row.get("cash_burn_months_pct"),
            "interest_coverage_ratio_pct": row.get("interest_coverage_ratio_pct"),
            "mahalanobis": row.get("mahalanobis"),
        }
    return out


def _cached_results() -> Dict[str, Dict[str, float]]:
    global _CACHED_RESULTS
    if _CACHED_RESULTS is None:
        _CACHED_RESULTS = _load_results_map()
    return _CACHED_RESULTS


def _usd_entries(companyfacts: dict, tag: str) -> List[dict]:
    units = (
        companyfacts.get("facts", {})
        .get("us-gaap", {})
        .get(tag, {})
        .get("units", {})
        .get("USD", [])
    )
    return units or []


def _latest_value(companyfacts: dict, tag: str) -> Optional[float]:
    best = None
    best_end = None
    for entry in _usd_entries(companyfacts, tag):
        end = entry.get("end")
        val = entry.get("val")
        if end is None or val is None:
            continue
        if best_end is None or end > best_end:
            best_end = end
            best = val
    return best


def parse_xbrl_metrics(companyfacts: dict) -> Dict[str, float]:
    """Extract cash-burn months and interest coverage from SEC companyfacts.

    Formulas mirror ``valuation_alpha.pipeline._derive_metrics``: cash burn is
    3 * cash / operating expenses (months of runway); interest coverage is
    operating income / interest expense. Missing tags yield no keys.
    """
    out: Dict[str, float] = {}
    op_income = _latest_value(companyfacts, "OperatingIncomeLoss")
    interest = _latest_value(companyfacts, "InterestExpense")
    cash = _latest_value(companyfacts, "CashAndCashEquivalentsAtCarryingValue")
    op_expenses = _latest_value(companyfacts, "OperatingExpenses")
    if op_income is not None and interest not in (None, 0):
        out["interest_coverage_ratio_pct"] = float(op_income) / float(interest)
    if cash is not None and op_expenses not in (None, 0):
        out["cash_burn_months_pct"] = 3.0 * float(cash) / float(op_expenses)
    return out


def _alpha_for(
    ticker: str,
    prices: pd.DataFrame,
    factors: pd.DataFrame,
    cached: Dict[str, Dict[str, float]],
) -> Tuple[float, str]:
    try:
        if ticker in prices.columns:
            ret = prices[ticker].pct_change().dropna()
            ret = ret.iloc[-_ALPHA_WINDOW_DAYS:]
            if len(ret) >= 60:
                result = ff5_residual_alpha(ret, factors, horizon_days=_ALPHA_WINDOW_DAYS)
                if result and isinstance(result, dict) and "alpha_annualized" in result:
                    return float(result["alpha_annualized"]), "live_ff5"
    except Exception:
        pass
    cached_val = cached.get(ticker, {}).get("alpha_3y_ann")
    if cached_val is not None and not pd.isna(cached_val):
        return float(cached_val), "results_runa_cached"
    return np.nan, "NaN"


def _xbrl_for(ticker: str) -> Tuple[float, float, str]:
    if not ENABLE_LIVE_XBRL:
        return np.nan, np.nan, "NaN"
    try:
        cik = resolve_cik(ticker)
        if not cik:
            return np.nan, np.nan, "NaN"
        facts = fetch_companyfacts(cik)
        if not facts:
            return np.nan, np.nan, "NaN"
        metrics = parse_xbrl_metrics(facts)
        cb = metrics.get("cash_burn_months_pct", np.nan)
        ic = metrics.get("interest_coverage_ratio_pct", np.nan)
        if pd.isna(cb) and pd.isna(ic):
            return np.nan, np.nan, "NaN"
        return cb, ic, "live_sec"
    except Exception:
        return np.nan, np.nan, "NaN"


def _mahalanobis_for(
    ticker: str, cached: Dict[str, Dict[str, float]]
) -> Tuple[float, str]:
    val = cached.get(ticker, {}).get("mahalanobis")
    if val is not None and not pd.isna(val):
        return float(val), "results_runa_cached"
    return np.nan, "NaN"


def build_names_frame(tickers: List[str]) -> pd.DataFrame:
    """Build the discovery names frame with real alpha / XBRL / mahalanobis.

    Returns a DataFrame with columns [ticker, alpha_3y_ann,
    cash_burn_months_pct, interest_coverage_ratio_pct, mahalanobis]. NaN when a
    value is unknown (never fabricated). Attaches df.attrs["provenance"] =
    {ticker: {column: src}} with src in {"live_ff5", "results_runa_cached",
    "live_sec", "NaN"}. Raises nothing on network failure (empty/NaN rows).
    """
    try:
        prices = fetch_prices(tickers, _DEFAULT_START, _DEFAULT_END)
    except Exception:
        prices = pd.DataFrame()
    try:
        factors = fetch_ff5_factors()
    except Exception:
        factors = pd.DataFrame()
    cached = _cached_results()

    rows = []
    provenance: Dict[str, Dict[str, str]] = {}
    for ticker in tickers:
        alpha, alpha_src = _alpha_for(ticker, prices, factors, cached)
        cb, ic, xbrl_src = _xbrl_for(ticker)
        maha, maha_src = _mahalanobis_for(ticker, cached)
        rows.append(
            {
                "ticker": ticker,
                "alpha_3y_ann": alpha,
                "cash_burn_months_pct": cb,
                "interest_coverage_ratio_pct": ic,
                "mahalanobis": maha,
            }
        )
        provenance[ticker] = {
            "alpha_3y_ann": alpha_src,
            "cash_burn_months_pct": xbrl_src,
            "interest_coverage_ratio_pct": xbrl_src,
            "mahalanobis": maha_src,
        }
    df = pd.DataFrame(
        rows,
        columns=[
            "ticker",
            "alpha_3y_ann",
            "cash_burn_months_pct",
            "interest_coverage_ratio_pct",
            "mahalanobis",
        ],
    )
    df.attrs["provenance"] = provenance
    return df


def _employee_sentiment(conn, ticker: str) -> Optional[float]:
    rows = conn.execute(
        "SELECT glassdoor_normalized, comparably_normalized"
        " FROM glassdoor_comparably_audit"
        " WHERE ticker = ? ORDER BY date DESC LIMIT 1",
        (ticker,),
    ).fetchall()
    if not rows:
        return None
    row = rows[0]
    val = row["glassdoor_normalized"]
    if val is None or pd.isna(val):
        val = row["comparably_normalized"]
    if val is None or pd.isna(val):
        return None
    return float(max(0.0, min(1.0, val)))


def _developer_momentum(conn, ticker: str) -> Optional[float]:
    rows = conn.execute(
        "SELECT stars, forks FROM github_org_metrics WHERE ticker = ?",
        (ticker,),
    ).fetchall()
    if not rows:
        return None
    total = 0.0
    for row in rows:
        stars = row["stars"] or 0
        forks = row["forks"] or 0
        total += (math.log1p(stars) + math.log1p(forks)) / 15.0
    return float(max(0.0, min(1.0, total)))


def _product_breadth(conn, ticker: str) -> Optional[float]:
    rows = conn.execute(
        "SELECT rating FROM product_intel_reviews WHERE ticker = ?",
        (ticker,),
    ).fetchall()
    ratings = [r["rating"] for r in rows if r["rating"] is not None]
    if not ratings:
        return None
    return float(max(0.0, min(1.0, statistics.median(ratings) / 5.0)))


def qualitative_signals(ticker: str) -> Tuple[Dict[str, float], Dict[str, str]]:
    """Return (moat_signals, provenance) for AlternativeStrategyPipeline.run.

    Real cached signals where they exist; every missing key defaults to 0.5
    tagged "default_neutral" so the pipeline returns an honest "hold", never
    "avoid". Any DB error is treated as no cached data.
    """
    signals = {key: 0.5 for key in _MOAT_KEYS}
    provenance = {key: "default_neutral" for key in _MOAT_KEYS}
    try:
        conn = get_connection()
        emp = _employee_sentiment(conn, ticker)
        if emp is not None:
            signals["employee_sentiment"] = emp
            provenance["employee_sentiment"] = "cached:glassdoor_comparably_audit"
        dev = _developer_momentum(conn, ticker)
        if dev is not None:
            signals["developer_momentum"] = dev
            provenance["developer_momentum"] = "cached:github_org_metrics"
        prod = _product_breadth(conn, ticker)
        if prod is not None:
            signals["product_breadth"] = prod
            provenance["product_breadth"] = "cached:product_intel_reviews"
    except Exception:
        pass
    return signals, provenance


def normalize_mahalanobis(df: pd.DataFrame) -> pd.DataFrame:
    """Percentile-rank non-NaN mahalanobis values to 0-1 within the batch.

    NaN values stay NaN. Mutates a copy and returns it.
    """
    out = df.copy()
    mask = out["mahalanobis"].notna()
    if mask.any():
        out.loc[mask, "mahalanobis"] = out.loc[mask, "mahalanobis"].rank(pct=True)
    return out


def coverage_summary(tickers: List[str]) -> Dict[str, int]:
    """Count provenance source tags across build_names_frame rows.

    Counts the alpha_3y_ann source per row; NaN-tagged rows count as
    "default_neutral" so the counts sum to len(tickers).
    """
    df = build_names_frame(tickers)
    provenance = df.attrs.get("provenance", {})
    counts = {
        "live_ff5": 0,
        "results_runa_cached": 0,
        "live_sec": 0,
        "default_neutral": 0,
    }
    for ticker in tickers:
        src = provenance.get(ticker, {}).get("alpha_3y_ann", "NaN")
        if src == "NaN":
            counts["default_neutral"] += 1
        elif src in counts:
            counts[src] += 1
    return counts