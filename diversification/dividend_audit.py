"""Stable-dividend audit (Phase 2, D-20260803-004).

Multi-source dividend audit for the stable-dividend basket:

- Primary feed: yfinance per-share dividend histories (ex-date indexed),
  consumed at each decision date on a TRAILING-ONLY expanding window (no
  lookahead — a name only enters the basket once its own history qualifies).
- Second source (data-integrity gate, run once at sim start): the SEC XBRL
  ``CommonStockDividendsPerShareDeclared`` tag via
  ``valuation_alpha.datastore.xbrl_financials``, cross-checked against the
  yfinance trailing-12m payout within a relative tolerance. If SEC EDGAR is
  unreachable the check degrades to status "NA" (documented, same pattern as
  the Phase-1 FRED fallback).

Admission gates (all must hold at a decision date):
  - >= ``WINDOW_YEARS`` of dividend history with no skipped year in the window;
  - no year-over-year payout cut beyond ``MAX_YOY_CUT``;
  - trailing-12m yield >= ``MIN_YIELD``;
  - name passes the REIT/BDC/MLP keyword screen.

If fewer than ``MIN_CANDIDATES`` pass, the equity sleeve falls back to bills
(``FALLBACK_TICKER``) — the CEO-approved minimum-candidates floor so the sim
cannot starve in thin periods (e.g., early 2018-2020 expanding windows).

All thresholds are pre-registered module constants, never fit. Yields feed the
fee-coverage measurement only, never selection (auditor requirement).
"""

import numpy as np
import pandas as pd

from diversification.sleeves import DIVIDEND_EXCLUDED_TICKERS as _EXCLUDED_TICKERS

WINDOW_YEARS = 5
MIN_YIELD = 0.03
MAX_YOY_CUT = 0.5
MIN_CANDIDATES = 3
FALLBACK_TICKER = "SHY"
YEAR_DAYS = 365.25
REL_TOL = 0.10
XBRL_TAG = "CommonStockDividendsPerShareDeclared"
XBRL_FIELDS = {"dividend_ps": XBRL_TAG}
NAME_EXCLUSIONS = ("REIT", "BDC", "MLP", "REALTY", "REAL ESTATE")


def _as_series(dividend_series):
    s = pd.Series(dividend_series).dropna()
    s.index = pd.to_datetime(s.index)
    s = s[~s.index.duplicated()].sort_index()
    return s


def audit_dividend_history(dividend_series, price, date, window_years=WINDOW_YEARS,
                           min_yield=MIN_YIELD, max_yoy_cut=MAX_YOY_CUT):
    """Evaluate one dividend history against the stable-dividend gates.

    ``dividend_series``: per-share dividends indexed by ex-date.
    ``price``: current share price at ``date``.
    Returns ``(passed, reasons)``; reasons list every failed gate.
    """
    reasons = []
    ser = _as_series(dividend_series)
    ser = ser[ser.index <= pd.Timestamp(date)]
    if ser.empty:
        return False, ["no dividend history at date"]
    span_days = (ser.index[-1] - ser.index[0]).days
    if span_days < window_years * YEAR_DAYS:
        reasons.append(
            f"history span {span_days / YEAR_DAYS:.1f}y < {window_years}y"
        )
    annual = ser.groupby(ser.index.year).sum()
    first_year = int(annual.index.min())
    last_year = int(annual.index.max())
    missing = [y for y in range(first_year, last_year + 1) if y not in annual.index]
    if missing:
        reasons.append(f"skipped year(s): {missing}")
    if len(annual) > 1:
        # Compare COMPLETE calendar years only: the current, partially-elapsed
        # year is excluded (its year-to-date sum would falsely read as a cut
        # against the prior full year). A real cut shows up one year later.
        year_now = int(pd.Timestamp(date).year)
        full_years = [y for y in sorted(annual.index) if y < year_now]
        drops = {}
        for i in range(1, len(full_years)):
            y_prev, y_cur = full_years[i - 1], full_years[i]
            if annual[y_prev] > 0:
                rel = (annual[y_cur] - annual[y_prev]) / annual[y_prev]
                if rel < -max_yoy_cut:
                    drops[y_cur] = rel
        if drops:
            reasons.append(
                "y/y payout cut >{:.0%}: {}".format(
                    max_yoy_cut, {int(y): round(float(v), 3) for y, v in drops.items()}
                )
            )
    trail = ser[ser.index >= pd.Timestamp(date) - pd.Timedelta(days=YEAR_DAYS)].sum()
    if price is not None and price > 0:
        yield_t = float(trail) / float(price)
        if yield_t < min_yield:
            reasons.append(f"trailing yield {yield_t:.3f} < {min_yield:.3f}")
    return (not reasons), reasons


def screen_dividend_name(name, excluded=NAME_EXCLUSIONS):
    """Return True when the name is NOT an excluded structure (REIT/BDC/MLP)."""
    n = str(name).upper()
    return not any(k.upper() in n for k in excluded)


def audit_basket(candidates, dividends, prices, date, min_candidates=MIN_CANDIDATES,
                 fallback=FALLBACK_TICKER, excluded_tickers=_EXCLUDED_TICKERS, **kwargs):
    """Admit candidates whose dividend histories clear the gates at ``date``.

    OOS: only history at/under ``date`` is used. Returns
    ``(basket, rejected, fallback_used)`` where ``fallback_used`` means fewer
    than ``min_candidates`` passed and the equity sleeve must fall back to
    ``fallback`` (bills).
    """
    basket = []
    rejected = {}
    for name in candidates:
        if name in excluded_tickers:
            rejected[name] = ["ticker excluded (REIT/BDC/MLP)"]
            continue
        if not screen_dividend_name(name):
            rejected[name] = ["name excluded (REIT/BDC/MLP)"]
            continue
        div = dividends.get(name)
        price = None
        if prices is not None and name in getattr(prices, "columns", []):
            p = prices[name].asof(date)
            price = float(p) if p == p else None
        if div is None or div.empty:
            rejected[name] = ["no dividend data"]
            continue
        ok, reasons = audit_dividend_history(div, price, date, **kwargs)
        if ok:
            basket.append(name)
        else:
            rejected[name] = reasons
    return basket, rejected, len(basket) < min_candidates


def xbrl_dividend_crosscheck(ticker, cik, dividend_series, date,
                             fetch_companyfacts=None, extract=None):
    """Cross-check yfinance trailing-12m payout against SEC XBRL per-share
    dividends. Returns ``(status, detail)`` with status in PASS / FAIL / NA.
    NA covers: EDGAR unreachable, tag absent from 10-Q filings, or the
    cross-check inputs being unusable (documented degradation).
    """
    if fetch_companyfacts is None or extract is None:
        return ("NA", "xbrl tooling unavailable")
    facts = fetch_companyfacts(cik)
    if not facts:
        return ("NA", "EDGAR unreachable — documented degradation")
    df = extract(facts, XBRL_FIELDS)
    if df is None or df.empty:
        return ("NA", f"tag {XBRL_TAG} not in 10-Q filings")
    cutoff = pd.Timestamp(date) - pd.Timedelta(days=YEAR_DAYS)
    if "filed_date" in df.columns:
        df = df[df["filed_date"] <= pd.Timestamp(date)]
    xb = float(df.loc[df.index >= cutoff, "dividend_ps"].sum())
    ser = _as_series(dividend_series)
    ser = ser[(ser.index >= cutoff) & (ser.index <= pd.Timestamp(date))]
    yf = float(ser.sum())
    if xb <= 0 or yf <= 0:
        return ("NA", "no overlapping payout on both sources")
    if abs(xb - yf) / max(xb, yf) <= REL_TOL:
        return ("PASS", f"xb {xb:.3f} vs yf {yf:.3f} within {REL_TOL:.0%}")
    return ("FAIL", f"xb {xb:.3f} vs yf {yf:.3f} diverge > {REL_TOL:.0%}")


def xbrl_crosscheck_all(candidates, dividends, date, resolve_cik=None,
                        fetch_companyfacts=None, extract=None):
    """Run the SEC XBRL cross-check over the candidate set; best-effort rows."""
    rows = []
    for name in candidates:
        cik = None
        if resolve_cik is not None:
            try:
                cik = resolve_cik(name)
            except Exception:
                cik = None
        if not cik:
            rows.append((name, "NA", "CIK unresolved"))
            continue
        status, detail = xbrl_dividend_crosscheck(
            name, str(cik), dividends.get(name, pd.Series(dtype=float)),
            date, fetch_companyfacts=fetch_companyfacts, extract=extract,
        )
        rows.append((name, status, detail))
    return rows
