"""Reinvestment-rate discovery signal (D-20260802-002, CEO-approved re-thesis).

The discovery thesis changed: small caps are gated on REINVESTMENT RATE, not
profitability, because small businesses are legitimately unprofitable while
reinvesting to grow. This module implements:

  1. Profit-agnostic reinvestment rate: how much of the company's operating
     cash generation is plowed back into capex + R&D (and acquisitions when
     available). Damodaran's growth identity: expected_growth =
     reinvestment_rate * return_on_invested_capital.
  2. A 3-5 YEAR holding window (small caps cannot be expected to re-rate in
     1-3 years; the illiquidity premium is earned over the graduation cycle).
  3. A cohort test case: high-reinvestment names vs profitable names vs the
     rest, evaluated on forward 3y/5y returns.
  4. Reinvestment screen: a rankable signal for the discovery universe.

Reference: ``.agents/project/org/research/small-cap-graduates.md`` (graduation
statistics: ~1/3 of small-cap turnover is graduation; size premium IS the
graduation effect; retained-earnings/plowback predicts long-horizon returns;
avoid the asset-growth anomaly by qualifying reinvestment with ROIC).
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

# Configurable weights live in config/weights*.yaml (invariant 4); these are
# module defaults mirroring the YAML until a weight file is wired.
MIN_QUARTERS = 4               # minimum trailing quarters for a stable signal
REINVEST_PLOWBACK_FLOOR = 0.30 # invest >= 30% of operating cash into growth
HORIZON_3Y = 756
HORIZON_5Y = 1260
QUALITY_ROIC_FLOOR = 0.10      # expected_growth uses ROIC; floor to avoid noise
MAX_REINVEST_RATE = 5.0        # sanity cap on reinvestment/OCF ratio
ASSET_GROWTH_WARN = 0.40       # asset-growth anomaly: >40% annual asset growth warns


@dataclass
class ReinvestmentMetrics:
    ticker: str
    reinvestment_rate: float       # (capex + rd) / OCF, trailing 4Q, profit-agnostic
    reinvestment_intensity: float  # (capex + rd) / revenue
    ocf: float
    capex: float
    rd: float
    roic: float
    expected_growth: float         # reinvestment_rate * roic
    profitable: bool               # trailing net income > 0
    asset_growth_1y: float
    pass_signal: bool
    reason: str = ""


def _trailing4q(quarterly: pd.DataFrame, col: str) -> float:
    if col not in quarterly.columns:
        return np.nan
    s = quarterly[col].dropna()
    if len(s) < 2:
        return np.nan
    return float(s.tail(min(4, len(s))).sum())


def _last_valid(quarterly: pd.DataFrame, col: str) -> float:
    if col not in quarterly.columns:
        return np.nan
    s = quarterly[col].dropna()
    if s.empty:
        return np.nan
    return float(s.iloc[-1])


def compute_reinvestment_metrics(
    quarterly: pd.DataFrame, ticker: str = "",
    roic_floor: float = QUALITY_ROIC_FLOOR,
) -> ReinvestmentMetrics:
    """Compute profit-agnostic reinvestment metrics from quarterly XBRL facts.

    Reinvestment rate = (capex + rd) / OCF (trailing 4Q). Both numerator and
    denominator are allowed to be negative — a company burning cash while
    investing hard is exactly the profile we want to surface, NOT exclude.
    ``profitable`` is reported (for the cohort test case) but never gates.
    """
    if quarterly is None or quarterly.empty:
        return ReinvestmentMetrics(ticker, np.nan, np.nan, np.nan, np.nan, np.nan,
                                   np.nan, np.nan, False, np.nan, False, "no_fundamentals")

    capex = _trailing4q(quarterly, "capex")
    rd = _trailing4q(quarterly, "rd")
    ocf = _trailing4q(quarterly, "ocf")
    revenue = _trailing4q(quarterly, "revenue")
    ni = _trailing4q(quarterly, "net_income")

    capex_mag = abs(capex) if capex == capex else np.nan
    reinv_num = capex_mag + (rd if rd == rd else 0.0)
    reinvestment_rate = np.nan
    if ocf == ocf and abs(ocf) > 1e-9:
        reinvestment_rate = reinv_num / ocf
        if abs(reinvestment_rate) > MAX_REINVEST_RATE:
            reinvestment_rate = np.copysign(MAX_REINVEST_RATE, reinvestment_rate)
    reinvestment_intensity = reinv_num / revenue if revenue == revenue and abs(revenue) > 1e-9 else np.nan

    roic = _last_valid(quarterly, "roic")
    expected_growth = np.nan
    if reinvestment_rate == reinvestment_rate and roic == roic:
        expected_growth = reinvestment_rate * roic
    elif roic == roic and roic > roic_floor and reinvestment_intensity == reinvestment_intensity:
        expected_growth = reinvestment_intensity * roic

    asset_growth_1y = np.nan
    if "assets" in quarterly.columns:
        a = quarterly["assets"].dropna()
        if len(a) >= 5:
            y_ago = a.iloc[-5]
            if y_ago and y_ago != 0:
                asset_growth_1y = float(a.iloc[-1] / y_ago - 1.0)

    profitable = bool(ni == ni and ni > 0)

    reasons = []
    if reinvestment_rate != reinvestment_rate:
        reasons.append("no_ocf_capex")
    elif reinvestment_rate < REINVEST_PLOWBACK_FLOOR:
        reasons.append(f"plowback<{REINVEST_PLOWBACK_FLOOR:g}")
    if asset_growth_1y == asset_growth_1y and asset_growth_1y > ASSET_GROWTH_WARN:
        reasons.append("asset_growth_anomaly_warning")
    pass_signal = (
        reinvestment_rate == reinvestment_rate
        and reinvestment_rate >= REINVEST_PLOWBACK_FLOOR
    )

    return ReinvestmentMetrics(
        ticker=ticker,
        reinvestment_rate=reinvestment_rate,
        reinvestment_intensity=reinvestment_intensity,
        ocf=ocf,
        capex=capex,
        rd=rd,
        roic=roic,
        expected_growth=expected_growth,
        profitable=profitable,
        asset_growth_1y=asset_growth_1y,
        pass_signal=pass_signal,
        reason=";".join(reasons),
    )


def reinvestment_screen(
    quarterly_by_ticker: Dict[str, pd.DataFrame],
    moat_scores: Optional[Dict[str, float]] = None,
    roic_floor: float = QUALITY_ROIC_FLOOR,
) -> pd.DataFrame:
    """Rank a universe by reinvestment-rate signal (profit-agnostic).

    ``moat_scores`` (ticker -> 0..1 product-uniqueness/moat composite) is a
    continuous tilt, not a gate: +0.25 for a moat score >= 0.7, +0.1 for
    >= 0.5, 0 otherwise — mirrors the Glassdoor continuous-tilt ruling style.
    Returns a DataFrame sorted by score, with pass/fail and reasons.
    """
    rows = []
    for ticker, q in quarterly_by_ticker.items():
        m = compute_reinvestment_metrics(q, ticker, roic_floor)
        if not m.pass_signal:
            rows.append({
                "ticker": ticker, "reinvestment_rate": m.reinvestment_rate,
                "reinvestment_intensity": m.reinvestment_intensity,
                "expected_growth": m.expected_growth, "ocf": m.ocf,
                "profitable": m.profitable, "pass_signal": False,
                "reason": m.reason, "moat": np.nan, "score": np.nan,
            })
            continue
        moat = float(moat_scores.get(ticker)) if moat_scores and moat_scores.get(ticker) is not None else np.nan
        moat_tilt = 0.0
        if moat == moat:
            if moat >= 0.7:
                moat_tilt = 0.25
            elif moat >= 0.5:
                moat_tilt = 0.1
        # Score: expected growth (or reinvestment intensity fallback), with
        # moat tilt. Profitability is NOT scored (thesis is profit-agnostic).
        base = m.expected_growth if m.expected_growth == m.expected_growth else m.reinvestment_rate
        if base != base:
            base = m.reinvestment_intensity if m.reinvestment_intensity == m.reinvestment_intensity else 0.0
        rows.append({
            "ticker": ticker, "reinvestment_rate": m.reinvestment_rate,
            "reinvestment_intensity": m.reinvestment_intensity,
            "expected_growth": m.expected_growth, "ocf": m.ocf,
            "profitable": m.profitable, "pass_signal": True,
            "reason": m.reason, "moat": moat, "score": base + moat_tilt,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    return df.sort_values("score", ascending=False, na_position="last").reset_index(drop=True)


def cohort_returns(
    prices: pd.DataFrame,
    quarterly_by_ticker: Dict[str, pd.DataFrame],
    start: pd.Timestamp,
    horizons: Optional[Dict[str, int]] = None,
    min_quarters: int = MIN_QUARTERS,
) -> pd.DataFrame:
    """Forward-return evaluation of cohorts: high-reinvestment vs profitable vs rest.

    For each name with >= ``min_quarters`` of fundamentals by ``start``, compute
    forward returns over each horizon (default 3y=756, 5y=1260 trading days) and
    tag its cohort (mutually exclusive, mirroring the CEO's head-to-head):
      - HIGH_REINVEST : pass_signal AND NOT profitable (pure reinvestment gamble)
      - PROFITABLE    : profitable AND NOT pass_signal (pure profitability)
      - BOTH          : profitable AND pass_signal (reinvestors that also profit)
      - OTHER         : neither signal nor profitability

    Returns per-name forward returns with cohort tags (row per ticker, one
    column per horizon + cohort + metrics).
    """
    horizons = horizons or {"fwd_3y": HORIZON_3Y, "fwd_5y": HORIZON_5Y}
    out = []
    for ticker, q in quarterly_by_ticker.items():
        m = compute_reinvestment_metrics(q, ticker)
        if q is None or q.empty or len(q) < min_quarters:
            continue
        if ticker not in prices.columns:
            continue
        px = prices[ticker].dropna()
        if px.empty:
            continue
        entry_idx = px.index.searchsorted(pd.Timestamp(start))
        if entry_idx >= len(px):
            continue
        p0 = float(px.iloc[entry_idx])
        if p0 == 0:
            continue
        row = {
            "ticker": ticker,
            "reinvestment_rate": m.reinvestment_rate,
            "profitable": m.profitable,
            "pass_signal": m.pass_signal,
            "expected_growth": m.expected_growth,
        }
        if m.pass_signal and not m.profitable:
            row["cohort"] = "HIGH_REINVEST"
        elif m.profitable and not m.pass_signal:
            row["cohort"] = "PROFITABLE"
        elif m.profitable and m.pass_signal:
            row["cohort"] = "BOTH"
        else:
            row["cohort"] = "OTHER"
        for label, horizon in horizons.items():
            end_idx = entry_idx + horizon
            if end_idx >= len(px):
                p1 = float(px.iloc[-1])
                row[label] = (p1 / p0 - 1.0) if p0 else np.nan
                row[label + "_complete"] = False
            else:
                p1 = float(px.iloc[end_idx])
                row[label] = (p1 / p0 - 1.0) if p0 else np.nan
                row[label + "_complete"] = True
        out.append(row)
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out)
    df["cohort"] = pd.Categorical(df["cohort"], ["HIGH_REINVEST", "PROFITABLE", "BOTH", "OTHER"])
    return df


def cohort_summary(cohort_returns_df: pd.DataFrame, horizon: str = "fwd_3y") -> pd.DataFrame:
    """Aggregate forward returns by cohort for the test-case report."""
    if cohort_returns_df is None or cohort_returns_df.empty:
        return pd.DataFrame()
    if horizon not in cohort_returns_df.columns:
        return pd.DataFrame()
    g = cohort_returns_df.groupby("cohort", observed=True)[horizon]
    out = pd.DataFrame({
        "n": g.size(),
        "mean_fwd_return": g.mean(),
        "median_fwd_return": g.median(),
        "win_rate": g.apply(lambda s: (s > 0).mean()),
    })
    return out


def format_cohort_report(summary: pd.DataFrame, horizon: str = "fwd_3y") -> str:
    """Render the cohort test-case results as a markdown report block."""
    if summary is None or summary.empty:
        return f"# Reinvestment cohort test ({horizon})\n\nNo data.\n"
    lines = [f"# Reinvestment cohort test — {horizon} forward returns", ""]
    lines.append("| Cohort | N | Mean fwd | Median fwd | Win rate |")
    lines.append("|---|---|---|---|---|")
    for cohort, row in summary.iterrows():
        lines.append(
            f"| {cohort} | {int(row['n'])} | {row['mean_fwd_return']*100:6.1f}% | "
            f"{row['median_fwd_return']*100:6.1f}% | {row['win_rate']*100:5.1f}% |"
        )
    lines.append("")
    lines.append("_HIGH_REINVEST = plowback>=30% of OCF into capex+R&D and NOT profitable; "
                 "PROFITABLE = trailing NI>0; OTHER = neither._")
    return "\n".join(lines)
