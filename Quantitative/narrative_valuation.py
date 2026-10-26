"""
Narrative Valuation (Phase 8 WS-6) — bigpickle-worker

Time-varying 10-year DCF on an EQUITY basis producing the `narrative_v2`
per-filing group (schema contract, ingestion/valuation_schema.py) plus the
`going_concern_iv` basis column the schema's survival-adjusted invariant
expects.

DEFECT 2 fix (C6.1) — reinvestment-rate identity corrected to an EBIT(1-tau)
basis (the legacy column used a revenue denominator and broke the
growth = RR x ROIC identity):

    RR_t          = (Rev_t - Rev_{t-1}) / S_C  /  EBIT(1-tau)_t
    Growth_t      = RR_t x ROIC_t
    FCF_t         = EBIT(1-tau)_t x (1 - RR_t)

Frozen narrative mechanics (config/pre_registration_phase8.yaml
phase8.narrative_valuation + phase8_bigpickle_analysis.md §5.7):

  * Horizon 10y = 7 transition years (convergence) + 3 mature years.
  * Young (< 5y public):  high growth declining LINEARLY to industry growth by
    year 7; margins converge to industry median by year 7; reinvestment driven
    by SALES-TO-CAPITAL (S/C = industry for young firms).
  * Mature (>= 5y public): growth = R_f - Inflation (steady state);
    margins converge to industry median (mid-cycle); reinvestment = g/ROIC.
  * Terminal value: TV = FCF_10 x (1 + g_T) / (WACC - g_T) with the frozen
    WACC - g > 50bps guard: when violated, g_T is frozen to WACC - 50bps.
  * Unfunded-growth (sc_constraint gate, phase8.sc_constraint):
        Required = DeltaRev / S_C
        Available = OCF + Cash + Credit_Capacity
        Credit_Capacity = sector_target_debt_ratio x Total_Assets - Total_Debt
        Required > 1.2 x Available  ->  unfunded_growth_flag = True,
        g_T x 0.5 haircut, WACC + 200bps penalty applied to the DCF
        (terminal_growth_haircut / wacc_penalty_bps_applied are LOGGED, not
        inferred — schema contract).
  * No-lookahead: every input is PIT as-of the filing's `filed_date`.

Inputs are row-level and already annualized (the caller — integration or the
production pipeline — is responsible for TTM/annualizing period quanta; see
tests/test_phase2_bigpickle_integration.py for the raw-facts annualizer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import numpy as np
import sys

# ----------------------------------------------------------------------------
# Frozen constants (phase8.narrative_valuation + wacc defaults)
# ----------------------------------------------------------------------------

NARRATIVE_DEFAULTS: Dict[str, Any] = {
    # horizon / convergence
    "horizon_years": 10,
    "transition_years": 7,
    # mature steady state
    "inflation": 0.02,                 # R_f - Inflation frozen deflator (Damodaran)
    # growth sanity clamps for the young path
    "max_young_growth": 0.40,          # narrative growth never projects > 40%/yr
    "min_young_growth": -0.25,
    # Sector TAM x Penetration (Phase 8 WS-6, valuation_alpha.young_firm_valuation):
    # opt-in per-call `tam_ceiling` overrides this config key.  None -> legacy
    # LINEAR revenue-driven young path (revenue_growth_path) — backward compat.
    "tam_ceiling": None,
    # reinvestment path caps
    "max_reinvestment_rate": 0.95,     # mature RR cap (g/ROIC) — RR>0.95 means
                                       # the entire NOPAT is reinvested at most
    "roic_min_denom": 1e-4,
    # terminal
    "terminal_growth_guard_bps": 50,   # frozen: WACC - g_T >= 50bps
    "mature_roic_floor": 1e-4,
    # unfunded-growth / sc_constraint (phase8.sc_constraint)
    "unfunded_threshold": 1.2,
    "growth_haircut": 0.5,
    "wacc_penalty_bps": 200,
    "sc_ratio_min": 1e-6,              # schema: sc_ratio_used > 0
    "sc_ratio_max": 10.0,              # schema: sc_ratio_used <= 10 (winsorize)
    # credit capacity formula inputs (frozen constants sourced from Damodaran
    # betas_latest.csv 2026.09 -> sector target D/(D+E); fallback 0.25)
    "sector_target_debt_ratio": None,  # per-call override expected
    "default_target_debt_ratio": 0.25,
    # liquidation
    "liquidation_tangible_factor": 0.70,
}

MISSING_OK = lambda v: v is None or (isinstance(v, float) and np.isnan(v))  # noqa: E731


def clamp(v: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, v)))


def reinvestment_rate_v2(delta_rev: float, sc_ratio: float, ebit: float,
                         tax_rate: float, config: Optional[Dict[str, Any]] = None) -> float:
    """
    Corrected reinvestment rate (C6.1):

        RR = (DeltaRev / S_C) / EBIT(1 - tau)

    Clamped at >= 0 (schema: reinvestment_rate_v2 >= 0).  When EBIT(1-tau) <= 0
    the firm cannot reinvest — RR = 0 (FCF = NOPAT, i.e. burn/restore).
    """
    cfg = {**NARRATIVE_DEFAULTS, **(config or {})}
    nopat = ebit * (1.0 - tax_rate)
    if nopat <= 0.0 or sc_ratio <= 0.0:
        return 0.0
    # DeltaRev may be negative (shrinking firm) -> the identity reports 0.
    return max(0.0, (delta_rev / sc_ratio) / nopat)


def revenue_growth_path(g_start: float, g_industry: float,
                        transition_years: int, horizon: int) -> list[float]:
    """Young-firm growth path: linear decline from g_start to g_industry across
    the transition years, then g_industry flat."""
    g = [0.0] * horizon
    for t in range(1, horizon + 1):
        w = min(t / transition_years, 1.0)
        g[t - 1] = g_start + (g_industry - g_start) * w
    return g


def margin_path(margin_0: float, margin_target: float,
                transition_years: int, horizon: int) -> list[float]:
    """Margins converge linearly to the industry mid-cycle target over the
    transition window (both cohorts), then hold target."""
    m = [0.0] * horizon
    for t in range(1, horizon + 1):
        w = min(t / transition_years, 1.0)
        m[t - 1] = margin_0 + (margin_target - margin_0) * w
    return m


def terminal_value(fcf_10: float, g_terminal: float, wacc: float,
                   config: Optional[Dict[str, Any]] = None) -> tuple[float, float, Dict[str, Any]]:
    """
    TV = FCF_10 x (1 + g_T) / (WACC - g_T).

    Frozen guard: WACC - g_T > 50bps; if violated, g_T is FROZEN to
    WACC - 50bps (the terminal value never discounts at a spread below the
    guard, which would otherwise explode as WACC -> g_T).
    Returns (tv, g_used, provenance).
    """
    cfg = {**NARRATIVE_DEFAULTS, **(config or {})}
    guard = float(cfg["terminal_growth_guard_bps"]) / 1e4
    prov: Dict[str, Any] = {}
    if wacc - g_terminal < guard:
        g_used = wacc - guard
        prov["g_terminal_frozen"] = True
        prov["g_terminal_raw"] = round(float(g_terminal), 4)
    else:
        g_used = float(g_terminal)
    tv = fcf_10 * (1.0 + g_used) / (wacc - g_used)
    return tv, g_used, prov


def credit_capacity(sector_target_debt_ratio: Optional[float], total_assets: Optional[float],
                    total_debt: Optional[float], config: Optional[Dict[str, Any]] = None) -> tuple[Optional[float], float]:
    """
    Frozen formula (C7.2, phase8.sc_constraint.credit_capacity_formula):

        Credit_Capacity = sector_target_debt_ratio x Total_Assets - Total_Debt

    Returns (credit_capacity, target_debt_ratio_used).
    """
    cfg = {**NARRATIVE_DEFAULTS, **(config or {})}
    ratio = sector_target_debt_ratio if sector_target_debt_ratio is not None and sector_target_debt_ratio > 0 \
        else float(cfg["default_target_debt_ratio"])
    if MISSING_OK(total_assets) or MISSING_OK(total_debt):
        return None, ratio
    ta = float(total_assets)
    if ta <= 0.0:
        return None, ratio
    return ratio * ta - float(total_debt), ratio


def unfunded_growth_check(delta_rev: float, sc_ratio: float,
                          ocf: Optional[float], cash: Optional[float],
                          credit_capacity: Optional[float],
                          config: Optional[Dict[str, Any]] = None) -> tuple[bool, Optional[float], float, float, Optional[float]]:
    """
    sc_constraint gate (C7.3):

        Required = DeltaRev / S_C          (annualized, PIT)
        Available = OCF + Cash + Credit_Capacity
        unfunded  <=> Required > 1.2 x Available

    Returns (unfunded_growth_flag, gap_pct, growth_haircut, wacc_penalty_bps,
             available_funding).  gap_pct = (Required - Available) / Available
    when Available > 0; NaN when Available <= 0 (negative funding capacity —
    documented WS-7.3 handling: any positive Required is unfunded).  DeltaRev
    <= 0 can never flag (no growth to fund).
    """
    cfg = {**NARRATIVE_DEFAULTS, **(config or {})}
    threshold = float(cfg["unfunded_threshold"])
    if MISSING_OK(delta_rev) or float(delta_rev) <= 0.0 or sc_ratio <= 0.0:
        return False, 0.0, 0.0, 0.0, None
    required = float(delta_rev) / float(sc_ratio)
    ocf_v = 0.0 if MISSING_OK(ocf) else float(ocf)
    cash_v = 0.0 if MISSING_OK(cash) else float(cash)
    cc_v = 0.0 if (credit_capacity is None or MISSING_OK(credit_capacity)) else float(credit_capacity)
    available = ocf_v + cash_v + cc_v
    if required > threshold * available:
        gap = (required - available) / available if available > 0.0 else np.nan
        return (True, float(gap) if available > 0.0 else None,
                float(cfg["growth_haircut"]), float(cfg["wacc_penalty_bps"]), available)
    gap = (required - available) / available if available > 0.0 else None
    return False, (float(gap) if gap is not None else None), 0.0, 0.0, available


def sc_ratio_used(company_sc: Optional[float], industry_sc: Optional[float],
                  is_young: bool, cross_sectional_p95: Optional[float],
                  config: Optional[Dict[str, Any]] = None) -> float:
    """
    Frozen S/C resolution + P95 clamp + schema winsorize
    (schema: 0 < sc_ratio_used <= 10):

      * Young (< 5y public)  -> industry S/C (the firm's own history is too
        short; C6.1 "S/C = industry").
      * Mature               -> company 3-yr median S/C when estimable, else
        industry S/C.
      * A zero / missing S/C clamps to the cross-sectional P95 (C7.3).
      * Floor 1e-6 (schema min); extreme ratios winsorized at 10.0 (schema
        max) so the recorded value and the DCF mechanic share one number.
    """
    cfg = {**NARRATIVE_DEFAULTS, **(config or {})}
    floor = float(cfg["sc_ratio_min"])
    cap = float(cfg["sc_ratio_max"])
    v: Optional[float]
    if is_young:
        v = None if MISSING_OK(industry_sc) else float(industry_sc)
    else:
        v = None if MISSING_OK(company_sc) else float(company_sc)
        if v is None and not MISSING_OK(industry_sc):
            v = float(industry_sc)
    if v is None or v <= 0.0:
        v = None if (cross_sectional_p95 is None or MISSING_OK(cross_sectional_p95)) else float(cross_sectional_p95)
    if v is None or v <= 0.0:
        return floor
    return min(max(float(v), floor), cap)


# ----------------------------------------------------------------------------
# Result container (full narrative_v2 row contract + audit extras)
# ----------------------------------------------------------------------------

@dataclass
class NarrativeValuationResult:
    # narrative_v2 schema columns
    p_survival: Optional[float]
    liquidation_value_ps: Optional[float]
    survival_adjusted_iv: Optional[float]
    sc_ratio_used: float
    reinvestment_rate_v2: float
    unfunded_growth_flag: bool
    reinvestment_gap_pct: Optional[float]
    terminal_growth_haircut: float
    wacc_penalty_bps_applied: float
    # basis column supplied to satisfy the schema survival<=GC invariant
    going_concern_iv: Optional[float]
    going_concern_iv_ps: Optional[float]
    # audit extras (non-schema, written to the audit trail)
    wacc_used: float
    g_terminal_used: float
    available_funding: Optional[float]
    required_reinvestment: Optional[float]
    years_public_used: Optional[float]
    provenance: Dict[str, Any] = field(default_factory=dict)


def project_fcf_path(revenue_0: float, margin_0: float, tax_rate: float,
                     g_path: list[float], margin_path_: list[float],
                     sc: float, roic: float, young: bool,
                     config: Optional[Dict[str, Any]] = None) -> list[float]:
    """
    10-year FCF path (FCFF, NOPAT - Reinvestment):

      young : RR_t = max(0, (Rev_t - Rev_{t-1})/S_C / EBIT(1-tau)_t)
              (S/C-driven; a negative-NOPAT year reinvests nothing and burns)
      mature: RR_t = clamp(g_t / ROIC, 0, 0.95)   (reinvestment = g/ROIC)
    """
    cfg = {**NARRATIVE_DEFAULTS, **(config or {})}
    horizon = int(cfg["horizon_years"])
    rev = revenue_0
    fcf = []
    nopat_prev = margin_0 * revenue_0 * (1.0 - tax_rate)
    for t in range(horizon):
        g = g_path[t]
        rev_next = rev * (1.0 + g)
        nopat = rev_next * margin_path_[t] * (1.0 - tax_rate)
        if young:
            if nopat > 0.0 and sc > 0.0:
                rr = max(0.0, (rev_next - rev) / sc / nopat)
            else:
                rr = 0.0
        else:
            roic_eff = max(roic, float(cfg["roic_min_denom"]))
            rr = clamp(g / roic_eff, 0.0, float(cfg["max_reinvestment_rate"])) \
                if g > 0.0 else 0.0
        fcf.append(nopat * (1.0 - rr))
        rev = rev_next
    return fcf


def going_concern_iv(fcf_path: list[float], wacc: float, g_terminal: float,
                     net_debt: Optional[float], shares_outstanding: Optional[float],
                     config: Optional[Dict[str, Any]] = None) -> tuple[Optional[float], Optional[float], Dict[str, Any]]:
    """
    EV = PV10(FCFF) + PV(TV); Equity = EV - Net_Debt (Net_Debt = Total_Debt -
    Cash, so equity IV + Net_Cash mirrors the liquidation basis — no double
    count, C6.3).  Returns (equity_iv_total, equity_iv_ps, provenance).
    """
    cfg = {**NARRATIVE_DEFAULTS, **(config or {})}
    horizon = len(fcf_path)
    ev = sum(fcf_path[t] / (1.0 + wacc) ** (t + 1) for t in range(horizon))
    tv, g_used, prov_tv = terminal_value(fcf_path[-1], g_terminal, wacc, cfg)
    ev += tv / (1.0 + wacc) ** horizon
    prov = {"ev_fcff": round(ev, 2), **prov_tv, "g_terminal_used": g_used}
    net_debt_v = 0.0 if MISSING_OK(net_debt) else float(net_debt)
    equity = ev - net_debt_v
    if equity < 0.0:
        prov["equity_negative"] = True
        return max(equity, 0.0), None, prov
    ps = None
    if not MISSING_OK(shares_outstanding) and float(shares_outstanding) > 0.0:
        ps = equity / float(shares_outstanding)
    return equity, ps, prov


def compute_narrative_valuation(
    revenue: Optional[float], ebit: Optional[float], tax_rate: float,
    operating_margin: Optional[float], roic: Optional[float],
    revenue_yoy: Optional[float], delta_rev: Optional[float],
    wacc: Optional[float], rf_10y: float, inflation: float,
    years_public: Optional[float], is_young: bool,
    company_sc: Optional[float], industry_sc: Optional[float],
    cross_sectional_p95: Optional[float],
    ocf: Optional[float], cash: Optional[float],
    total_debt: Optional[float], total_assets: Optional[float],
    sector_target_debt_ratio: Optional[float],
    net_debt: Optional[float], shares_outstanding: Optional[float],
    sector_median_margin: Optional[float],
    icr: Optional[float] = None,
    stockholders_equity: Optional[float] = None,
    intangibles: Optional[float] = None,
    config: Optional[Dict[str, Any]] = None,
    tam_ceiling: Optional[float] = None,
) -> NarrativeValuationResult:
    """
    Full per-filing narrative valuation (PIT, annualized inputs).

    Computes in order:
      1. g_T base = R_f - Inflation (mature steady state) with the 50bps guard.
      2. Young path (S/C reinvestment) vs mature path (g/ROIC reinvestment).
      3. sc_constraint -> unfunded flag -> g_T haircut + WACC penalty.
      4. FCF projection + EV -> equity IV (per share and total).
      5. p_survival cascade, liquidation value (equity basis), survival-
         adjusted IV (invariant capped, see survival_adjustment.py).
    """
    cfg = {**NARRATIVE_DEFAULTS, **(config or {})}
    from .survival_adjustment import (  # local import: no circulars at load
        liquidation_value_ps,
        p_survival_priority_cascade,
        survival_adjusted_value,
    )

    prov: Dict[str, Any] = {}

    # -- 0. input guards -----------------------------------------------------
    if MISSING_OK(revenue) or revenue <= 0.0:
        return NarrativeValuationResult(
            p_survival=None, liquidation_value_ps=None, survival_adjusted_iv=None,
            sc_ratio_used=float(cfg["sc_ratio_min"]), reinvestment_rate_v2=0.0,
            unfunded_growth_flag=False, reinvestment_gap_pct=None,
            terminal_growth_haircut=0.0, wacc_penalty_bps_applied=0.0,
            going_concern_iv=None, going_concern_iv_ps=None,
            wacc_used=0.0, g_terminal_used=0.0, available_funding=None,
            required_reinvestment=None, years_public_used=years_public,
            provenance={"empty_input": True})
    rev = float(revenue)
    tax = clamp(float(tax_rate), 0.0, 0.35)
    margin_0 = operating_margin if not MISSING_OK(operating_margin) else (
        (float(ebit) / rev) if not MISSING_OK(ebit) else 0.0)
    roic_v = roic if not MISSING_OK(roic) else None
    yrs = float(years_public) if not MISSING_OK(years_public) else 20.0
    young = bool(is_young)
    wacc_base = float(wacc) if not MISSING_OK(wacc) else 0.095

    # -- 1. growth & margins --------------------------------------------------
    g_industry = float(rf_10y) - float(inflation)
    g_industry = max(g_industry, 0.0)
    g_t = g_industry
    if young:
        # revenue_yoy calibrates the STARTING growth only (g_start); the
        # Sector TAM x Penetration path decays growth toward 0 as revenue
        # approaches the penetration ceiling (opt-in tam_ceiling).
        g_start = float(revenue_yoy) if not MISSING_OK(revenue_yoy) else g_industry
        g_start = clamp(g_start, float(cfg["min_young_growth"]), float(cfg["max_young_growth"]))
        tam_ceil = tam_ceiling
        if tam_ceil is None:
            tam_ceil = cfg.get("tam_ceiling")
        if tam_ceil is not None and float(tam_ceil) > 0.0:
            from valuation_alpha.young_firm_valuation import young_growth_path_tam  # local: leaf module
            g_path = young_growth_path_tam(
                rev, g_start, float(tam_ceil),
                int(cfg["transition_years"]), int(cfg["horizon_years"]),
                float(cfg["min_young_growth"]), float(cfg["max_young_growth"]))
            prov["growth_model"] = "sector_tam_x_penetration"
            prov["tam_ceiling"] = float(tam_ceil)
            prov["growth_calibration"] = g_start
        else:
            g_path = revenue_growth_path(g_start, g_industry,
                                         int(cfg["transition_years"]), int(cfg["horizon_years"]))
    else:
        g_path = [g_industry] * int(cfg["horizon_years"])
    margin_target = float(sector_median_margin) if not MISSING_OK(sector_median_margin) else margin_0
    m_path = margin_path(margin_0, margin_target,
                         int(cfg["transition_years"]), int(cfg["horizon_years"]))

    # -- 2. S/C + row reinvestment rate ---------------------------------------
    sc = sc_ratio_used(company_sc, industry_sc, young, cross_sectional_p95, cfg)
    delta = float(delta_rev) if not MISSING_OK(delta_rev) else 0.0
    rr_row = reinvestment_rate_v2(delta, sc, rev * margin_0, tax, cfg)

    # -- 3. sc_constraint / unfunded growth -----------------------------------
    cc, ratio_used = credit_capacity(sector_target_debt_ratio, total_assets, total_debt, cfg)
    unfunded, gap, haircut, penalty_bps, available = unfunded_growth_check(
        delta, sc, ocf, cash, cc, cfg)
    wacc_used = wacc_base
    if unfunded:
        g_t = g_t * (1.0 - haircut)
        wacc_used = wacc_base + penalty_bps / 1e4
    prov.update({
        "sc_ratio_raw": sc, "sector_target_debt_ratio_used": ratio_used,
        "credit_capacity": cc, "unfunded_growth_flag": unfunded,
        "gap_pct": gap, "growth_haircut": haircut, "wacc_penalty_bps": penalty_bps,
        "wacc_base": wacc_base, "wacc_used": wacc_used,
    })

    # -- 4. DCF -----------------------------------------------------------------
    fcf = project_fcf_path(rev, margin_0, tax, g_path, m_path, sc, roic_v if roic_v else 0.0,
                           young, cfg)
    net_debt_v = float(net_debt) if not MISSING_OK(net_debt) else (
        (float(total_debt) - float(cash)) if not MISSING_OK(total_debt) and not MISSING_OK(cash) else 0.0)
    gc_total, gc_ps, prov_gc = going_concern_iv(fcf, wacc_used, g_t, net_debt_v,
                                                shares_outstanding, cfg)
    prov.update({k: v for k, v in prov_gc.items() if k not in prov})

    # -- 5. survival + liquidation (equity basis, no cash double-count) --------
    # ICR is a survival-pass input supplied by the caller (interest_coverage
    # column); the narrative orchestrator hands the survival helpers the same
    # frozen defaults so the self-contained row case remains deterministic; the
    # integration's survival pass overwrites p_survival with the ICR-informed
    # cascade without touching the DCF basis.
    icr_v = float(icr) if not MISSING_OK(icr) else None
    p_surv, branch, prov_p = p_survival_priority_cascade(icr_v, yrs, roic_v, wacc_base, cfg)
    prov.update(prov_p)
    lv_ps, prov_lv = liquidation_value_ps(
        stockholders_equity, intangibles, cash, total_debt, shares_outstanding,
        float(cfg["liquidation_tangible_factor"]))
    prov.update({"liquidation_prov": prov_lv})
    sav, prov_sav = survival_adjusted_value(gc_ps, lv_ps, p_surv, lv_cap=True)
    prov.update({"survival_prov": prov_sav})

    return NarrativeValuationResult(
        p_survival=p_surv,
        liquidation_value_ps=lv_ps,
        survival_adjusted_iv=sav,
        sc_ratio_used=sc,
        reinvestment_rate_v2=rr_row,
        unfunded_growth_flag=bool(unfunded),
        reinvestment_gap_pct=(float(gap) if gap is not None else None),
        terminal_growth_haircut=float(haircut),
        wacc_penalty_bps_applied=float(penalty_bps),
        going_concern_iv=gc_total,
        going_concern_iv_ps=gc_ps,
        wacc_used=float(wacc_used),
        g_terminal_used=float(prov.get("g_terminal_used", g_t)),
        available_funding=available,
        required_reinvestment=(delta / sc if sc > 0.0 and delta > 0.0 else None),
        years_public_used=yrs,
        provenance=prov,
    )


# ----------------------------------------------------------------------------
# Self-test (python -m Quantitative.narrative_valuation)
# ----------------------------------------------------------------------------

def _self_test(config: Optional[Dict[str, Any]] = None) -> bool:
    cfg = {**NARRATIVE_DEFAULTS, **(config or {})}
    from .survival_adjustment import SURVIVAL_DEFAULTS

    # 1. reinvestment identity on EBIT(1-tau) basis (C6.1)
    rr = reinvestment_rate_v2(100.0, 2.0, 100.0, 0.21, cfg)
    assert abs(rr - (100.0 / 2.0) / (100.0 * 0.79)) < 1e-9, rr
    assert reinvestment_rate_v2(-50.0, 2.0, 100.0, 0.21, cfg) == 0.0   # shrinking
    assert reinvestment_rate_v2(100.0, 2.0, -10.0, 0.21, cfg) == 0.0   # loss-making

    # 2. terminal value guard freezes g when WACC - g < 50bps
    tv, g_used, prov = terminal_value(100.0, 0.055, 0.06, cfg)
    assert abs(g_used - (0.06 - 0.005)) < 1e-9 and prov.get("g_terminal_frozen")
    tv, g_used, prov = terminal_value(100.0, 0.01, 0.08, cfg)
    assert abs(g_used - 0.01) < 1e-9 and not prov.get("g_terminal_frozen")

    # 3. growth = RR x ROIC identity holds on the path base
    g = revenue_growth_path(0.20, 0.02, 7, 10)
    assert abs(g[0] - (0.20 + (0.02 - 0.20) * (1.0 / 7.0))) < 1e-9
    assert abs(g[6] - 0.02) < 1e-9          # converged to industry by transition end
    assert abs(g[9] - 0.02) < 1e-9          # flat thereafter (years 8-10)

    # 4. mature RR = g/ROIC, capped
    fcf = project_fcf_path(100.0, 0.10, 0.21, [0.02] * 10, [0.10] * 10,
                           2.0, 0.10, False, cfg)
    nopat0 = 100.0 * 1.02 * 0.10 * 0.79
    assert abs(fcf[0] - nopat0 * (1 - 0.02 / 0.10)) < 1e-6

    # 5. unfunded threshold 1.2x
    flag, gap, hc, pen, avail = unfunded_growth_check(200.0, 1.0, 50.0, 0.0, 0.0, cfg)
    assert flag and hc == 0.5 and pen == 200.0 and abs(avail - 50.0) < 1e-9
    flag, gap, hc, pen, _ = unfunded_growth_check(50.0, 1.0, 50.0, 0.0, 0.0, cfg)
    assert not flag and hc == 0.0 and pen == 0.0
    flag, _, _, _, _ = unfunded_growth_check(-10.0, 1.0, 50.0, 0.0, 0.0, cfg)
    assert not flag  # DeltaRev<=0 never flags (C7.3)

    # 6. credit capacity frozen formula
    cc, ratio = credit_capacity(0.20, 1000.0, 100.0, cfg)
    assert abs(cc - (0.20 * 1000.0 - 100.0)) < 1e-9

    # 7. S/C resolution: young -> industry; mature -> company; P95 clamp
    assert abs(sc_ratio_used(None, 1.5, True, 1.9, cfg) - 1.5) < 1e-9
    assert abs(sc_ratio_used(1.2, 1.5, False, 1.9, cfg) - 1.2) < 1e-9
    assert abs(sc_ratio_used(None, 0.0, False, 1.9, cfg) - 1.9) < 1e-9
    assert sc_ratio_used(None, None, False, None, cfg) >= cfg["sc_ratio_min"]

    # 8. end-to-end row: mature profitable firm, funded growth, WACC 8%
    res = compute_narrative_valuation(
        revenue=1000.0, ebit=100.0, tax_rate=0.21, operating_margin=0.10,
        roic=0.12, revenue_yoy=0.03, delta_rev=30.0, wacc=0.08,
        rf_10y=0.0424, inflation=0.02, years_public=15.0, is_young=False,
        company_sc=2.0, industry_sc=1.8, cross_sectional_p95=2.5,
        ocf=80.0, cash=120.0, total_debt=200.0, total_assets=1400.0,
        sector_target_debt_ratio=0.20, net_debt=80.0, shares_outstanding=100.0,
        sector_median_margin=0.10, config=cfg)
    assert not res.unfunded_growth_flag and res.wacc_penalty_bps_applied == 0.0
    assert res.sc_ratio_used == 2.0 and res.reinvestment_rate_v2 > 0.0
    assert res.going_concern_iv_ps is not None and res.going_concern_iv_ps > 0.0
    # mature, superior returns -> moat branch base p (no ICR supplied -> mature)
    assert res.p_survival == SURVIVAL_DEFAULTS["survival_mature"]
    if res.survival_adjusted_iv and res.going_concern_iv_ps:
        assert res.survival_adjusted_iv <= res.going_concern_iv_ps * 1.0001

    # 9. unfunded branch applies haircut + penalty, logged
    res2 = compute_narrative_valuation(
        revenue=500.0, ebit=80.0, tax_rate=0.21, operating_margin=0.16,
        roic=0.15, revenue_yoy=0.18, delta_rev=90.0, wacc=0.08,
        rf_10y=0.0424, inflation=0.02, years_public=12.0, is_young=False,
        company_sc=0.5, industry_sc=0.8, cross_sectional_p95=1.5,
        ocf=20.0, cash=5.0, total_debt=400.0, total_assets=800.0,
        sector_target_debt_ratio=0.20, net_debt=395.0, shares_outstanding=10.0,
        sector_median_margin=0.16, config=cfg)
    assert res2.unfunded_growth_flag
    assert res2.terminal_growth_haircut == 0.5 and res2.wacc_penalty_bps_applied == 200.0
    assert abs(res2.wacc_used - (0.08 + 0.02)) < 1e-9

    # 10. young-firm Sector TAM x Penetration path (opt-in tam_ceiling):
    #     revenue_yoy calibrates g_start only; ceiling decays growth to 0 as
    #     revenue approaches it.  Default (None) keeps the legacy linear path.
    res_young_legacy = compute_narrative_valuation(
        revenue=1000.0, ebit=100.0, tax_rate=0.21, operating_margin=0.10,
        roic=0.15, revenue_yoy=0.20, delta_rev=200.0, wacc=0.095,
        rf_10y=0.0424, inflation=0.02, years_public=3.0, is_young=True,
        company_sc=None, industry_sc=1.5, cross_sectional_p95=2.5,
        ocf=80.0, cash=120.0, total_debt=200.0, total_assets=2000.0,
        sector_target_debt_ratio=0.20, net_debt=80.0, shares_outstanding=100.0,
        sector_median_margin=0.10, config=cfg)
    assert res_young_legacy.provenance.get("growth_model") is None
    assert res_young_legacy.going_concern_iv_ps is not None

    res_young_tam = compute_narrative_valuation(
        revenue=1000.0, ebit=100.0, tax_rate=0.21, operating_margin=0.10,
        roic=0.15, revenue_yoy=0.20, delta_rev=200.0, wacc=0.095,
        rf_10y=0.0424, inflation=0.02, years_public=3.0, is_young=True,
        company_sc=None, industry_sc=1.5, cross_sectional_p95=2.5,
        ocf=80.0, cash=120.0, total_debt=200.0, total_assets=2000.0,
        sector_target_debt_ratio=0.20, net_debt=80.0, shares_outstanding=100.0,
        sector_median_margin=0.10, config=cfg, tam_ceiling=5_000.0)
    assert res_young_tam.provenance.get("growth_model") == "sector_tam_x_penetration"
    assert abs(res_young_tam.provenance["tam_ceiling"] - 5000.0) < 1e-9
    assert abs(res_young_tam.provenance["growth_calibration"] - 0.20) < 1e-9
    assert res_young_tam.going_concern_iv_ps is not None

    print("narrative_valuation self-test: OK (10 groups)")
    return True


if __name__ == "__main__":  # python -m Quantitative.narrative_valuation --test
    if "--test" in sys.argv:
        raise SystemExit(0 if _self_test() else 1)
    _self_test()