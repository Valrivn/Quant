"""Sentinel funnel gates — G1 survival/solvency and G2 fundamentals.

Pure functions over PIT-filtered fundamentals. Fail-closed: a gate that
cannot be evaluated fails (``insufficient_data``), never passes silently.

G1 Altman Z uses book equity for the market-value term when no price data is
passed (conservative), and adds a cash-runway solvency leg.
"""

import math
from typing import Dict, List, Optional, Tuple

import pandas as pd


def _num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def pit_filter(df: pd.DataFrame, as_of: str) -> pd.DataFrame:
    """Keep rows that were public by ``as_of`` (filed_date <= as_of)."""
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    for col in ("filed_date", "fiscal_end"):
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce")
    as_of_ts = pd.Timestamp(as_of)
    if "filed_date" in out.columns:
        out = out[out["filed_date"] <= as_of_ts]
    return out.sort_values("filed_date" if "filed_date" in out.columns else "fiscal_end")


def _as_row(row, field: str) -> Optional[float]:
    if field not in row:
        return None
    return _num(row[field])


def _altman_z(latest: dict) -> Tuple[Optional[float], str]:
    ta = _as_row(latest, "total_assets")
    tl = _as_row(latest, "total_liabilities")
    if ta is None or ta <= 0:
        return None, "no_total_assets"
    wc = (_as_row(latest, "current_assets") or 0.0) - (_as_row(latest, "current_liabilities") or 0.0)
    re = _as_row(latest, "retained_earnings") or 0.0
    ebit = _as_row(latest, "ebit") or 0.0
    be = _as_row(latest, "equity") or 0.0
    sales = _as_row(latest, "revenue") or 0.0
    tl_safe = tl if tl and tl > 0 else ta
    z = (1.2 * wc / ta) + (1.4 * re / ta) + (3.3 * ebit / ta) + (0.6 * be / tl_safe) + (1.0 * sales / ta)
    return z, ""


def g1_survival_solvency(
    fundamentals: pd.DataFrame, as_of: str,
    z_floor: float = 1.1, runway_quarters_floor: int = 4,
    min_quarters_data: int = 4, z_book_equity_floor: Optional[float] = None,
) -> Tuple[bool, str, Dict]:
    """Fail-closed survival & solvency gate.

    Requires ``min_quarters_data`` PIT quarters, Altman Z >= the applicable
    floor on the latest quarter, and a cash runway >= ``runway_quarters_floor``
    (infinite when the trailing burn is zero).

    The Z floor is mode-aware: the market-value Altman Z cutoff is
    ``z_floor``, while the conservative book-equity variant (used when no
    price data is wired, see ``uses_book_equity``) uses the lower
    ``z_book_equity_floor`` cutoff (config ``gates.g1_survival``).
    """
    df = pit_filter(fundamentals, as_of)
    if df.empty or len(df) < min_quarters_data:
        return False, f"insufficient_data:{len(df)}/{min_quarters_data}q", {"quarters": len(df)}

    # Fundamentals rows can be sparse (per-CIK companyfacts outer join emits
    # fiscal ends where only some fields exist). Use the most recent row with
    # the balance-sheet fields the score needs, not blindly the last row.
    recs = df.to_dict("records")
    latest = None
    for r in reversed(recs):
        if _num(r.get("total_assets")) is not None and _num(r.get("total_liabilities")) is not None:
            latest = r
            break
    if latest is None:
        latest = recs[-1]
    z, z_reason = _altman_z(latest)
    if z is None:
        return False, f"g1:{z_reason}", {}

    metrics = {
        "altman_z": round(z, 3),
        "fiscal_end": str(latest.get("fiscal_end", "")),
        "uses_book_equity": True,
    }
    z_floor_applied = z_book_equity_floor if metrics["uses_book_equity"] else z_floor
    if z_floor_applied is None:
        z_floor_applied = z_floor
    metrics["z_floor"] = z_floor_applied

    ocf_rows = [dict(r) for r in df.to_dict("records") if _num(r.get("ocf")) is not None]
    burn = 0.0
    if ocf_rows:
        k10 = [r for r in ocf_rows if str(r.get("form", "")).upper() == "10-K"]
        if k10:
            annual_ocf = _num(k10[-1]["ocf"])
            burn = max(0.0, -annual_ocf / 4.0) if annual_ocf is not None else 0.0
        else:
            last_ocf = _num(ocf_rows[-1]["ocf"])
            burn = max(0.0, -last_ocf) if last_ocf is not None else 0.0

    cash = None
    for r in reversed(recs):
        c = _num(r.get("cash"))
        if c is not None:
            cash = c
            break
    if cash is None:
        return False, "g1:no_cash", metrics
    metrics["cash"] = round(cash, 3)
    if burn > 0:
        runway = cash / burn
        metrics["runway_quarters"] = round(runway, 2)
        if runway < runway_quarters_floor:
            return False, f"g1:runway<{runway_quarters_floor}q({runway:.1f})", metrics
    else:
        metrics["runway_quarters"] = None  # no burn -> self-sustaining

    if z < z_floor_applied:
        return False, f"g1:altman_z<{z_floor_applied}({z:.2f})", metrics

    return True, "", metrics


def g2_fundamentals(
    fundamentals: pd.DataFrame, as_of: str,
    ocf_positive_quarters: int = 3, gross_margin_floor: float = 0.20,
    capex_tracked_quarters: int = 3, min_quarters_trend: int = 3,
) -> Tuple[bool, str, Dict]:
    """Fail-closed fundamentals gate.

    OCF > 0 in >= ``ocf_positive_quarters`` of the trailing 4 quarters; gross
    margin >= ``gross_margin_floor`` in >= ``min_quarters_trend`` of the
    trailing ``min_quarters_trend`` quarters; CAPEX present in >=
    ``capex_tracked_quarters`` of the trailing 4 quarters.
    """
    df = pit_filter(fundamentals, as_of)
    if df.empty:
        return False, "g2:no_data", {}

    recs = df.to_dict("records")
    # Trailing windows count only rows that actually carry the field: the
    # companyfacts outer join can emit sparse fiscal ends (cash-only or
    # balance-only rows), which must not count as "missing" reports.
    ocf_rows = [r for r in recs if _num(r.get("ocf")) is not None][-4:]
    ocf_pos = 0
    for r in ocf_rows:
        if _num(r.get("ocf")) > 0:
            ocf_pos += 1
    ocf_present = len(ocf_rows)
    capex_rows = [r for r in recs if _num(r.get("capex")) is not None][-4:]
    capex_present = len(capex_rows)

    # Margin quarters: skip balance-only filings (no revenue); require the last
    # ``min_quarters_trend`` margin-bearing quarters to clear the floor.
    margins = [_num(r.get("gross_margin")) for r in df.tail(8).to_dict("records")]
    margins = [m for m in margins if m is not None][-min_quarters_trend:]
    margin_ok = sum(1 for m in margins if m >= gross_margin_floor)

    metrics = {
        "ocf_positive": ocf_pos,
        "ocf_present": ocf_present,
        "margin_ok": margin_ok,
        "margins_present": len(margins),
        "capex_tracked": capex_present,
    }

    if ocf_pos < ocf_positive_quarters:
        return False, f"g2:ocf_pos<{ocf_positive_quarters}({ocf_pos}/4)", metrics
    if len(margins) < min_quarters_trend:
        return False, f"g2:margin_coverage_gap({len(margins)}/{min_quarters_trend})", metrics
    if margin_ok < min_quarters_trend:
        return False, f"g2:margin<{gross_margin_floor}({margin_ok}/{min_quarters_trend})", metrics
    if capex_present < capex_tracked_quarters:
        return False, f"g2:capex_untracked({capex_present}/{capex_tracked_quarters})", metrics

    return True, "", metrics


def run_gates(
    fundamentals: pd.DataFrame, as_of: str, cfg: Dict,
) -> List[Dict]:
    """Run G1 and G2 against the config; returns per-gate verdict dicts."""
    g1 = g1_survival_solvency(
        fundamentals, as_of,
        z_floor=cfg["gates"]["g1_survival"]["altman_z_floor"],
        runway_quarters_floor=cfg["gates"]["g1_survival"]["cash_runway_quarters_floor"],
        min_quarters_data=cfg["gates"]["g1_survival"]["min_quarters_data"],
        z_book_equity_floor=cfg["gates"]["g1_survival"].get("z_book_equity_floor"),
    )
    g2 = g2_fundamentals(
        fundamentals, as_of,
        ocf_positive_quarters=cfg["gates"]["g2_fundamentals"]["ocf_positive_quarters"],
        gross_margin_floor=cfg["gates"]["g2_fundamentals"]["gross_margin_floor"],
        capex_tracked_quarters=cfg["gates"]["g2_fundamentals"]["capex_tracked_quarters"],
        min_quarters_trend=cfg["gates"]["g2_fundamentals"]["min_quarters_trend"],
    )
    return [
        {"gate": "g1_survival", "passed": g1[0], "reason": g1[1], "metrics": g1[2]},
        {"gate": "g2_fundamentals", "passed": g2[0], "reason": g2[1], "metrics": g2[2]},
    ]
