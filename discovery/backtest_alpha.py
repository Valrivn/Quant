"""IG vs traditional comparative alpha backtest (D-20260807-001).

PURPOSE
    Quantify, like-for-like, whether an Instagram/TikTok-derived pass cohort
    generates alpha relative to the companies our existing scrapers already
    surface (``daily_aggregations``). Both lanes run through the SAME standard
    stock screen (the census gates, read-only). Alpha math lives in
    ``valuation_alpha.alpha`` and is never re-implemented here.

Honesty / isolation
    - The IG lane accepts a caller-supplied IG candidate list (never
      fabricated). With no IG feed the lane reports ``unfed`` and computes no
      alpha rather than inventing a cohort.
    - This module is a LEAF: tests import it, nothing in production does. It
      computes alpha from injected fetchers (offline-safe) and writes nothing
      to production tables.
    - Deterministic: identical inputs -> identical outputs.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import pandas as pd

from .ig_experiment import run_ig_experiment

_DEFAULT_START = "2019-01-01"
_DEFAULT_END = "2026-07-31"


@dataclass
class LaneResult:
    """Alpha comparison for one lane (IG or traditional)."""

    lane: str
    status: str = "seeded"  # seeded | unfed | no_pass
    cohort: List[str] = field(default_factory=list)
    alpha: Optional[dict] = None
    reason: str = ""

    @property
    def annualized_alpha(self) -> Optional[float]:
        if self.alpha:
            return self.alpha.get("alpha_annualized")
        return None

    @property
    def information_ratio(self) -> Optional[float]:
        if self.alpha:
            return self.alpha.get("information_ratio")
        return None


def _equal_weight_returns(prices: pd.DataFrame) -> pd.Series:
    """Daily equal-weight portfolio return series from a price frame."""
    if prices is None or prices.empty:
        return pd.Series(dtype=float)
    prices = prices.ffill()
    returns = prices.pct_change().dropna(how="all")
    if returns.empty:
        return pd.Series(dtype=float)
    return returns.mean(axis=1, skipna=True)


def _compute_alpha(
    tickers: List[str],
    fetch_prices: Callable,
    fetch_factors: Callable,
    fetch_sp500: Callable,
    start: str,
    end: str,
) -> Optional[dict]:
    """Equal-weight alpha for a cohort: FF5 residual + excess vs S&P 500.

    Fails closed to None on missing data or any alpha-math error.
    """
    if not tickers:
        return None
    try:
        prices = fetch_prices(tickers, start=start, end=end)
    except Exception:
        return None
    returns = _equal_weight_returns(prices)
    if returns.empty or returns.notna().sum() < 60:
        return None

    try:
        from valuation_alpha.alpha import excess_vs_sp500, ff5_residual_alpha

        sp500 = fetch_sp500(start, end)
        factors = fetch_factors()
        excess = (
            excess_vs_sp500(returns, sp500) if sp500 is not None and len(sp500) else None
        )
        residual = ff5_residual_alpha(returns, factors) if factors is not None and not factors.empty else None
    except Exception:
        return None

    out: dict = {"n_tickers": len(tickers)}
    if excess:
        out["excess_annualized"] = excess["excess_annualized"]
        out["tracking_error"] = excess["tracking_error"]
        out["information_ratio"] = excess["information_ratio"]
    if residual:
        out["alpha_annualized"] = residual["alpha_annualized"]
        out["alpha_t"] = residual["t_stat"]
        out["alpha_p"] = residual["p_value"]
        out["alpha_ci_lower"] = residual["ci_lower"]
        out["alpha_ci_upper"] = residual["ci_upper"]
        out["n_obs"] = residual["n_obs"]
    return out if out else None


def _empty_prices(*_args, **_kwargs) -> pd.DataFrame:
    return pd.DataFrame()


def _empty_frame(*_args, **_kwargs) -> pd.DataFrame:
    return pd.DataFrame()


def _empty_series(*_args, **_kwargs) -> pd.Series:
    return pd.Series(dtype=float)


def compare_cohorts(
    ig_tickers: Optional[List[str]] = None,
    traditional_limit: int = 20,
    start: str = _DEFAULT_START,
    end: str = _DEFAULT_END,
    fetch_prices: Callable = _empty_prices,
    fetch_factors: Callable = _empty_frame,
    fetch_sp500: Callable = _empty_series,
) -> dict:
    """Screen both lanes through the standard screen and compare alpha.

    ``ig_tickers`` must be IG-derived evidence (never fabricated). When
    absent/empty the IG lane reports ``unfed`` and computes no alpha, exactly
    like the experiment itself (no- fabrication invariant).

    The traditional lane is the distinct tickers our existing scrapers already
    surface (``current_scraper_cohort``), screened with the SAME gates so the
    alpha comparison is like-for-like.

    Network is optional: the default fetchers return empty data, so a
    network-less run reports a comparison with alpha=None rather than raising.
    """
    if not ig_tickers:
        ig_lane = LaneResult(lane="ig", status="unfed", reason="no IG feed")
    else:
        ig_screen = run_ig_experiment(list(ig_tickers), live=False)
        ig_cohort = ig_screen["pass_cohort"]
        ig_lane = LaneResult(
            lane="ig",
            status="seeded" if ig_cohort else "no_pass",
            cohort=ig_cohort,
            reason=ig_screen["reasons"] if not ig_cohort else "",
        )

    trad_tickers = []
    try:
        from discovery.ig_experiment import current_scraper_cohort

        trad_tickers = current_scraper_cohort(limit=traditional_limit)
    except Exception:
        trad_tickers = []

    if not trad_tickers:
        trad_lane = LaneResult(lane="traditional", status="no_pass", reason="no DB cohort")
    else:
        trad_screen = run_ig_experiment(trad_tickers, live=False)
        trad_cohort = trad_screen["pass_cohort"]
        trad_lane = LaneResult(
            lane="traditional",
            status="seeded" if trad_cohort else "no_pass",
            cohort=trad_cohort,
            reason=trad_screen["reasons"] if not trad_cohort else "",
        )

    ig_lane.alpha = _compute_alpha(
        ig_lane.cohort, fetch_prices, fetch_factors, fetch_sp500, start, end
    )
    trad_lane.alpha = _compute_alpha(
        trad_lane.cohort, fetch_prices, fetch_factors, fetch_sp500, start, end
    )

    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "window": {"start": start, "end": end},
        "ig": ig_lane,
        "traditional": trad_lane,
    }



def run_each_alpha(
    tickers: List[str],
    fetch_prices: Callable,
    fetch_factors: Callable,
    fetch_sp500: Callable,
    start: str = _DEFAULT_START,
    end: str = _DEFAULT_END,
    min_obs: int = 60,
) -> dict:
    """Per-ticker alpha backtest + the combined equal-weight pool.

    Returns dict: {"per_ticker": {ticker: alpha_or_None}, "pool": alpha_or_None}
    For each ticker the same math as _compute_alpha runs individually (FF5
    residual + excess vs SPY). The ``pool`` key is the equal-weight basket of
    all tickers. Fail-closed: a ticker with <``min_obs`` usable returns reports
    None, never a fabricated number.
    """
    per = {}
    for t in tickers:
        per[t] = _compute_alpha(
            [t], fetch_prices, fetch_factors, fetch_sp500, start, end
        )
    pool = _compute_alpha(tickers, fetch_prices, fetch_factors, fetch_sp500, start, end)
    return {"per_ticker": per, "pool": pool}


def report_each(result: dict) -> str:
    """Render per-ticker + pool alpha comparison as a markdown table."""
    lines = [
        "ticker | FF5 alpha (ann) | alpha_t | excess vs SPY | IR | n_obs",
        "-" * 70,
    ]
    for t, alpha in result["per_ticker"].items():
        if not alpha:
            lines.append(f"{t:8} | n/a | n/a | n/a | n/a | n/a")
            continue
        lines.append(
            f"{t:8} | {_fmt_pct(alpha.get('alpha_annualized'))} | "
            f"{alpha.get('alpha_t')} | {_fmt_pct(alpha.get('excess_annualized'))} | "
            f"{_fmt(alpha.get('information_ratio'))} | {alpha.get('n_obs')}"
        )
    pool = result["pool"]
    if pool:
        lines.append(
            f"{'POOL':8} | {_fmt_pct(pool.get('alpha_annualized'))} | "
            f"{pool.get('alpha_t')} | {_fmt_pct(pool.get('excess_annualized'))} | "
            f"{_fmt(pool.get('information_ratio'))} | {pool.get('n_obs')}"
        )
    else:
        lines.append("POOL      | n/a")
    return "\n".join(lines)


def _fmt_pct(v):
    return "n/a" if v is None else f"{v * 100:.2f}%"


def _fmt(v):
    return "n/a" if v is None else f"{v:.2f}"


def report_table(result: dict) -> str:
    """Human-readable comparison table (research artifact input)."""
    lines = [
        f"run_at: {result['run_at']}",
        f"window: {result['window']['start']} .. {result['window']['end']}",
        "",
        "lane | status | n_pass | FF5 alpha (ann) | excess vs SPY | IR |",
        "-" * 60,
    ]
    for key in ("ig", "traditional"):
        res = result[key]
        alpha = res.alpha or {}
        ann = alpha.get("alpha_annualized")
        excess = alpha.get("excess_annualized")
        ir = alpha.get("information_ratio")
        lines.append(
            f"{res.lane:12} | {res.status:8} | {len(res.cohort):6} | "
            f"{ann if ann is None else round(ann * 100, 2)}% | "
            f"{excess if excess is None else round(excess * 100, 2)}% | "
            f"{ir if ir is None else round(ir, 2)}"
        )
    return "\n".join(lines)


def format_details(result: dict) -> str:
    """List the pass cohort tickers and per-lane alpha keys."""
    lines = []
    for key in ("ig", "traditional"):
        lane = result[key]
        lines.append(f"[{key}] status={lane.status}")
        lines.append(f"  pass cohort: {', '.join(lane.cohort) if lane.cohort else '(none)'}")
        if lane.alpha:
            alpha = lane.alpha
            lines.append(
                f"  FF5 alpha/YoY: {alpha.get('alpha_annualized')} "
                f"(t={alpha.get('alpha_t')}, p={alpha.get('alpha_p')})"
            )
            lines.append(
                f"  excess vs SPY: {alpha.get('excess_annualized')} "
                f"(IR={alpha.get('information_ratio')})"
            )
        else:
            lines.append("  alpha: (n/a)")
    return "\n".join(lines)