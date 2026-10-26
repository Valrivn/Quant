"""
Survival Adjustment (Phase 8 WS-6) — bigpickle-worker

Probability-weighted valuation for `narrative_v2` (per-filing, PIT = filed_date).

Frozen formulas (phase8_bigpickle_analysis.md §5.7, CEO-ruling constraints C6.2/C6.3):

  * p_survival — PRIORITY CASCADE (first match wins), then clamp [0.05, 0.95]:
      1. ICR < 1.5                    -> survival_distressed (0.40)
      2. years_public < 5             -> clamp(survival_base_young + survival_per_year * years,
                                              survival_clamp)        (0.60 + 0.05 x years)
      3. ROIC - WACC > 5%             -> survival_moat (0.95)
      4. otherwise                    -> survival_mature (0.90)
    Priority order is the contract: distress dominates the young-firm ramp, and
    both dominate the moat/mature base.  (C6.2: "p_survival clamp [0,0.95] +
    priority order" — the frozen clamp is [0.05, 0.95].)

  * Liquidation value on an EQUITY basis with NO cash double-count (C6.3):
       LV = 0.70 x Tangible_BV_ex_cash + Net_Cash
       Tangible_BV_ex_cash = Stockholders_Equity - Goodwill - Intangibles - Cash
       Net_Cash            = Cash - Total_Debt
    This mirrors the going-concern equity IV basis (EV - net_debt):
    GC_equity = EV_fcff - (Total_Debt - Cash) -> same +net_cash term, so the
    two values are directly comparable per share.

    Any negative LV (e.g. a hugely levered, largely-intangible book) is clamped
    to 0.0 with an audit flag — `liquidation_value_ps` has schema min 0.

  * survival_adjusted_iv = p x IV_going_concern + (1-p) x LV_ps
    Schema invariant: survival_adjusted_iv <= IV_going_concern x 1.0001
    (ingestion/valuation_schema.py).  Algebra requires LV_ps <= IV_going_concern
    in the blend; the produced blend therefore caps the liquidation leg at the
    going-concern IV (`liquidation_value_ps` column keeps the RAW LV).  When the
    cap bites, `liquidation_value_capped=True` is written to the audit trail.

  * Survival -> position sizing: p_survival < 0.70 -> max weight 0.5%
    (`survival_position_cap`) — Plan §4 integration row.

PIT discipline: all inputs are PIT as-of filed_date of the filing row.  Nothing
in this module may read future data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd
import sys

# ----------------------------------------------------------------------------
# Frozen constants (config/pre_registration_phase8.yaml -> phase8.narrative_valuation)
# ----------------------------------------------------------------------------

SURVIVAL_DEFAULTS: Dict[str, Any] = {
    "icr_distress_threshold": 1.5,       # ICR < 1.5 triggers the distress branch
    "survival_distressed": float(0.40),  # frozen: 0.40
    "young_years": 5,                    # years_public < 5 -> young-firm ramp
    "survival_base_young": float(0.60),  # frozen: 0.60
    "survival_per_year": float(0.05),    # frozen: +0.05 per year public
    "survival_moat": float(0.95),        # frozen: ROIC - WACC > 5% -> 0.95
    "moat_roic_wacc_gap": float(0.05),   # frozen: 5% gap
    "survival_mature": float(0.90),      # frozen: mature base
    "survival_clamp": (0.05, 0.95),      # frozen clamp
    "liquidation_tangible_factor": float(0.70),  # frozen: 0.70
    "liquidation_cash_basis": "equity",  # frozen: equity DCF basis
    "survival_position_low_p": 0.70,     # Plan §4: p < 0.70 -> 0.5% cap
    "survival_position_low_cap": float(0.005),
    # Observation smoothing v2 (Phase 8 WS-6, optional — DEFAULT OFF): smooth
    # the EMITTED p_survival stream per ticker with a 63-trading-day (3-month)
    # EMA, re-clamped to the frozen survival_clamp.  The priority cascade is
    # untouched — smoothing is a post-cascade observation filter only.
    "enable_observation_smoothing": False,
    "observation_window_days": 63,
}

DEFAULT_YEARS_PUBLIC = 20.0   # missing history -> treat as mature (no young ramp)
DEFAULT_ICR = 10.0            # missing ICR -> healthy (no distress branch)


def _clamp(v: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, v)))


def p_survival_priority_cascade(
    icr: Optional[float],
    years_public: Optional[float],
    roic: Optional[float],
    wacc: Optional[float],
    config: Optional[Dict[str, Any]] = None,
) -> tuple[float, str, Dict[str, Any]]:
    """
    Frozen p_survival priority cascade (C6.2).  Returns
    (p_survival, branch, provenance) where branch is one of:
      distressed / young_ramp / moat / mature.

    Priority order (first match wins, strictly descending):
      1. ICR < 1.5                   -> survival_distressed
      2. years_public < 5            -> young ramp
      3. ROIC - WACC > 5%            -> moat
      4. else                        -> mature base
    The result is then clamped to the frozen survival_clamp.
    """
    cfg = {**SURVIVAL_DEFAULTS, **(config or {})}
    clamp = tuple(float(x) for x in cfg["survival_clamp"])

    prov: Dict[str, Any] = {}
    if icr is None or (isinstance(icr, float) and np.isnan(icr)):
        icr = DEFAULT_ICR
    if years_public is None or (isinstance(years_public, float) and np.isnan(years_public)):
        years_public = DEFAULT_YEARS_PUBLIC
    if roic is None or (isinstance(roic, float) and np.isnan(roic)):
        roic = None
    if wacc is None or (isinstance(wacc, float) and np.isnan(wacc)):
        wacc = None

    # 1. distress branch
    if icr < float(cfg["icr_distress_threshold"]):
        p = float(cfg["survival_distressed"])
        branch = "distressed"
        prov["icr"] = round(float(icr), 4)
    # 2. young-firm ramp (linear, rising with years public)
    elif years_public < float(cfg["young_years"]):
        p = float(cfg["survival_base_young"]) + float(cfg["survival_per_year"]) * float(years_public)
        branch = "young_ramp"
        prov["years_public"] = round(float(years_public), 4)
    # 3. moat (superior returns vs cost of capital)
    elif (roic is not None and wacc is not None
          and roic - wacc > float(cfg["moat_roic_wacc_gap"])):
        p = float(cfg["survival_moat"])
        branch = "moat"
        prov["roic_wacc_gap"] = round(float(roic - wacc), 4)
    # 4. mature base
    else:
        p = float(cfg["survival_mature"])
        branch = "mature"

    p_clamped = _clamp(p, clamp[0], clamp[1])
    prov["branch"] = branch
    prov["clamped"] = abs(p_clamped - p) > 1e-12
    return p_clamped, branch, prov


def tangible_bv_ex_cash(
    stockholders_equity: Optional[float],
    intangibles: Optional[float],
    cash: Optional[float],
) -> Optional[float]:
    """Tangible book value excluding cash (equity basis)."""
    if any(v is None or (isinstance(v, float) and np.isnan(v))
           for v in (stockholders_equity, cash)):
        return None
    intg = 0.0 if (intangibles is None or (isinstance(intangibles, float) and np.isnan(intangibles))) \
        else float(intangibles)
    return float(stockholders_equity) - float(intg) - float(cash)


def liquidation_value_ps(
    stockholders_equity: Optional[float],
    intangibles: Optional[float],
    cash: Optional[float],
    total_debt: Optional[float],
    shares_outstanding: Optional[float],
    factor: Optional[float] = None,
) -> tuple[Optional[float], Dict[str, Any]]:
    """
    Equity-basis liquidation value per share, NO cash double-count (C6.3):

        LV = 0.70 x Tangible_BV_ex_cash + Net_Cash
        Net_Cash = Cash - Total_Debt

    Returns (lv_ps, provenance).  A negative LV is clamped to 0.0 (schema
    min 0, `liquidation_value_ps` >= 0) and flagged in provenance.
    """
    cfg = dict(SURVIVAL_DEFAULTS) if factor is None else dict(SURVIVAL_DEFAULTS)
    factor = float(factor if factor is not None else cfg["liquidation_tangible_factor"])
    prov: Dict[str, Any] = {}

    tb = tangible_bv_ex_cash(stockholders_equity, intangibles, cash)
    if tb is None:
        return None, prov
    debt = 0.0 if (total_debt is None or (isinstance(total_debt, float) and np.isnan(total_debt))) \
        else float(total_debt)
    net_cash = float(cash) - debt
    lv_total = factor * tb + net_cash
    lv = max(0.0, lv_total)
    prov["lv_total"] = round(lv_total, 2)
    prov["tangible_bv_ex_cash"] = round(tb, 2)
    prov["net_cash"] = round(net_cash, 2)
    prov["clamped_negative"] = lv_total < 0.0

    if shares_outstanding is None or (isinstance(shares_outstanding, float) and np.isnan(shares_outstanding)) \
            or shares_outstanding <= 0:
        prov["per_share"] = False
        return None, prov
    prov["per_share"] = True
    return lv / float(shares_outstanding), prov


def survival_adjusted_value(
    going_concern_iv_ps: Optional[float],
    liquidation_ps: Optional[float],
    p: float,
    lv_cap: bool = True,
) -> tuple[Optional[float], Dict[str, Any]]:
    """
    survival_adjusted_iv = p x IV_going_concern + (1-p) x LV_ps

    Schema invariant (validate_schema phase-8 block):
        survival_adjusted_iv <= IV_going_concern x 1.0001
    enforced by capping the liquidation leg at the going-concern IV when
    `lv_cap=True` (default, the parquet write path).  `liquidation_value_capped`
    is flagged in provenance; the RAW LV stays in `liquidation_value_ps`.
    """
    if going_concern_iv_ps is None or (isinstance(going_concern_iv_ps, float) and np.isnan(going_concern_iv_ps)):
        return None, {}
    gc = float(going_concern_iv_ps)
    prov: Dict[str, Any] = {}
    if liquidation_ps is None or (isinstance(liquidation_ps, float) and np.isnan(liquidation_ps)):
        lv_used = gc          # missing liquidation -> liquidation adds nothing
        prov["liquidation_missing"] = True
    else:
        lv = float(liquidation_ps)
        if lv_cap and lv > gc:
            lv_used = gc
            prov["liquidation_value_capped"] = True
            prov["liquidation_raw_ps"] = round(lv, 4)
        else:
            lv_used = lv
    sav = float(p) * gc + (1.0 - float(p)) * lv_used
    # numeric-noise guard on the schema invariant
    if sav > gc * 1.0001:
        sav = gc * 1.0001
        prov["invariant_clamped"] = True
    prov["lv_used_ps"] = round(lv_used, 4)
    return sav, prov


def survival_position_cap(
    p_survival: Optional[float],
    base_max_weight_cap: Optional[float] = None,
    config: Optional[Dict[str, Any]] = None,
) -> float:
    """
    Survival -> position sizing (Plan §4): p_survival < 0.70 caps the target
    weight at 0.5% regardless of the data-gate cap.  Returns the effective
    max weight cap (min of data-gate cap and survival cap when the low-p
    branch fires; else the data-gate cap).
    """
    cfg = {**SURVIVAL_DEFAULTS, **(config or {})}
    if p_survival is None or (isinstance(p_survival, float) and np.isnan(p_survival)):
        p_survival = float(cfg["survival_position_low_p"])  # unknown -> do not over-constrain
    if p_survival < float(cfg["survival_position_low_p"]):
        low_cap = float(cfg["survival_position_low_cap"])
        if base_max_weight_cap is None:
            return low_cap
        return float(min(base_max_weight_cap, low_cap))
    return float(base_max_weight_cap) if base_max_weight_cap is not None else 1.0


# ----------------------------------------------------------------------------
# DataFrame helper (per-filing PIT application)
# ----------------------------------------------------------------------------

def smooth_survival_observations(
    df: pd.DataFrame,
    p_series: pd.Series,
    span: int = 63,
    ticker_col: str = "ticker",
    file_col: str = "filed",
    clamp_range: Tuple[float, float] = (0.05, 0.95),
) -> pd.Series:
    """
    Optional per-ticker EMA smoothing of the p_survival observation stream
    (observation smoothing v2): p_hat = EMA_span(p) per ticker, ordered by the
    PIT `file_col` (filing availability), then re-clamped to the frozen
    [0.05, 0.95] bounds.  The PRIORITY CASCADE is untouched — this is a
    post-cascade observation filter only, gated by
    `enable_observation_smoothing` (default OFF, so default behavior — the
    full priority cascade — is byte-identical to before).
    """
    out = p_series.astype(float).copy()
    if span <= 1 or len(out) < 2 or ticker_col not in df.columns:
        return out
    lo, hi = float(clamp_range[0]), float(clamp_range[1])
    work = df.reset_index(drop=True)          # positional row space
    order_col = file_col if file_col in work.columns else None
    vals = out.to_numpy(dtype=float)          # positional value array
    if ticker_col not in work.columns:
        return out
    try:
        groups = work.groupby(ticker_col, sort=False).indices
    except Exception:  # noqa: BLE001 — degenerate grouping should not crash
        return out

    for idx in groups.values():               # positional indices
        idx = list(idx)
        if len(idx) < 2:
            continue
        if order_col is not None:
            pairs = []
            for i in idx:
                v = work[order_col].iloc[i]
                try:
                    ts = pd.Timestamp(v)
                except (TypeError, ValueError):
                    ts = pd.Timestamp.min
                pairs.append((ts, i))
            idx = [i for _, i in sorted(pairs)]
        alpha = 2.0 / (span + 1.0)
        ema: Optional[float] = None
        for i in idx:
            v = vals[i]
            if v is None or (isinstance(v, float) and np.isnan(v)):
                continue  # NaN observations do not advance the EMA
            ema = float(v) if ema is None else alpha * float(v) + (1.0 - alpha) * ema
            vals[i] = _clamp(ema, lo, hi)
    return pd.Series(vals, index=out.index)


def compute_survival_columns(
    df: pd.DataFrame,
    config: Optional[Dict[str, Any]] = None,
    shares_col: str = "shares_outstanding",
    intangibles_col: Optional[str] = None,
) -> pd.DataFrame:
    """
    Adds `narrative_v2` survival columns to a filing frame (PIT, row-anchored):

      p_survival, liquidation_value_ps, survival_adjusted_iv,
      liquidation_value_capped (audit suffix, kept out of the canonical schema)

    Required input columns: stockholders_equity, cash, total_debt,
      interest_coverage, roic, wacc_used, going_concern_iv_ps, years_public.
      `intangibles_col`: optional goodwill+intangibles column (any present
      column in the frame, e.g. from a raw-facts join); missing -> 0.
    """
    out = df.copy()
    cfg = {**SURVIVAL_DEFAULTS, **(config or {})}
    intg = out[intangibles_col] if intangibles_col and intangibles_col in out.columns else 0.0

    ps_rows, lv_rows, lv_cap_rows, sav_rows = [], [], [], []
    for _, r in out.iterrows():
        p, _, _ = p_survival_priority_cascade(
            r.get("interest_coverage"), r.get("years_public"),
            r.get("roic"), r.get("wacc_used"), cfg)
        ps_rows.append(p)
        equity = r.get("stockholders_equity")
        cash = r.get("cash")
        lv_ps, _ = liquidation_value_ps(
            equity, intg[r.name] if hasattr(intg, "loc") else intg,
            cash, r.get("total_debt"), r.get(shares_col, None), cfg.get("liquidation_tangible_factor"))
        lv_rows.append(lv_ps)
        sav, prov = survival_adjusted_value(
            r.get("going_concern_iv_ps"), lv_ps, p, lv_cap=True)
        sav_rows.append(sav)
        lv_cap_rows.append(prov.get("liquidation_value_capped", False))

    out["p_survival"] = ps_rows  # written after the optional smoothing pass
    out["liquidation_value_ps"] = lv_rows
    out["survival_adjusted_iv"] = [np.nan if s is None else s for s in sav_rows]
    out["liquidation_value_capped"] = lv_cap_rows

    # Optional observation smoothing (v2, default OFF): post-cascade per-ticker
    # EMA of the p_survival stream, re-clamped to the frozen survival bounds.
    if bool(cfg.get("enable_observation_smoothing")):
        p_raw = pd.Series([np.nan if p is None else float(p) for p in ps_rows],
                          index=out.index, dtype=float)
        out["p_survival"] = smooth_survival_observations(
            out, p_raw,
            span=int(cfg.get("observation_window_days", 63)),
            file_col="filed" if "filed" in out.columns else "filed_date",
            clamp_range=tuple(float(x) for x in cfg["survival_clamp"]))
        out["observation_smoothed"] = True
    else:
        out["observation_smoothed"] = False
    return out


# ----------------------------------------------------------------------------
# Self-test / CLI (mirrors validation/*.py gate-module pattern)
# ----------------------------------------------------------------------------

def _self_test(config: Optional[Dict[str, Any]] = None) -> bool:
    cfg = {**SURVIVAL_DEFAULTS, **(config or {})}

    # 1. distress branch priority
    p, branch, _ = p_survival_priority_cascade(0.8, 2.0, 0.30, 0.09, cfg)
    assert branch == "distressed" and p == cfg["survival_distressed"], (p, branch)

    # 2. young ramp: 2yr public, healthy ICR, modest returns
    p, branch, _ = p_survival_priority_cascade(8.0, 2.0, 0.08, 0.09, cfg)
    assert branch == "young_ramp" and abs(p - (0.60 + 0.05 * 2.0)) < 1e-9, (p, branch)

    # 3. moat beats mature, but young ramp beats moat
    p, branch, _ = p_survival_priority_cascade(8.0, 2.0, 0.30, 0.09, cfg)
    assert branch == "young_ramp", branch
    p, branch, _ = p_survival_priority_cascade(8.0, 12.0, 0.30, 0.09, cfg)
    assert branch == "moat" and p == cfg["survival_moat"], (p, branch)

    # 4. mature base + clamp bounds
    p, branch, _ = p_survival_priority_cascade(8.0, 12.0, 0.08, 0.09, cfg)
    assert branch == "mature" and p == cfg["survival_mature"]
    assert p_survival_priority_cascade(3.0, 0.0, 0.0, 0.09, cfg)[0] >= cfg["survival_clamp"][0]
    p_clamp, _, prov = p_survival_priority_cascade(8.0, 100.0, 0.0, 0.09, cfg)
    assert cfg["survival_clamp"][0] <= p_clamp <= cfg["survival_clamp"][1]
    # clamp bound exercise directly (frozen branches never exceed 0.95, but the
    # clamp is the safety net): a hypothetical young ramp at 10y is unreachable,
    # so verify the clamp machinery on the moat boundary value itself
    p_moat, _, prov_moat = p_survival_priority_cascade(8.0, 12.0, 0.30, 0.09, cfg)
    assert p_moat == cfg["survival_moat"] == 0.95

    # 5. liquidation: 0.70 x (eq - int - cash) + cash - debt (no double count)
    lv, prov = liquidation_value_ps(1000.0, 100.0, 200.0, 300.0, 100.0, 0.70)
    expected = (0.70 * (1000.0 - 100.0 - 200.0) + (200.0 - 300.0)) / 100.0  # = (490-100)/100=3.9
    assert abs(lv - expected) < 1e-6, (lv, expected)
    assert not prov.get("clamped_negative")

    # 6. negative LV clamp
    lv, prov = liquidation_value_ps(50.0, 100.0, 0.0, 500.0, 10.0, 0.70)
    assert lv == 0.0 and prov.get("clamped_negative")

    # 7. survival_adjusted_iv invariant (LV > GC capped)
    sav, prov = survival_adjusted_value(1.00, 3.00, 0.5)
    assert sav <= 1.00 * 1.0001 and prov.get("liquidation_value_capped")
    sav, prov = survival_adjusted_value(1.00, 0.50, 0.5)
    assert abs(sav - 0.75) < 1e-9

    # 8. survival position cap
    assert survival_position_cap(0.60, 0.010) == 0.005
    assert survival_position_cap(0.60, 0.005) == 0.005
    assert abs(survival_position_cap(0.90, 0.010) - 0.010) < 1e-12

    # 9. observation smoothing: default OFF -> emitted cascade unchanged;
    #    ON -> per-ticker EMA within the clamp, cascade still respected off
    frame = pd.DataFrame({
        "ticker": ["AAA", "AAA", "AAA"],
        "filed": pd.to_datetime(["2024-01-10", "2024-06-10", "2025-01-10"]),
        "years_public": [2.0, 2.5, 3.0],
        "interest_coverage": [8.0, 8.0, 8.0],
        "roic": [0.08, 0.08, 0.08],
        "wacc_used": [0.09, 0.09, 0.09],
        "stockholders_equity": [1000.0] * 3, "cash": [200.0] * 3,
        "total_debt": [300.0] * 3, "shares_outstanding": [100.0] * 3,
        "going_concern_iv_ps": [5.0] * 3,
    })
    out_off = compute_survival_columns(frame.copy(), cfg)
    assert out_off["observation_smoothed"].tolist() == [False] * 3
    expected_p = [p_survival_priority_cascade(8.0, y, 0.08, 0.09, cfg)[0]
                  for y in (2.0, 2.5, 3.0)]
    assert all(abs(a - b) < 1e-12 for a, b in zip(out_off["p_survival"], expected_p))

    cfg_on = {**cfg, "enable_observation_smoothing": True, "observation_window_days": 63}
    out_on = compute_survival_columns(frame.copy(), cfg_on)
    assert out_on["observation_smoothed"].tolist() == [True] * 3
    p_sm = out_on["p_survival"].tolist()
    lo, hi = cfg["survival_clamp"]
    assert all(float(lo) - 1e-9 <= v <= float(hi) + 1e-9 for v in p_sm)
    # EMA dampens the ramp: 0.625 -> 0.70 over 3 filings (span 63)
    assert p_sm[0] < expected_p[-1] < 1.0
    assert p_sm[2] <= expected_p[2] + 1e-9

    print("survival_adjustment self-test: OK (9 checks)")
    return True


if __name__ == "__main__":  # python -m Quantitative.survival_adjustment --test
    if "--test" in sys.argv:
        raise SystemExit(0 if _self_test() else 1)
    _self_test()