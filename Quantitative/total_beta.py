"""
Total Beta (Phase 8 WS-5.5 / WS-6 C5.6) — bigpickle-worker

Pure-function implementation of the frozen Total Beta parameterization
(phase8_bigpickle_analysis.md §5.5, config/pre_registration_phase8.yaml
phase8.total_beta) plus the `beta_wacc_v2` filing-row helpers.

DEFECT 3 fix — the raw ratio beta_L / rho degenerates when the market
correlation rho -> 0.  Frozen rules:

  * rho_used            = max(rho_market_60m, correlation_floor 0.25);
    NEGATIVE rho is treated as unreported (clamped to the floor), never
    amplified (beta_L / -0.05 would invert the sign into a short beta).
    rho_used missing -> neutral 0.5 handled upstream; here None in -> None out.

  * total_beta_raw      = beta_L / rho_used

  * total_beta_adj      = max_total_beta x sigmoid(total_beta_raw / sigmoid_transition_point)
                        = 5.0 / (1 + exp(-total_beta_raw / 3.0))
    -> monotone, strictly in (0, 5.0), approaches the 5.0 ceiling from below
    (mirrors ingestion/wacc_engine_v2._sigmoid_ceiling_beta, ceiling 5.0 /
    transition 3.0, so the two engines agree where both are used).

  * Application criteria (phase8.total_beta.apply_to) — Total Beta replaces
    plain beta ONLY for:
        small_cap     : market_cap < small_cap_threshold_usd (2e9)
        private       : is_private True
        concentrated  : position_pct_nav > position_threshold_pct (0.03)
        adv_low       : adv_usd < adv_threshold_usd (5e6)
    Public large-cap liquid names are NOT total-beta adjusted (plain beta).

Per-filing `beta_wacc_v2` helper columns (schema contract, --list group):

  beta_unlevered_sector : Damodaran 94-industry beta_U, 7 custom-sector median
                          (frozen constants below, provenance
                          data/damodaran/betas_latest.csv v2026.09 ->
                          sub_area -> custom sector)   [source sector_median_fallback]
  beta_levered_v2       : Hamada levering, net-debt basis:
                          beta_L = beta_U x (1 + (1 - tau) x D/E_net)
  d_e_market_net        : max(Net_Debt, 0) / Market_Cap, cap 20.0 (schema max)
  beta_source           : sector_median_fallback / universe_median_fallback
                          (Appendix B layer 1-2)
  tax_rate_applied      : tau_eff if 0<=tau_eff<=0.35 else 0.21 (frozen)
  debt_mv_source        : book (no marked-to-market debt inputs upstream)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np

# ----------------------------------------------------------------------------
# Frozen constants
# ----------------------------------------------------------------------------

TOTAL_BETA_DEFAULTS: Dict[str, Any] = {
    "correlation_floor": 0.25,           # phase8.total_beta.correlation_floor
    "max_total_beta": 5.0,               # phase8.total_beta.max_total_beta
    "sigmoid_transition_point": 3.0,     # phase8.total_beta.sigmoid_transition_point
    "apply_to": ["small_cap", "private", "concentrated", "adv_low"],
    "small_cap_threshold_usd": 2e9,      # phase8.total_beta.small_cap_threshold_usd
    "adv_threshold_usd": 5e6,            # phase8.total_beta.adv_threshold_usd
    "position_threshold_pct": 0.03,      # phase8.total_beta.position_threshold_pct
    "tax_fallback": 0.21,                # phase8.wacc_engine.tax_fallback
    "financials_d_e_cap": 0.60,          # financials carve-out: D/E cap (sector P95 proxy)
    "d_e_market_net_max": 20.0,          # schema max for d_e_market_net
}

# Frozen 7 custom-sector median unlevered betas + universe median, derived from
# data/damodaran/betas_latest.csv (2026.09 capture) via industry -> sub_area
# (config/industry_beta.yaml) -> custom sector (scripts/refresh_damodaran.py
# SUB_AREA_TO_CUSTOM_SECTOR).  Sector median fallback = Appendix B layer 2.
CUSTOM_SECTOR_BETA_U: Dict[str, float] = {
    "Consumer": 0.7372,
    "Energy": 0.3867,
    "Financial": 0.4373,
    "Hardware": 0.8635,
    "Healthcare": 0.8291,
    "Industrial": 0.7932,
    "Software": 0.9553,
}
UNIVERSE_MEDIAN_BETA_U = 0.7401

# Frozen Damodaran sector revenue pools (USD, aggregate, 2026.09 capture) used
# by the young-firm Sector TAM x Penetration growth model (Phase 8 WS-6,
# valuation_alpha/young_firm_valuation.py).  `sector_tam_usd()` overrides these
# with data/damodaran/industry_revenue.csv when that file exists (future
# capture); the frozen table is the permanent fallback so TAM never goes
# unset.  Provenance tag mirrors the betas_latest.csv 2026.09 capture.
SECTOR_TAM_USD_2026: Dict[str, float] = {
    "Tech": 3.2e12,
    "Healthcare": 2.7e12,
    "Financial": 3.4e12,
    "Consumer": 2.9e12,
    "Industrial": 2.4e12,
    "Energy": 1.3e12,
    "default": 1.0e12,
}
_INDUSTRY_REVENUE_CSV = "data/damodaran/industry_revenue.csv"


def sector_tam_usd(sector: Optional[str]) -> Optional[float]:
    """
    Damodaran sector revenue pool (USD, 2026).  When the
    `data/damodaran/industry_revenue.csv` capture exists it is authoritative
    (csv columns: sector, revenue_usd); otherwise the frozen
    SECTOR_TAM_USD_2026 table serves.  Unknown sectors fall back to
    `default`.  Returns None only when both sources are unusable.
    """
    file_tam: Dict[str, float] = {}
    try:
        if Path(_INDUSTRY_REVENUE_CSV).exists():
            import pandas as pd
            df = pd.read_csv(_INDUSTRY_REVENUE_CSV, encoding="utf-8")
            if "sector" in df.columns and "revenue_usd" in df.columns:
                for _, r in df.iterrows():
                    try:
                        file_tam[str(r["sector"])] = float(r["revenue_usd"])
                    except (TypeError, ValueError):
                        continue
    except Exception:  # noqa: BLE001 — a bad capture must not break the model
        file_tam = {}
    pool = (file_tam or SECTOR_TAM_USD_2026)
    if sector is not None and str(sector) in pool:
        return float(pool[str(sector)])
    if "default" in pool:
        return float(pool["default"])
    return None

BETA_SOURCE_SECTOR = "sector_median_fallback"
BETA_SOURCE_UNIVERSE = "universe_median_fallback"


# ----------------------------------------------------------------------------
# Pure functions
# ----------------------------------------------------------------------------

def tax_rate_applied(tax_eff: Optional[float], config: Optional[Dict[str, Any]] = None) -> float:
    """Frozen tax rule: tau_eff if 0 <= tau_eff <= 0.35 else 0.21."""
    cfg = {**TOTAL_BETA_DEFAULTS, **(config or {})}
    tau = tax_eff
    if tau is None or (isinstance(tau, float) and np.isnan(tau)):
        return float(cfg["tax_fallback"])
    tau = float(tau)
    if 0.0 <= tau <= 0.35:
        return tau
    return float(cfg["tax_fallback"])


def sector_beta_u_unlevered(custom_sector: Optional[str], config: Optional[Dict[str, Any]] = None
                            ) -> Tuple[float, str]:
    """Damodaran 7 custom-sector median beta_U (Appendix B layer 2)."""
    if custom_sector is not None and str(custom_sector) in CUSTOM_SECTOR_BETA_U:
        return float(CUSTOM_SECTOR_BETA_U[str(custom_sector)]), BETA_SOURCE_SECTOR
    return float(UNIVERSE_MEDIAN_BETA_U), BETA_SOURCE_UNIVERSE


def d_e_market_net(net_debt: Optional[float], market_cap: Optional[float],
                   config: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """D/E on market values, net-debt basis: max(Net_Debt, 0) / Market_Cap,
    capped at the schema max 20.0.  None when either input is missing/<=0."""
    cfg = {**TOTAL_BETA_DEFAULTS, **(config or {})}
    if net_debt is None or market_cap is None:
        return None
    nd = float(net_debt)
    mc = float(market_cap)
    if np.isnan(nd) or np.isnan(mc) or mc <= 0.0 or nd <= 0.0:
        return None
    return float(min(nd / mc, float(cfg["d_e_market_net_max"])))


def hamada_lever(beta_unlevered: float, d_e: Optional[float], tau: float,
                 is_financial: bool = False,
                 config: Optional[Dict[str, Any]] = None) -> float:
    """
    Hamada re-levering on the NET-DEBT basis (cash excluded, C5.2):
        beta_L = beta_U x (1 + (1 - tau) x D/E_net)
    Missing D/E -> beta_L = beta_U (debt_missing flag upstream).
    Financials carve-out: D/E capped at the frozen sector P95 proxy (0.60)
    before levering — raw sector beta is retained for Financials (bank
    leverage multiples are not meaningfully Hamada-adjusted).
    """
    cfg = {**TOTAL_BETA_DEFAULTS, **(config or {})}
    if d_e is None or (isinstance(d_e, float) and np.isnan(d_e)) or d_e <= 0.0:
        return float(beta_unlevered)
    de = float(d_e)
    if is_financial:
        de = min(de, float(cfg["financials_d_e_cap"]))
    return float(beta_unlevered * (1.0 + (1.0 - tau) * de))


def total_beta_raw(beta_levered: float, rho_market_60m: Optional[float],
                   config: Optional[Dict[str, Any]] = None) -> Tuple[Optional[float], float, Dict[str, Any]]:
    """
    beta_L / max(rho, correlation_floor).  Negative rho clamps to the floor
    (never amplifies/inverts); missing rho returns (None, 0.5-neutral handled
    by caller with a rho_degraded flag, mirroring Appendix B step 4).
    Returns (total_beta_raw, rho_used, provenance).
    """
    cfg = {**TOTAL_BETA_DEFAULTS, **(config or {})}
    floor = float(cfg["correlation_floor"])
    prov: Dict[str, Any] = {}
    if rho_market_60m is None or (isinstance(rho_market_60m, float) and np.isnan(rho_market_60m)):
        rho_used = 0.5  # neutral (Appendix B step 4)
        prov["rho_degraded"] = True
        return None, rho_used, prov
    rho = float(rho_market_60m)
    if rho < 0.0:
        rho_used = floor
        prov["rho_negative_clamped"] = True
    else:
        rho_used = max(rho, floor)
    return float(beta_levered) / rho_used, rho_used, prov


def total_beta_adjusted(beta_levered: float, rho_market_60m: Optional[float],
                        config: Optional[Dict[str, Any]] = None) -> Tuple[Optional[float], float, Dict[str, Any]]:
    """
    Frozen Total Beta application:
        total_beta_raw = beta_L / max(rho, 0.25)
        total_beta_adj = 5.0 / (1 + exp(-total_beta_raw / 3.0))
    Missing rho -> (None, neutral 0.5, rho_degraded) — the caller falls back
    to plain beta for the row and flags it.
    """
    raw, rho_used, prov = total_beta_raw(beta_levered, rho_market_60m, config)
    if raw is None:
        return None, rho_used, prov
    cfg = {**TOTAL_BETA_DEFAULTS, **(config or {})}
    ceiling = float(cfg["max_total_beta"])
    transition = float(cfg["sigmoid_transition_point"])
    adj = ceiling / (1.0 + np.exp(-raw / transition))
    prov["total_beta_raw"] = round(raw, 4)
    return float(adj), rho_used, prov


def total_beta_applies(
    market_cap: Optional[float],
    adv_usd: Optional[float],
    position_pct_nav: Optional[float],
    is_private: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    Frozen application criteria (phase8.total_beta.apply_to): Total Beta is
    applied to small_cap (< $2B) OR private OR concentrated (>3% NAV) OR
    low-ADV (< $5M).  Public liquid large-caps keep plain beta.
    Returns (applies, reason) where reason is the first matching criterion
    ('small_cap' / 'private' / 'concentrated' / 'adv_low') or '' when exempt.
    """
    cfg = {**TOTAL_BETA_DEFAULTS, **(config or {})}
    if "private" in cfg["apply_to"] and is_private:
        return True, "private"
    if "small_cap" in cfg["apply_to"] \
            and market_cap is not None and float(market_cap) < float(cfg["small_cap_threshold_usd"]):
        return True, "small_cap"
    if "concentrated" in cfg["apply_to"] \
            and position_pct_nav is not None and float(position_pct_nav) > float(cfg["position_threshold_pct"]):
        return True, "concentrated"
    if "adv_low" in cfg["apply_to"] \
            and adv_usd is not None and float(adv_usd) < float(cfg["adv_threshold_usd"]):
        return True, "adv_low"
    return False, ""


@dataclass
class TotalBetaResult:
    beta_unlevered_sector: float
    beta_levered_v2: float
    beta_source: str
    d_e_market_net: Optional[float]
    rho_if_used: Optional[float]        # value actually used in the sigmoid
    total_beta_if_applied: Optional[float]
    total_beta_applied: bool
    apply_reason: str
    tax_rate_applied: float
    debt_mv_source: str
    provenance: Dict[str, Any]


def compute_total_beta(
    beta_unlevered: Optional[float],
    beta_levered: float,
    beta_source: str,
    d_e_net: Optional[float],
    rho_market_60m: Optional[float],
    market_cap: Optional[float],
    adv_usd: Optional[float],
    position_pct_nav: Optional[float],
    is_private: bool = False,
    config: Optional[Dict[str, Any]] = None,
    debt_mv_source: str = "book",
) -> TotalBetaResult:
    """
    Row-level Total Beta orchestrator: applies the sigmoid-ceiling Total Beta
    only when the frozen criteria fire; otherwise the row keeps plain beta
    (total_beta = None -> plain beta used downstream, schema allows nulls).
    """
    cfg = {**TOTAL_BETA_DEFAULTS, **(config or {})}
    applies, reason = total_beta_applies(market_cap, adv_usd, position_pct_nav,
                                         is_private, cfg)
    prov: Dict[str, Any] = {"apply_criteria": (applies, reason)}
    if applies:
        adj, rho_used, prov_rho = total_beta_adjusted(beta_levered, rho_market_60m, cfg)
        prov.update(prov_rho)
    else:
        adj, rho_used, prov_rho = None, None, {"exempt_public_liquid": True}
        prov.update(prov_rho)
    return TotalBetaResult(
        beta_unlevered_sector=float(beta_unlevered) if beta_unlevered is not None else UNIVERSE_MEDIAN_BETA_U,
        beta_levered_v2=float(beta_levered),
        beta_source=beta_source,
        d_e_market_net=d_e_net,
        rho_if_used=rho_used,
        total_beta_if_applied=adj,
        total_beta_applied=applies,
        apply_reason=reason,
        tax_rate_applied=tax_rate_applied(None, cfg),  # caller overrides with row tau
        debt_mv_source=debt_mv_source,
        provenance=prov,
    )


def compute_beta_wacc_columns(
    df,
    market_cap_col: str = "market_cap",
    net_debt_col: str = "net_debt",
    tax_col: Optional[str] = "tax_rate_applied",
    sector_col: str = "sector",
    financial_sectors: Tuple[str, ...] = ("Financial",),
    rho_col: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
):
    """
    Per-filing beta_wacc_v2 group builder (PIT, row-anchored on filed_date).

    Produces: beta_unlevered_sector, beta_levered_v2, beta_source,
    d_e_market_net, total_beta (when applied), rho_market_60m (rho_if_used),
    tax_rate_applied, debt_mv_source, plus audit suffix total_beta_applied /
    total_beta_reason.

    `rho_col`: per-row rolling 60-month SPY correlation (None -> synthetic/
    upstream-supplied vector or a column of the frame).
    """
    cfg = {**TOTAL_BETA_DEFAULTS, **(config or {})}
    out = df.copy()

    def _tax(row):
        tau = row.get(tax_col) if tax_col else None
        return tax_rate_applied(None if _ismissing(tau) else tau, cfg)

    def _ismissing(v):
        return v is None or (isinstance(v, float) and np.isnan(v))

    beta_u_rows, beta_l_rows, src_rows, de_rows = [], [], [], []
    tau_rows, rho_rows, tb_rows, tb_applied, tb_reason = [], [], [], [], []
    for _, r in out.iterrows():
        sector = r.get(sector_col)
        bu, src = sector_beta_u_unlevered(sector, cfg)
        tau = _tax(r)
        is_fin = sector in financial_sectors if sector is not None else False
        de = d_e_market_net(r.get(net_debt_col), r.get(market_cap_col), cfg) \
            if not _ismissing(r.get(net_debt_col)) and not _ismissing(r.get(market_cap_col)) else None
        bl = hamada_lever(bu, de, tau, is_financial=is_fin, config=cfg)
        rho = r.get(rho_col) if rho_col and rho_col in out.columns else None
        bl_eff = bl
        if rho is not None and not _ismissing(rho):
            adj, rho_used, _ = total_beta_adjusted(bl, rho, cfg)
        else:
            adj, rho_used = None, None
        applies, reason = total_beta_applies(
            r.get(market_cap_col), None, None, False, cfg)
        beta_u_rows.append(bu); beta_l_rows.append(bl); src_rows.append(src)
        de_rows.append(de); tau_rows.append(tau); rho_rows.append(rho_used)
        tb_rows.append(adj if applies and adj is not None else None)
        tb_applied.append(applies); tb_reason.append(reason)

    out["beta_unlevered_sector"] = beta_u_rows
    out["beta_levered_v2"] = beta_l_rows
    out["beta_source"] = src_rows
    out["d_e_market_net"] = de_rows
    out["tax_rate_applied"] = tau_rows
    out["debt_mv_source"] = "book"
    out["rho_market_60m"] = rho_rows
    out["total_beta"] = tb_rows
    out["total_beta_applied"] = tb_applied
    out["total_beta_reason"] = tb_reason
    return out


# ----------------------------------------------------------------------------
# Self-test (python -m Quantitative.total_beta)
# ----------------------------------------------------------------------------

def _self_test(config: Optional[Dict[str, Any]] = None) -> bool:
    cfg = {**TOTAL_BETA_DEFAULTS, **(config or {})}

    # 1. rho floor + negative clamp
    raw, rho_used, prov = total_beta_raw(1.2, 0.05, cfg)
    assert abs(raw - 1.2 / 0.25) < 1e-9 and rho_used == 0.25, (raw, rho_used)
    raw, rho_used, prov = total_beta_raw(1.2, -0.1, cfg)
    assert rho_used == 0.25 and prov.get("rho_negative_clamped"), (rho_used, prov)
    raw, rho_used, prov = total_beta_raw(1.2, None, cfg)
    assert raw is None and rho_used == 0.5 and prov.get("rho_degraded")

    # 2. sigmoid ceiling endpoints (matches wacc_engine_v2 contract)
    adj, rho_used, _ = total_beta_adjusted(7.2, 1.0, cfg)   # regressed β=7.2, rho 1.0
    assert abs(adj - 4.5841) < 1e-3, adj                    # test_wacc_engine_v2 pins 4.5841
    adj, _, _ = total_beta_adjusted(0.5, 0.8, cfg)          # raw = 0.5/0.8 = 0.625
    expected = 5.0 / (1.0 + np.exp(-0.625 / 3.0))           # ~2.758 (sigmoid compression)
    assert abs(adj - expected) < 1e-6 and 0.0 < adj < 5.0
    # matched to wacc_engine_v2._sigmoid_ceiling_beta at the same (raw, τ):
    x = 0.625
    assert abs(adj - 5.0 / (1.0 + np.exp(-x / 3.0))) < 1e-9
    adj, _, _ = total_beta_adjusted(30.0, 0.25, cfg)        # huge raw -> ceiling
    assert abs(adj - 5.0) < 1e-9 and adj <= 5.0

    # 3. application criteria
    assert total_beta_applies(1.5e9, 1e8, 0.01, False, cfg) == (True, "small_cap")
    assert total_beta_applies(5e9, 1e8, 0.01, True, cfg) == (True, "private")
    assert total_beta_applies(5e9, 1e8, 0.05, False, cfg) == (True, "concentrated")
    assert total_beta_applies(5e9, 1e6, 0.01, False, cfg) == (True, "adv_low")
    assert total_beta_applies(5e9, 1e8, 0.01, False, cfg) == (False, "")
    # criteria order: private first, then small_cap, concentrated, adv_low
    assert total_beta_applies(1e9, 1e6, 0.05, True, cfg)[1] == "private"

    # 4. Hamada net-debt + financials carve-out
    assert abs(hamada_lever(0.9, 0.5, 0.21, False) - 0.9 * (1 + 0.79 * 0.5)) < 1e-9
    assert abs(hamada_lever(0.4373, 5.0, 0.21, True) - 0.4373 * (1 + 0.79 * 0.60)) < 1e-9

    print("total_beta self-test: OK (4 groups)")
    return True


if __name__ == "__main__":  # python -m Quantitative.total_beta
    _self_test()